"""HTTP request handling for the read-only local dashboard.

Routing, auth (token/Host/Origin), method enforcement and security headers
all live here. Nothing in this module -- or anywhere in this package --
imports ``workflow``, ``service``, ``publishing``, GitHub mutation helpers,
``subprocess`` or any shell helper. All data comes from the injectable
providers in :mod:`software_agent_factory.dashboard.snapshot`.
"""

from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit

from . import assets
from .sanitize import sanitize_run_detail, sanitize_run_summary
from .security import (
    TOKEN_HEADER,
    TOKEN_QUERY_PARAM,
    host_header_is_valid,
    origin_header_is_valid,
    token_matches,
)
from .snapshot import MIN_SNAPSHOT_LIMIT, clamp_pagination, is_valid_run_id, to_json_safe

_logger = logging.getLogger("software_agent_factory.dashboard")

#: Hard ceiling on the number of query-string fields ``parse_qs`` will
#: accept. The dashboard only ever reads ``token``/``limit``/``offset``, so
#: anything beyond a handful of fields is either a mistake or an attempt to
#: force excessive parsing work; either way it is rejected with a clean 400
#: rather than left to raise an uncaught ``ValueError`` mid-request.
_MAX_QUERY_FIELDS = 16


#: Security headers applied to every response, success or error. No
#: ``Access-Control-*`` header is ever set: this is a same-origin-only
#: viewer, not a cross-origin API.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)

