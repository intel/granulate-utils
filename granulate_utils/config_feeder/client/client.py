import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, DefaultDict, Dict, Optional, cast

from pydantic import BaseModel
from requests import Session
from requests.exceptions import ConnectionError, JSONDecodeError

from granulate_utils.config_feeder.client.bigdata import get_node_info
from granulate_utils.config_feeder.client.exceptions import APIError, ClientError
from granulate_utils.config_feeder.client.logging import get_logger
from granulate_utils.config_feeder.client.models import CollectionResult, ConfigType
from granulate_utils.config_feeder.client.yarn.collector import YarnConfigCollector
from granulate_utils.config_feeder.client.yarn.models import YarnConfig
from granulate_utils.config_feeder.core.errors import raise_for_code
from granulate_utils.config_feeder.core.models.aggregation import CreateNodeConfigsRequest, CreateNodeConfigsResponse
from granulate_utils.config_feeder.core.models.cluster import (
    CloudProvider,
    ClusterCreate,
    CreateClusterRequest,
    CreateClusterResponse,
)
from granulate_utils.config_feeder.core.models.node import CreateNodeRequest, CreateNodeResponse, NodeCreate, NodeInfo
from granulate_utils.config_feeder.core.models.yarn import NodeYarnConfigCreate

DEFAULT_API_SERVER_ADDRESS = "https://api.granulate.io/config-feeder/api/v1"
DEFAULT_REQUEST_TIMEOUT = 3

logger = get_logger()


class ConfigFeederClient:
    def __init__(
        self,
        token: str,
        service: str,
        *,
        server_address: Optional[str] = None,
        yarn: bool = True,
    ) -> None:
        if not token or not service:
            raise ClientError("Token and service must be provided")
        self._token = token
        self._service = service
        self._cluster_id: Optional[str] = None
        self._server_address: str = server_address.rstrip("/") if server_address else DEFAULT_API_SERVER_ADDRESS
        self._is_yarn_enabled = yarn
        self._yarn_collector = YarnConfigCollector()
        self._last_hash: DefaultDict[ConfigType, Dict[str, str]] = defaultdict(dict)
        self._init_api_session()

    @property
    def logger(self) -> logging.Logger:
        return get_logger()

    def collect(self) -> None:
        if (node_info := get_node_info()) is None:
            logger.warning("not a Big Data host, skipping")
            return None

        collection_result = asyncio.run(self._collect(node_info))
        if collection_result.is_empty:
            logger.info("no configs to submit")
            return None

        self._submit_node_configs(collection_result)

    async def _collect(self, node_info: NodeInfo) -> CollectionResult:
        results = await asyncio.gather(
            self._collect_yarn_config(node_info),
        )
        return CollectionResult(node=node_info, yarn_config=results[0])

    async def _collect_yarn_config(self, node_info: NodeInfo) -> Optional[YarnConfig]:
        if not self._is_yarn_enabled:
            return None
        logger.info("YARN config collection starting")
        yarn_config = await self._yarn_collector.collect(node_info)
        logger.info("YARN config collection finished")
        return yarn_config

    def _submit_node_configs(
        self,
        collection_result: CollectionResult,
    ) -> None:
        external_id = collection_result.node.external_id
        request = self._get_configs_request(collection_result)
        if request is None:
            logger.info(f"skipping node {external_id}, configs are up to date")
            return None

        node_id = self._register_node(collection_result.node)
        logger.info(f"sending configs for node {external_id}")
        response = CreateNodeConfigsResponse(**self._api_request("POST", f"/nodes/{node_id}/configs", request))

        if response.yarn_config is not None:
            assert request.yarn_config is not None
            self._last_hash[ConfigType.YARN][external_id] = collection_result.yarn_config_hash

    def _register_node(
        self,
        node: NodeInfo,
    ) -> str:
        if self._cluster_id is None:
            self._register_cluster(node.provider, node.external_cluster_id)
        assert self._cluster_id is not None

        logger.debug(f"registering node {node.external_id}")
        request = CreateNodeRequest(
            node=NodeCreate(
                external_id=node.external_id,
                is_master=node.is_master,
            ),
            allow_existing=True,
        )
        response = CreateNodeResponse(**self._api_request("POST", f"/clusters/{self._cluster_id}/nodes", request))
        return response.node.id

    def _register_cluster(self, provider: CloudProvider, external_id: str) -> None:
        logger.debug(f"registering cluster {external_id}")
        request = CreateClusterRequest(
            cluster=ClusterCreate(
                service=self._service,
                provider=provider,
                external_id=external_id,
            ),
            allow_existing=True,
        )
        response = CreateClusterResponse.parse_obj(self._api_request("POST", "/clusters", request))
        self._cluster_id = response.cluster.id

    def _get_configs_request(self, configs: CollectionResult) -> Optional[CreateNodeConfigsRequest]:
        yarn_config = self._get_yarn_config_if_changed(configs)

        if yarn_config is None:
            return None

        return CreateNodeConfigsRequest(
            yarn_config=yarn_config,
        )

    def _get_yarn_config_if_changed(self, configs: CollectionResult) -> Optional[NodeYarnConfigCreate]:
        if configs.yarn_config is None:
            return None
        if self._last_hash[ConfigType.YARN].get(configs.node.external_id) == configs.yarn_config_hash:
            logger.debug("YARN config is up to date")
            return None
        return NodeYarnConfigCreate(config_json=json.dumps(configs.yarn_config.config))

    def _api_request(
        self,
        method: str,
        path: str,
        request_data: Optional[BaseModel] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Dict[str, Any]:
        try:
            resp = self._session.request(
                method,
                f"{self._server_address}{path}",
                json=request_data.dict() if request_data else None,
                timeout=timeout,
            )
            if resp.ok:
                return cast(Dict[str, Any], resp.json())
            try:
                res = resp.json()
                if "detail" in res:
                    raise APIError(res["detail"], path, resp.status_code)
                error = res["error"]
                raise_for_code(error["code"], error["message"])
                return cast(Dict[str, Any], res)
            except (KeyError, JSONDecodeError):
                raise APIError(resp.text or resp.reason, path, resp.status_code)
        except ConnectionError:
            raise ClientError(f"could not connect to {self._server_address}")

    def _init_api_session(self) -> None:
        self._session = Session()
        self._session.headers.update({"Accept": "application/json", "GProfiler-API-Key": self._token})
