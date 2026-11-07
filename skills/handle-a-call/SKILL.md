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
| `tool_call` `ask_orchestrator` | Reply in seconds with a contextual answer (see below): `POST /v1/calls/{id}/tool-results` `{ "tool_call_id", "output": { "answer": "…" } }` |
| Timeout risk | If you cannot answer, the bridge injects “use fallback, do not invent” |
| Need a silent steer | `POST …/guidelines` `{ "text": "Wrap up.", "mode": "steer" }` |
| Need a spoken line | `{ "mode": "speak" }` (Grok `force_message`) |
| `tool_call` `hangup` | Bridge BYEs after goodbye grace — report the `summary` / `reason` (Live may classify a goodbye delegation as hangup, or fall back after "I'll hang up") |
| You decide to stop | `POST …/hangup` `{ "reason": "orchestrator" }` |
| Voicemail | SCRIPT + Live prompts: short message (who + why), goodbye, hang up. Do not wait in silence |
| Idle (`idle_timeout`) | Bridge BYEs after `CALL_IDLE_TIMEOUT_SECONDS` (default 30) with no user/assistant speech. Comfort-noise RTP does not count. `0` disables. Suspended while a warm handoff is holding or relayed |
| `handoff` | Stages of an opted-in warm handoff. Do not answer `human_answered` with `/tool-results` — the bridge ACKs that tool |

Do not tell the callee about tool names or the control plane.

### Answering `ask_orchestrator` (contextual, never canned)

Each delegation is a real question. Read the `tool_call` `arguments` (question + context) **and the last few transcript turns**, then answer with your own judgment. Never auto-reply with a template, a static facts block, or "answer only with these facts" — a background monitor may *forward* tool calls to you, but must not answer them by itself.

Each `answer` is one short, speakable instruction:

1. **Outcome** — what you did, noted, or know (`Noted: Fri 25 Sep remote work, I'll pass it on.`).
2. **What to say** — the line to confirm aloud, in the call language.
3. **Next step** — e.g. `Ask if anything else; if not, goodbye then hang up.`

| Situation | Answer |
|---|---|
| Fact you have | Just that fact + next step. Not the whole brief |
| Request to note / do something | Confirm it is noted (or done) + say it aloud + next step |
| Needs real work or time | Reply **immediately** with a holding line (`Say you're checking, keep them on the line.`), do the work, then `POST …/guidelines` `{ "mode": "speak" }` with the result |
| Unknown / out of scope | Say so briefly (`I don't have that; offer to follow up.`). Do not invent |
| Callee is wrapping up / said goodbye | Short confirmation + `Say goodbye and hang up.` Do not ask open questions or reopen the conversation |

Keep it fast (real-time first): answer within a few seconds, one or two sentences. Do not send a `speak`/`steer` that contradicts or reopens a goodbye the model just said.

### Warm handoff

Add `handoff` (or `handoff_to`) to the SCRIPT. The owner number is `HANDOFF_OWNER_NUMBER` unless that SCRIPT sets `handoff.to`. Put the connecting line and the owner context line in the call language. Set `voice.max_duration_seconds` long enough for the queue plus the conversation (`HANDOFF_MAX_HOLD_SECONDS`, default 600, stops a hold that never reaches a person).

Watch `handoff` events (`holding` → `human_answered` → `announcing` → `dialing_owner` → `merged`, or `owner_unavailable` / `hold_timeout`). Grok calls `human_answered` itself. GPT-Live client delegation cannot name that tool (PR #12). Until then, when the transcript shows a live person, post:

```http
POST /v1/calls/{id}/handoff
{ "action": "human_answered", "summary": "Support agent greeted us" }
```

Do not infer that inside the bridge from keywords. The SIP line must allow two simultaneous calls (OVH Découverte does not).

## 4. Report the outcome

After `call.state` is `ended` or `failed`, summarize for the operator:

- Number (as dialed), language
- `hangup_reason` / `error`
- Goals met or fallback used
- Facts collected (only what was said)
- Whether a follow-up SCRIPT is needed

Then stop. Do not place a second call until `/v1/status` shows no active call.
