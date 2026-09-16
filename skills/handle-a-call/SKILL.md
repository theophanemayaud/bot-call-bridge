---
name: handle-a-call
description: Build a SCRIPT, dial through call-bridge, watch events, answer ask_orchestrator, steer, hang up, and report the outcome.
---

# Handle a call

You orchestrate. The bridge speaks and signals.

## Preconditions

`GET /health` is `ok` and `sip.state` is `registered` (`BRIDGE_MODE=live`) or mock. One call at a time. If setup is incomplete, use **setup**.

## 1. Build the SCRIPT

Do not embed secrets or unnecessary personal numbers in git. Shape:

```json
{
  "call": { "to": "+33XXXXXXXXX", "language": "fr" },
  "mission": {
    "disclosure": "… AI disclosure in the callee language …",
    "goals": ["…"],
    "constraints": ["…"],
    "fallbacks": ["voicemail / refuse / busy → short line, hangup"],
    "speak_disclosure_first": true
  },
  "voice": { "voice": "eve", "max_duration_seconds": 180 }
}
```

`call.to` may be `+E.164`; the bridge rewrites `+` → `00` for SIP. Always set a real disclosure.

## 2. Dial and subscribe

Open `WS /v1/calls/{id}/events` as soon as you have the id (or subscribe to `/v1/events` first).

```http
POST /v1/calls
{ "script": { … } }
```

Watch `call.state`: `dialing` → `ringing` / `bridged`. `failed` + `voice.error` / SIP detail → **troubleshoot**, do not invent a conversation.

## 3. During the call

| Event | Your job |
|---|---|
| `transcript` | Track goals. Cumulative user text on Grok may rewrite itself — use the latest snapshot |
| `tool_call` `ask_orchestrator` | Reply in seconds: `POST /v1/calls/{id}/tool-results` `{ "tool_call_id", "output": { "answer": "…" } }` |
| Timeout risk | If you cannot answer, the bridge injects “use fallback, do not invent” |
| Need a silent steer | `POST …/guidelines` `{ "text": "Wrap up.", "mode": "steer" }` |
| Need a spoken line | `{ "mode": "speak" }` (Grok `force_message`) |
| `tool_call` `hangup` | Bridge BYEs after goodbye grace — report the `summary` / `reason` (Live may classify a goodbye delegation as hangup, or fall back after "I'll hang up") |
| You decide to stop | `POST …/hangup` `{ "reason": "orchestrator" }` |
| Voicemail | SCRIPT + Live prompts: short message (who + why), goodbye, hang up. Do not wait in silence |
| Idle (`idle_timeout`) | Bridge BYEs after `CALL_IDLE_TIMEOUT_SECONDS` (default 30) with no user/assistant speech. Comfort-noise RTP does not count. `0` disables |

Do not tell the callee about tool names or the control plane.

## 4. Report the outcome

After `call.state` is `ended` or `failed`, summarize for the operator:

- Number (as dialed), language
- `hangup_reason` / `error`
- Goals met or fallback used
- Facts collected (only what was said)
- Whether a follow-up SCRIPT is needed

Then stop. Do not place a second call until `/v1/status` shows no active call.
