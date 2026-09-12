SIP is configured with `SIP_REGISTRAR`, `SIP_PROXY`, `SIP_DOMAIN`, `SIP_USERNAME`, `SIP_PASSWORD`. The bridge is not a carrier.

Prefer OVH (softphone SIP line, no hardware lock) when the bridge runs on a cloud or Grok-bot box — see docs/providers/ovh.md. VoipWise often returns 500 from datacenter IPs; treat it as residential-only (docs/providers/voipwise.md).

Voice: Grok (`XAI_API_KEY`, `VOICE_PROVIDER=grok`) or OpenAI Realtime (`OPENAI_API_KEY`, `VOICE_PROVIDER=openai`) for PCMA duplex.
