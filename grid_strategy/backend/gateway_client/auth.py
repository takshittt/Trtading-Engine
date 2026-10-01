"""Service-identity auth against the Gateway.

grid is a machine/service: it exchanges its client_id + client_secret
(GATEWAY_CLIENT_ID / GATEWAY_CLIENT_SECRET, issued by the Gateway's
scripts/register_service.py) for a short-lived, scoped *service* JWT. That token
is attached to every Gateway REST call and the ticker WebSocket URL.
"""

import httpx

from config.settings import settings


async def fetch_service_token() -> str:
    """POST the client credentials to the Gateway and return a fresh service JWT.
    Raises httpx.HTTPStatusError on 401 (bad/absent credentials)."""
    async with httpx.AsyncClient(base_url=settings.gateway_base_url, timeout=15.0) as c:
        r = await c.post("/api/auth/service-token", json={
            "client_id": settings.gateway_client_id,
            "client_secret": settings.gateway_client_secret,
        })
        r.raise_for_status()
        return r.json()["token"]
