"""Tests for the passkeys Nova keeps on disk."""

import logging
import os
import stat

from voice_assistant.passkeys import Passkey, PasskeyStore


def _key(n=1):
    return Passkey(id=f"id{n}", name=f"Phone {n}", public_key=f"pk{n}", algorithm=-7, counter=0,
                   origin="https://nova.example.ts.net", added=1000.0 + n)


def _store(tmp_path):
    return PasskeyStore(str(tmp_path / "passkeys.json"))


def test_a_passkey_is_kept_across_a_restart(tmp_path):
    assert _store(tmp_path).add(_key()) is None
    assert _store(tmp_path).get("id1") == _key()
    assert _store(tmp_path).all() == [_key()]


def test_at_most_three(tmp_path):
    store = _store(tmp_path)
    for n in (1, 2, 3):
        assert store.add(_key(n)) is None
    assert store.add(_key(4)) == "Nova keeps at most 3 passkeys."
    assert [each.id for each in store.all()] == ["id1", "id2", "id3"]


def test_the_same_one_twice_is_refused(tmp_path):
    store = _store(tmp_path)
    store.add(_key())
    assert store.add(_key()) == "That passkey is already added."
    assert len(store.all()) == 1


def test_remove_and_counter(tmp_path):
    store = _store(tmp_path)
    store.add(_key())
    store.set_counter("id1", 7)
    assert _store(tmp_path).get("id1").counter == 7
    assert store.remove("id1") is True
    assert store.remove("id1") is False
    assert _store(tmp_path).all() == []


def test_an_unreadable_file_is_none_and_logged(tmp_path, caplog):
    (tmp_path / "passkeys.json").write_text("{not json")
    store = _store(tmp_path)
    with caplog.at_level(logging.ERROR):
        assert store.all() == []
        assert store.get("id1") is None
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_the_file_is_private(tmp_path):
    _store(tmp_path).add(_key())
    assert stat.S_IMODE(os.stat(tmp_path / "passkeys.json").st_mode) == 0o600
