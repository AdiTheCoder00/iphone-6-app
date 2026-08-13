"""Shared-token authentication.

Every route except /health requires the token once COMPANION_TOKEN is set.
This became necessary the moment tools could act on the machine: before that,
an unauthenticated request from anyone on the WiFi could set a reminder, which
is a nuisance; now it could lock the screen.
"""

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

HEADER = "X-Companion-Token"

# /health stays open so the phone can tell "backend down" from "backend up but
# I have the wrong token" — and so it stays useful for diagnostics. It exposes
# only status, model name and TTS on/off.
EXEMPT_PATHS = {"/health"}


class CompanionTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        expected = settings.companion_token
        if not expected:
            return await call_next(request)

        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        supplied = request.headers.get(HEADER) or ""

        # EventSource cannot set request headers — there is no API for it — so
        # /events also accepts the token as a query parameter. That does mean
        # it lands in access logs, which is why it is allowed for this one
        # read-only endpoint and nowhere else.
        if not supplied and request.url.path == "/events":
            supplied = request.query_params.get("token") or ""

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
