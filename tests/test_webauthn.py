"""Tests for the WebAuthn checks - a passkey's registration and its approvals.

Each test makes its own FakeAuthenticator, which signs the way a phone does, and bends
one part of what it sends to show that part is checked.
"""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fake_authenticator import FakeAuthenticator
from voice_assistant.webauthn import (
    ES256,
    RS256,
    NotVerified,
    b64url,
    check_assertion,
    check_registration,
    from_b64url,
    rp_id,
)

CHALLENGE = bytes(range(32))
ORIGIN = "https://nova.example.ts.net"


def _check(fake, answer, stored_counter=0, challenge=CHALLENGE):
    return check_assertion(
        challenge,
        ORIGIN,
        fake.public_key_der,
        fake.algorithm,
        stored_counter,
        answer["client_data_json"],
        answer["authenticator_data"],
        answer["signature"],
    )


def _register(answer, challenge=CHALLENGE):
    return check_registration(
        challenge,
        ORIGIN,
        answer["client_data_json"],
        answer["authenticator_data"],
        answer["public_key_der"],
        answer["algorithm"],
    )


def test_a_good_assertion_gives_the_new_counter():
    fake = FakeAuthenticator()
    assert _register(fake.register(CHALLENGE)) == 0
    assert _check(fake, fake.assert_(CHALLENGE, counter_step=1), stored_counter=0) == 1


def test_rs256_is_checked_too():
    fake = FakeAuthenticator(RS256)
    assert _register(fake.register(CHALLENGE)) == 0
    assert _check(fake, fake.assert_(CHALLENGE, counter_step=1), stored_counter=0) == 1


def test_a_wrong_type_is_refused():
    fake = FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(CHALLENGE, type="webauthn.create"))


def test_an_assertion_for_another_challenge_is_refused():
    fake = FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(bytes(32)))


def test_another_origin_is_refused():
    fake = FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(CHALLENGE, origin="https://evil.example.ts.net"))


def test_a_cross_origin_assertion_is_refused():
    """A page from another site framing ours says so in crossOrigin, and isn't trusted."""
    fake = FakeAuthenticator()
    # Signed as it is, so it's the crossOrigin check alone which refuses it.
    answer = fake.assert_(CHALLENGE, cross_origin=True)
    assert json.loads(answer["client_data_json"])["crossOrigin"] is True
    with pytest.raises(NotVerified, match="wrong origin"):
        _check(fake, answer)


def test_another_rp_id_is_refused():
    fake = FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(CHALLENGE, rp="example.com"))


def test_user_presence_and_verification_are_both_needed():
    for flags in (0x01, 0x04):
        fake = FakeAuthenticator()
        with pytest.raises(NotVerified):
            _check(fake, fake.assert_(CHALLENGE, flags=flags))


def test_a_counter_which_hasnt_gone_up_is_refused():
    fake = FakeAuthenticator()
    fake.counter = 4
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(CHALLENGE, counter_step=1), stored_counter=5)
    keeps_none = FakeAuthenticator()
    assert _check(keeps_none, keeps_none.assert_(CHALLENGE, counter_step=0), stored_counter=0) == 0


def test_a_counter_gone_back_to_zero_is_refused():
    """Once a counter has been seen, an authenticator can't claim to keep none."""
    fake = FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, fake.assert_(CHALLENGE, counter_step=0), stored_counter=5)


