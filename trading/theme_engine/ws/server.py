from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.ws.schemas import (
    build_error_payload,
    build_heartbeat_payload,
    build_stock_theme_state_payload,
    build_theme_detail_payload,
    build_theme_rank_payload,
    parse_subscribe_request,
)

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover - optional dependency shell
    FastAPI = None
    WebSocket = None
    WebSocketDisconnect = Exception


def create_app(db) -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed. Install requirements-ws.txt to run the theme WS server.")
    repository = ThemeEngineRepository(db)
    provider = DynamicThemeContextProvider(repository)
    app = FastAPI(title="Dynamic Theme Engine")
    clients: set[WebSocket] = set()

    @app.get("/health")
    async def health():
        return {"ok": True, "theme_engine": "running" if provider.is_ready() else "warming"}

    @app.get("/api/themes/rank")
    async def theme_rank(top_n: int = 20):
        return build_theme_rank_payload(repository.get_latest_theme_rank(top_n), top_n=top_n)

    @app.get("/api/themes/{theme_id}")
    async def theme_detail(theme_id: str):
        theme = repository.get_canonical_theme(theme_id)
        if theme is None:
            return build_error_payload(f"theme not found: {theme_id}", code="NOT_FOUND")
        return build_theme_detail_payload(
            theme_id,
            theme,
            repository.get_members_by_theme(theme_id, active=True),
            provider.get_theme_activity(theme_id),
        )

    @app.get("/api/stocks/{stock_code}/themes")
    async def stock_themes(stock_code: str):
        return build_stock_theme_state_payload(provider.get_stock_theme_state(stock_code))

    @app.websocket("/ws/themes")
    async def ws_themes(websocket: WebSocket):
        token = websocket.query_params.get("token")
        api_key = os.environ.get("THEME_WS_API_KEY")
        if api_key and token != api_key:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        clients.add(websocket)
        try:
            await websocket.send_json(build_heartbeat_payload())
            while True:
                raw = await websocket.receive_text()
                request = parse_subscribe_request(json.loads(raw))
                if request["action"] != "subscribe":
                    await websocket.send_json(build_error_payload("unsupported action", code="BAD_REQUEST"))
                    continue
                await _send_subscription_snapshot(websocket, request, repository, provider)
                await asyncio.sleep(0)
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)

    return app


async def _send_subscription_snapshot(websocket, request, repository, provider) -> None:
    channels = set(request["channels"])
    if "theme_rank" in channels:
        await websocket.send_json(
            build_theme_rank_payload(repository.get_latest_theme_rank(request["top_n"]), top_n=request["top_n"])
        )
    if "theme_detail" in channels:
        for theme_id in request["theme_ids"]:
            theme = repository.get_canonical_theme(theme_id)
            if theme is not None:
                await websocket.send_json(
                    build_theme_detail_payload(
                        theme_id,
                        theme,
                        repository.get_members_by_theme(theme_id, active=True),
                        provider.get_theme_activity(theme_id),
                    )
                )
    if "stock_theme_state" in channels:
        for stock_code in request["stock_codes"]:
            await websocket.send_json(build_stock_theme_state_payload(provider.get_stock_theme_state(stock_code)))
