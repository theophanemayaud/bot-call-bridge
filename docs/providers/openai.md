# OpenAI GPT-Live-1 (voice)

Set `VOICE_PROVIDER=openai` and paste `OPENAI_API_KEY`. Leave the other OpenAI env defaults unless you know you need a different voice.

This path is **GPT-Live only** (`gpt-live-1` over `wss://api.openai.com/v1/live/sessions`). It is not OpenAI Realtime (`/v1/realtime`, `session.update`, `semantic_vad`, `interrupt_response`). SIP stays on this process — do not switch the phone hop to OpenAI direct SIP.

## Capabilities (this bridge)

| Feature | Status |
|---|---|
| Codec | Native **PCMA** 8 kHz both ways (no local resample). Live **requires** `audio.format.rate` on `audio/pcma` |
| Turn taking | Model + prompt (Backchannel / Interruption / Silence policies). No Realtime VAD knobs |
| Tools | Client delegation (`delegation.type=client`) → `session.delegation.created` → classified as `ask_orchestrator` or `hangup` from the current closer (bye / take care / au revoir / ha det bra / I'll hang up). Explicit "I'll hang up" with no follow-up delegation still BYEs after a short settle. |
| Voicemail | Live instructions: short message (who + why), goodbye, then delegate hangup. Do not sit silent after the greeting |
| Idle hangup | Bridge-wide `CALL_IDLE_TIMEOUT_SECONDS` (default 30) — not Live-specific. See `AGENTS.md` |
| Steers | `session.instructions.append` (steer) / verbatim disclosure via instructions.append |
| Orchestrator answers | `session.commentary.append` (spoken, may paraphrase) |
| Probe | `POST /v1/voice/probe` |

**PCMA pitfall:** Live **requires** `{"type":"audio/pcma","rate":8000}`. Omitting `rate` (the old Realtime GA trick) is wrong here. SIP still forwards 8 kHz a-law as-is.

Live does **not** emit `response.audio.done` / `response.done` for spoken turns. Downlink playout is whatever `session.output_audio.delta` chunks have been queued. Barge-in flushes that queue when the first `session.input_transcript.delta` arrives.

## Hangup (client delegation)

Live `session.delegation.created` has metadata only — no tool name. The bridge maps that notice to `hangup` vs `ask_orchestrator` from the **current** closer (assistant text after the last user turn):

- Soft closers (`bye`, `take care`, `au revoir`, `ha det bra`, …) count as hangup **only when Live already delegated**. They do not auto-BYE on their own (mid-call "I'll take care of the booking" must not hang up).
- Explicit intent (`I'll hang up`, `je raccroche`, `jeg legger på`) still BYEs if the model says it will hang up and then never delegates. The fallback waits until assistant audio/transcript has been idle for ~1.8s and cancels on user barge-in. It does **not** add steady-state playout delay.
- `ask_orchestrator` no longer blocks a later `hangup`: the session consumes hangup while an orchestrator answer is still pending, then SIP BYE.

SCRIPT + Live instructions tell the model: after goodbye, delegate hangup in the same turn — do not ask the orchestrator "what next" when the call is over.

## Defaults in `.env.example`

```bash
VOICE_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_LIVE_URL=wss://api.openai.com/v1/live/sessions
OPENAI_LIVE_MODEL=gpt-live-1
OPENAI_VOICE=marin
```

`OPENAI_REALTIME_*` and `OPENAI_TRANSCRIBE_MODEL` are gone. `VOICE_PROVIDER=openai` means Live-1.

## Example costs (guide — verify on [OpenAI pricing](https://platform.openai.com/docs/pricing))

GPT-Live-1 voice is billed at about **$0.05 per minute**, per second (not rounded up). Delegated backend work (Call/orchestrator, or a future Responses backend) is **separate**.

| Item | Ballpark |
|---|---|
| Voice session (`gpt-live-1`) | **~$0.05/min** of session duration |
| Client-delegation / orchestrator | Your Call agent / other backend, not OpenAI voice minutes |

Always re-check the live pricing page before production.

## SIP cost is separate

Phone minutes are billed by the SIP carrier (see [ovh.md](ovh.md)). OpenAI charges stack on top. This bridge remains a **server audio bridge** (OVH SIP + RTP PCMA ↔ Live WebSocket).
