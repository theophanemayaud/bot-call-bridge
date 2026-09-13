# Bot Call Bridge — agent context

This process is the telephony + speech-to-speech worker. Skills to copy into the bot later: `skills/setup`, `skills/troubleshoot`, `skills/handle-a-call` (optional `skills/package-call-bot`). Persona stub: `bot/PROFILE.md`.

## What this repo is

Headless **server audio bridge**: SIP/RTP on one side, a voice-model WebSocket on the other. No local microphone or speaker.

The Grok Bot **Call** agent (or any orchestrator) owns the SCRIPT, answers `ask_orchestrator` quickly, and injects steers. This process owns SIP, RTP, and the voice WebSocket. It does **not** own the conversation script.

## Goals

**Real-time (happy path).** A primary system goal is minimum end-to-end latency on the normal conversational path. Prefer solutions that do not permanently delay the media stream — no constant prebuffer / jitter delay as the default fix. When fixing duplex micro-cuts, prefer selective barge-in flush / policy changes over adding steady-state playout delay. *(Théophane, 2026-09-13)*

Also true and durable:

- **Server audio bridge** — OVH SIP (caller ID + credit) ↔ this process ↔ voice provider (GPT-Live-1 / Grok / mock). Do not switch the phone hop to OpenAI direct SIP by default.
- **Wire audio** — PCMA 8 kHz, 20 ms frames. No resample unless a future provider cannot take PCMA.
- **Ownership** — Call / orchestrator owns SCRIPT + tool answers; this bridge owns SIP / RTP / voice WS.
- **One line** — `MAX_CONCURRENT_CALLS=1`. Fail a second `start_call`.
- **Voicemail** — short message (who + why), goodbye, hang up. Do not sit in silence after a greeting. Idle safety net: `CALL_IDLE_TIMEOUT_SECONDS` (default 30, `0` disables).
- **OpenAI path** — GPT-Live-1 (`VOICE_PROVIDER=openai`). Not OpenAI Realtime VAD (`/v1/realtime`, `semantic_vad`, `interrupt_response`).

## Architecture

SIP UAC + RTP (PCMA) + `VoiceAgentProvider` behind an HTTP/WS control plane (`HTTP_PORT`, default `43123`).

**Reference live SIP:** [OVH softphone line](docs/providers/ovh.md). Other carriers: [docs/providers](docs/providers/README.md). Voice: [GPT-Live-1](docs/providers/openai.md), [Grok](docs/providers/xai-grok.md).

## Call-agent integration

The Call agent owns the SCRIPT and steers. It does **not** own SIP, RTP, or the voice WebSocket.

## Control plane

Base URL is this process (`HTTP_PORT`, default `43123`).

| Action | Endpoint |
|---|---|
| Place a call | `POST /v1/calls` `{ "script": {…}, "provider": "grok" }` |
| Call state | `GET /v1/calls/{id}` |
| Hang up | `POST /v1/calls/{id}/hangup` `{ "reason": "orchestrator" }` |
| Mid-call steer | `POST /v1/calls/{id}/guidelines` `{ "text": "…", "mode": "steer" \| "speak" }` |
| Answer a tool | `POST /v1/calls/{id}/tool-results` `{ "tool_call_id", "output" }` |
| Live events | `WS /v1/calls/{id}/events` or `WS /v1/events` |
| Health | `GET /health` · `GET /v1/status` |

`mode=steer` is a system instruction (not spoken). `mode=speak` is a verbatim TTS line (`force_message` on Grok).

## SCRIPT

See `examples/script.sample.json`. Required:

- `call.to` — dial string. `+33…` is rewritten to `00…` (OVH and similar European UACs).
- `call.language` — BCP-47 / short code; also Grok `language_hint`.
- `mission.disclosure` — AI disclosure. Spoken first when `speak_disclosure_first` is true.
- `mission.goals` — at least one.
- `mission.constraints` / `mission.fallbacks` — optional prompt bullets.

Do not put secrets or real personal numbers in the SCRIPT checked into git.

## Events the Call agent must handle quickly

Subscribe to the call WebSocket before or immediately after `POST /v1/calls`.

- `call.state` — `dialing` → `ringing`/`bridged` → `waiting_orchestrator` → `ended` / `failed`
- `transcript` — `{ role, text, final }`
- `tool_call` — `{ tool, tool_call_id, arguments }`
- `voice.error`, `sip.invite`, `sip.hangup`

### `ask_orchestrator`

Reply in a few seconds:

```json
{
  "tool_call_id": "…",
  "output": {
    "answer": "Confirm Tuesday 10:00. If they push back, offer Thursday 14:00."
  }
}
```

Timeout (`TOOL_TIMEOUT_SECONDS`): the bridge tells the model to use a fallback and **not invent facts**.

### `hangup`

The model calls `hangup` (Grok / function tools) or Live client-delegates after a spoken goodbye. The bridge ACKs, waits `HANGUP_GRACE_SECONDS`, then SIP BYE. The control API can hang up at any time.

**Voicemail:** SCRIPT + Live instructions tell the agent to leave a short message (who + why), say goodbye, then hang up. Do not sit in silence after a greeting.

**Idle safety net:** `CALL_IDLE_TIMEOUT_SECONDS` (default 30, `0` disables) BYEs any call with no user transcript and no assistant transcript/audio for that long (`hangup_reason=idle_timeout`). Comfort-noise RTP does not count. `max_duration` remains the hard cap.

## Voice providers

`VoiceAgentProvider` in `src/call_bridge/voice/provider.py` is the only interface SIP/RTP talks to.

- **Grok** — implemented.
- **OpenAI GPT-Live-1** — implemented (`VOICE_PROVIDER=openai`, PCMA duplex, client delegation).
- **mock** — `BRIDGE_MODE=mock`.

Native wire audio is **PCMA 8 kHz**. Do not add a resample path unless a future voice provider cannot take PCMA.

## One line

`MAX_CONCURRENT_CALLS=1`. Fail a second `start_call`.

## Fail-fast

Live boot without `SIP_REGISTRAR` / `SIP_DOMAIN` / `SIP_USERNAME` / `SIP_PASSWORD` and the active voice key (`XAI_API_KEY` or `OPENAI_API_KEY`) exits. A failed REGISTER fails process start.

## What not to add

- Twilio / Telnyx as a required hop
- Local microphone or speaker capture
- Auth on this control plane (keep the process private until the Call agent has a token story)
- Backwards-compat shims for SCRIPT or event names
- Personal numbers or SIP passwords in repo files
- Permanent playout delay / constant prebuffer as the default latency tradeoff (see [Goals](#goals))
