# Adding a SIP provider

Call Bridge is a SIP user-agent, not a carrier. It REGISTER/INVITE/BYEs against whatever registrar you put in `SIP_*`. Voice stays behind `VoiceAgentProvider`.

## What the bridge needs from a provider

| Need | Env | Notes |
|---|---|---|
| Registrar host | `SIP_REGISTRAR` | INVITE/REGISTER Request-URI host |
| Outbound proxy | `SIP_PROXY` | Optional. Empty → send UDP to the registrar |
| Auth realm / From host | `SIP_DOMAIN` | Digest realm is whatever the 401/407 sends; From URI uses this |
| Username | `SIP_USERNAME` | Often the line number in `00…` international form |
| Password | `SIP_PASSWORD` | Never commit |
| Codec | `SIP_CODEC=pcma` | Bridge offers G.711 a-Law only |
| Transport | `SIP_TRANSPORT=udp` | UDP only in this milestone |
| Reachable RTP | `SIP_ADVERTISE_HOST` + RTP range | Far end must send audio back to you |

Dial rewrite: `+33…` → `00…` (`normalize_dial_user`). Keep that if the provider is European / Betamax-style. If a future provider wants raw `+E.164`, add a setting — do not special-case one carrier in `session.py`.

## Checklist for a new `docs/providers/<name>.md`

1. Product type (softphone line vs hardware-locked vs trunk).
2. Manager path for login, domain, proxy, password reset.
3. Whether REGISTER from **cloud/datacenter IPs** works (this is the Grok Bot box question).
4. IP allowlist / restriction steps.
5. Digest, UDP, PCMA notes.
6. Dial format (`00` vs `+`).
7. STUN / NAT advice.
8. Pricing order-of-magnitude, labelled as a guide — verify in the manager.
9. Example `.env` block with **placeholders only**.

Then link the file from the table below and from `skills/setup/SKILL.md`.

## Documented providers

### SIP / VoIP

| Provider | Cloud / bot-box egress | Doc |
|---|---|---|
| **OVH** (softphone SIP line) | Proven reference path | [ovh.md](ovh.md) |
| VoipWise | Often `500` from datacenter IPs; residential/softphone only | [voipwise.md](voipwise.md) |

### Voice (Realtime)

| Provider | Switch | Doc |
|---|---|---|
| **OpenAI Realtime** | `VOICE_PROVIDER=openai` + `OPENAI_API_KEY` | [openai.md](openai.md) |
| **xAI Grok Voice** | `VOICE_PROVIDER=grok` + `XAI_API_KEY` | [xai-grok.md](xai-grok.md) |

Do not add Twilio/Telnyx as required for MVP. Optional later providers get their own markdown — no code fork.
