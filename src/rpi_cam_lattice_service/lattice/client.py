"""``LatticeClient``: one object per process holding the TLS transport, the
auth provider, and a thin client per Lattice API (entities, video, tasks).

The facade delegates each call so consumers (the publish loop, the video
ingress, the task handler, the scripts) see a flat method set. The task
surface is limited to what an agent needs: listening for tasks routed to the
entity and reporting their status.
"""

from __future__ import annotations

from collections.abc import Iterator

from anduril.entitymanager.v1.entity_manager_api_pub_pb import (
    GetEntityResponse,
    PublishEntityRequest,
)
from anduril.taskmanager.v1.task_manager_api_pub_pb import ListenAsAgentResponse
from anduril.taskmanager.v1.task_pub_pb import ErrorCode, Status, TaskVersion

from ..config import Config
from .auth import AuthProvider
from .entities import EntityClient
from .tasks import TaskClient
from .transport import base_url, build_http_client
from .video import SrtIngressInfo, VideoClient


class LatticeClient:
    """Facade over the per-API clients, sharing one transport and auth provider.

    Note: scripts/send_task.py subclasses this and reaches ``self._tasks.stub``
    and ``self._auth`` to add the operator-side calls the daemon must not have.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = build_http_client(config)
        self._auth = AuthProvider(config, self._http)
        url = base_url(config)
        self._entities = EntityClient(url, self._http, self._auth)
        self._video = VideoClient(url, self._http, self._auth)
        self._tasks = TaskClient(url, self._http, self._auth)

    # -- entities ------------------------------------------------------------

    def publish_entity(
        self, request: PublishEntityRequest, *, timeout_ms: int | None = None
    ) -> None:
        self._entities.publish_entity(request, timeout_ms=timeout_ms)

    def get_entity(
        self, entity_id: str, *, timeout_ms: int | None = None
    ) -> GetEntityResponse:
        return self._entities.get_entity(entity_id, timeout_ms=timeout_ms)

    # -- video ---------------------------------------------------------------

    def create_srt_ingress(
        self,
        *,
        title: str,
        passphrase: str = "",
        ingress_id: str | None = None,
        timeout_ms: int | None = 30000,
    ) -> SrtIngressInfo:
        return self._video.create_srt_ingress(
            title=title, passphrase=passphrase, ingress_id=ingress_id, timeout_ms=timeout_ms
        )

    def delete_srt_ingress(
        self, ingress_id: str, *, timeout_ms: int | None = 30000
    ) -> None:
        self._video.delete_srt_ingress(ingress_id, timeout_ms=timeout_ms)

    # -- tasks (agent side only) ---------------------------------------------
    # Deliberately no task creation here: the daemon is an agent. Tasks are
    # created by operators (Lattice UI) or, for testing only, by
    # scripts/send_task.py.

    def listen_as_agent(
        self, entity_id: str, *, heartbeat_interval_ms: int = 30000
    ) -> Iterator[ListenAsAgentResponse]:
        return self._tasks.listen_as_agent(
            entity_id, heartbeat_interval_ms=heartbeat_interval_ms
        )

    def update_task_status(
        self,
        *,
        task_id: str,
        definition_version: int,
        status_version: int,
        status: Status,
        agent_entity_id: str,
        error_message: str | None = None,
        error_code: ErrorCode = ErrorCode.FAILED,
        timeout_ms: int | None = 10000,
    ) -> TaskVersion:
        return self._tasks.update_task_status(
            task_id=task_id,
            definition_version=definition_version,
            status_version=status_version,
            status=status,
            agent_entity_id=agent_entity_id,
            error_message=error_message,
            error_code=error_code,
            timeout_ms=timeout_ms,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        # Close the pyqwest client.
        self._http = None

    def __enter__(self) -> "LatticeClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
