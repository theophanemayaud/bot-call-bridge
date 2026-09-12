from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from call_bridge.config import Settings, get_settings
from call_bridge.events import EventBus
from call_bridge.script import CallScript
from call_bridge.session import CallManager
from call_bridge.sip.client import LiveSipEndpoint
from call_bridge.sip.mock import MockSipEndpoint
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.provider import VoiceSessionConfig
from call_bridge.voice.tools import default_call_tools

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


class StartCallBody(BaseModel):
    script: CallScript
    provider: Literal["grok", "openai", "mock"] | None = None


class HangupBody(BaseModel):
    reason: str = "orchestrator"


class GuidelineBody(BaseModel):
    text: str = Field(..., min_length=1)
    mode: Literal["steer", "speak"] = "steer"


class ToolResultBody(BaseModel):
    tool_call_id: str
    output: dict[str, Any]


class ProbeBody(BaseModel):
    provider: Literal["grok", "openai"] | None = None


def create_sip(settings: Settings):
    if settings.bridge_mode == "mock":
        return MockSipEndpoint()
    settings.require_live_sip()
    return LiveSipEndpoint(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    bus: EventBus = app.state.bus
    sip = create_sip(settings)
    app.state.sip = sip
    try:
        await sip.start()
    except Exception as exc:
        log.error("SIP start failed: %s", exc)
        if settings.bridge_mode == "live":
            raise
    manager = CallManager(settings, sip, bus)
    app.state.manager = manager
    sip_pump = asyncio.create_task(_forward_sip_events(sip, bus), name="sip-events")
    yield
    sip_pump.cancel()
    for record in list(manager.calls.values()):
        if record.state not in {"ended", "failed"}:
            try:
                await manager.hangup(record.id, reason="shutdown")
            except Exception:
                pass
    await sip.stop()


async def _forward_sip_events(sip, bus: EventBus) -> None:
    from call_bridge.events import BridgeEvent

    try:
        async for kind, payload in sip.events():
            bus.publish(BridgeEvent(type=kind, payload=payload))  # type: ignore[arg-type]
    except asyncio.CancelledError:
        raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Call Bridge",
        description="SIP ↔ Grok Voice Realtime control plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.bus = EventBus()

    @app.get("/")
    async def console():
        index = WEB_DIR / "console.html"
        if not index.exists():
            raise HTTPException(404, "operator console missing")
        return FileResponse(index)

    @app.get("/health")
    async def health():
        manager: CallManager = app.state.manager
        sip = manager.sip.register_status()
        healthy = sip.state in {"registered", "registering"} or settings.bridge_mode == "mock"
        return {
            "ok": healthy,
            "mode": settings.bridge_mode,
            "sip": sip.state,
            "voice_provider": settings.voice_provider,
        }

    @app.get("/v1/status")
    async def status():
        manager: CallManager = app.state.manager
        return manager.status()

    @app.post("/v1/calls")
    async def start_call(body: StartCallBody):
        manager: CallManager = app.state.manager
        try:
            record = await manager.start_call(body.script, provider_name=body.provider)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return record.as_dict()

    @app.get("/v1/calls")
    async def list_calls():
        manager: CallManager = app.state.manager
        return [record.as_dict() for record in manager.calls.values()]

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: str):
        manager: CallManager = app.state.manager
        try:
            return manager.get(call_id).as_dict()
        except KeyError as exc:
            raise HTTPException(404, f"unknown call {call_id}") from exc

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, body: HangupBody | None = None):
        manager: CallManager = app.state.manager
        reason = body.reason if body else "orchestrator"
        try:
            record = await manager.hangup(call_id, reason=reason)
        except KeyError as exc:
            raise HTTPException(404, f"unknown call {call_id}") from exc
        return record.as_dict()

    @app.post("/v1/calls/{call_id}/guidelines")
    async def guidelines(call_id: str, body: GuidelineBody):
        manager: CallManager = app.state.manager
        try:
            await manager.inject_guideline(call_id, body.text, mode=body.mode)
        except KeyError as exc:
            raise HTTPException(404, f"unknown call {call_id}") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/calls/{call_id}/tool-results")
    async def tool_results(call_id: str, body: ToolResultBody):
        manager: CallManager = app.state.manager
        try:
            await manager.submit_tool_result(call_id, body.tool_call_id, body.output)
        except KeyError as exc:
            raise HTTPException(404, f"unknown call or tool_call_id") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/voice/probe")
    async def voice_probe(body: ProbeBody | None = None):
        """Open a voice session, wait for session.ready, close. Live-key check."""
        requested = (body.provider if body else None) or settings.voice_provider
        name: Literal["grok", "openai", "mock"]
        if settings.bridge_mode == "mock":
            name = "mock"
        else:
            name = requested
            if name in {"grok", "openai"}:
                try:
                    settings.require_live_voice(name)
                except RuntimeError as exc:
                    raise HTTPException(502, str(exc)) from exc
        provider = create_voice_provider(settings, name=name)
        probe_voice = settings.openai_voice if name == "openai" else settings.xai_voice
        config = VoiceSessionConfig(
            instructions="Probe only. Say nothing.",
            voice=probe_voice,
            language="en",
            keyterms=[],
            tools=default_call_tools(),
        )
        try:
            await provider.connect(config)
            ready = False
            async for event in provider.events():
                if event.type == "session.ready":
                    ready = True
                    break
                if event.type in {"error", "closed"}:
                    break
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            await provider.close()
        return {"ok": ready, "provider": name}

    @app.websocket("/v1/events")
    async def events_ws(ws: WebSocket):
        await ws.accept()
        bus: EventBus = app.state.bus
        for event in bus.history():
            await ws.send_json(event.as_dict())
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                await ws.send_json(event.as_dict())
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    @app.websocket("/v1/calls/{call_id}/events")
    async def call_events_ws(ws: WebSocket, call_id: str):
        await ws.accept()
        bus: EventBus = app.state.bus
        for event in bus.history(call_id):
            await ws.send_json(event.as_dict())
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.call_id == call_id:
                    await ws.send_json(event.as_dict())
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)

    return app
