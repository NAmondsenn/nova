"""
WebAuthn Module.

The checks on a passkey - its registration on the settings page, and each approval
of a big action from his phone - for the approval service and the settings page.

Registration takes the public key the browser hands over (SubjectPublicKeyInfo DER,
from AuthenticatorAttestationResponse.getPublicKey) and its algorithm, with
attestation "none", so nothing here parses CBOR or COSE: the first approval proves
the phone holds the matching private key.

An approval (an assertion) is only trusted when every one of these holds: the client
data is for "webauthn.get", for this request's challenge and this page's origin, and
not from inside another site's frame; the authenticator data is for this page's host
(the relying party id), with the user both present and verified - a tap alone isn't
enough, it must be his fingerprint or PIN; the sign counter has gone up, unless the
authenticator keeps none; and the signature checks against the stored public key.
Anything else, including anything which can't be parsed, raises NotVerified, never
another exception, so a caller only has one thing to catch.

Nothing here logs. A challenge, an assertion, a key or a credential id must never reach
the log; NotVerified's message is a short reason with none of them in it.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

# COSE algorithm numbers, as the browser's getPublicKeyAlgorithm gives them.
ES256 = -7
RS256 = -257

# The longest base64url text taken in - far more than any challenge, signature or
# client data a phone sends, so a huge post can't make the brain decode it.
MAX_B64URL = 4096
_B64URL = re.compile(r"[A-Za-z0-9_-]*")

# Flags in the authenticator data.
_USER_PRESENT = 0x01
_USER_VERIFIED = 0x04

# RP id hash (32 bytes), flags (1) and sign counter (4).
_AUTH_DATA_MIN = 37

# Shorter challenges than this are a bug in the caller, not something to check against.
_MIN_CHALLENGE = 16

# The smallest RSA key taken, as Windows Hello and every current authenticator use.
_MIN_RSA_BITS = 2048


class NotVerified(Exception):
    """A registration or assertion which mustn't be trusted; the message is the reason, for the log only."""


def b64url(data: bytes) -> str:
    """Base64url without padding, as WebAuthn writes a challenge into the client data."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def from_b64url(text: str) -> bytes:
    """The bytes of base64url text, with or without its padding.

    Raises:
        NotVerified: If it isn't a string of base64url, or is over MAX_B64URL characters.
    """
    if not isinstance(text, str) or len(text) > MAX_B64URL:
        raise NotVerified("not base64url")
    bare = text.rstrip("=")
    if len(text) - len(bare) > 2 or not _B64URL.fullmatch(bare) or len(bare) % 4 == 1:
        raise NotVerified("not base64url")
    try:
        return base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))
    except (binascii.Error, ValueError):
        raise NotVerified("not base64url") from None


def rp_id(origin: str) -> str:
    """The relying party id for a page's origin: its host.

    Only an https origin is taken - WebAuthn needs a secure page - with no path, query,
    fragment, login or port other than 443, as the settings page's Tailscale address is.

    Raises:
        NotVerified: If the origin is anything else.
    """
    if not isinstance(origin, str):
        raise NotVerified("bad origin")
    try:
        parts = urlsplit(origin)
        parts.port  # raises ValueError for a port which isn't a number
    except ValueError:
        raise NotVerified("bad origin") from None
    host = parts.hostname
    if (
        parts.scheme != "https"
        or not host
        or not host.isascii()
        or parts.path
        or parts.query
        or parts.fragment
        # No login, and no port but 443: the netloc is the host alone, or with ":443".
        or parts.netloc not in (host, host + ":443")
    ):
        raise NotVerified("bad origin")
    return host


def _check_client_data(expected_type: str, challenge: bytes, origin: str, client_data_json: bytes) -> None:
    """Checks the browser's client data is of this type, for this challenge and origin."""
    if not isinstance(challenge, bytes) or len(challenge) < _MIN_CHALLENGE:
        raise NotVerified("no challenge")
    # Bytes only, as the browser sends them: json would take text too, which then isn't what was signed.
    if not isinstance(client_data_json, bytes):
        raise NotVerified("client data isn't bytes")
    try:
        data = json.loads(client_data_json)
    # RecursionError is JSON nested deeper than Python goes, which no browser sends.
    except (TypeError, ValueError, RecursionError):
        raise NotVerified("client data isn't JSON") from None
    if not isinstance(data, dict):
        raise NotVerified("client data isn't an object")
    if data.get("type") != expected_type:
        raise NotVerified("wrong type")
    if not hmac.compare_digest(from_b64url(data.get("challenge")), challenge):
        raise NotVerified("wrong challenge")
    if data.get("origin") != origin or data.get("crossOrigin") is True:
        raise NotVerified("wrong origin")


