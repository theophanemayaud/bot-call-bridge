# OVH SIP (reference live path)

Proven cloud-friendly option for a Grok Bot / datacenter box. Use a **SIP line without OVH hardware** so the login can REGISTER from software (this bridge). A line shipped with an OVH desk phone cannot be registered on a softphone or UAC.

Official: [Register a SIP line on a softphone](https://docs.ovhcloud.com/fr/guides/web-cloud/phone-and-fax/voip/register-sip-softphone).

## Order / product

In OVHcloud: **Telecom → VoIP & Fax**. Order a **ligne SIP sans téléphone** (Découverte is enough for one outbound agent). Do not pick a bundle that locks SIP credentials to an OVH handset.

## Manager path (login, domain, proxy, password)

1. **Telecom → VoIP & Fax** → billing/telephony group → the SIP line.
2. **Gestion → Informations générales**.
3. Under **Informations SIP** copy:
   - **Login / User name** — international form, e.g. `0033XXXXXXXXX` (not `+33…`).
   - **Domain / Registrar** — often `sip3.ovh.fr`. **Verify in the manager**; some lines use another host (`sip.ovh.fr`, `sip-domain.io`, …).
   - **Proxy sortant** — outbound proxy. If it equals the domain, leave `SIP_PROXY` empty.
4. SIP password: **Gestion** → reset / set a strong password ([modifier le mot de passe SIP](https://docs.ovhcloud.com/fr/guides/web-cloud/phone-and-fax/voip/)). You cannot recover the original; only reset.

Recommended REGISTER expiry: **1800 s** (`SIP_REGISTER_EXPIRES=1800`).

## IP allowlist

OVH can restrict REGISTER to listed public IPs (**Gestion → Restrictions SIP par IP**, up to six entries, typically `x.x.x.x/32`).

If the Grok Bot box has a **stable egress IP**, allowlist it. If egress **NATs or rotates**, either disable the restriction or the bridge will look “registered” then fail mid-refresh (`403` / silent REGISTER fail). See `skills/troubleshoot`.

## REGISTER / INVITE

| Item | Value |
|---|---|
| Transport | UDP `5060` |
| Auth | HTTP Digest (401/407) |
| Codec | Offer **PCMA** (G.711 a-Law) only — this bridge already does |
| Login | `00` + country + national (example shape `0033XXXXXXXXX`) |
| Dial | Same rewrite: SCRIPT `+33…` → `sip:0033…@<registrar>` |

`SIP_*` mapping:

```bash
SIP_REGISTRAR=sip3.ovh.fr          # Domain/Registrar from manager
SIP_PROXY=                         # outbound proxy if different
SIP_DOMAIN=sip3.ovh.fr
SIP_USERNAME=0033XXXXXXXXX
SIP_PASSWORD=
SIP_LOCAL_PORT=5062
SIP_REGISTER_EXPIRES=1800
```

Open UDP `SIP_LOCAL_PORT` and `SIP_RTP_PORT_START`–`END` on the box. Set `SIP_ADVERTISE_HOST` to the public IPv4 if STUN is off or one-way audio appears.

## Découverte pricing (guide rates)

Label: **public grid / product pages, not a quote.** Verify in the manager and [OVH VoIP tariffs](https://www.ovhtelecom.fr/telephonie/decouvrez/tarifs_telephonie.xml) before you rely on a number.

| Item | Ballpark (HT) | Notes |
|---|---|---|
| Line (Découverte) | ~0.99 € / month | One simultaneous call |
| FR fixed | Often **included** on Découverte | Caps apply (distinct numbers / 60 min per call on included destinations — check current offer) |
| FR mobile | **~0.08 € / min** | Billed per second; not included on Découverte |
| NO mobile | **~0.012 € / min** | Public grid; confirm destination still billed this way |
| Hors-forfait ceiling | **~150 € HT / billing group** default after the line is established | New groups may start ~10 € HT; raise via deposit if needed. Hitting 100% **suspends the group**. |

Included destinations changed during 2026 (Découverte recentred). Do not treat “Norway fixed included” as forever — read the current offer footnotes.

## Why this is the bot-box default

OVH accepts REGISTER from typical cloud egress. Combined with an IP allowlist and a hors-forfait ceiling, it is the documented path for an unattended Call agent. VoipWise is documented separately and is **not** recommended for datacenter IPs.
