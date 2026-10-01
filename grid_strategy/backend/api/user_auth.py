"""Human login for grid's dashboard.

grid has NO user store — the Gateway is the single identity provider. This
endpoint verifies the submitted credentials against the Gateway (proxied to
/api/auth/signin) and relays the Gateway-issued user JWT back to the browser.

This gives grid its own independent login session: you can be signed into the
Gateway as one user and into grid as a different (Gateway-registered) user —
the two tokens live in separate origins and are never shared.
"""
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import settings


class LoginRequest(BaseModel):
    email: str
    password: str


def build_user_auth_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(req: LoginRequest):
        async with httpx.AsyncClient(base_url=settings.gateway_base_url, timeout=15.0) as c:
            try:
                resp = await c.post("/api/auth/signin",
                                    json={"email": req.email, "password": req.password})
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Gateway unreachable: {e}")

        if resp.status_code != 200:
            detail = "Invalid email or password"
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            # relay client errors as-is; treat Gateway server errors as auth failure
            raise HTTPException(status_code=resp.status_code if resp.status_code < 500 else 401,
                                detail=detail)

        data = resp.json()
        return {"token": data["token"], "user": data["user"]}

    return router