def _check_authenticator_data(origin: str, authenticator_data: bytes) -> int:
    """Checks the authenticator data is for this page's host, with him present and verified.

    Returns:
        The sign counter it holds.
    """
    if not isinstance(authenticator_data, bytes) or len(authenticator_data) < _AUTH_DATA_MIN:
        raise NotVerified("authenticator data too short")
    expected = hashlib.sha256(rp_id(origin).encode("ascii")).digest()
    if not hmac.compare_digest(authenticator_data[:32], expected):
        raise NotVerified("wrong relying party")
    flags = authenticator_data[32]
    if not flags & _USER_PRESENT or not flags & _USER_VERIFIED:
        raise NotVerified("user not present and verified")
    return int.from_bytes(authenticator_data[33:37], "big")


def _load_key(public_key_der: bytes, algorithm: int):
    """The public key, if it's of the kind its algorithm says: P-256 for ES256, RSA for RS256."""
    try:
        key = load_der_public_key(public_key_der)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        raise NotVerified("not a public key") from None
    if algorithm == ES256:
        if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
            return key
    elif algorithm == RS256:
        if isinstance(key, rsa.RSAPublicKey) and key.key_size >= _MIN_RSA_BITS:
            return key
    else:
        raise NotVerified("unknown algorithm")
    raise NotVerified("key doesn't match its algorithm")


def check_registration(
    challenge: bytes,
    origin: str,
    client_data_json: bytes,
    authenticator_data: bytes,
    public_key_der: bytes,
    algorithm: int,
) -> int:
    """Checks a new passkey from `navigator.credentials.create`, with attestation "none".

    Args:
        challenge: The challenge the settings page gave for this registration.
        origin: The settings page's own origin (its Tailscale https address).
        client_data_json: The browser's client data, as sent.
        authenticator_data: The authenticator data, as sent.
        public_key_der: The new public key, SubjectPublicKeyInfo DER.
        algorithm: ES256 or RS256.

    Returns:
        The sign counter to store with the passkey.

    Raises:
        NotVerified: If any check fails.
    """
    _check_client_data("webauthn.create", challenge, origin, client_data_json)
    counter = _check_authenticator_data(origin, authenticator_data)
    _load_key(public_key_der, algorithm)
    return counter


def check_assertion(
    challenge: bytes,
    origin: str,
    public_key_der: bytes,
    algorithm: int,
    stored_counter: int,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes,
) -> int:
    """Checks an approval from `navigator.credentials.get` against a stored passkey.

    Args:
        challenge: The open request's own single-use challenge.
        origin: The settings page's own origin (its Tailscale https address).
        public_key_der: The stored public key, SubjectPublicKeyInfo DER.
        algorithm: The stored algorithm, ES256 or RS256.
        stored_counter: The sign counter stored after the last approval.
        client_data_json: The browser's client data, as sent.
        authenticator_data: The authenticator data, as sent.
        signature: The signature over the authenticator data and the client data's hash.

    Returns:
        The new sign counter to store.

    Raises:
        NotVerified: If any check fails.
    """
    _check_client_data("webauthn.get", challenge, origin, client_data_json)
    counter = _check_authenticator_data(origin, authenticator_data)
    # A counter of 0 both times is an authenticator which keeps none; otherwise it must
    # have gone up, or this could be a copy of the passkey or a replay.
    if (counter or stored_counter) and counter <= stored_counter:
        raise NotVerified("counter hasn't gone up")
    if not isinstance(signature, bytes):
        raise NotVerified("bad signature")
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    key = _load_key(public_key_der, algorithm)
    try:
        if algorithm == ES256:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError):
        raise NotVerified("bad signature") from None
    return counter
