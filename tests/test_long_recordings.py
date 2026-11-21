"""
Long Recordings Test
Checks the small record the brain keeps of push-to-talk recordings whose speech
runs over five seconds: only those, one JSON line each, the newest 50 kept, the
words only when given, and nothing ever raised for a file which can't be written.
"""

import json
import logging
from datetime import datetime

import pytest

from voice_assistant import long_recordings
from voice_assistant.long_recordings import LONGEST_SPEECH, MOST, note_long_recording

REPORT = {"ended_by": "limit", "loudness": 0.31, "levels": [0.2, 0.31, 0.05], "speech": [0.9, 0.95, 0.1],
          "words_quiet": 0.4}
AT = datetime(2026, 11, 21, 9, 30, 5, 123456)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "long-recordings.jsonl")


@pytest.fixture(autouse=True)
def warn_afresh(monkeypatch):
    """Each test starts as if nothing had yet failed, so whether a warning is logged doesn't depend on test order."""
    monkeypatch.setattr(long_recordings, "_warned", False)


def lines(path):
    with open(path) as f:
        return [json.loads(line) for line in f.read().splitlines()]


def test_only_speech_over_five_seconds_is_noted(path):
    assert LONGEST_SPEECH == 5.0
    assert not note_long_recording(path, AT, 5.0, REPORT)
    assert not note_long_recording(path, AT, 2.0, REPORT)
    assert note_long_recording(path, AT, 5.01, REPORT)

    assert [line["speech_seconds"] for line in lines(path)] == [5.01]


def test_a_recording_which_is_not_noted_leaves_no_file(path, tmp_path):
    note_long_recording(path, AT, 3.0, REPORT)

    assert list(tmp_path.iterdir()) == []


def test_the_line_has_the_numbers_the_recorder_saw_and_the_words(path):
    note_long_recording(path, AT, 7.456, REPORT, words="Play some music and then turn it down")

    (line,) = lines(path)
    assert line == {"at": "2026-11-21T09:30:05", "speech_seconds": 7.46, **REPORT,
                    "words": "Play some music and then turn it down"}


def test_nothing_in_the_line_is_audio(path):
    # Every value is a number, a string, or a list of numbers: nothing which could be samples of sound.
    note_long_recording(path, AT, 9.0, REPORT, words="Hello")

    (line,) = lines(path)
    for key, value in line.items():
        if isinstance(value, list):
            assert all(isinstance(number, float) for number in value), key
        else:
            assert value is None or isinstance(value, (str, int, float)), key


def test_without_words_the_line_leaves_them_out(path):
    note_long_recording(path, AT, 9.0, REPORT)
    note_long_recording(path, AT, 9.0, REPORT, words=None)

    assert all("words" not in line for line in lines(path))


def test_no_words_heard_is_kept_as_none_heard(path):
    # Words kept but none heard is what the recorder's noise-only recordings look like, and isn't the same as not keeping them.
    note_long_recording(path, AT, 9.0, REPORT, words="")

    assert lines(path)[0]["words"] == ""


def test_each_recording_is_a_line_of_its_own(path):
    for seconds in (6.0, 7.0, 8.0):
        note_long_recording(path, AT, seconds, REPORT)

    assert [line["speech_seconds"] for line in lines(path)] == [6.0, 7.0, 8.0]


def test_the_newest_fifty_are_kept(path):
    assert MOST == 50
    for number in range(MOST):
        note_long_recording(path, AT, 6.0 + number, REPORT)
    assert len(lines(path)) == MOST

    # The 51st takes the place of the oldest.
    note_long_recording(path, AT, 100.0, REPORT)

    kept = [line["speech_seconds"] for line in lines(path)]
    assert len(kept) == MOST
    assert kept[0] == 7.0
    assert kept[-1] == 100.0


def test_a_file_already_over_the_most_is_cut_back(path):
    # Left by an older version, or kept by hand, with more than should be there.
    with open(path, "w") as f:
        for number in range(MOST + 10):
            f.write(json.dumps({"speech_seconds": 6.0 + number}) + "\n")

    note_long_recording(path, AT, 99.0, REPORT)

    kept = [line["speech_seconds"] for line in lines(path)]
    assert len(kept) == MOST
    assert kept[0] == 6.0 + 11 and kept[-1] == 99.0


