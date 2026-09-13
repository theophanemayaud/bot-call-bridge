from __future__ import annotations

from call_bridge.sip.protocol import (
    PCMA_PT,
    RtpPacket,
    SipMessage,
    build_sdp,
    dial_request_uri,
    parse_rtp,
    parse_sdp,
    parse_sip,
)


def test_sip_roundtrip_register():
    raw = (
        b"SIP/2.0 401 Unauthorized\r\n"
        b"Via: SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bKabc;received=1.2.3.4\r\n"
        b'WWW-Authenticate: Digest realm="voipwise.com", nonce="n1", algorithm=MD5\r\n'
        b"Call-ID: xyz@voipwise.com\r\n"
        b"CSeq: 1 REGISTER\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    msg = parse_sip(raw)
    assert msg.status_code() == 401
    assert msg.cseq() == (1, "REGISTER")
    assert "voipwise.com" in (msg.get("www-authenticate") or "")
    rebuilt = parse_sip(msg.to_bytes())
    assert rebuilt.status_code() == 401
    assert rebuilt.get("call-id") == "xyz@voipwise.com"


def test_request_serialize_sets_content_length():
    msg = SipMessage(start_line="INVITE sip:0033@sip.voipwise.com SIP/2.0")
    msg.set("via", "SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bK1")
    msg.body = "v=0\r\n"
    data = msg.to_bytes()
    assert b"Content-Length: 5" in data


def test_sdp_pcma_only():
    sdp = build_sdp(origin_ip="203.0.113.5", rtp_port=40000, session_id=9)
    parsed = parse_sdp(sdp)
    assert parsed == {"ip": "203.0.113.5", "port": 40000, "payload": PCMA_PT}
    assert "PCMA/8000" in sdp
    assert "PCMU" not in sdp


def test_rtp_roundtrip():
    packet = RtpPacket(payload=b"\xd5" * 160, sequence=7, timestamp=160, ssrc=99, marker=True)
    parsed = parse_rtp(packet.to_bytes())
    assert parsed.sequence == 7
    assert parsed.timestamp == 160
    assert parsed.ssrc == 99
    assert parsed.marker is True
    assert parsed.payload == b"\xd5" * 160


def test_dial_request_uri_rewrites_plus():
    assert dial_request_uri("+33XXXXXXXXX", "sip.voipwise.com") == (
        "sip:0033XXXXXXXXX@sip.voipwise.com"
    )
