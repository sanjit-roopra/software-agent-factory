"""Security primitives for the read-only local dashboard (ADR-016).

Everything in this module is deliberately small and dependency-free: token
generation/comparison, loopback bind-host validation and strict same-origin
``Host``/``Origin`` checks. Nothing here mutates a run, a workspace or
configuration, and nothing here imports outside the standard library.
"""

from __future__ import annotations

import secrets

#: Number of random bytes used for the per-process dashboard token. 32 bytes
#: (256 bits) of ``secrets.token_urlsafe`` output is well beyond brute-force
#: range for a token that only needs to survive the lifetime of one process.
TOKEN_BYTES = 32

#: Header a browser-side script must use to authenticate API/asset requests
#: it issues itself (as opposed to the initial page navigation, which can
#: only carry the token as a query parameter).
TOKEN_HEADER = "X-Factory-Token"

#: Query parameter accepted as an alternative to the header, used for the
#: initial HTML page load where a browser cannot attach a custom header.
TOKEN_QUERY_PARAM = "token"

#: The one literal address this server ever binds to. Deliberately a single
#: constant rather than a set of "loopback-equivalent" aliases: accepting
#: other 127.0.0.0/8 literals (127.0.0.2, ...) or an IPv6 loopback literal
#: (``::1``) here would claim a guarantee this implementation does not keep,
#: since it never actually listens on those addresses.
LOOPBACK_HOST = "127.0.0.1"


def generate_token() -> str:
    """Return a fresh, cryptographically random per-process token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_matches(expected: str, candidate: str | None) -> bool:
    """Constant-time comparison of ``candidate`` against ``expected``.

    Returns ``False`` (rather than raising) for any non-string or empty
    candidate so callers can use this directly as a boolean guard.
    """
    if not candidate or not isinstance(candidate, str):
        return False
    return secrets.compare_digest(expected, candidate)


class InvalidBindHostError(ValueError):
    """Raised when a requested bind host is not the one supported literal."""


def validate_bind_host(requested_host: str) -> str:
    """Return ``LOOPBACK_HOST`` if ``requested_host`` names it, or raise.

    Accepts only the literal ``127.0.0.1`` and the literal string
    ``localhost`` (normalized to ``127.0.0.1``, since a browser is what
    actually resolves that name, not this process). Everything else --
    every other ``127.0.0.0/8`` literal, ``::1`` and any other IPv6 address,
    ``0.0.0.0``, ``::``, bare hostnames, and DNS names that merely *resolve*
    to loopback -- is rejected. This server has no IPv6 listening path and
    binds to exactly one address, so nothing beyond that single literal is
    ever trusted here; resolution is deliberately not attempted, since a name
    that resolves to loopback today can resolve elsewhere tomorrow (DNS
    rebinding).
    """
    candidate = (requested_host or "").strip().lower()
    if candidate in (LOOPBACK_HOST, "localhost"):
        return LOOPBACK_HOST
    raise InvalidBindHostError(
        f"Refusing to bind dashboard to non-loopback host: {requested_host!r}"
    )


def expected_origin(bound_host: str, port: int) -> str:
    """The exact origin this server serves, given what it actually bound to."""
    return f"http://{bound_host}:{port}"


def host_header_is_valid(host_header: str | None, bound_host: str, port: int) -> bool:
    """Strictly validate ``Host`` against the exact host+port actually bound.

    Guards against DNS rebinding: only the literal ``host:port`` pair the
    server is actually bound to is accepted -- never ``localhost``, an IPv6
    loopback literal, or any other alias, even though a browser might
    consider those interchangeable. There is exactly one correct value.
    """
    if not host_header:
        return False
    return host_header.strip().lower() == f"{bound_host}:{port}"


def origin_header_is_valid(origin_header: str | None, bound_host: str, port: int) -> bool:
    """Validate ``Origin``: absent is fine, present must be the exact origin.

    No alias is accepted here either: ``Origin: http://localhost:{port}``
    against a server bound to ``127.0.0.1`` is rejected, because it is not
    byte-for-byte the origin this server actually serves.
    """
    if origin_header is None or origin_header == "":
        return True
    return origin_header.strip().lower() == expected_origin(bound_host, port).lower()