def test_a_tampered_signature_is_refused():
    for algorithm in (ES256, RS256):
        fake = FakeAuthenticator(algorithm)
        answer = fake.assert_(CHALLENGE)
        signature = bytearray(answer["signature"])
        signature[len(signature) // 2] ^= 0x01
        answer["signature"] = bytes(signature)
        with pytest.raises(NotVerified):
            _check(fake, answer)


def test_a_signature_from_another_key_is_refused():
    fake, other = FakeAuthenticator(), FakeAuthenticator()
    with pytest.raises(NotVerified):
        _check(fake, other.assert_(CHALLENGE))


def test_a_short_or_odd_authenticator_data_is_refused():
    fake = FakeAuthenticator()
    answer = fake.assert_(CHALLENGE)
    # Cut to 36 bytes and signed so, it's the length alone which refuses it.
    answer["authenticator_data"] = answer["authenticator_data"][:36]
    answer["signature"] = fake.sign(answer["authenticator_data"], answer["client_data_json"])
    with pytest.raises(NotVerified, match="too short"):
        _check(fake, answer)
    for client_data_json in (b"not json", b"[1, 2]", b'{"type": 5}', b"\xff\xfe", b'{"type": "webauthn.get"}'):
        answer = fake.assert_(CHALLENGE)
        answer["client_data_json"] = client_data_json
        with pytest.raises(NotVerified):
            _check(fake, answer)
    answer = fake.assert_(CHALLENGE)
    with pytest.raises(NotVerified):
        check_assertion(CHALLENGE, ORIGIN, b"not a key", ES256, 0, answer["client_data_json"],
                        answer["authenticator_data"], answer["signature"])
    with pytest.raises(NotVerified):
        check_assertion(CHALLENGE, ORIGIN, fake.public_key_der, 12345, 0, answer["client_data_json"],
                        answer["authenticator_data"], answer["signature"])


def test_client_data_nested_too_deep_is_refused():
    fake = FakeAuthenticator()
    answer = fake.assert_(CHALLENGE)
    answer["client_data_json"] = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(NotVerified, match="isn't JSON"):
        _check(fake, answer)
    answer = fake.register(CHALLENGE)
    answer["client_data_json"] = b'{"a":' * 100_000 + b"1" + b"}" * 100_000
    with pytest.raises(NotVerified, match="isn't JSON"):
        _register(answer)


def test_client_data_as_text_is_refused():
    """Only the bytes the browser sent were signed: text, even of the same JSON, isn't checked."""
    fake = FakeAuthenticator()
    answer = fake.register(CHALLENGE)
    answer["client_data_json"] = answer["client_data_json"].decode()
    with pytest.raises(NotVerified, match="isn't bytes"):
        _register(answer)


def test_registration_checks_type_challenge_origin_and_flags():
    fake = FakeAuthenticator()
    fake.counter = 7
    assert _register(fake.register(CHALLENGE)) == 7
    for answer in (
        fake.register(CHALLENGE, type="webauthn.get"),
        fake.register(bytes(32)),
        fake.register(CHALLENGE, origin="https://evil.example.ts.net"),
        fake.register(CHALLENGE, flags=0x41),
        fake.register(CHALLENGE, rp="example.com"),
    ):
        with pytest.raises(NotVerified):
            _register(answer)


def test_registration_refuses_a_key_of_another_kind():
    p384 = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    answer = FakeAuthenticator().register(CHALLENGE)
    answer["public_key_der"] = p384
    with pytest.raises(NotVerified):
        _register(answer)
    answer = FakeAuthenticator(RS256).register(CHALLENGE)
    answer["algorithm"] = ES256
    with pytest.raises(NotVerified):
        _register(answer)
    answer = FakeAuthenticator(ES256).register(CHALLENGE)
    answer["algorithm"] = RS256
    with pytest.raises(NotVerified):
        _register(answer)


def test_rp_id_wants_https():
    for origin in (
        "http://nova.example.ts.net",
        "https://nova.example.ts.net:8443",
        "https://nova.example.ts.net/settings",
        "https://nova.example.ts.net?x=1",
        "https://user@nova.example.ts.net",
        "https://",
        "nova.example.ts.net",
    ):
        with pytest.raises(NotVerified):
            rp_id(origin)
    assert rp_id(ORIGIN) == "nova.example.ts.net"
    assert rp_id("https://nova.example.ts.net:443") == "nova.example.ts.net"


def test_base64url_round_trips_and_refuses_anything_else():
    for data in (b"", b"\x00", CHALLENGE, bytes(range(256))):
        text = b64url(data)
        assert "=" not in text
        assert from_b64url(text) == data
    for text in ("a+b/", "abc$", "a", "A" * 4097, 5, None):
        with pytest.raises(NotVerified):
            from_b64url(text)
