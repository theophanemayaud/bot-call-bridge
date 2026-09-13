from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


AudioEncoding = Literal["pcma", "pcmu", "pcm16"]


@dataclass(slots=True, frozen=True)
class AudioFormat:
    """Provider-agnostic wire format for the virtual mic/speaker.

    SIP/RTP on this bridge is G.711 a-Law @ 8 kHz. Providers that accept
    native PCMA (Grok Voice, OpenAI GPT-Live) should keep this as-is.
    """

    encoding: AudioEncoding = "pcma"
    sample_rate_hz: int = 8000
    transport: Literal["json", "binary"] = "json"


@dataclass(slots=True, frozen=True)
class TurnDetection:
    # Grok Realtime only. GPT-Live has no turn_detection / interrupt_response;
    # duplex yield is model + prompt.
    # server_vad = silence energy. semantic_vad is unused on the Live path.
    # none = Grok omits VAD (orchestrator / model must continue).
    kind: Literal["server_vad", "semantic_vad", "none"] = "semantic_vad"
    threshold: float | None = None
    silence_duration_ms: int | None = 500
    prefix_padding_ms: int | None = 300
    idle_timeout_ms: int | None = None
    # Unused on GPT-Live. Kept for Grok / shared VoiceSessionConfig.
    eagerness: str | None = "auto"
    # Unused on GPT-Live. Grok Realtime barge-in when using server_vad.
    interrupt_response: bool = True



@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class VoiceSessionConfig:
    instructions: str
    voice: str
    language: str | None
    keyterms: list[str]
    tools: list[ToolSpec]
    audio_in: AudioFormat = field(default_factory=AudioFormat)
    audio_out: AudioFormat = field(default_factory=AudioFormat)
    turn_detection: TurnDetection = field(default_factory=TurnDetection)
    speak_first: str | None = None
    transcribe_model: str | None = None
    enable_web_search: bool = False


@dataclass(slots=True)
class VoiceEvent:
    """Normalized events every VoiceAgentProvider must emit.

    type values:
      session.ready, audio.out, transcript, speech.started, speech.stopped,
      response.done, tool_call, error, closed
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class VoiceAgentProvider(ABC):
    """Thin speech-to-speech adapter. SIP/RTP never imports Grok or OpenAI types."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def connect(self, config: VoiceSessionConfig) -> None:
        """Open the realtime session and apply config. Fail fast on auth/protocol errors."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the realtime socket. Idempotent."""

    @abstractmethod
    async def send_audio(self, frame: bytes) -> None:
        """Push one codec frame from the SIP virtual mic (PCMA 20ms / 160 bytes)."""

    @abstractmethod
    async def inject_guideline(self, text: str) -> None:
        """Mid-call steer from the orchestrator. Not spoken unless speak_verbatim is used."""

    @abstractmethod
    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        """Speak a hard-coded line (disclosure, orchestrator-forced utterance)."""

    @abstractmethod
    async def submit_tool_result(
        self,
        call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
    ) -> None:
        """Return a function-call result and optionally ask the model to continue."""

    @abstractmethod
    def events(self) -> AsyncIterator[VoiceEvent]:
        """Live event stream for the call session / orchestrator."""
