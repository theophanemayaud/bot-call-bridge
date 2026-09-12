from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("BRIDGE_MODE", "mock")
os.environ.setdefault("VOICE_PROVIDER", "grok")
os.environ.setdefault("HTTP_PORT", "43123")


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("BRIDGE_MODE", "mock")
    from call_bridge.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def client(settings):
    from call_bridge.app import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
