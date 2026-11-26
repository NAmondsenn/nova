"""
Update Receive Test
A satellite takes an update only once it proves it came from its brain on this connection.
"""

import logging

import pytest

from voice_assistant import protocol, update_receive
from voice_assistant.update_receive import UpdateReceiver, UpdateRefused

PASSWORD = "hunter2-password"
NONCES = ("sat-nonce-aaaa", "brain-nonce-bbbb")
VERSION = "a" * 40
CHUNK = protocol.UPDATE_CHUNK


def archive_of(size: int) -> bytes:
    return bytes((i * 7) % 256 for i in range(size))


def make_offer(archive: bytes, nonces=NONCES, version=VERSION, fmt="zip", update_id="u1", **over):
    offer = {
        "id": update_id, "version": version, "format": fmt, "size": len(archive),
        "chunks": -(-len(archive) // CHUNK), "retry": False,
        "proof": protocol.update_proof(PASSWORD, nonces[0], nonces[1], version, fmt, archive),
    }
    offer.update(over)
    return offer


def pieces(archive: bytes):
    return [archive[i:i + CHUNK] for i in range(0, len(archive), CHUNK)]


def feed(receiver, offer, chunks, update_id=None):
    update_id = update_id or offer["id"]
    for index, payload in enumerate(chunks):
        receiver.chunk({"id": update_id, "index": index}, payload)
    return receiver.end({"id": update_id})


def reason_of(call):
    with pytest.raises(UpdateRefused) as caught:
        call()
    return caught.value.reason


def test_a_whole_update_from_the_brain_verifies():
    archive = archive_of(CHUNK * 2 + 100)
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive)
    receiver.offer(offer, NONCES)
    verified = feed(receiver, offer, pieces(archive))
    assert verified == (VERSION, "zip", archive, False)


def test_a_tampered_archive_is_refused_as_not_from_the_brain():
    archive = archive_of(CHUNK + 10)
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive)
    receiver.offer(offer, NONCES)
    chunks = pieces(archive)
    chunks[1] = bytes([chunks[1][0] ^ 1]) + chunks[1][1:]
    assert reason_of(lambda: feed(receiver, offer, chunks)) == update_receive.NOT_FROM_THE_BRAIN


def test_another_commit_is_refused_as_not_from_the_brain():
    archive = archive_of(1000)
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive)
    offer["version"] = "b" * 40
    receiver.offer(offer, NONCES)
    assert reason_of(lambda: feed(receiver, offer, pieces(archive))) == update_receive.NOT_FROM_THE_BRAIN


def test_a_replay_on_another_connection_is_refused():
    archive = archive_of(1000)
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive, nonces=("sat-A", "brain-A"))
    receiver.offer(offer, ("sat-B", "brain-B"))
    assert reason_of(lambda: feed(receiver, offer, pieces(archive))) == update_receive.NOT_FROM_THE_BRAIN


