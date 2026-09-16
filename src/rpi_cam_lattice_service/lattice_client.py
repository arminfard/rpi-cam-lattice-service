"""Builds the EntityManager, VideoManager and TaskManager Connect clients with
TLS and auth wired in.

The TaskManager surface is limited to what an agent needs:
listening for tasks routed to the entitym and reporting their status.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from protobuf import Oneof
from connectrpc.protocol import ProtocolType
from pyqwest import SyncClient, SyncHTTPTransport

from anduril.entitymanager.v1.entity_manager_api_pub_connect import (
    EntityManagerAPIClientSync,
)
from anduril.entitymanager.v1.entity_manager_api_pub_pb import (
    GetEntityRequest,
    PublishEntityRequest,
)
from anduril.taskmanager.v1.task_manager_api_pub_connect import (
    TaskManagerAPIClientSync,
)
from anduril.taskmanager.v1.task_manager_api_pub_pb import (
    EntityIds,
    ListenAsAgentRequest,
    ListenAsAgentResponse,
    UpdateStatusRequest,
)
from anduril.taskmanager.v1.task_pub_pb import (
    ErrorCode,
    Principal,
    Status,
    StatusUpdate,
    System,
    TaskError,
    TaskStatus,
    TaskVersion,
)
from anduril.videomanager.v1.create_ingress_stream_request_pub_pb import (
    CreateIngressStreamRequest,
)
from anduril.videomanager.v1.srt_settings_pub_pb import SrtSettings
from anduril.videomanager.v1.video_manager_api_pub_connect import (
    VideoManagerAPIClientSync,
)

from .auth import AuthProvider
from .config import Config, ConfigError
from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class SrtIngressInfo:
    """Result of registering an SRT ingress stream with Lattice VideoManager."""

    video_id: str  # the ingress stream id — advertised on the entity's Media
    push_url: str  # the SRT URL the producer (MediaMTX) should push to
    session_id: str  # the SRT streamid the producer must include


class LatticeClient:
    """A thin wrapper around the generated EntityManager Connect client."""

    def __init__(self, config: Config) -> None:
        self._config = config

        if config.skip_tls_verify_requested:
            logger.warning(
                "SKIP_TLS_VERIFY is set but is not supported by the gRPC/Connect "
                "transport (TLS verification cannot be disabled). To connect to "
                "an offline environment, set LATTICE_CA_CERT_PATH to the "
                "Anduril-issued CA certificate instead."
            )

        # Recommended offline mechanism: trust the Anduril-issued CA certificate
        # as an additional root, keeping full TLS verification enabled.
        ca_cert: bytes | None = None
        if config.ca_cert_path:
            try:
                with open(config.ca_cert_path, "rb") as handle:
                    ca_cert = handle.read()
            except OSError as exc:
                raise ConfigError(
                    f"failed to read LATTICE_CA_CERT_PATH ({config.ca_cert_path}): {exc}"
                ) from exc
            logger.info(
                "trusting Anduril-issued CA certificate",
                ca_cert_path=config.ca_cert_path,
            )

        transport = SyncHTTPTransport(
            tls_ca_cert=ca_cert,
            # Also trust the OS trust store so public/cloud endpoints keep working.
            tls_include_system_certs=True,
        )
        self._http = SyncClient(transport)
        self._auth = AuthProvider(config, self._http)

        self._client = EntityManagerAPIClientSync(
            f"https://{config.lattice_endpoint}",
            http_client=self._http,
            protocol=ProtocolType.GRPC,
        )
        self._video = VideoManagerAPIClientSync(
            f"https://{config.lattice_endpoint}",
            http_client=self._http,
            protocol=ProtocolType.GRPC,
        )
        self._tasks = TaskManagerAPIClientSync(
            f"https://{config.lattice_endpoint}",
            http_client=self._http,
            protocol=ProtocolType.GRPC,
        )

    def publish_entity(
        self, request: PublishEntityRequest, *, timeout_ms: int | None = None
    ) -> None:
        """Publish (upsert) an entity, attaching fresh auth metadata."""
        self._client.publish_entity(
            request, headers=self._auth.headers(), timeout_ms=timeout_ms
        )

    def get_entity(self, entity_id: str, *, timeout_ms: int | None = None):
        """Read an entity back (used by the verification script)."""
        return self._client.get_entity(
            GetEntityRequest(entity_id=entity_id),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )

    def create_srt_ingress(
        self,
        *,
        title: str,
        passphrase: str = "",
        ingress_id: str | None = None,
        timeout_ms: int | None = 30000,
    ) -> SrtIngressInfo:
        """Register an SRT ingress stream with Lattice VideoManager.

        Returns the video id (to advertise on the entity) and the SRT URL the
        producer should push to. Raises on failure — the caller decides whether
        to proceed without video.
        """
        request = CreateIngressStreamRequest(
            title=title,
            ingress_id=ingress_id,
            ingress=Oneof("srt", SrtSettings(passphrase=passphrase or None)),
        )
        response = self._video.create_ingress_stream(
            request, headers=self._auth.headers(), timeout_ms=timeout_ms
        )
        oneof = response.ingress  # Oneof(field, value)
        if oneof is None or oneof.field != "srt":
            raise RuntimeError(
                f"VideoManager returned unexpected ingress type: "
                f"{getattr(oneof, 'field', None)}"
            )
        srt = oneof.value
        return SrtIngressInfo(
            video_id=response.ingress_id,
            push_url=srt.url,
            session_id=srt.session_id,
        )

    # -- tasks ---------------------------------------------------------------

    def listen_as_agent(
        self, entity_id: str, *, heartbeat_interval_ms: int = 30000
    ) -> Iterator[ListenAsAgentResponse]:
        """Open the long-lived agent stream for tasks routed to ``entity_id``.

        No client timeout is applied: the stream is meant to stay open. The
        server sends a heartbeat every ``heartbeat_interval_ms`` (0 disables
        heartbeats) so a silent connection can be told apart from a quiet one.
        """
        request = ListenAsAgentRequest(
            agent_selector=Oneof("entity_ids", EntityIds(entity_ids=[entity_id])),
            heartbeat_interval_ms=heartbeat_interval_ms,
        )
        return self._tasks.listen_as_agent(
            request, headers=self._auth.headers(), timeout_ms=None
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
        """Report a task status change on behalf of the agent entity.

        ``status_version`` must be strictly greater than the last version
        Lattice holds for the task, otherwise the update is ignored.
        """
        task_status = TaskStatus(status=status)
        if error_message:
            task_status.task_error = TaskError(code=error_code, message=error_message)
        update = StatusUpdate(
            version=TaskVersion(
                task_id=task_id,
                definition_version=definition_version,
                status_version=status_version,
            ),
            status=task_status,
            author=Principal(agent=Oneof("system", System(entity_id=agent_entity_id))),
        )
        response = self._tasks.update_status(
            UpdateStatusRequest(status_update=update),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )
        return response.task.version

    # Deliberately no create_task here: the daemon is an *agent*. Tasks are
    # created by operators (Lattice UI) or, for testing only, by
    # scripts/send_task.py, which extends this client for that purpose.

    def close(self) -> None:
        # Close the pyqwest client.
        self._http = None

    def __enter__(self) -> "LatticeClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
