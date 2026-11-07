Call-bridge control plane (default `http://127.0.0.1:43123`). Install the process from https://github.com/theophanemayaud/bot-call-bridge. Secrets (`SIP_*`, voice API keys) stay on the operator machine — this memory is the HTTP/WS contract only.

Endpoints:

- `POST /v1/calls` with `{ "script": {…} }`
- `WS /v1/calls/{id}/events` for transcript + `ask_orchestrator`
- `POST /v1/calls/{id}/tool-results` `{ tool_call_id, output }`
- `POST /v1/calls/{id}/guidelines` `{ text, mode: steer|speak }`
- `POST /v1/calls/{id}/hangup`
- `POST /v1/calls/{id}/handoff` `{ "action": "human_answered" | "bring_in_owner", "summary" }` when the SCRIPT set `handoff` (Grok calls the named tool; GPT-Live client delegation needs this POST)
- `GET /health` and `GET /v1/status` for REGISTER + active call
- `handoff` events: holding → human_answered → merged, or owner_unavailable / hold_timeout
- Owner number is `HANDOFF_OWNER_NUMBER` on the bridge, not in this memory. The SIP line needs two simultaneous calls.
- Voicemail: agent leaves a short message then hangs up (Live delegates hangup)
- `CALL_IDLE_TIMEOUT_SECONDS` (default 30) BYEs a silent call (`hangup_reason=idle_timeout`)
