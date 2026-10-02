"""HTTP request handling for the local dashboard.

Routing, auth (token/Host/Origin), method enforcement and security headers
all live here. ``GET`` serves reads. The only ``POST`` routes are the approve and
answer actions (ADR-033): they check the transport here and the run in
:mod:`.actions`. Nothing in this module -- or anywhere in this package --
imports ``workflow``, ``service``, ``publishing``, GitHub mutation helpers,
``subprocess`` or any shell helper. All data comes from the injectable
providers in :mod:`software_agent_factory.dashboard.snapshot`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from io import BufferedIOBase
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..models import ResumeClassification
from . import assets
from .actions import ResumeActions, accept_action
from .responses import WriteRejected
from .sanitize import sanitize_health, sanitize_run_detail, sanitize_run_summary
from .security import (
    TOKEN_QUERY_PARAM,
    header_token_matches,
    host_header_is_valid,
    origin_header_is_valid,
    required_origin_is_valid,
    token_matches,
)
from .snapshot import MIN_SNAPSHOT_LIMIT, clamp_pagination, is_valid_run_id, to_json_safe
from .view import compare_view, project_view, run_detail_view

if TYPE_CHECKING:
    from .server import DashboardServer

_logger = logging.getLogger("software_agent_factory.dashboard")


def _log_failure_type(message: str, exc: BaseException, *args: object) -> None:
    """Log a failure by its exception type only.

    The message and traceback of a failure can quote run data or a plan
    answer, so neither reaches the log.
    """
    _logger.error(message + ": %s", *args, type(exc).__name__)


#: One event for every approve or answer request, accepted or refused. It holds the run id
#: and the result. It never holds the token or the body.
_audit_logger = logging.getLogger("software_agent_factory.dashboard.audit")

#: Hard ceiling on the number of query-string fields ``parse_qs`` will
#: accept. The dashboard only ever reads ``token``, ``limit``, ``offset`` and the
#: compare ids ``a`` and ``b``, so anything beyond a handful of fields is either a
#: mistake or an attempt to force excessive parsing work; either way it is rejected
#: with a clean 400 rather than left to raise an uncaught ``ValueError`` mid-request.
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


class _UnknownRun:
    """What a read of a run returns when the provider does not know the run."""


_UNKNOWN_RUN = _UnknownRun()
#: The decoded path, so a run id such as ``../etc`` reaches the id check as a ``400`` and is
#: not mistaken for an unknown route.
_ACTION_PREFIX = "/api/runs/"
_ACTION_KINDS = {
    "approve": ResumeClassification.RISK_APPROVAL,
    "answer": ResumeClassification.PLAN_DECISION,
}
#: Largest request body a write accepts. A body of exactly this size is accepted.
MAX_BODY_BYTES = 16 * 1024
#: A refused write whose body is still unread is read and dropped after the answer when it is
#: no larger than this, so the connection stays in step. A larger one closes the connection.
_MAX_DISCARDED_BODY_BYTES = 4 * MAX_BODY_BYTES
#: A ``Content-Length`` with more digits than this is too large to be a size we would accept.
#: It also keeps ``int`` cheap and safe.
_MAX_LENGTH_DIGITS = 12
#: The length of a ``Content-Length`` with too many digits: over every limit, so the write is
#: refused as too large and its body is never dropped.
_OVERSIZED_LENGTH = _MAX_DISCARDED_BODY_BYTES + 1
_MAX_LOGGED_RUN_ID_LENGTH = 128
_LOG_UNSAFE_PATTERN = re.compile(r"[^\x20-\x7e]")
_MAX_LOGGED_PATH_LENGTH = 200
_MAX_LOGGED_METHOD_LENGTH = 16


def _log_safe_text(raw: object, limit: int) -> str:
    """Client-influenced text made safe for a log line.

    Replaces every control or non-ASCII character so a client cannot forge log
    records with embedded newlines or terminal escapes, and bounds the length.
    """
    cleaned = _LOG_UNSAFE_PATTERN.sub("?", "" if raw is None else str(raw))
    return cleaned[:limit]


def _log_safe_path(raw_path: str) -> str:
    """Request path made safe for a log line.

    Drops the query string first: it may carry the dashboard token, and that
    must never reach a log.
    """
    return _log_safe_text(raw_path.split("?", 1)[0], _MAX_LOGGED_PATH_LENGTH)


def _log_safe_method(raw_method: str | None) -> str:
    """Request method made safe for a log line.

    ``BaseHTTPRequestHandler`` takes the method from the request line before
    any check runs, so an unknown method such as ``\\x1b[2J`` reaches
    ``log_message`` verbatim.
    """
    return _log_safe_text(raw_method, _MAX_LOGGED_METHOD_LENGTH)


def _declared_length(raw: str | None) -> int | None:
    """The body length a ``Content-Length`` header declares, or ``None`` when it has none."""
    if raw is None or not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw) if len(raw) <= _MAX_LENGTH_DIGITS else _OVERSIZED_LENGTH


class _WriteBody:
    """The body of one write: its declared length, read at most once, and what is left of it.

    The length is parsed once, from the header. After a refusal the unread part is still on
    the wire: :meth:`remaining_to_drain` says how much to drop so the connection stays in step.
    """

    def __init__(self, raw_length: str | None) -> None:
        self.length = _declared_length(raw_length)
        self._unread = self.length

    def read(self, rfile: BufferedIOBase) -> bytes:
        """The whole body. A missing length is ``400``, a long one ``413``, a silent one ``408``."""
        length = self.length
        if length is None:
            raise WriteRejected(HTTPStatus.BAD_REQUEST, "invalid content length")
        if length > MAX_BODY_BYTES:
            raise WriteRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        # Until the read ends, the place of the stream is not known.
        self._unread = None
        try:
            raw = rfile.read(length)
        except TimeoutError:
            # A read that timed out cannot be read again, so there is nothing to drop either.
            raise WriteRejected(HTTPStatus.REQUEST_TIMEOUT, "the body did not arrive") from None
        self._unread = 0
        return raw

    def remaining_to_drain(self) -> int | None:
        """How many bytes to drop after the answer, or ``None`` when they cannot be dropped.

        A body whose size is unknown or large, or whose read failed, cannot be dropped: the
        connection must close instead, so the next request is never read from the middle of it.
        """
        unread = self._unread
        if unread is None or unread > _MAX_DISCARDED_BODY_BYTES:
            return None
        return unread


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Handles one dashboard request. ``self.server`` is a ``DashboardServer``."""

    server: DashboardServer
    server_version = "SoftwareAgentFactoryDashboard/1"
    protocol_version = "HTTP/1.1"

    # -- stdlib method hooks -------------------------------------------------

    def setup(self) -> None:
        super().setup()
        # Every read of the socket gives up after this long. Without it a client that
        # declares a body and sends none holds a thread and its socket open.
        self.connection.settimeout(self.server.request_timeout_seconds)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming convention
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802
        route = self._action_route()
        if route is None:
            self._method_not_allowed()
            return
        self._serve_action(*route)

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
        _logger.info(
            "%s %s -> %s",
            _log_safe_method(getattr(self, "command", None)),
            # parse_request sets path only after the request line parses; on a
            # malformed line this hook still runs, via send_error.
            _log_safe_path(getattr(self, "path", "")),
            _log_safe_text(args[-1], _MAX_LOGGED_PATH_LENGTH) if args else "",
        )

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

        bound_host, port = self.server.address

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
            _logger.exception("Unhandled dashboard error for path %s", _log_safe_path(path))
            self._respond_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal error"},
                send_body,
            )

    def _decoded_path(self) -> str:
        """The request path without its query, percent-decoded. Empty if it cannot be parsed."""
        try:
            return unquote(urlsplit(self.path).path)
        except ValueError:  # e.g. an unterminated IPv6 literal in the target
            return ""

    def _action_route(self) -> tuple[ResumeActions, str, str] | None:
        """The actions, run id and action name this path names, or ``None`` when it names none.

        Only a server that has actions routes the approve and answer paths.
        """
        actions = self.server.resume_actions
        path = self._decoded_path()
        if actions is None or not path.startswith(_ACTION_PREFIX):
            return None
        # Split at the last "/" without a regex, so a hostile path cannot make matching slow.
        run_id, _, action = path[len(_ACTION_PREFIX) :].rpartition("/")
        if not run_id or action not in _ACTION_KINDS:
            return None
        return actions, run_id, action

    def _method_not_allowed(self) -> None:
        # The body of a refused write, if any, stays unread.
        self.close_connection = True
        allow = "POST" if self._action_route() is not None else "GET, HEAD"
        self._respond_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method not allowed"},
            send_body=True,
            extra_headers=(("Allow", allow),),
        )

    def _token_is_valid(self, query: dict[str, list[str]]) -> bool:
        if header_token_matches(self.server.token, self.headers):
            return True
        query_values = query.get(TOKEN_QUERY_PARAM)
        query_token = query_values[0] if query_values else None
        return token_matches(self.server.token, query_token)

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
        if path == "/api/projects":
            self._serve_projects(send_body)
            return
        if path == "/api/compare":
            self._serve_compare(query, send_body)
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
            return sanitize_health(provider())
        except Exception:  # noqa: BLE001 - a broken health check is itself a
            # finding, not a reason to fail the whole summary response.
            _logger.exception("Health provider failed")
            return {"error": "health check unavailable"}

    def _serve_runs(self, query: dict[str, list[str]], send_body: bool) -> None:
        raw_limit = query.get("limit", [None])[0]
        raw_offset = query.get("offset", [None])[0]
        bounds = clamp_pagination(raw_limit, raw_offset)
        if bounds is None:
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid pagination"}, send_body)
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

    def _serve_projects(self, send_body: bool) -> None:
        provider = self.server.project_provider
        if provider is None:
            self._respond_json(HTTPStatus.OK, {"projects": []}, send_body)
            return
        try:
            payload = to_json_safe(provider())
            raw_projects = payload.get("projects", []) if isinstance(payload, dict) else []
            projects = (
                [project_view(project) for project in raw_projects]
                if isinstance(raw_projects, list)
                else []
            )
        except Exception:  # noqa: BLE001 - provider failures are degraded, not fatal
            _logger.exception("Project provider returned unsanitizable data")
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "project snapshot unavailable"},
                send_body,
            )
            return
        self._respond_json(HTTPStatus.OK, {"projects": projects}, send_body)

    def _serve_run_detail(self, raw_run_id: str, send_body: bool) -> None:
        if not is_valid_run_id(raw_run_id):
            self._respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)
            return
        sanitized = self._load_run(
            raw_run_id, lambda detail: run_detail_view(detail, self._resume_requests), send_body
        )
        if sanitized is not None:
            self._respond_json(HTTPStatus.OK, sanitized, send_body)

    def _serve_compare(self, query: dict[str, list[str]], send_body: bool) -> None:
        a_id = query.get("a", [""])[0]
        b_id = query.get("b", [""])[0]
        if a_id == b_id or not (is_valid_run_id(a_id) and is_valid_run_id(b_id)):
            self._respond_json(HTTPStatus.BAD_REQUEST, {"error": "invalid run ids"}, send_body)
            return
        a_detail = self._read_run(a_id, sanitize_run_detail, send_body)
        if a_detail is None:
            return
        b_detail = self._read_run(b_id, sanitize_run_detail, send_body)
        if b_detail is None:
            return
        if isinstance(a_detail, _UnknownRun) or isinstance(b_detail, _UnknownRun):
            # The labels name the request side only, so the answer holds no run data.
            missing = [
                label
                for label, detail in (("a", a_detail), ("b", b_detail))
                if isinstance(detail, _UnknownRun)
            ]
            self._respond_json(
                HTTPStatus.NOT_FOUND, {"error": "not found", "missing": missing}, send_body
            )
            return
        self._respond_json(HTTPStatus.OK, compare_view(a_id, a_detail, b_id, b_detail), send_body)

    def _load_run(
        self, run_id: str, build: Callable[[Any], dict[str, Any]], send_body: bool
    ) -> dict[str, Any] | None:
        """One valid run id's detail through ``build``, or ``None`` after answering why not.

        An unknown run is ``404``. See :meth:`_read_run` for the rest.
        """
        detail = self._read_run(run_id, build, send_body)
        if isinstance(detail, _UnknownRun):
            self._respond_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, send_body)
            return None
        return detail

    def _read_run(
        self, run_id: str, build: Callable[[Any], dict[str, Any]], send_body: bool
    ) -> dict[str, Any] | _UnknownRun | None:
        """One valid run id's detail through ``build``, ``_UnknownRun`` or ``None``.

        An unknown run is the caller's to answer. A provider that fails or returns data
        ``build`` cannot sanitize is ``503``, answered here, and gives ``None``. The answer
        never holds run data. ``build`` must go through :func:`sanitize_run_detail`, so only
        allowlisted fields ever leave this process, no matter what the provider handed back.
        """
        try:
            detail = self.server.run_detail_provider(run_id)
        except Exception as exc:  # noqa: BLE001
            # The type only: the message and traceback can quote run data.
            _log_failure_type("Run detail provider failed for run %s", exc, run_id)
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "run detail unavailable"}, send_body
            )
            return None
        if detail is None:
            return _UNKNOWN_RUN
        try:
            return build(detail)
        except TypeError as exc:
            _log_failure_type(
                "Run detail provider returned unsanitizable data for run %s", exc, run_id
            )
            self._respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "run detail unavailable"}, send_body
            )
            return None

    def _resume_requests(self, run_id: str, episode_id: str) -> list[object]:
        """The queued requests of one episode. A reader failure shows none and is logged."""
        reader = self.server.resume_request_reader
        if reader is None:
            return []
        try:
            return list(reader(run_id, episode_id))
        except Exception as exc:  # noqa: BLE001 - a damaged request must not hide the run
            # The type only: the message and traceback can quote a plan answer.
            _log_failure_type("Resume request reader failed for run %s", exc, run_id)
            return []

    # -- writes -----------------------------------------------------------------

    def _serve_action(self, actions: ResumeActions, raw_run_id: str, action_name: str) -> None:
        """Run one approve or answer request and write its one audit event."""
        body = _WriteBody(self.headers.get("Content-Length"))
        status, payload, result = self._action_outcome(
            actions, raw_run_id, _ACTION_KINDS[action_name], body
        )
        to_drop = body.remaining_to_drain()
        if to_drop is None:
            self.close_connection = True
        _audit_logger.info(
            "dashboard %s run=%s result=%s",
            action_name,
            _log_safe_text(raw_run_id, _MAX_LOGGED_RUN_ID_LENGTH),
            result,
        )
        self._respond_json(status, payload, send_body=True)
        self._drop_body(to_drop or 0)

    def _drop_body(self, count: int) -> None:
        """Read and discard the unread rest of a refused write's body, after the answer.

        After the answer, so a client that lies about its length only blocks itself, for at
        most the timeout. Then the connection closes: its next request would start in the
        middle of the body.
        """
        try:
            self.rfile.read(count)
        except TimeoutError:
            self.close_connection = True

    def _action_outcome(
        self,
        actions: ResumeActions,
        raw_run_id: str,
        kind: ResumeClassification,
        body: _WriteBody,
    ) -> tuple[HTTPStatus, dict[str, object], str]:
        """The status, JSON body and audit result of one write. Never raises."""
        try:
            self._check_write_headers()
            payload = accept_action(actions, kind, raw_run_id, self._read_json_body(body))
        except WriteRejected as rejected:
            return rejected.status, rejected.payload, rejected.result
        except Exception as exc:  # noqa: BLE001 - never leak internals to the client
            # The type only: the message and traceback can quote a plan answer.
            _log_failure_type("Unhandled dashboard error for an action on a run", exc)
            error = HTTPStatus.INTERNAL_SERVER_ERROR
            return error, {"error": "internal error"}, str(error.value)
        return HTTPStatus.ACCEPTED, payload, str(HTTPStatus.ACCEPTED.value)

    def _check_write_headers(self) -> None:
        """``Host``, ``Origin`` and the token header, in that order. A write needs all three."""
        bound_host, port = self.server.address
        if not host_header_is_valid(self.headers.get("Host"), bound_host, port):
            raise WriteRejected(HTTPStatus.BAD_REQUEST, "invalid host")
        if not required_origin_is_valid(self.headers.get("Origin"), bound_host, port):
            raise WriteRejected(HTTPStatus.FORBIDDEN, "invalid origin")
        if not header_token_matches(self.server.token, self.headers):
            raise WriteRejected(HTTPStatus.UNAUTHORIZED, "unauthorized")

    def _read_json_body(self, body: _WriteBody) -> object:
        """The decoded JSON body of a write: ``415``, ``413`` or ``400`` when it cannot be."""
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise WriteRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
        raw = body.read(self.rfile)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):  # bad bytes, bad JSON or absurdly deep nesting
            raise WriteRejected(HTTPStatus.BAD_REQUEST, "the body is not valid JSON") from None

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
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            self.wfile.write(body)
