---
name: package-call-bot
description: Assemble a Grok Bot share staging folder from this repo’s skills, memories, and docs. No secrets.
---

# Package Call bot

From the repo root:

```bash
./scripts/package-call-bot.sh
# or: ./scripts/package-call-bot.sh /tmp/call-bot-package
```

Writes a staging directory (default `dist/call-bot-package/`) containing:

| Staging path | Source | Grok Bot share input (conceptual) |
|---|---|---|
| `PROFILE.md` | `bot/PROFILE.md` | Bot persona / system profile |
| `skills/*/SKILL.md` | `skills/` | Skill documents |
| `memories/*.md` | `bot/memories/` | Seed memories |
| `docs/` excerpts | README + provider index | Operator context, not secrets |
| `MANIFEST.md` | generated | How to map into `create_bot_share_json` (or the current export UI) |

**Do not** copy `.env`, passwords, or live numbers. If an export tool asks for API keys, the operator pastes them at share time — this script will not.

After packaging, attach the skills and profile in the Grok Bot builder. Recipients must clone https://github.com/theophanemayaud/bot-call-bridge to install the bridge. Point the bot at a **private** bridge URL; the package is instructions, not the SIP stack.
