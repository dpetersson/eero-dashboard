"""LAN-only guard.

The dashboard holds a full-control eero session, so it must never be reachable
from outside the home network. Remote management stays with the eero app.

With ``WATCH_LAN_ONLY=true`` (default) every ``/api/*`` request whose client
address is not loopback or RFC 1918 private is rejected with 403. Set
``WATCH_ALLOWED_CIDRS`` (comma list) to tighten this to the actual home subnet.

This only sees the real client address when the backend runs with host
networking or directly on the host. Behind a Docker bridge every request
arrives from the bridge gateway, so the check passes trivially; the real
protection there is not publishing the port beyond the LAN.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from features.watch import config

logger = logging.getLogger("eero.lan_guard")


def _allowed(host: str | None) -> bool:
    if not host:
        return False
    if host == "testclient":
        # Starlette's TestClient sentinel. request.client comes from the ASGI
        # server's socket address, never from a header, so this is not spoofable.
        return True
    try:
        addr = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    cidrs = config.allowed_cidrs()
    if cidrs:
        for c in cidrs:
            try:
                if addr in ipaddress.ip_network(c, strict=False):
                    return True
            except ValueError:
                logger.warning("ignoring bad WATCH_ALLOWED_CIDRS entry %r", c)
        return addr.is_loopback
    return addr.is_loopback or addr.is_private or addr.is_link_local


class LanOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not config.lan_only():
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        host = request.client.host if request.client else None
        if not _allowed(host):
            logger.warning("rejected non-LAN request from %s to %s", host, request.url.path)
            return JSONResponse(status_code=403, content={
                "error": {"code": "lan_only", "message": "This dashboard is only available from the local network"}})
        return await call_next(request)
