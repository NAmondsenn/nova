"""
Music Resume Test
The music kept for a satellite across an update, carried on once.
"""

import logging
import time

from voice_assistant import music_resume
from voice_assistant.music_resume import MusicResume

STATE = {"queue": ["a song"], "index": 0, "position": 42.0}


def test_kept_music_comes_back_once(tmp_path):
    kept = MusicResume(str(tmp_path / "update" / "music.json"))
    kept.keep("bedroom", STATE)

    assert kept.take("bedroom") == STATE
    assert kept.take("bedroom") is None


def test_music_kept_longer_than_ten_minutes_isnt_resumed(tmp_path, monkeypatch):
    kept = MusicResume(str(tmp_path / "music.json"))
    now = time.time()
    monkeypatch.setattr(music_resume.time, "time", lambda: now)
    kept.keep("bedroom", STATE)
    kept.keep("pc", STATE)

    monkeypatch.setattr(music_resume.time, "time", lambda: now + 601)
    assert kept.take("bedroom") is None
    # Taken out either way, so it's never played later.
    monkeypatch.setattr(music_resume.time, "time", lambda: now + 1)
    assert kept.take("bedroom") is None
    monkeypatch.setattr(music_resume.time, "time", lambda: now + 600)
    assert kept.take("pc") == STATE


def test_music_kept_survives_a_restart(tmp_path):
    path = str(tmp_path / "update" / "music.json")
    MusicResume(path).keep("bedroom", STATE)

    assert MusicResume(path).take("bedroom") == STATE


def test_a_missing_or_broken_file_reads_as_nothing(tmp_path):
    path = tmp_path / "music.json"
    kept = MusicResume(str(path))
    assert kept.take("bedroom") is None
    path.write_text("{not json")
    assert kept.take("bedroom") is None
    kept.keep("bedroom", STATE)
    assert kept.take("bedroom") == STATE


def test_a_clock_gone_backwards_is_too_old_and_a_dropped_entry_is_logged(tmp_path, monkeypatch, caplog):
    kept = MusicResume(str(tmp_path / "music.json"))
    now = time.time()
    monkeypatch.setattr(music_resume.time, "time", lambda: now)
    kept.keep("bedroom", STATE)
    kept.keep("pc", STATE)

    monkeypatch.setattr(music_resume.time, "time", lambda: now - 5)
    assert kept.take("bedroom") is None
    monkeypatch.setattr(music_resume.time, "time", lambda: now + 700)
    with caplog.at_level(logging.INFO, logger="voice_assistant.music_resume"):
        assert kept.take("pc") is None
    assert any("pc" in record.message and "700" in record.message and "too long" in record.message
               for record in caplog.records)
