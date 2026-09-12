from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. SIP registrar / proxy / domain are provider-specific — see docs/providers/."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bridge_mode: Literal["mock", "live"] = Field(
        default="mock",
        description="mock = no live SIP or voice sockets. live = real SIP registrar + voice provider.",
    )
    voice_provider: Literal["grok", "openai"] = "grok"

    http_host: str = "0.0.0.0"
    http_port: int = 43123
    log_level: str = "INFO"

    xai_api_key: str = ""
    xai_realtime_url: str = "wss://api.x.ai/v1/realtime"
    xai_voice_model: str = "grok-voice-think-fast-2.0"
    xai_voice: str = "eve"
    xai_transcribe_model: str = "grok-transcribe"

    openai_api_key: str = ""
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    openai_realtime_model: str = "gpt-realtime"
    openai_voice: str = "marin"
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"

    # Provider-specific. Leave empty in mock mode. Live requires registrar + domain + creds.
    sip_registrar: str = ""
    sip_proxy: str = ""
    sip_port: int = 5060
    sip_transport: Literal["udp"] = "udp"
    sip_domain: str = ""
    sip_username: str = ""
    sip_password: str = ""
    sip_display_name: str = ""
    sip_local_host: str = "0.0.0.0"
    sip_local_port: int = 5062
    sip_advertise_host: str = ""
    sip_rtp_port_start: int = 40000
    sip_rtp_port_end: int = 40100
    sip_codec: Literal["pcma"] = "pcma"
    sip_register_expires: int = 1800
    sip_user_agent: str = "call-bridge/0.1"

    stun_enabled: bool = True
    stun_server: str = ""
    stun_port: int = 3478

    tool_timeout_seconds: float = 20.0
    hangup_grace_seconds: float = 2.5
    max_concurrent_calls: int = 1

    def sip_proxy_host(self) -> str:
        """Outbound proxy, or the registrar when SIP_PROXY is unset."""
        return self.sip_proxy or self.sip_registrar

    def require_live_sip(self) -> None:
        missing = [
            name
            for name, val in (
                ("SIP_REGISTRAR", self.sip_registrar),
                ("SIP_DOMAIN", self.sip_domain),
                ("SIP_USERNAME", self.sip_username),
                ("SIP_PASSWORD", self.sip_password),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"live mode requires {', '.join(missing)}. "
                "Copy .env.example and fill values from docs/providers/ for your SIP provider."
            )

    def require_live_voice(self, provider: str | None = None) -> None:
        chosen = provider or self.voice_provider
        if chosen == "grok" and not self.xai_api_key:
            raise RuntimeError(
                "live mode with VOICE_PROVIDER=grok requires XAI_API_KEY. "
                "Get a key at https://console.x.ai/"
            )
        if chosen == "openai" and not self.openai_api_key:
            raise RuntimeError(
                "live mode with VOICE_PROVIDER=openai requires OPENAI_API_KEY. "
                "Get a key at https://platform.openai.com/"
            )

    def sip_from_display(self) -> str:
        return self.sip_display_name or self.sip_username or "call-bridge"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
