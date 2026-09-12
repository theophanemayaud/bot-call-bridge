# OpenAI Realtime (voice)

Set `VOICE_PROVIDER=openai` and paste `OPENAI_API_KEY`. Leave the other OpenAI env defaults unless you know you need a different model/voice.

## Capabilities (this bridge)

| Feature | Status |
|---|---|
| Codec | Native **PCMA** 8 kHz both ways (no local resample) |
| Turn taking | Default **`semantic_vad`** (`eagerness=auto`) — closer to ChatGPT Advanced Voice than silence-only VAD |
| Overlap / duplex | `interrupt_response=false` by default — assistant can keep speaking while the callee talks |
| Tools | `ask_orchestrator`, `hangup` (audible goodbye then BYE) |
| Disclosure | `speak_first` / SCRIPT disclosure via `response.create` |
| Probe | `POST /v1/voice/probe` |

**PCMA pitfall:** OpenAI GA rejects nested `audio.input.format.rate` on `audio/pcma`. This repo omits `rate` for G.711. Sending `rate` leaves the session on PCM 24 kHz → severe static on the phone.

## Defaults in `.env.example`

```bash
VOICE_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_REALTIME_URL=wss://api.openai.com/v1/realtime
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_VOICE=marin
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
```

## Example costs (guide — verify on [OpenAI pricing](https://platform.openai.com/docs/pricing))

Realtime bills **audio tokens**, not a flat $/min. Rough planning conversions (duration → tokens): ~1 input audio token / 100 ms of user audio, ~1 output audio token / 50 ms of model audio.

| Model family (list prices per 1M audio tokens) | Listen (user) | Speak (model) | Ballpark full-duplex minute* |
|---|---|---|---|
| Flagship realtime (e.g. gpt-realtime / 2.1 class) | ~$32 / 1M ≈ **~$0.019/min** heard | ~$64 / 1M ≈ **~$0.077/min** spoken | **~$0.05–0.10/min** typical mixed call |
| Mini realtime | ~$10 / 1M ≈ **~$0.006/min** | ~$20 / 1M ≈ **~$0.024/min** | **~$0.015–0.03/min** |

\*Order of magnitude for budgeting only. Silence under VAD often bills little/no input. Optional transcription is a separate rate card. Always re-check the live pricing page before production.

## SIP cost is separate

Phone minutes are billed by the SIP carrier (see [ovh.md](ovh.md)). OpenAI charges stack on top.
