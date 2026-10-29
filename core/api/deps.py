"""Authentication for the local API (PLAN.md §9).

§9 requires a random per-launch token on every route, so that neither another local
process nor a web page the user happens to have open can query their index. A web page
is the sharper threat: any page can issue cross-origin requests to ``127.0.0.1``, and
without a token it could read the user's email.

Applied as a *router-level* dependency rather than per-route decoration. A new route
added later inherits the requirement automatically; the failure mode of forgetting is
therefore "insecure by default" only if someone explicitly bypasses the router, which is
visible in review.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from core.config import get_settings

#: Header the Tauri host and UI use (§13.1). `Authorization: Bearer` is also accepted so
#: the API stays usable with ordinary curl defaults.
AUTH_HEADERS = ("x-local-token", "authorization")


def extract_token(request: Request) -> str | None:
    """Pull a candidate token from either accepted header."""
    for header in AUTH_HEADERS:
        raw = request.headers.get(header)
        if not raw:
            continue
        if header == "authorization":
            # Accept "Bearer <token>" and a bare token.
            parts = raw.split(None, 1)
            return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else raw
        return raw
    return None


#: Routes allowed to take the token from the query string.
#:
#: `EventSource` — the only SSE API in the WKWebView — cannot set request headers, so the
#: browser development flow (PLAN.md §4 "UI host" row) has no way to authenticate
#: `/events` other than a query parameter. Scoped to this one route because a token in a
#: URL can end up in logs and history, which is why every other route is header-only and
#: the shipped app proxies the stream from Rust with a header instead (§13.1).
#: ``/debug/log`` is included for the same reason but only so a final batch can be sent
#: with ``navigator.sendBeacon`` from ``pagehide``; the endpoint is local-only and the
#: log middleware redacts the query string before writing it.
QUERY_TOKEN_ROUTES = frozenset({"/events", "/debug/log"})


def query_token(request: Request) -> str | None:
    """Token from the query string, for the routes that permit it."""
    if request.url.path not in QUERY_TOKEN_ROUTES:
        return None
    return request.query_params.get("token")


async def require_token(request: Request) -> None:
    """Reject the request unless it carries the current per-launch token.

    Raises 401 rather than 403: without a valid token the caller is not authenticated at
    all, and 401 stops well-behaved clients from retrying.
    """
    expected = get_settings().api_token
    provided = extract_token(request) or query_token(request)

    if provided is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing local API token (PLAN.md §9)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time compare: the token guards a private index, and a timing side channel
    # on a loopback socket is still a side channel.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid local API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
