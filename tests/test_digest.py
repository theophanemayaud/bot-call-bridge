from __future__ import annotations

from call_bridge.sip.protocol import digest_response, format_authorization, parse_authenticate


def test_rfc2617_auth_qop():
    challenge = parse_authenticate(
        'Digest realm="testrealm@host.com", '
        'qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )
    params = digest_response(
        username="Mufasa",
        password="Circle Of Life",
        method="GET",
        uri="/dir/index.html",
        challenge=challenge,
        cnonce="0a4f113b",
        nc="00000001",
    )
    assert params["response"] == "6629fae49393a05397450978507c4ef1"
    header = format_authorization(params)
    assert header.startswith("Digest ")
    assert "qop=auth" in header


def test_parse_authenticate_quotes():
    parsed = parse_authenticate('Digest realm="voipwise.com", nonce="abc", algorithm=MD5')
    assert parsed["realm"] == "voipwise.com"
    assert parsed["nonce"] == "abc"
    assert parsed["algorithm"] == "MD5"
