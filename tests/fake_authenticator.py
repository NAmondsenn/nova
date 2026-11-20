"""A stand-in for a phone's passkey, for the WebAuthn tests.

It builds the client data and authenticator data a browser and phone would, and signs
them with a real key from `cryptography`, so the checks in voice_assistant.webauthn are
tested against the same bytes a phone sends. Every part can be bent - the type, origin,
relying party, flags and counter - to make the assertions the checks must refuse.
"""

import hashlib
import json
import os
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from voice_assistant.webauthn import ES256, RS256, b64url

ORIGIN = "https://nova.example.ts.net"


class FakeAuthenticator:
    """One passkey: a key pair, a credential id and a sign counter.

    Args:
        algorithm: ES256 (a P-256 key, as most phones make) or RS256 (a 2048-bit RSA key,
            as Windows Hello makes).
        origin: The page the browser says it's on, used unless a call overrides it.
    """

    def __init__(self, algorithm=ES256, origin=ORIGIN):
        if algorithm == ES256:
            self._key = ec.generate_private_key(ec.SECP256R1())
        elif algorithm == RS256:
            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            raise ValueError("unknown algorithm")
        self.algorithm = algorithm
        self.origin = origin
        self.credential_id = os.urandom(16)
        self.public_key_der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.counter = 0

    def _client_data(self, type_, challenge, origin):
        return json.dumps(
            {
                "type": type_,
                "challenge": b64url(challenge),
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def _authenticator_data(self, rp, flags, counter):
        rp = rp or urlsplit(self.origin).hostname
        return hashlib.sha256(rp.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big")

    def register(self, challenge, *, origin=None, type="webauthn.create", rp=None, flags=0x45):
        """What `navigator.credentials.create` hands the page, with attestation "none".

        The flags default to user present, user verified and attested credential data
        included, as a phone sends them. The attested credential data (AAGUID, credential
        id) follows the counter, without the COSE key, which the checks never read.
        """
        authenticator_data = (
            self._authenticator_data(rp, flags, self.counter)
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
        )
        return {
            "client_data_json": self._client_data(type, challenge, origin),
            "authenticator_data": authenticator_data,
            "public_key_der": self.public_key_der,
            "algorithm": self.algorithm,
            "credential_id": self.credential_id,
        }

    def assert_(self, challenge, *, origin=None, type="webauthn.get", rp=None, flags=0x05, counter_step=1):
        """What `navigator.credentials.get` hands the page: a signed assertion.

        The counter goes up by `counter_step` first (0 for an authenticator which keeps
        no counter, or to repeat one), and the signature covers the authenticator data
        followed by the SHA-256 of the client data, as the WebAuthn spec has it.
        """
        self.counter += counter_step
        client_data_json = self._client_data(type, challenge, origin)
        authenticator_data = self._authenticator_data(rp, flags, self.counter)
        signed = authenticator_data + hashlib.sha256(client_data_json).digest()
        if self.algorithm == ES256:
            signature = self._key.sign(signed, ec.ECDSA(hashes.SHA256()))
        else:
            signature = self._key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return {
            "client_data_json": client_data_json,
            "authenticator_data": authenticator_data,
            "signature": signature,
            "credential_id": self.credential_id,
        }