_RUN_DETAIL_PATTERN = re.compile(r"^/api/runs/([^/]+)$")


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Handles one dashboard request. ``self.server`` is a ``DashboardServer``."""

    server_version = "SoftwareAgentFactoryDashboard/1"
    protocol_version = "HTTP/1.1"

    # -- stdlib method hooks -------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Log method + path only. Never the query string: it may carry the
        dashboard token, and that must never reach a log."""
        path_without_query = self.path.split("?", 1)[0]
        _logger.info("%s %s -> %s", self.command, path_without_query, args[-1] if args else "")

    # -- dispatch -------------------------------------------------------------

    def _dispatch(self, *, send_body: bool) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        try:
            query = parse_qs(
                split.query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=_MAX_QUERY_FIELDS,
            )
        except ValueError:
            # Malformed or excessive query string (e.g. more fields than
            # max_num_fields allows): a clean 400, never an unhandled
            # exception bubbling out of request parsing.
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid query"}, send_body)
            return

        bound_host = self.server.server_address[0]
        port = self.server.server_address[1]

        if not host_header_is_valid(self.headers.get("Host"), bound_host, port):
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid host"}, send_body)
            return
        if not origin_header_is_valid(self.headers.get("Origin"), bound_host, port):
            self._respond_json(HTTPStatus.FORBIDDEN, {"error": "invalid origin"}, send_body)
            return
        if not self._token_is_valid(query):
            self._respond_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, send_body)
            return

        try:
            self._route(path, query, send_body)
        except Exception:  # noqa: BLE001 - never leak internals to the client
            _logger.exception("Unhandled dashboard error for path %s", path.split("?", 1)[0])
            self._respond_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal error"},
                send_body,
            )

    def _method_not_allowed(self) -> None:
        self._respond_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method not allowed"},
            send_body=True,
            extra_headers=(("Allow", "GET, HEAD"),),
        )

    def _token_is_valid(self, query: dict[str, list[str]]) -> bool:
        expected = self.server.token
        header_token = self.headers.get(TOKEN_HEADER)
        if token_matches(expected, header_token):
            return True
        query_values = query.get(TOKEN_QUERY_PARAM)
        query_token = query_values[0] if query_values else None
        return token_matches(expected, query_token)

    # -- routing ---------------------------------------------------------------

    def _route(self, path: str, query: dict[str, list[str]], send_body: bool) -> None:
        if path == "/":
            self._serve_index(send_body)
            return
        if path == "/assets/app.js":
            self._respond_bytes(
                HTTPStatus.OK,
                "text/javascript; charset=utf-8",
                assets.APP_JS.encode("utf-8"),
                send_body,
            )
            return
        if path == "/assets/style.css":
            self._respond_bytes(
                HTTPStatus.OK,
                "text/css; charset=utf-8",
                assets.STYLE_CSS.encode("utf-8"),
                send_body,
            )
            return
        if path == "/healthz":
            self._respond_json(HTTPStatus.OK, {"status": "ok"}, send_body)
            return
        if path == "/api/summary":
            self._serve_summary(send_body)
            return
        if path == "/api/runs":
            self._serve_runs(query, send_body)
            return
        detail_match = _RUN_DETAIL_PATTERN.match(path)
        if detail_match:
            self._serve_run_detail(detail_match.group(1), send_body)
            return
        self._respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)

    def _serve_index(self, send_body: bool) -> None:
        html = assets.render_index_html(token=self.server.token)
        self._respond_bytes(
            HTTPStatus.OK, "text/html; charset=utf-8", html.encode("utf-8"), send_body
        )

    def _serve_summary(self, send_body: bool) -> None:
        try:
            # The real build_monitoring_snapshot() rejects limit <= 0, and a
            # summary has no use for the run page anyway, so request the
            # smallest legal page and drop it below.
            snapshot = self.server.snapshot_provider(limit=MIN_SNAPSHOT_LIMIT, offset=0)
        except Exception:  # noqa: BLE001 - provider failures are degraded, not fatal
            _logger.exception("Snapshot provider failed for /api/summary")
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "snapshot unavailable"},
                send_body,
            )
            return
        payload = to_json_safe(snapshot)
        if isinstance(payload, dict):
            payload = {key: value for key, value in payload.items() if key not in ("runs", "page")}
        payload["health"] = self._collect_health()
        self._respond_json(HTTPStatus.OK, payload, send_body)

    def _collect_health(self) -> object:
        provider = self.server.health_provider
        if provider is None:
            return None
        try:
            return to_json_safe(provider())
        except Exception:  # noqa: BLE001 - a broken health check is itself a
            # finding, not a reason to fail the whole summary response.
            _logger.exception("Health provider failed")
            return {"error": "health check unavailable"}

    def _serve_runs(self, query: dict[str, list[str]], send_body: bool) -> None:
        raw_limit = query.get("limit", [None])[0]
        raw_offset = query.get("offset", [None])[0]
        bounds = clamp_pagination(raw_limit, raw_offset)
        if bounds is None:
            self._respond_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid pagination"}, send_body
            )
            return
        limit, offset = bounds

        try:
            snapshot = self.server.snapshot_provider(limit=limit, offset=offset)
        except Exception:  # noqa: BLE001
            _logger.exception("Snapshot provider failed for /api/runs")
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "snapshot unavailable"},
                send_body,
            )
            return

        payload = to_json_safe(snapshot)
        raw_runs = payload.get("runs", []) if isinstance(payload, dict) else []
        page = payload.get("page") if isinstance(payload, dict) else None
        if not isinstance(page, dict):
            page = payload.get("pagination", {}) if isinstance(payload, dict) else {}
        page = dict(page)

        try:
            # Data minimization happens here, in the handler, regardless of
            # what the provider actually returned: only fields the UI
            # renders ever leave this process. A provider that accidentally
            # includes a log, a diff, a prompt or a token in a run object
            # cannot leak it through this response.
            if isinstance(raw_runs, list):
                runs = [sanitize_run_summary(run) for run in raw_runs]
            else:
                runs = []
        except TypeError:
            _logger.exception("Snapshot provider returned an unsanitizable run for /api/runs")
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "snapshot unavailable"},
                send_body,
            )
            return

        # The limit/offset actually applied are authoritative regardless of
        # what the provider echoes back.
        page["limit"] = limit
        page["offset"] = offset
        page["returned"] = len(runs)
        self._respond_json(HTTPStatus.OK, {"runs": runs, "page": page}, send_body)

    def _serve_run_detail(self, raw_run_id: str, send_body: bool) -> None:
        if not is_valid_run_id(raw_run_id):
            self._respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)
            return

        try:
            detail = self.server.run_detail_provider(raw_run_id)
        except Exception:  # noqa: BLE001
            _logger.exception("Run detail provider failed for run %s", raw_run_id)
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "run detail unavailable"},
                send_body,
            )
            return

        if detail is None:
            self._respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)
            return

        try:
            # Same data-minimization guarantee as run summaries: only the
            # allowlisted detail/attempt fields ever leave this process, no
            # matter what the provider actually handed back.
            sanitized = sanitize_run_detail(detail)
        except TypeError:
            _logger.exception(
                "Run detail provider returned unsanitizable data for run %s", raw_run_id
            )
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "run detail unavailable"},
                send_body,
            )
            return

        self._respond_json(HTTPStatus.OK, sanitized, send_body)

    # -- response helpers -------------------------------------------------------

    def _respond_json(
        self,
        status: HTTPStatus,
        payload: object,
        send_body: bool,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._respond_bytes(
            status, "application/json; charset=utf-8", body, send_body, extra_headers=extra_headers
        )

    def _respond_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        send_body: bool,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)
