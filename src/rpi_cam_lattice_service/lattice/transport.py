"""HTTP/2 transport for the Connect (gRPC) clients, with TLS wired in."""

from __future__ import annotations

from pyqwest import SyncClient, SyncHTTPTransport

from ..config import Config, ConfigError
from ..logging_setup import get_logger

logger = get_logger(__name__)


def base_url(config: Config) -> str:
    return f"https://{config.lattice_endpoint}"


def build_http_client(config: Config) -> SyncClient:
    """Build the shared pyqwest client, trusting an Anduril-issued CA if configured.

    Recommended offline mechanism: trust the Anduril-issued CA certificate as
    an additional root while keeping full TLS verification enabled. The OS
    trust store is included too so public/cloud endpoints keep working.
    """
    if config.skip_tls_verify_requested:
        logger.warning(
            "SKIP_TLS_VERIFY is set but is not supported by the gRPC/Connect "
            "transport (TLS verification cannot be disabled). To connect to "
            "an offline environment, set LATTICE_CA_CERT_PATH to the "
            "Anduril-issued CA certificate instead."
        )

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
            "trusting Anduril-issued CA certificate", ca_cert_path=config.ca_cert_path
        )

    transport = SyncHTTPTransport(tls_ca_cert=ca_cert, tls_include_system_certs=True)
    return SyncClient(transport)
