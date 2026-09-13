Call-bridge control plane (default `http://127.0.0.1:43123`):

- `POST /v1/calls` with `{ "script": {…} }`
- `WS /v1/calls/{id}/events` for transcript + `ask_orchestrator`
- `POST /v1/calls/{id}/tool-results` `{ tool_call_id, output }`
- `POST /v1/calls/{id}/guidelines` `{ text, mode: steer|speak }`
- `POST /v1/calls/{id}/hangup`
- `GET /health` and `GET /v1/status` for REGISTER + active call
- Voicemail: agent leaves a short message then hangs up (Live delegates hangup)
- `CALL_IDLE_TIMEOUT_SECONDS` (default 30) BYEs a silent call (`hangup_reason=idle_timeout`)
