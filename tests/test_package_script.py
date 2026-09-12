from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_call_bot_writes_staging(tmp_path: Path):
    dest = tmp_path / "call-bot-package"
    script = ROOT / "scripts" / "package-call-bot.sh"
    proc = subprocess.run(
        ["bash", str(script), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert dest.joinpath("PROFILE.md").is_file()
    assert dest.joinpath("MANIFEST.md").is_file()
    assert dest.joinpath("skills/setup/SKILL.md").is_file()
    assert dest.joinpath("skills/troubleshoot/SKILL.md").is_file()
    assert dest.joinpath("skills/handle-a-call/SKILL.md").is_file()
    assert dest.joinpath("skills/package-call-bot/SKILL.md").is_file()
    assert dest.joinpath("memories/providers.md").is_file()
    assert dest.joinpath("docs/providers/ovh.md").is_file()
    assert "XAI_API_KEY=" not in dest.joinpath("PROFILE.md").read_text()
    assert "Wrote" in proc.stdout
