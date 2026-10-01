from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import principal_from_token
from db.engine import SessionLocal
from ticker_manager import ticker_manager

router = APIRouter()


@router.websocket("/api/ws/ticker")
async def ws_ticker(websocket: WebSocket):
    # Browsers can't set Authorization headers on a WebSocket, so the JWT is
    # passed as a ?token= query param. Accept a human user OR a service that holds
    # the "market" scope; reject before accepting otherwise.
    token = websocket.query_params.get("token") or ""
    db = SessionLocal()
    try:
        principal = principal_from_token(token, db)
    finally:
        db.close()
    allowed = principal is not None and (
        principal.kind == "user" or "market" in principal.scopes
    )
    if not allowed:
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    await ticker_manager.add_client(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # symbols are already "EXCHANGE|TOKEN" format from the frontend
            symbols = data.get("symbols", [])
            action = data.get("action")
            if action == "subscribe":
                ticker_manager.subscribe(symbols)
                # Replay any already-cached quotes to this client right away -
                # subscribe() no-ops for tokens the broker is already streaming,
                # so without this a (re)connected client sees nothing until the
                # next organic tick.
                await ticker_manager.send_snapshot(websocket, symbols)
            elif action == "unsubscribe":
                ticker_manager.unsubscribe(symbols)
    except WebSocketDisconnect:
        ticker_manager.remove_client(websocket)
