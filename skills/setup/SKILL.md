---
name: setup
description: Install call-bridge, configure a SIP provider and Grok Voice, verify mock then live REGISTER.
---

# Setup

Bring a Linux box from zero to a registered SIP line + Grok Voice. Provider-specific values live in `docs/providers/` — do not invent hosts or logins.

## 1. Install the bridge

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Python 3.12+. Control plane defaults to `http://127.0.0.1:43123`.

## 2. Mock verify (no secrets)

```bash
BRIDGE_MODE=mock python -m call_bridge
```

`GET /health` → `ok: true`, `sip: registered`, `mode: mock`. Operator console `/` can place `examples/script.sample.json`. `pytest` must be green.

## 3. Choose a VoIP provider

| Egress | Start here |
|---|---|
| Cloud / VPS / Grok Bot box | [OVH softphone line](../../docs/providers/ovh.md) — documented working path |
| Home / residential IP only | [VoipWise](../../docs/providers/voipwise.md) or OVH |
| New carrier | [How to add a provider](../../docs/providers/README.md) |

Need a **software SIP line** (no hardware lock). Copy from the provider manager into `.env`:

- `SIP_REGISTRAR`, `SIP_DOMAIN` (required live)
- `SIP_PROXY` if the outbound proxy differs
- `SIP_USERNAME` (often `00` + country + number)
- `SIP_PASSWORD`
- `SIP_LOCAL_PORT=5062` unless you bind 5060 as root
- `SIP_ADVERTISE_HOST` = public IPv4 if NAT/STUN is unreliable
- If the provider has an **IP allowlist**, add this box’s **current egress** IP

If REGISTER from a datacenter returns `500` / policy reject, switch provider — do not fight it in code.

## 4. Voice provider

Live path: `VOICE_PROVIDER=grok` and `XAI_API_KEY` from https://console.x.ai/

Useful knobs: `XAI_VOICE_MODEL=grok-voice-think-fast-2.0`, `XAI_VOICE=eve`. Audio is PCMA 8 kHz — do not change codec to match a headset.

`VOICE_PROVIDER=openai` uses OpenAI GPT-Live-1 (`OPENAI_API_KEY`, optional `OPENAI_VOICE` / `OPENAI_LIVE_MODEL`). Same SIP/PCMA path as Grok. Realtime `semantic_vad` / `interrupt_response` do not apply.

## 5. Live REGISTER check

```bash
set -a && source .env && set +a
BRIDGE_MODE=live VOICE_PROVIDER=grok python -m call_bridge
```

Boot **exits** if SIP fields or `XAI_API_KEY` are missing, or REGISTER fails. Success:

```bash
curl -s localhost:43123/health
curl -s localhost:43123/v1/status
```

Expect `sip.state == registered`. Then `POST /v1/voice/probe` (live opens a short Grok session).

Open UDP `SIP_LOCAL_PORT` and `SIP_RTP_PORT_*` on the host firewall. One registration / one call (`MAX_CONCURRENT_CALLS=1`).

If this fails, use **troubleshoot**. To place a SCRIPT, use **handle-a-call**.
