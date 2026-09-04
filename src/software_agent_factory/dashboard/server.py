"""The dashboard's HTTP server: binding, token lifecycle and startup.

``DashboardServer`` is a small ``ThreadingHTTPServer`` subclass that refuses
to bind to anything but a loopback address, generates (or accepts) a
per-process token, and exposes the two injectable providers the request
handler calls into. Starting the dashboard is always an explicit action by
whoever imports and calls :func:`create_server` -- nothing in this package
starts a server as a side effect of import.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import ThreadingHTTPServer

from .handler import DashboardRequestHandler
from .security import generate_token, validate_bind_host
from .snapshot import HealthProvider, RunDetailProvider, SnapshotProvider

#: Binding to port 0 asks the OS for an ephemeral free port, which is the
#: right default for both tests (no port collisions) and casual local use
#: (an operator who wants a fixed, memorable port can still request one).
DEFAULT_PORT = 0

DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True, kw_only=True)
class DashboardConfig:
    """Everything needed to start a dashboard instance.

    ``snapshot_provider`` and ``run_detail_provider`` are the only required
    points of contact with real data; both are mandatory so a caller cannot
    accidentally stand up a dashboard with no data source. ``health_provider``
    is optional -- a dashboard with no configured health source simply
    reports ``health: null`` rather than refusing to start. Pass fakes in
    tests and thin wrappers around ``observability.build_monitoring_snapshot``
    / a safe run lookup / ``doctor.run_doctor`` in production wiring.
    """

    snapshot_provider: SnapshotProvider
    run_detail_provider: RunDetailProvider
    health_provider: HealthProvider | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None


class DashboardServer(ThreadingHTTPServer):
    """A loopback-only, read-only HTTP server for the local dashboard."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: DashboardConfig) -> None:
        bind_host = validate_bind_host(config.host)
        self.token: str = config.token or generate_token()
        self.snapshot_provider: SnapshotProvider = config.snapshot_provider
        self.run_detail_provider: RunDetailProvider = config.run_detail_provider
        self.health_provider: HealthProvider | None = config.health_provider
        super().__init__((bind_host, config.port), DashboardRequestHandler)

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    @property
    def dashboard_url(self) -> str:
        """A ready-to-open browser URL, including the auth token.

        This is the one sanctioned place the token is printed/returned to an
        operator; it must never be written through the logging module (see
        ``DashboardRequestHandler.log_message``).
        """
        return f"{self.base_url}/?token={self.token}"


def create_server(config: DashboardConfig) -> DashboardServer:
    """Construct (but do not start serving on) a dashboard server."""
    return DashboardServer(config)
