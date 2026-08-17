"""Shared-token authentication.

Every route except /health requires the token once COMPANION_TOKEN is set.
This became necessary the moment tools could act on the machine: before that,
an unauthenticated request from anyone on the WiFi could set a reminder, which
is a nuisance; now it could lock the screen.

EventSource cannot set request headers, so /events additionally accepts a
short-lived one-time ticket minted from the token-protected /events-ticket
endpoint. The shared token itself never rides a URL: a query-string token
would leak into browser history, Referer headers and proxy logs, and the
access-log redaction in main.py could not reach any of those.
"""

import hmac
import logging
import secrets
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

HEADER = "X-Companion-Token"

# /health stays open so the phone can tell "backend down" from "backend up but
# I have the wrong token" — and so it stays useful for diagnostics. It exposes
# only status, model name and TTS on/off.
EXEMPT_PATHS = {"/health"}

# A ticket lives only long enough for the client to dial in and dies on first
# use, so it is safe in a URL even though it rides one. 60s covers the gap
# between minting the ticket and the EventSource handshake, with slack for a
# slow phone radio.
TICKET_TTL_SECONDS = 60.0

# A client that keeps dialling (or an attacker hammering /events-ticket) must
# not be able to grow this dict without bound.
_MAX_TRACKED_TICKETS = 64

_tickets: dict[str, float] = {}


def _expire_tickets() -> None:
    now = time.monotonic()
    expired = [t for t, exp in _tickets.items() if exp <= now]
    for t in expired:
        _tickets.pop(t, None)


def issue_sse_ticket() -> str:
    """Mint a short-lived, single-use ticket for opening an /events stream."""
    _expire_tickets()
    while len(_tickets) >= _MAX_TRACKED_TICKETS:
        # Dicts preserve insertion order: drop the oldest ticket first. An
        # evicted ticket simply forces the client to mint another one.
        _tickets.pop(next(iter(_tickets)))
    ticket = secrets.token_urlsafe(32)
    _tickets[ticket] = time.monotonic() + TICKET_TTL_SECONDS
    return ticket


def consume_sse_ticket(ticket: str) -> bool:
    """Redeem a ticket. Returns False once per ticket — a ticket is one-shot."""
    exp = _tickets.pop(ticket, None)
    if exp is None:
        return False
    return exp > time.monotonic()


class CompanionTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        expected = settings.companion_token
        if not expected:
            return await call_next(request)

        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        supplied = request.headers.get(HEADER) or ""

        # EventSource cannot set request headers — there is no API for it — so
        # /events also accepts a one-time ticket as a query parameter. The
        # ticket is minted by /events-ticket, which itself requires the token
        # in the header, so the shared secret never leaves that channel.
        if not supplied and request.url.path == "/events":
            ticket = request.query_params.get("ticket") or ""
            if not consume_sse_ticket(ticket):
                logger.warning(
                    "Rejected %s %s from %s (bad or expired SSE ticket)",
                    request.method,
                    request.url.path,
                    request.client.host if request.client else "?",
                )
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # compare_digest so a wrong token cannot be recovered by timing.
        if not hmac.compare_digest(supplied, expected):
            logger.warning(
                "Rejected %s %s from %s (bad or missing token)",
                request.method,
                request.url.path,
                request.client.host if request.client else "?",
            )
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)
