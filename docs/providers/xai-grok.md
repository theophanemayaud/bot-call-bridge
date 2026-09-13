# xAI Grok Voice Realtime

Set `VOICE_PROVIDER=grok` and paste `XAI_API_KEY`. Other Grok env defaults are ready to use.

## Capabilities (this bridge)

| Feature | Status |
|---|---|
| Codec | Native **PCMA** 8 kHz both ways |
| Turn taking | `server_vad` (Grok); bridge maps semantic defaults when needed |
| Tools | `ask_orchestrator`, `hangup` |
| Disclosure | `force_message` / speak-first path |
| Probe | `POST /v1/voice/probe` |

Proven live on OVH SIP + this bridge (PCMA duplex). Prefer OVH Via `received=` advertise on multi-path cloud boxes.

## Defaults in `.env.example`

```bash
VOICE_PROVIDER=grok
XAI_API_KEY=
XAI_REALTIME_URL=wss://api.x.ai/v1/realtime
XAI_VOICE_MODEL=grok-voice-think-fast-2.0
XAI_VOICE=eve
XAI_TRANSCRIBE_MODEL=grok-transcribe
```

## Example costs (guide — verify in the [xAI console](https://console.x.ai))

xAI Voice is billed from your xAI / Grok Voice credits (top-ups in the console). There is **no stable public $/min table** as clear as OpenAI’s token card — treat the console balance and usage pages as source of truth.

Planning tips:

- Top up a small credit pack before the first live call and watch burn on a 1–2 minute test.
- SIP carrier minutes (OVH) are **separate** from xAI credits.
- For an OpenAI-style token estimate while comparing vendors, use [openai.md](openai.md) ballparks, then measure Grok on a short call.

## When to pick Grok vs OpenAI

| Prefer Grok when… | Prefer OpenAI when… |
|---|---|
| You already have xAI Voice credits / Grok tooling | You want published GPT-Live $/min pricing |
| You want Realtime function tools (`session.update`) | You want full-duplex Live + client delegation |

Switch with `VOICE_PROVIDER=` only — no code change.
