# OpenAI GPT-Live-1 (voice)

Set `VOICE_PROVIDER=openai` and paste `OPENAI_API_KEY`. Leave the other OpenAI env defaults unless you know you need a different voice or backend.

This path is **GPT-Live only** (`gpt-live-1` over `wss://api.openai.com/v1/live/sessions`). It is not OpenAI Realtime (`/v1/realtime`, `session.update`, `semantic_vad`, `interrupt_response`). SIP stays on this process — do not switch the phone hop to OpenAI direct SIP.

## Capabilities (this bridge)

| Feature | Status |
|---|---|
| Codec | Native **PCMA** 8 kHz both ways (no local resample). Live **requires** `audio.format.rate` on `audio/pcma` |
| Turn taking | Model + prompt (Backchannel / Interruption / Silence policies). No Realtime VAD knobs |
| Tools | Responses delegation. The backend calls named `hangup` and `ask_orchestrator`. The bridge does not guess a tool from spoken words |
| Voicemail | Live instructions: short message (who + why), goodbye, then delegate so the backend calls `hangup`. Do not sit silent after the greeting |
| Idle hangup | Bridge-wide `CALL_IDLE_TIMEOUT_SECONDS` (default 30) — no user or assistant speech at all, not a goodbye classifier. See `AGENTS.md` |
| Steers | `session.instructions.append` with `delegation_id: null` (steer and verbatim disclosure) |
| Orchestrator answers | Speak the answer with `session.commentary.append` (`delegation_id` null). Then `session.update` sets backend `tool_choice` to `none`, `response.item.create` returns `function_call_output`, and `response.create` asks for a final message. `tool_choice` returns to `required` when that response completes. `hangup` submits the result and does not continue |
| Probe | `POST /v1/voice/probe` |

**PCMA pitfall:** Live **requires** `{"type":"audio/pcma","rate":8000}`. Omitting `rate` (the old Realtime GA trick) is wrong here. SIP still forwards 8 kHz a-law as-is.

Live does **not** emit `response.audio.done` / `response.done` for spoken turns. Downlink playout is whatever `session.output_audio.delta` chunks have been queued. Barge-in flushes that queue when the first `session.input_transcript.delta` arrives. Handling a tool event does not pause that audio path.

## Named tools (responses delegation)

Session start sets:

```json
{
  "delegation": {
    "type": "responses",
    "responses": {
      "model": "gpt-6-luna",
      "instructions": "…route exactly one function…",
      "tools": ["hangup", "ask_orchestrator"],
      "tool_choice": "required",
      "parallel_tool_calls": false,
      "reasoning": {"effort": "none"},
      "service_tier": "auto",
      "max_output_tokens": 256
    }
  }
}
```

`session.delegation.created` is only a timer mark (`target: "responses"`, no tool name). The call arrives later as `response.event` → `response.output_item.done` with a `function_call` item (`call_id`, `name`, `arguments`). An arguments-done event is ignored.

`tool_choice` starts as `required`, so a delegation becomes a named tool. Leaving it required after the result makes the backend call `ask_orchestrator` again (a confirmation) instead of a final message, and Live never speaks the answer. Before `response.create`, the bridge sets `tool_choice` to `none`. Another `ask_orchestrator` on a delegation that already has a result is answered locally with that result. It is not sent to the orchestrator. When the orchestrator call opens, one commentary gives Live the context that the orchestrator is checking and that it may take a few seconds, and that Live can let the callee know if it wants. It does not prescribe the words. That is one commentary event, not a playout buffer.

| Tool | Who runs it |
|---|---|
| `ask_orchestrator` | Emitted on the call events WebSocket (`question` argument). Answer with `POST /v1/calls/{id}/tool-results`. Same transcript stream as before |
| `hangup` | Local. ACK the function, keep playing queued goodbye audio, wait `HANGUP_GRACE_SECONDS`, SIP BYE. Does not wait on an orchestrator reply |

The bridge logs `delegation_ms` from `session.delegation.created` to the function call. That wait is the backend model, not a playout buffer. RTP is not held for it.

If Live says goodbye and never produces a `hangup` call, the line stays up. The orchestrator watches transcripts and hangs up with `POST /v1/calls/{id}/hangup`. Spoken words are not a hangup signal.

### Adding a tool later

`dial_digits` (DTMF) is not implemented. To add it: a `ToolSpec` in `default_call_tools()`, the name in `LOCAL_TOOL_NAMES`, and a branch in the session dispatcher next to `hangup`. Orchestrator tools go in `ORCHESTRATOR_TOOL_NAMES` instead. The backend prompt says to call exactly one function from the tool list.

## Backend model and cost

Default backend is **`gpt-6-luna`** with **`reasoning.effort=none`**.

OpenAI's Live delegation guide says to start with `gpt-6-luna` (use `gpt-6-sol` only for harder backend tasks). Luna is the current efficient model for short tool routing, and `none` is its lowest effort (no reasoning tokens). `low` / `medium` / `high` / `xhigh` / `max` are available via `OPENAI_LIVE_REASONING_EFFORT` if a real call mis-routes. `max_output_tokens` defaults to 256, which is enough for one short function call at effort `none`; raise it if you raise effort, because reasoning tokens share that budget.

Prices below are from the [OpenAI pricing](https://platform.openai.com/docs/pricing) page and the [gpt-6-luna](https://developers.openai.com/api/docs/models/gpt-6-luna) model page as checked for this change. Standard tier, short context (≤272K):

| Item | Price |
|---|---|
| Input | **$0.10 / 1M tokens** |
| Cached input | **$0.01 / 1M tokens** |
| Output | **$0.50 / 1M tokens** |
| Fast / `service_tier=priority` | **2×** those rates |

Illustrative one delegation (not measured on a live call): ~500 tokens of stable instructions and tool schemas, ~1,500 tokens of conversation context, ~60 tokens of function-call output, effort `none`.

- Uncached: about **$0.00023** `(2000 × $0.10 + 60 × $0.50) / 1e6`
- If the instruction prefix is cached: about **$0.00019**

Five delegations on a call are well under **$0.002**. Voice is separate: **`gpt-live-1` is $0.05 per minute**, billed per second. Phone minutes are the SIP carrier (see [ovh.md](ovh.md)).

`OPENAI_LIVE_SERVICE_TIER=priority` is Fast mode. Leave it at `auto` unless you have measured a need and accept the 2× token price.

Always re-check the live pricing page before production. This repo has not opened a real Live socket to confirm the nested event fields beyond the published guides.

## Defaults in `.env.example`

```bash
VOICE_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_LIVE_URL=wss://api.openai.com/v1/live/sessions
OPENAI_LIVE_MODEL=gpt-live-1
OPENAI_VOICE=marin
OPENAI_LIVE_BACKEND_MODEL=gpt-6-luna
OPENAI_LIVE_REASONING_EFFORT=none
OPENAI_LIVE_SERVICE_TIER=auto
OPENAI_LIVE_MAX_OUTPUT_TOKENS=256
```

`OPENAI_REALTIME_*` and `OPENAI_TRANSCRIBE_MODEL` are gone. `VOICE_PROVIDER=openai` means Live-1.

## SIP cost is separate

Phone minutes are billed by the SIP carrier (see [ovh.md](ovh.md)). OpenAI charges stack on top. This bridge remains a **server audio bridge** (OVH SIP + RTP PCMA ↔ Live WebSocket).
