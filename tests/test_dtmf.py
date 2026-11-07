import pytest

from call_bridge.sip.dtmf import alaw_to_linear, dtmf_frames, linear_to_alaw
from call_bridge.sip.protocol import PCMA_FRAME_BYTES, PCMA_SILENCE


def test_alaw_silence_matches_rtp_comfort_noise():
    assert linear_to_alaw(0) == 0xD5
    assert PCMA_SILENCE[0] == 0xD5


def test_alaw_roundtrip_stays_close():
    for sample in (0, 1000, -1000, 8000, -16000, 32000):
        recovered = alaw_to_linear(linear_to_alaw(sample))
        assert abs(recovered - sample) < 300


def test_dtmf_frames_are_pcma_and_not_silence():
    frames = dtmf_frames("5")
    assert frames
    assert all(len(frame) == PCMA_FRAME_BYTES for frame in frames)
    assert any(frame != PCMA_SILENCE for frame in frames)
    assert dtmf_frames("1") != dtmf_frames("2")


def test_dtmf_rejects_words():
    with pytest.raises(ValueError):
        dtmf_frames("one")
