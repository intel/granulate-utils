#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#
from __future__ import annotations

import json
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Type, TypeVar

import grpc  # type: ignore # no types-grpc sadly
import psutil

from granulate_utils.containers.container import Container, ContainersClientInterface, TimeInfo, Network
from granulate_utils.exceptions import ContainerNotFound, CriNotAvailableError
from granulate_utils.generated.containers.cri import v1, v1alpha2  # type: ignore
from granulate_utils.linux import ns
from granulate_utils.type_utils import assert_cast

RUNTIMES = [
    "/run/containerd/containerd.sock",
    "/var/run/crio/crio.sock",
]


class _Client:
    api: Any

    def __init__(self, path: str) -> None:
        self.path = path
        with self.stub() as stub:
            version = stub.Version(self.api.api_pb2.VersionRequest())
        self.runtime_name = version.runtime_name

    @contextmanager
    def stub(self):
        with grpc.insecure_channel(self.path) as channel:
            yield self.api.api_pb2_grpc.RuntimeServiceStub(channel)

    @staticmethod
    def _reconstruct_name(container) -> str:
        """
        Reconstruct the name that dockershim would have used, for compatibility with DockerClient.
        See makeContainerName in kubernetes/pkg/kubelet/dockershim/naming.go
        """
        # I know that those labels exist because CRI lists only k8s containers.
        container_name = container.labels["io.kubernetes.container.name"]
        sandbox_name = container.labels["io.kubernetes.pod.name"]
        namespace = container.labels["io.kubernetes.pod.namespace"]
        sandbox_uid = container.labels["io.kubernetes.pod.uid"]
        restart_count = container.annotations["io.kubernetes.container.restartCount"]
        return "_".join(["k8s", container_name, sandbox_name, namespace, sandbox_uid, restart_count])

    def list_containers(self, all_info: bool) -> List[Container]:
        containers = []
        with self.stub() as stub:
            for runtime_container in stub.ListContainers(self.api.api_pb2.ListContainersRequest()).containers:
                if all_info:
                    container = self._get_container(stub, runtime_container.id, verbose=True)
                    if container is not None:
                        containers.append(container)
                else:
                    containers.append(self._create_container(runtime_container, None))
        return containers
    
    R = TypeVar("R")
    
    @staticmethod
    def _wrap_grpc_request(func: Callable[[], R]) -> Optional[R]:
        try:
            return func()
        except grpc._channel._InactiveRpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise

    def _get_container(self, stub, container_id: str, *, verbose: bool) -> Optional[Container]:
        status_response = self._wrap_grpc_request(lambda: stub.ContainerStatus(self.api.api_pb2.ContainerStatusRequest(container_id=container_id, verbose=verbose)))
        if status_response is None:
            return None
        
        pid: Optional[int] = json.loads(status_response.info.get("info", "{}")).get("pid")
        return self._create_container(status_response.status, pid)  

    def get_container(self, container_id: str, all_info: bool) -> Optional[Container]:
        with self.stub() as stub:
            return self._get_container(stub, container_id, verbose=all_info)
    
  
    def _get_networks(self, stub, pod_sandbox_id: str) -> Any:
        stats = self._wrap_grpc_request(lambda: stub.PodSandboxStats(self.api.api_pb2.PodSandboxStatsRequest(pod_sandbox_id=pod_sandbox_id)))
        if stats is None:
            return None
        return stats.stats.linux.network.interfaces
    
    def _container_sandbox_mapping(self, stub) -> dict[str, str]:
        containers = self._wrap_grpc_request(lambda: stub.ListContainers(self.api.api_pb2.ListContainersRequest()))
        return {
            container.id: container.pod_sandbox_id
            for container in containers.containers
        }        
    
    def get_networks(self, container_id: str) -> list[Network]:       
        with self.stub() as stub:
            sandbox_id = self._container_sandbox_mapping(stub)[container_id]
            net_interfaces = self._get_networks(stub, sandbox_id)
            
            return [
                Network(
                    name=net_interface.name,
                    rx_bytes=net_interface.rx_bytes,
                    rx_errors=net_interface.rx_errors,
                    tx_bytes=net_interface.tx_bytes,
                    tx_errors=net_interface.tx_errors,
                )
                for net_interface in net_interfaces
                if net_interface.name.startswith("eth")
            ]

    def _create_container(
        self,
        container,
        pid: Optional[int],
    ) -> Container:
        time_info: Optional[TimeInfo] = None
        if isinstance(container, self.api.api_pb2.ContainerStatus):
            created_at_ns = assert_cast(int, container.created_at)
            started_at_ns = assert_cast(int, container.started_at)
            create_time = datetime.fromtimestamp(created_at_ns / 1e9, tz=timezone.utc)
            start_time = None
            # from ContainerStatus message docs, 0 == not started
            if started_at_ns != 0:
                start_time = datetime.fromtimestamp(started_at_ns / 1e9, tz=timezone.utc)
            time_info = TimeInfo(create_time=create_time, start_time=start_time)

        process: Optional[psutil.Process] = None
        if pid is not None and pid != 0:
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(pid)

        return Container(
            runtime=self.runtime_name,
            name=self._reconstruct_name(container),
            id=container.id,
            labels=container.labels,
            running=container.state == self.api.api_pb2.CONTAINER_RUNNING,
            process=process,
            time_info=time_info,
            networks=self.get_networks(container.id),
        )


class V1Alpha2Client(_Client):
    api = v1alpha2


class V1Client(_Client):
    api = v1


T = TypeVar("T", bound=_Client)


def _try_cri_client(path: str, client: Type[T]) -> Optional[T]:
    try:
        return client(path)
    except grpc.RpcError:
        return None


def _get_client(path: str) -> V1Client | V1Alpha2Client | None:
    path = "unix://" + ns.resolve_host_root_links(path)
    return _try_cri_client(path, V1Client) or _try_cri_client(path, V1Alpha2Client)


class CriClient(ContainersClientInterface):
    def __init__(self) -> None:
        self._clients = []
        for path in RUNTIMES:
            cl = _get_client(path)
            if cl:
                self._clients.append(cl)

        if not self._clients:
            raise CriNotAvailableError(f"CRI is not available at any of {RUNTIMES}")

    def list_containers(self, all_info: bool) -> List[Container]:
        containers: List[Container] = []
        for client in self._clients:
            containers += client.list_containers(all_info)
        return containers

    def get_container(self, container_id: str, all_info: bool) -> Container:
        for client in self._clients:
            container = client.get_container(container_id, all_info)
            if container is not None:
                return container
        raise ContainerNotFound(container_id)

    def get_runtimes(self) -> List[str]:
        return [client.runtime_name for client in self._clients]
