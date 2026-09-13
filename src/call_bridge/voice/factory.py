from __future__ import annotations

from typing import Literal

from call_bridge.config import Settings
from call_bridge.voice.grok import GrokVoiceProvider
from call_bridge.voice.mock import MockVoiceProvider
from call_bridge.voice.openai import OpenAILiveProvider
from call_bridge.voice.provider import VoiceAgentProvider


def create_voice_provider(
    settings: Settings,
    *,
    name: Literal["grok", "openai", "mock"] | None = None,
) -> VoiceAgentProvider:
    chosen = name or ("mock" if settings.bridge_mode == "mock" else settings.voice_provider)
    if chosen == "mock":
        return MockVoiceProvider()
    if chosen == "grok":
        return GrokVoiceProvider(settings)
    if chosen == "openai":
        return OpenAILiveProvider(settings)
    raise ValueError(f"unknown voice provider: {chosen}")
