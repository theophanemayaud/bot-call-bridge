from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from call_bridge.script import CallScript, normalize_dial_user

SAMPLE = json.loads(Path("examples/script.sample.json").read_text())


def test_sample_script_validates():
    script = CallScript.model_validate(SAMPLE)
    text = script.build_instructions()
    assert "Tuesday 10:00" in text
    assert script.mission.disclosure in text
    assert "+33XXXXXXXXX" == script.call.to
    assert "hangup" in text
    assert "ask_orchestrator" in text


def test_script_instructions_require_short_voicemail_then_hangup():
    script = CallScript.model_validate(SAMPLE)
    text = script.build_instructions()
    assert "VOICEMAIL" in text
    assert "short message" in text
    assert "who you are" in text
    assert "hang up" in text
    assert "Do not sit in silence after the greeting." in text


def test_requires_disclosure_and_goals():
    bad = {
        "call": {"to": "+33XXXXXXXXX", "language": "fr"},
        "mission": {"disclosure": "hi", "goals": []},
    }
    with pytest.raises(ValidationError):
        CallScript.model_validate(bad)


def test_normalize_plus_to_00():
    assert normalize_dial_user("+33XXXXXXXXX") == "0033XXXXXXXXX"
    assert normalize_dial_user("0033142867800") == "0033142867800"
    assert normalize_dial_user("+33 1 42 86 78 00") == "0033142867800"
