# VoipWise

Works from **residential / MobileVOIP-style softphones**. **Not recommended** for a Grok Bot box or other **cloud/datacenter egress** — REGISTER/INVITE from those IPs often comes back **500** (or equivalent provider reject) after VoipWise’s consumer anti-abuse filters.

Use [OVH](ovh.md) for the cloud-friendly live path.

## Softphone settings (when you are on a residential IP)

| Setting | Typical value |
|---|---|
| Registrar | `sip.voipwise.com` |
| Proxy | `sip.voipwise.com` |
| Outbound proxy | empty |
| Port | `5060` UDP |
| Domain / realm | `voipwise.com` |
| Username / password | VoipWise account (SIP must be enabled in their portal) |
| Codec | G.711 a-Law only |
| STUN | `stun.voipwise.com:3478` |

```bash
SIP_REGISTRAR=sip.voipwise.com
SIP_PROXY=sip.voipwise.com
SIP_DOMAIN=voipwise.com
SIP_USERNAME=
SIP_PASSWORD=
STUN_SERVER=stun.voipwise.com
STUN_PORT=3478
```

`+33…` is rewritten to `00…`, which matches Betamax-family dialling.

## If you see 500 from a VPS / bot box

Treat it as a **provider policy**, not a bridge bug. Move the UAC to a residential egress or switch provider (OVH). Do not add retries that hide a 500.
