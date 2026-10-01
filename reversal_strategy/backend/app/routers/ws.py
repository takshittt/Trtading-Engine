"""Dashboard WebSocket — pushes signals, positions, orders, alerts, logs, prices."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.sso import user_from_token
from app.core.state import STATE

router = APIRouter()


@router.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket):
    # A browser can't set an Authorization header on a WebSocket, so the Gateway
    # user token rides as ?token=. Reject before accepting if it's missing/invalid.
    if user_from_token(ws.query_params.get("token") or "") is None:
        await ws.close(code=1008)  # policy violation
        return
    await STATE.hub.connect(ws)
    try:
        # Periodic price snapshot so the UI always has fresh LTPs even between events.
        while True:
            await asyncio.sleep(2)
            await ws.send_json({"event": "prices", "data": STATE.prices})
    except WebSocketDisconnect:
        STATE.hub.disconnect(ws)
    except Exception:
        STATE.hub.disconnect(ws)
