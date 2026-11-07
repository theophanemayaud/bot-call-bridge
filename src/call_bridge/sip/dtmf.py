"""In-band G.711 A-law DTMF tones.

The SDP offer stays PCMA-only (no RFC 4733 telephone-event) so ordinary calls
do not change codec negotiation. IVRs on a clear PCMA path detect these tones.
A carrier that strips in-band DTMF will not hear them.
"""

from __future__ import annotations

import math

from call_bridge.sip.protocol import PCMA_FRAME_BYTES, PCMA_RATE

_DTMF_FREQ: dict[str, tuple[int, int]] = {
    "1": (697, 1209),
    "2": (697, 1336),
    "3": (697, 1477),
    "4": (770, 1209),
    "5": (770, 1336),
    "6": (770, 1477),
    "7": (852, 1209),
    "8": (852, 1336),
    "9": (852, 1477),
    "*": (941, 1209),
    "0": (941, 1336),
    "#": (941, 1477),
}

# ITU-T G.711 A-law segment ends, after the sample has been shifted down by 3.
_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def linear_to_alaw(sample: int) -> int:
    """Encode one 16-bit PCM sample as G.711 A-law. Zero encodes as 0xD5."""
    if sample > 32767:
        sample = 32767
    elif sample < -32768:
        sample = -32768
    magnitude = sample >> 3
    if magnitude >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        magnitude = -magnitude - 1
        if magnitude < 0:
            magnitude = 0
    seg = 8
    for index, limit in enumerate(_SEG_AEND):
        if magnitude <= limit:
            seg = index
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (magnitude >> 1) & 0x0F
    else:
        aval |= (magnitude >> seg) & 0x0F
    return aval ^ mask


def alaw_to_linear(alaw: int) -> int:
    """Decode one G.711 A-law byte to a 16-bit PCM sample."""
    value = alaw ^ 0x55
    quantized = (value & 0x0F) << 4
    seg = (value & 0x70) >> 4
    if seg == 0:
        quantized += 8
    elif seg == 1:
        quantized += 0x108
    else:
        quantized += 0x108
        quantized <<= seg - 1
    return quantized if (value & 0x80) else -quantized


def dtmf_frames(
    digits: str,
    *,
    tone_ms: int = 80,
    gap_ms: int = 80,
    amplitude: float = 0.45,
) -> list[bytes]:
    """PCMA frames for `digits`. Each digit is `tone_ms` of tone plus `gap_ms` of silence."""
    cleaned = "".join(digits.split())
    if not cleaned or any(ch not in _DTMF_FREQ for ch in cleaned):
        raise ValueError("digits must be 0-9, *, #")
    tone_n = max(1, int(PCMA_RATE * tone_ms / 1000))
    gap_n = max(0, int(PCMA_RATE * gap_ms / 1000))
    pcm: list[int] = []
    for digit in cleaned:
        low, high = _DTMF_FREQ[digit]
        for i in range(tone_n):
            sample = amplitude * (
                math.sin(2 * math.pi * low * i / PCMA_RATE)
                + math.sin(2 * math.pi * high * i / PCMA_RATE)
            )
            pcm.append(int(max(-1.0, min(1.0, sample)) * 32767))
        pcm.extend([0] * gap_n)
    encoded = bytes(linear_to_alaw(sample) for sample in pcm)
    pad = (-len(encoded)) % PCMA_FRAME_BYTES
    if pad:
        encoded += bytes([0xD5]) * pad
    return [
        encoded[offset : offset + PCMA_FRAME_BYTES]
        for offset in range(0, len(encoded), PCMA_FRAME_BYTES)
    ]
