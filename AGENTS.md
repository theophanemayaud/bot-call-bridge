# Call-agent integration

This process is the telephony + speech-to-speech worker. The Grok Bot **Call** agent (or any orchestrator) owns the SCRIPT, answers `ask_orchestrator` quickly, and injects steers. It does **not** own SIP, RTP, or the Grok Voice WebSocket.

Skills to copy into the bot later: `skills/setup`, `skills/troubleshoot`, `skills/handle-a-call` (optional `skills/package-call-bot`). Persona stub: `bot/PROFILE.md`.

**Reference live SIP:** [OVH softphone line](docs/providers/ovh.md). Other carriers: [docs/providers](docs/providers/README.md).

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

The model calls `hangup`; the bridge ACKs, waits `HANGUP_GRACE_SECONDS`, then SIP BYE. The control API can hang up at any time.

## Voice providers

`VoiceAgentProvider` in `src/call_bridge/voice/provider.py` is the only interface SIP/RTP talks to.

- **Grok** — implemented.
- **OpenAI Realtime** — implemented (`VOICE_PROVIDER=openai`, PCMA duplex).
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