def test_what_is_said_in_other_languages_is_kept_whole(path):
    note_long_recording(path, AT, 9.0, REPORT, words="Qu'est-ce que c'est, ça ? 你好")

    assert lines(path)[0]["words"] == "Qu'est-ce que c'est, ça ? 你好"


def test_a_file_which_cannot_be_written_logs_once_and_returns_false(tmp_path, caplog):
    # Its folder isn't there, so nothing can be made in it.
    path = str(tmp_path / "missing" / "long-recordings.jsonl")

    with caplog.at_level(logging.WARNING, logger=long_recordings.logger.name):
        assert note_long_recording(path, AT, 9.0, REPORT) is False
        assert note_long_recording(path, AT, 9.0, REPORT) is False

    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1


def test_a_failure_which_has_passed_is_warned_of_again_if_it_comes_back(path, tmp_path, caplog):
    # Once the file works the warning is spent, so a later failure isn't missed for good.
    missing = str(tmp_path / "missing" / "long-recordings.jsonl")

    with caplog.at_level(logging.WARNING, logger=long_recordings.logger.name):
        note_long_recording(missing, AT, 9.0, REPORT)
        assert note_long_recording(path, AT, 9.0, REPORT) is True
        note_long_recording(missing, AT, 9.0, REPORT)

    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 2


def test_a_report_which_cannot_be_saved_is_not_raised(path):
    assert note_long_recording(path, AT, 9.0, {"ended_by": object()}) is False


def test_a_failed_write_leaves_what_was_kept(path, tmp_path, monkeypatch):
    note_long_recording(path, AT, 6.0, REPORT)
    before = open(path).read()

    def fail(source, destination):
        raise OSError("the disk is full")

    monkeypatch.setattr(long_recordings.os, "replace", fail)
    assert note_long_recording(path, AT, 7.0, REPORT) is False

    # What was kept is whole, and no half-made copy is left beside it.
    assert open(path).read() == before
    assert [file.name for file in tmp_path.iterdir()] == ["long-recordings.jsonl"]


def test_what_cannot_be_made_into_a_line_is_not_raised(path):
    # However it's called, keeping the note never stops a request being answered.
    assert note_long_recording(path, None, 9.0, REPORT) is False
    assert note_long_recording(path, AT, None, REPORT) is False
    assert note_long_recording(path, AT, 9.0, None) is False
    assert note_long_recording(path, AT, 9.0, ["not", "a", "report"]) is False


def test_words_kept_before_are_taken_out_once_they_are_not_kept(path):
    # With "Keep what's said in the logs" turned off, the next note takes the words out of the
    # ones already kept, and leaves the rest of each line as it was.
    note_long_recording(path, AT, 6.0, REPORT, words="Play some music")
    note_long_recording(path, AT, 7.0, REPORT, words="")
    note_long_recording(path, AT, 8.0, REPORT)

    kept = lines(path)
    assert [line["speech_seconds"] for line in kept] == [6.0, 7.0, 8.0]
    assert all("words" not in line for line in kept)
    assert "Play some music" not in open(path).read()
    assert kept[0] == {"at": "2026-11-21T09:30:05", "speech_seconds": 6.0, **REPORT}


def test_words_kept_before_stay_while_they_are_kept(path):
    note_long_recording(path, AT, 6.0, REPORT, words="Play some music")
    note_long_recording(path, AT, 7.0, REPORT, words="What time is it")

    assert [line["words"] for line in lines(path)] == ["Play some music", "What time is it"]


def test_a_line_which_cant_be_read_goes_once_the_words_are_not_kept(path):
    # Its words can't be taken out of it, so it goes rather than keep what may be them.
    with open(path, "w") as f:
        f.write('{"speech_seconds": 6.0, "words": "Play so\n')
        f.write(json.dumps("Play some music") + "\n")
        f.write(json.dumps({"speech_seconds": 7.0}) + "\n")

    note_long_recording(path, AT, 8.0, REPORT)

    assert [line["speech_seconds"] for line in lines(path)] == [7.0, 8.0]


def test_a_folder_where_the_file_should_be_is_not_raised(tmp_path):
    folder = tmp_path / "long-recordings.jsonl"
    folder.mkdir()

    assert note_long_recording(str(folder), AT, 9.0, REPORT) is False
