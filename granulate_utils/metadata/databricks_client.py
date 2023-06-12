#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#

import json
import logging
import os
import re
import time
from typing import Dict, Optional

import requests

from granulate_utils.exceptions import DatabricksJobNameDiscoverException

HOST_KEY_NAME = "*.sink.ganglia.host"
DATABRICKS_METRICS_PROP_PATH = "/databricks/spark/conf/metrics.properties"
CLUSTER_TAGS_KEY = "spark.databricks.clusterUsageTags.clusterAllTags"
CLUSTER_NAME_PROP = "spark.databricks.clusterUsageTags.clusterName"
SPARKUI_APPS_URL = "http://{}/api/v1/applications"
REQUEST_TIMEOUT = 5
JOB_NAME_KEY = "RunName"
CLUSTER_NAME_KEY = "ClusterName"
DEFAULT_WEBUI_PORT = 40001
DATABRICKS_JOBNAME_TIMEOUT_S = 2 * 60
RETRY_INTERVAL_S = 1

RUN_ID_REGEX = "run-\\d+-"


class DatabricksClient:
    def __init__(self, logger: logging.LoggerAdapter) -> None:
        self.logger = logger
        self.logger.debug("Getting Databricks job name")
        self.job_name = self.get_job_name()
        if self.job_name is None:
            self.logger.warning(
                "Failed initializing Databricks client. Databricks job name will not be included in ephemeral clusters."
            )
        else:
            self.logger.debug(f"Got Databricks job name: {self.job_name}")

    def _request_get(self, url: str) -> requests.Response:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp

    @staticmethod
    def get_webui_address() -> Optional[str]:
        with open(DATABRICKS_METRICS_PROP_PATH) as f:
            properties = f.read()
        try:
            host = dict([line.split("=", 1) for line in properties.splitlines()])[HOST_KEY_NAME]
        except KeyError as e:
            if e.args[0] == HOST_KEY_NAME:
                # Might happen while provisioning the cluster, retry.
                return None
            raise DatabricksJobNameDiscoverException(f"Failed to get Databricks webui address {properties=}") from e
        except Exception as e:
            raise DatabricksJobNameDiscoverException(f"Failed to get Databricks webui address {properties=}") from e
        return f"{host}:{DEFAULT_WEBUI_PORT}"

    def get_job_name(self) -> Optional[str]:
        # Retry in case of a connection error, as the metrics server might not be up yet.
        start_time = time.monotonic()
        while time.monotonic() - start_time < DATABRICKS_JOBNAME_TIMEOUT_S:
            try:
                if cluster_metadata := self._cluster_all_tags_metadata():
                    name = self._get_name_from_metadata(cluster_metadata)
                    if name:
                        self.logger.debug("Found name in metadata", job_name=name, cluster_metadata=cluster_metadata)
                        return name
                    else:
                        self.logger.debug("Failed to extract name from metadata", cluster_metadata=cluster_metadata)
                        return None
                else:
                    # No job name yet, retry.
                    time.sleep(RETRY_INTERVAL_S)
            except DatabricksJobNameDiscoverException:
                self.logger.exception("Failed to get Databricks job name")
                return None
            except Exception:
                self.logger.exception("Generic exception was raise during spark job name discovery")
                return None
        self.logger.info("Databricks get job name timeout, continuing...")
        return None

    @staticmethod
    def _get_name_from_metadata(metadata: Dict[str, str]) -> Optional[str]:
        if JOB_NAME_KEY in metadata:
            return f'job-{str(metadata[JOB_NAME_KEY]).replace(" ", "-").lower()}'
        elif CLUSTER_NAME_KEY in metadata:
            cluster_name_value = str(metadata[CLUSTER_NAME_KEY]).replace(" ", "-").lower()
            # We've tackled cases where the cluster name includes Run ID, we want to remove it.
            cluster_name_value = re.sub(RUN_ID_REGEX, "", cluster_name_value)
            return cluster_name_value
        return None

    def _cluster_all_tags_metadata(self) -> Optional[Dict[str, str]]:
        """
        Returns `includes spark.databricks.clusterUsageTags.clusterAllTags` tags as `Dict`.
        """
        if not os.path.isfile(DATABRICKS_METRICS_PROP_PATH):
            # We want to retry in case the cluster is still initializing, and the file is not yet deployed.
            return None
        webui = self.get_webui_address()
        if webui is None:
            # retry
            return None
        # The API used: https://spark.apache.org/docs/latest/monitoring.html#rest-api
        apps_url = SPARKUI_APPS_URL.format(webui)
        self.logger.debug("Databricks SparkUI address", apps_url=apps_url)
        try:
            response = self._request_get(apps_url)
        except requests.exceptions.RequestException:
            # Request might fail in cases where the cluster is still initializing, retrying.
            return None
        try:
            apps = response.json()
        except Exception as e:
            if "Spark is starting up. Please wait a while until it's ready" in response.text:
                # Spark is still initializing, retrying.
                # https://github.com/apache/spark/blob/38c41c/core/src/main/scala/org/apache/spark/ui/SparkUI.scala#L64
                return None
            else:
                raise DatabricksJobNameDiscoverException(
                    f"Failed to parse apps url response, query {response.text=}"
                ) from e
        if len(apps) == 0:
            # apps might be empty because of initialization, retrying.
            self.logger.debug("No apps yet, retrying.")
            return None

        env_url = f"{apps_url}/{apps[0]['id']}/environment"
        try:
            response = self._request_get(env_url)
        except Exception as e:
            # No reason for any exception, `environment` uri should be accessible if we have running apps.
            raise DatabricksJobNameDiscoverException(f"Environment request failed {env_url=}") from e
        try:
            env = response.json()
        except Exception as e:
            raise DatabricksJobNameDiscoverException(f"Environment request failed {response.text=}") from e
        props = env.get("sparkProperties")
        if props is None:
            raise DatabricksJobNameDiscoverException(f"sparkProperties was not found in {env=}")
        # Creating a dict of the relevant properties and their values.
        relevant_props_dict = {prop[0]: prop[1] for prop in props if [CLUSTER_TAGS_KEY, CLUSTER_NAME_PROP] in prop[0]}
        if len(relevant_props_dict) == 0:
            raise DatabricksJobNameDiscoverException(f"Failed to create dict of relevant properties {env=}")
        # First, trying to extract `CLUSTER_TAGS_KEY` property, in case not redacted.
        if (
            cluster_all_tags_value := relevant_props_dict.get(CLUSTER_TAGS_KEY)
        ) is not None and "redacted" not in cluster_all_tags_value:
            try:
                cluster_all_tags_value_json = json.loads(cluster_all_tags_value)
            except Exception as e:
                raise DatabricksJobNameDiscoverException(f"Failed to parse {cluster_all_tags_value}") from e
            return {cluster_all_tag["key"]: cluster_all_tag["value"] for cluster_all_tag in cluster_all_tags_value_json}
        # As a fallback, trying to extract `CLUSTER_NAME_PROP` property.
        elif (cluster_name_value := relevant_props_dict.get(CLUSTER_NAME_PROP)) is not None:
            return {CLUSTER_NAME_KEY: cluster_name_value}
        else:
            raise DatabricksJobNameDiscoverException(
                f"Failed to extract {CLUSTER_TAGS_KEY} or {CLUSTER_NAME_PROP} from {props=}"
            )