def test_too_big_is_refused_before_any_chunk():
    receiver = UpdateReceiver(PASSWORD, "zip")
    size = protocol.UPDATE_MOST + 1
    offer = make_offer(b"x", size=size, chunks=-(-size // CHUNK))
    assert reason_of(lambda: receiver.offer(offer, NONCES)) == update_receive.TOO_BIG


def test_chunks_out_of_order_or_too_many_are_refused():
    archive = archive_of(CHUNK + 5)
    offer = make_offer(archive)
    receiver = UpdateReceiver(PASSWORD, "zip")
    receiver.offer(offer, NONCES)
    assert reason_of(lambda: receiver.chunk({"id": "u1", "index": 1}, b"x")) == update_receive.CUT_SHORT
    # The transfer was dropped, so even the right chunk now is ignored and the end gives nothing.
    receiver.chunk({"id": "u1", "index": 0}, pieces(archive)[0])
    assert receiver.end({"id": "u1"}) is None
    # Past the offered size.
    receiver.offer(offer, NONCES)
    receiver.chunk({"id": "u1", "index": 0}, pieces(archive)[0])
    assert reason_of(lambda: receiver.chunk({"id": "u1", "index": 1}, b"x" * 6)) == update_receive.CUT_SHORT
    # One chunk too many.
    receiver.offer(offer, NONCES)
    feed_chunks = pieces(archive)
    receiver.chunk({"id": "u1", "index": 0}, feed_chunks[0])
    receiver.chunk({"id": "u1", "index": 1}, feed_chunks[1])
    assert reason_of(lambda: receiver.chunk({"id": "u1", "index": 2}, b"")) == update_receive.CUT_SHORT
    # A chunk over the chunk size.
    receiver.offer(offer, NONCES)
    assert reason_of(lambda: receiver.chunk({"id": "u1", "index": 0}, b"x" * (CHUNK + 1))) == update_receive.CUT_SHORT


def test_an_end_before_every_chunk_is_refused():
    archive = archive_of(CHUNK + 5)
    offer = make_offer(archive)
    receiver = UpdateReceiver(PASSWORD, "zip")
    receiver.offer(offer, NONCES)
    receiver.chunk({"id": "u1", "index": 0}, pieces(archive)[0])
    assert reason_of(lambda: receiver.end({"id": "u1"})) == update_receive.CUT_SHORT


def test_another_format_is_refused():
    archive = archive_of(100)
    receiver = UpdateReceiver(PASSWORD, "bundle")
    assert reason_of(lambda: receiver.offer(make_offer(archive), NONCES)) == update_receive.WRONG_FORMAT


@pytest.mark.parametrize("over", [
    {"id": 5}, {"id": ""}, {"id": "x" * 200}, {"size": True}, {"size": "100"}, {"chunks": True},
    {"chunks": 2}, {"retry": 1}, {"proof": "abc"}, {"proof": "Z" * 64}, {"proof": None},
    {"version": "short"}, {"size": 0, "chunks": 0}, {"size": -5, "chunks": 0},
])
def test_a_malformed_offer_is_refused_never_raised_raw(over):
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive_of(100), **over)
    assert reason_of(lambda: receiver.offer(offer, NONCES)) == update_receive.CUT_SHORT


def test_a_missing_field_is_refused():
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive_of(100))
    del offer["proof"]
    assert reason_of(lambda: receiver.offer(offer, NONCES)) == update_receive.CUT_SHORT


def test_a_new_offer_drops_the_one_before():
    old, new = archive_of(500), archive_of(600)
    receiver = UpdateReceiver(PASSWORD, "zip")
    old_offer = make_offer(old, update_id="old")
    new_offer = make_offer(new, update_id="new", version="c" * 40)
    receiver.offer(old_offer, NONCES)
    receiver.chunk({"id": "old", "index": 0}, old)
    receiver.offer(new_offer, NONCES)
    # The old transfer's leftovers are ignored, not refused, and don't spoil the new one.
    receiver.chunk({"id": "old", "index": 0}, old)
    assert receiver.end({"id": "old"}) is None
    verified = feed(receiver, new_offer, pieces(new))
    assert verified.version == "c" * 40 and verified.archive == new


def test_the_proof_is_never_logged(caplog):
    archive = archive_of(300)
    receiver = UpdateReceiver(PASSWORD, "zip")
    offer = make_offer(archive)
    tampered = make_offer(archive)
    with caplog.at_level(logging.DEBUG):
        receiver.offer(offer, NONCES)
        feed(receiver, offer, pieces(archive))
        receiver.offer(tampered, NONCES)
        with pytest.raises(UpdateRefused):
            feed(receiver, tampered, [b"x" * 300])
    text = caplog.text
    assert "checked out" in text and "didn't check out" in text
    for secret in (offer["proof"], PASSWORD, NONCES[0], NONCES[1]):
        assert secret not in text
