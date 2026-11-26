"""
Update Receive Module.

A satellite's end of an update - see protocol.py for the messages and remote.py's
send_update for the brain's end. The brain offers an update (its version, size and a
proof), sends it in chunks and says it has finished; this module collects the chunks in
memory and, only when the last one is in, checks the proof. A satellite never installs
code it can't prove came from its brain unaltered, so nothing here is written to the
disk: the archive is handed back as Verified once the proof is right, and not before.

The proof is an HMAC keyed with the satellite's password and bound to this connection's
two handshake nonces, so an update recorded on one connection is no good on another. The
check is logged as passed or failed and nothing more - never the proof, the password or a
nonce.
"""

import hmac
import logging
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

from voice_assistant import protocol

logger = logging.getLogger(__name__)

NOT_FROM_THE_BRAIN = "The update wasn't from the brain, so it wasn't installed."
TOO_BIG = "The update was bigger than a satellite takes."
CUT_SHORT = "The update arrived cut short or out of order."
WRONG_FORMAT = "The update wasn't in a form this satellite installs."

# A proof is a SHA-256 in hex.
_PROOF = re.compile(r"[0-9a-f]{64}")
# An offer's id is the brain's own short label for the transfer.
_ID_MOST = 64


class UpdateRefused(Exception):
    """An update which wasn't taken; reason is what to report in update_result's error."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Verified(NamedTuple):
    """An update which has proved it came from the brain."""

    version: str
    format: str
    archive: bytes
    retry: bool


def _is_int(value) -> bool:
    """An int, and not a bool - which Python counts as one but a size never is."""
    return isinstance(value, int) and not isinstance(value, bool)


class UpdateReceiver:
    """
    Collects one update at a time and verifies it.

    Args:
        password: This satellite's password, which keys the proof.
        update_format: The one archive format this satellite installs, from
            protocol.UPDATE_FORMATS.
    """

    def __init__(self, password: str, update_format: str):
        self._password = password
        self._format = update_format
        self._offer: Optional[Dict] = None
        self._nonces: Optional[Tuple[str, str]] = None
        self._chunks: List[bytes] = []
        self._received = 0

    def _drop(self):
        """Forgets any transfer in progress."""
        self._offer = None
        self._nonces = None
        self._chunks = []
        self._received = 0

    def offer(self, message: Dict, nonces: Tuple[str, str]) -> None:
        """
        Starts a transfer, dropping any before it - the newest offer wins.

        Args:
            message: The update_offer message.
            nonces: This connection's (satellite nonce, brain nonce).

        Raises:
            UpdateRefused: If the offer is malformed, in a format this satellite doesn't
                install, or too big.
        """
        self._drop()
        update_id = message.get("id")
        version = message.get("version")
        update_format = message.get("format")
        size = message.get("size")
        chunks = message.get("chunks")
        proof = message.get("proof")
        retry = message.get("retry")
        if not (isinstance(update_format, str) and update_format == self._format):
            raise UpdateRefused(WRONG_FORMAT)
        if not (isinstance(update_id, str) and 0 < len(update_id) <= _ID_MOST):
            raise UpdateRefused(CUT_SHORT)
        if not (isinstance(version, str) and protocol.COMMIT.fullmatch(version)):
            raise UpdateRefused(CUT_SHORT)
        if not (_is_int(size) and _is_int(chunks) and isinstance(retry, bool)
                and isinstance(proof, str) and _PROOF.fullmatch(proof)):
            raise UpdateRefused(CUT_SHORT)
        if size > protocol.UPDATE_MOST:
            raise UpdateRefused(TOO_BIG)
        # An empty archive is nothing to install.
        if size <= 0 or chunks != -(-size // protocol.UPDATE_CHUNK):
            raise UpdateRefused(CUT_SHORT)
        self._offer = {"id": update_id, "version": version, "format": update_format,
                       "size": size, "chunks": chunks, "proof": proof, "retry": retry}
        self._nonces = (str(nonces[0]), str(nonces[1]))

    def chunk(self, message: Dict, payload: bytes) -> None:
        """
        Takes the next chunk of the current transfer. Cheap by design: the brain gives
        each chunk a short send timeout, so nothing is hashed until end().

        Args:
            message: The update_chunk message.
            payload: The chunk's bytes.

        Raises:
            UpdateRefused: If the chunk is out of order, too big or past the offered size;
                the transfer is dropped. A chunk for any other transfer is ignored.
        """
        if self._offer is None or message.get("id") != self._offer["id"]:
            logger.debug("Ignoring an update chunk for a transfer which isn't the current one")
            return
        index = message.get("index")
        if (not _is_int(index) or index != len(self._chunks) or index >= self._offer["chunks"]
                or len(payload) > protocol.UPDATE_CHUNK
                or self._received + len(payload) > self._offer["size"]):
            self._drop()
            raise UpdateRefused(CUT_SHORT)
        self._chunks.append(payload)
        self._received += len(payload)

    def end(self, message: Dict) -> Optional[Verified]:
        """
        Finishes the current transfer and checks its proof.

        Args:
            message: The update_end message.

        Returns:
            The verified update, or None if the end was for another transfer (ignored).

        Raises:
            UpdateRefused: If chunks are missing or the proof doesn't check out.
        """
        offer = self._offer
        if offer is None or message.get("id") != offer["id"]:
            logger.debug("Ignoring the end of a transfer which isn't the current one")
            return None
        chunks, nonces = self._chunks, self._nonces
        complete = len(chunks) == offer["chunks"] and self._received == offer["size"]
        self._drop()
        if not complete:
            raise UpdateRefused(CUT_SHORT)
        archive = b"".join(chunks)
        expected = protocol.update_proof(self._password, nonces[0], nonces[1], offer["version"],
                                         offer["format"], archive)
        if not hmac.compare_digest(expected, offer["proof"]):
            logger.warning("The update's proof didn't check out")
            raise UpdateRefused(NOT_FROM_THE_BRAIN)
        logger.info("The update's proof checked out")
        return Verified(offer["version"], offer["format"], archive, offer["retry"])
