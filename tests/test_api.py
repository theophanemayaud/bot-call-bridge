from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

SAMPLE = json.loads(Path("examples/script.sample.json").read_text())


def test_health_and_status(client):
    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["mode"] == "mock"
    status = client.get("/v1/status").json()
    assert status["sip"]["state"] == "registered"
    assert status["voice_provider"] == "grok"


def test_console_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Call Bridge" in res.text


def test_script_rejected_when_empty_goals(client):
    bad = {
        "script": {
            "call": {"to": "+33XXXXXXXXX", "language": "fr"},
            "mission": {"disclosure": "I am an AI assistant calling on behalf of the clinic.", "goals": []},
        }
    }
    res = client.post("/v1/calls", json=bad)
    assert res.status_code == 422


def test_mock_call_lifecycle(client):
    res = client.post("/v1/calls", json={"script": SAMPLE, "provider": "mock"})
    assert res.status_code == 200, res.text
    call = res.json()
    call_id = call["id"]
    assert call["state"] in {"starting", "dialing", "ringing", "bridged", "waiting_orchestrator"}

    deadline = time.time() + 3
    pending = None
    while time.time() < deadline:
        body = client.get(f"/v1/calls/{call_id}").json()
        if body.get("pending_tools"):
            pending = body["pending_tools"][0]
            break
        time.sleep(0.05)
    assert pending is not None
    assert pending["tool"] == "ask_orchestrator"

    answer = client.post(
        f"/v1/calls/{call_id}/tool-results",
        json={"tool_call_id": pending["tool_call_id"], "output": {"answer": "Tuesday 10:00"}},
    )
    assert answer.status_code == 200

    steer = client.post(
        f"/v1/calls/{call_id}/guidelines",
        json={"text": "Wrap up, we will do this later.", "mode": "steer"},
    )
    assert steer.status_code == 200

    hang = client.post(f"/v1/calls/{call_id}/hangup", json={"reason": "test"})
    assert hang.status_code == 200
    ended = hang.json()
    assert ended["state"] in {"ending", "ended", "bridged", "waiting_orchestrator"}

    deadline = time.time() + 3
    while time.time() < deadline:
        body = client.get(f"/v1/calls/{call_id}").json()
        if body["state"] in {"ended", "failed"}:
            assert body["state"] == "ended"
            assert body["hangup_reason"] in {"test", "local", "orchestrator"}
            return
        time.sleep(0.05)
    raise AssertionError("call did not end")


def test_second_call_rejected_while_active(client):
    first = client.post("/v1/calls", json={"script": SAMPLE})
    assert first.status_code == 200
    second = client.post("/v1/calls", json={"script": SAMPLE})
    assert second.status_code == 409
    client.post(f"/v1/calls/{first.json()['id']}/hangup", json={"reason": "cleanup"})


def test_voice_probe_mock(client):
    res = client.post("/v1/voice/probe", json={})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["provider"] == "mock"


def test_event_websocket_receives_register(client):
    with client.websocket_connect("/v1/events") as ws:
        seen = []
        for _ in range(8):
            seen.append(ws.receive_json()["type"])
            if "sip.register" in seen:
                break
        assert "sip.register" in seen


def test_live_settings_fail_fast(monkeypatch):
    monkeypatch.setenv("BRIDGE_MODE", "live")
    monkeypatch.setenv("SIP_REGISTRAR", "")
    monkeypatch.setenv("SIP_DOMAIN", "")
    monkeypatch.setenv("SIP_USERNAME", "")
    monkeypatch.setenv("SIP_PASSWORD", "")
    monkeypatch.setenv("XAI_API_KEY", "")
    from call_bridge.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    with pytest.raises(RuntimeError, match="SIP_REGISTRAR"):
        settings.require_live_sip()
    settings = settings.model_copy(
        update={
            "sip_registrar": "sip.example.test",
            "sip_domain": "example.test",
            "sip_username": "u",
            "sip_password": "p",
        }
    )
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        settings.require_live_voice()
