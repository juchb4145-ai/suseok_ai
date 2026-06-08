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
    build_runtime_health_payload,
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


def create_app(db, runtime=None, broadcaster=None) -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed. Install requirements-ws.txt to run the theme WS server.")
    repository = ThemeEngineRepository(db)
    provider = DynamicThemeContextProvider(repository)
    app = FastAPI(title="Dynamic Theme Engine")
    if broadcaster is None and runtime is not None:
        broadcaster = getattr(runtime, "broadcaster", None)

    @app.get("/health")
    async def health():
        if runtime is not None:
            return runtime.health()
        return {"ok": True, "theme_engine": "running" if provider.is_ready() else "warming"}

    @app.get("/api/themes/rank")
    async def theme_rank(top_n: int = 20):
        rank = runtime.get_latest_rank(top_n) if runtime is not None else repository.get_latest_theme_rank(top_n)
        return build_theme_rank_payload(rank, top_n=top_n)

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

    @app.get("/api/theme-runtime/health")
    async def theme_runtime_health():
        if runtime is None:
            return build_runtime_health_payload({"running": False, "data_ready": provider.is_ready()})
        return build_runtime_health_payload(runtime.health())

    @app.websocket("/ws/themes")
    async def ws_themes(websocket: WebSocket):
        token = websocket.query_params.get("token")
        api_key = os.environ.get("THEME_WS_API_KEY")
        if api_key and token != api_key:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if broadcaster is not None:
            await broadcaster.register(websocket)
        try:
            await websocket.send_json(build_heartbeat_payload())
            while True:
                raw = await websocket.receive_text()
                try:
                    request = parse_subscribe_request(json.loads(raw))
                except json.JSONDecodeError:
                    await _send_theme_ws_error(websocket, "invalid websocket JSON message", code="BAD_MESSAGE")
                    continue
                except Exception:
                    await _send_theme_ws_error(websocket, "invalid subscribe request", code="BAD_REQUEST")
                    continue
                if request["action"] != "subscribe":
                    await _send_theme_ws_error(websocket, "unsupported action", code="BAD_REQUEST")
                    continue
                try:
                    await _send_subscription_snapshot(websocket, request, repository, provider, runtime)
                except Exception:
                    await _send_theme_ws_error(websocket, "subscription snapshot failed", code="SNAPSHOT_FAILED")
                await asyncio.sleep(0)
        except WebSocketDisconnect:
            pass
        finally:
            if broadcaster is not None:
                await broadcaster.unregister(websocket)

    return app


async def _send_theme_ws_error(websocket, message: str, *, code: str) -> None:
    await websocket.send_json(build_error_payload(message, code=code))


async def _send_subscription_snapshot(websocket, request, repository, provider, runtime=None) -> None:
    channels = set(request["channels"])
    if "theme_rank" in channels:
        rank = runtime.get_latest_rank(request["top_n"]) if runtime is not None else repository.get_latest_theme_rank(request["top_n"])
        await websocket.send_json(
            build_theme_rank_payload(rank, top_n=request["top_n"])
        )
    if "runtime_health" in channels:
        health = runtime.health() if runtime is not None else {"running": False, "data_ready": provider.is_ready()}
        await websocket.send_json(build_runtime_health_payload(health))
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
