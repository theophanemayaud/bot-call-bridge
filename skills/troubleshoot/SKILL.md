---
name: troubleshoot
description: Diagnose SIP REGISTER/INVITE, one-way audio, IP allowlists, datacenter blocks, digest/PCMA, and mid-call unregistration.
---

# Troubleshoot

Read `GET /health` and `GET /v1/status` first (`sip.state`, `sip.detail`, `mode`, `active_calls`). Process logs: `LOG_LEVEL=DEBUG python -m call_bridge`. Event stream: `WS /v1/events`.

## REGISTER

| Symptom | Likely cause | What to do |
|---|---|---|
| Boot exits “requires SIP_*” | Empty `.env` | Fill registrar, domain, username, password |
| **401** looping | Bad user/password or realm | Reset SIP password in the provider manager; username is often `00…` not `+…` |
| **403** | IP allowlist or account lock | Add this box’s **egress** IPv4 (`/32`) or disable restriction; confirm the line is a **softphone** line |
| **500** from consumer SIP | Datacenter / VPS IP blocked | Expected for VoipWise-class providers. Move to OVH or residential egress. Do not retry-storm |
| Timeout, no 401 | UDP filtered or wrong host | `SIP_REGISTRAR` / `SIP_PROXY` / `SIP_PORT=5060`; allow outbound+inbound UDP |
| Bind error on 5060 | Privileged port | `SIP_LOCAL_PORT=5062` |
| `sip.state` idle/failed after a while | Expiry or IP drift | See mid-call below |

Digest is HTTP Digest on 401/407. A single 401 then 200 is normal. Repeated 401 after the authorized REGISTER is a credential or username-format problem.

## INVITE

| Symptom | Likely cause | What to do |
|---|---|---|
| 404 / 484 | Dial string | SCRIPT `+33…` becomes `00…`. Confirm the provider wants that form |
| 486 / 603 | Callee busy / reject | Not a bridge bug |
| 403 on INVITE | Outbound barred, ceiling, or allowlist | Check hors-forfait / credit; OVH group suspends at the ceiling |
| 408 / timeout | Far end or proxy | Confirm REGISTER still `registered` |

## One-way audio / NAT

Far-end silence while the agent speaks (or the reverse): SDP `c=` is wrong.

1. Set `SIP_ADVERTISE_HOST` to the **public IPv4** the provider will send RTP to.
2. Or enable STUN (`STUN_SERVER` + `STUN_ENABLED=true`) and retry.
3. Open UDP `SIP_RTP_PORT_START`–`END` (and the local SIP port) inbound.
4. Codec must stay **PCMA**. The bridge does not offer PCMU/Opus.

## Datacenter vs residential

If the same account REGISTERs from a home softphone but **500**s from the bot box, it is **egress reputation**, not digest math. Use a cloud-friendly provider ([OVH](../../docs/providers/ovh.md)) or run the UAC on residential egress.

## Mid-call “not registered” / IP drift

Symptoms: first call works; later REGISTER refresh fails; or INVITE 403 after a quiet period.

- **Egress IP changed** (CGNAT, cloud NAT, new node) while the provider **IP allowlist** still has the old `/32`. Update the allowlist or pin egress.
- REGISTER expiry too long/short. OVH recommends **1800 s** (`SIP_REGISTER_EXPIRES`).
- Process lost UDP (interface flap). Restart the bridge; confirm `/v1/status` before the next SCRIPT.

## Codec

`SIP_CODEC=pcma` only. Provider “auto” is fine if they accept a-Law. If they require μ-Law only, that is a new provider doc + code change — do not silently transcode in the session layer.

## Voice side

`POST /v1/voice/probe` — live Grok/OpenAI should return `ok`. 502/auth errors → `XAI_API_KEY` / `OPENAI_API_KEY`. Pass `{"provider":"openai"}` to probe OpenAI while defaulting to Grok.

## After a fix

Re-check `/health` then place a short SCRIPT via **handle-a-call**. Do not log or commit passwords.
