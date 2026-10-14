# xAI Grok Voice 3.0

Set `VOICE_PROVIDER=grok` and paste `XAI_API_KEY`. Other Grok env defaults are ready to use.

Model id is **`grok-voice-3.0`**, pinned. `grok-voice-latest` is the alias and currently points at 3.0. `grok-voice-2.0` is deprecated. The Speech to Speech API (endpoint, session fields, events) is unchanged from 2.0, at the same price.

Sources checked 2026-10-14:

- [Speech to Speech](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech)
- [Pricing and rate limits](https://docs.x.ai/developers/models/speech-to-speech)
- [Release notes](https://docs.x.ai/developers/release-notes)
- [Realtime API reference](https://docs.x.ai/developers/rest-api-reference/inference/voice)
- [Announcement](https://x.ai/news/grok-voice-3)

Nothing in this repo has been run against a live Grok socket since the model pin. See [Verified vs not](#verified-vs-not).

## Capabilities (this bridge)

| Feature | Status |
|---|---|
| Endpoint | `wss://api.x.ai/v1/realtime?model=grok-voice-3.0` |
| Codec | Native **PCMA** 8 kHz both ways (`audio/pcma`, no `rate` field — rate is PCM-only) |
| Turn taking | `server_vad`. The shared config name `semantic_vad` is mapped to `server_vad`. `none` sends `null` |
| Reasoning | `reasoning.effort=none` (server default is `high`) |
| Tools | Named functions `hangup` and `ask_orchestrator` (`question` required). No transcript guessing |
| Live user transcript | `audio.input.transcription.model=grok-transcribe` so `…transcription.updated` / `completed` are emitted |
| Disclosure | `force_message` (verbatim). Not used for the orchestrator wait |
| Orchestrator wait | One `response.create` instruction for all open `ask_orchestrator` calls: checking, may take a few seconds, the model may tell the person. Not a conversation item and not a scripted line |
| Probe | `POST /v1/voice/probe` |

`hangup` is local: the bridge ACKs with `function_call_output`, keeps playing queued goodbye audio, waits `HANGUP_GRACE_SECONDS`, then SIP BYE. That wait does not block the event consumer, so the orchestrator still receives transcripts. `ask_orchestrator` is emitted on the call events WebSocket and answered with `POST /v1/calls/{id}/tool-results`. A future `dial_digits` (DTMF) tool is a `ToolSpec` plus `LOCAL_TOOL_NAMES` plus a branch next to `hangup` in the session dispatcher. It is not implemented.

If the model says goodbye and never calls `hangup`, the line stays up. The orchestrator watches the transcript and hangs up with `POST /v1/calls/{id}/hangup`.

## Session shape

`session.update` sends the documented fields this bridge uses:

- `voice`, `instructions`, `reasoning.effort` (`none` or `high`)
- `turn_detection.type` = `server_vad` (optional `idle_timeout_ms` only when set)
- `audio.input` / `audio.output` format `audio/pcma`, transport `json`
- `audio.input.transcription`: model `grok-transcribe`, `language_hint`, `keyterms` (max 100, 50 characters)
- `tools`: `{ "type": "function", "name", "description", "parameters" }` for `hangup` and `ask_orchestrator`

Undocumented VAD knobs (`threshold`, `silence_duration_ms`, `prefix_padding_ms`) are not sent. The Speech to Speech session table lists `type` and `idle_timeout_ms` only.

Function calls arrive as `response.function_call_arguments.done` (`name`, `call_id`, `arguments`). The bridge returns `conversation.item.create` with `function_call_output`. `function_call_output` is not billed as a text input. `response.create` continues the model after an orchestrator answer. `hangup` does not continue. xAI's overlap note says to wait until current playback finishes before `response.create`. The checking turn waits for audio already queued (capped, tool path only). There is no steady-state playout buffer.

## Defaults in `.env.example`

```bash
VOICE_PROVIDER=grok
XAI_API_KEY=
XAI_REALTIME_URL=wss://api.x.ai/v1/realtime
XAI_VOICE_MODEL=grok-voice-3.0
XAI_VOICE=eve
XAI_TRANSCRIBE_MODEL=grok-transcribe
XAI_REASONING_EFFORT=none
```

## Pricing (guide — verify in the [xAI console](https://console.x.ai) and the pricing page)

The pricing page (page date September 22, 2026) and the October 14, 2026 announcement agree: Grok Voice 3.0 is the same price as Grok Voice 2.0.

| Item | Price |
|---|---|
| Audio | **$0.08 / minute** or **$4.80 / hour** |
| Text input | **$0.004** per `conversation.item.create` |
| `function_call_output` | Not billed as text input |
| Items whose content is `input_audio` or `audio` | Audio meter, not the text fee |
| `response.create` | Not a text-input charge. Generated audio is on the audio meter |

`server_vad` sessions (this bridge) are billed for **session duration**. Push-to-talk sessions are billed only for audio sent and received; this bridge does not use push-to-talk.

Published limits: 10 concurrent sessions per team, 120 minutes max session, region `us-east-1`. This process still allows one call (`MAX_CONCURRENT_CALLS=1`). SIP carrier minutes are separate (see [ovh.md](ovh.md)).

## When to pick Grok vs OpenAI

| Prefer Grok when… | Prefer OpenAI when… |
|---|---|
| You want the published Grok Voice 3.0 speech-to-speech model | You want GPT-Live-1 at about $0.05/min |
| You want Realtime function tools on `session.update` | You want full-duplex Live (this tree still classifies Live delegations from the closer) |

Switch with `VOICE_PROVIDER=` only.

## Verified vs not

Verified in unit tests and the mock call (no live xAI or SIP):

- Default model id `grok-voice-3.0`, reasoning `none`, PCMA without a rate field, `server_vad`
- `hangup` and `ask_orchestrator` are sent as `type: function`, and `question` is required
- A `response.function_call_arguments.done` event becomes a `tool_call`. A goodbye transcript does not
- Checking context is one `response.create` instruction, not a conversation item and not `force_message`. A second ask while that turn is open does not send another `response.create`
- An answered call skips a late checking turn. The answer's `response.create` waits out an in-flight checking turn
- Mock call events include the live transcript and the `ask_orchestrator` tool. Hangup does not block that stream
- Spoken goodbye alone does not SIP BYE

Not verified on a live call:

- That `grok-voice-3.0` accepts this `session.update` and returns the documented events
- That a system-role `conversation.item.create` is accepted (the realtime reference lists user, assistant, function call, and function-call output; this bridge already used a system item for steers)
- That `response.create` before `function_call_output` is accepted for the checking turn
- End-to-end latency, barge-in, goodbye playout, or the $0.08/min meter on a real session
