#!/usr/bin/env bash
# Assemble a Grok Bot share staging folder from repo artifacts.
# Usage: ./scripts/package-call-bot.sh [dest-dir]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/dist/call-bot-package}"

if [[ -e "$DEST" ]]; then
  rm -rf "$DEST"
fi

mkdir -p "$DEST/skills" "$DEST/memories" "$DEST/docs/providers"

cp "$ROOT/bot/PROFILE.md" "$DEST/PROFILE.md"
cp "$ROOT/AGENTS.md" "$DEST/docs/AGENTS.md"

# Skills (setup / troubleshoot / handle-a-call / package-call-bot)
while IFS= read -r skill_md; do
  name="$(basename "$(dirname "$skill_md")")"
  mkdir -p "$DEST/skills/$name"
  cp "$skill_md" "$DEST/skills/$name/SKILL.md"
done < <(find "$ROOT/skills" -name SKILL.md | sort)

# Curated memories only — skip README index noise if you prefer; keep stubs.
cp "$ROOT/bot/memories/"*.md "$DEST/memories/"

# Operator docs without encouraging a VoipWise-only read
{
  echo "# Call Bridge (excerpt)"
  echo
  sed -n '1,40p' "$ROOT/README.md"
  echo
  echo "Full README is in the call-bridge repo."
} > "$DEST/docs/README.excerpt.md"

cp "$ROOT/docs/providers/README.md" "$DEST/docs/providers/README.md"
cp "$ROOT/docs/providers/ovh.md" "$DEST/docs/providers/ovh.md"
cp "$ROOT/docs/providers/voipwise.md" "$DEST/docs/providers/voipwise.md"
cp "$ROOT/examples/script.sample.json" "$DEST/docs/script.sample.json"

cat > "$DEST/MANIFEST.md" <<'EOF'
# Call bot package

Staging folder produced by `scripts/package-call-bot.sh`.
Contains **instructions only** — no SIP passwords, no `XAI_API_KEY`, no live numbers.

## Map to a Grok Bot share

When a share/export API or UI asks for pieces (names vary; treat this as the
`create_bot_share_json` input set), map:

| Share field (conceptual) | File in this folder |
|---|---|
| Profile / persona | `PROFILE.md` |
| Skills | `skills/*/SKILL.md` |
| Memories | `memories/control-plane.md`, `memories/providers.md` |
| Extra instructions | `docs/AGENTS.md` |
| Operator context | `docs/README.excerpt.md`, `docs/providers/` |

Do not upload `.env`. The operator configures the **private** bridge URL and
keys on the machine that runs `python -m call_bridge`.

## Verify

```
test -f PROFILE.md
test -f skills/setup/SKILL.md
test -f skills/troubleshoot/SKILL.md
test -f skills/handle-a-call/SKILL.md
```
EOF

echo "Wrote $DEST"
find "$DEST" -type f | sort
