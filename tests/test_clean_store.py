"""Tests for CleanStore - what Nova has heard of each song, its lists, swaps and check progress."""

import json
import logging
import os
import stat
import threading
from dataclasses import asdict

import pytest

from voice_assistant import clean_store, paths, storage
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.music_folder import Held

UPDATED = "2026-11-10T09:30:00.123456789Z"


def swap(key="song-1", **changes):
    """A swap as Task 7 saves one, with its Held as the dict dataclasses.asdict makes."""
    held = Held("Gunna/Album/01 - Endless.mp3", "Gunna/Album/01 - Endless.mp3",
                [("Gunna/Album/01 - Endless.lrc", "Gunna/Album/01 - Endless.lrc")])
    saved = {"key": key, "name": "Gunna - Endless", "title": "Endless", "artist": "Gunna", "album": "Album",
             "held": asdict(held), "tries": 1, "track_id": "3135556", "asked_at": 1763000000.0, "limit": 1763000600.0}
    saved.update(changes)
    return saved


def test_the_store_lives_in_the_data_folder():
    assert paths.CLEAN_SONGS_FILE == os.path.join(paths.DATA_DIR, "clean_songs.json")


def test_a_new_store_is_empty(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))

    assert store.verdict("a") is None
    assert not store.known("a", 1, UPDATED)
    assert store.listed("swapped") == []
    assert store.swaps() == []
    assert store.check() == {}


def test_a_verdict_survives_a_reload(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    verdict = Verdict("clean", 12, 1)
    CleanStore(path).set_verdict("abc", 4096, UPDATED, verdict, "Gunna - Endless")

    found = CleanStore(path).verdict("abc")

    assert found["verdict"] == asdict(verdict)
    assert found["size"] == 4096
    assert found["updated"] == UPDATED
    assert found["name"] == "Gunna - Endless"


def test_a_verdict_not_judged_keeps_its_reason(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    CleanStore(path).set_verdict("abc", 1, UPDATED, Verdict("not_judged", 0, 0, "no lyrics"), "X")

    assert CleanStore(path).verdict("abc")["verdict"] == {"kind": "not_judged", "expected": 0, "heard": 0,
                                                          "reason": "no lyrics"}


def test_a_new_verdict_replaces_the_old(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.set_verdict("abc", 1, UPDATED, Verdict("unsure", 10, 2), "X")

    store.set_verdict("abc", 2, "later", Verdict("explicit", 10, 9), "X")

    assert store.verdict("abc")["verdict"]["kind"] == "explicit"
    assert store.known("abc", 2, "later")
    assert not store.known("abc", 1, UPDATED)


def test_known_needs_the_same_size_and_updated(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    CleanStore(path).set_verdict("abc", 4096, UPDATED, Verdict("explicit", 10, 9), "X")
    store = CleanStore(path)

    assert store.known("abc", 4096, UPDATED)
    assert not store.known("abc", 4097, UPDATED)
    assert not store.known("abc", 4096, "2026-11-11T09:30:00Z")
    assert not store.known("other", 4096, UPDATED)


def test_a_verdict_handed_back_cant_change_the_store(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.set_verdict("abc", 1, UPDATED, Verdict("clean", 10, 0), "X")

    store.verdict("abc")["verdict"]["kind"] = "explicit"

    assert store.verdict("abc")["verdict"]["kind"] == "clean"


@pytest.mark.parametrize("kind", ["swapped", "only_clean", "unsure", "couldnt"])
def test_each_list_survives_a_reload(tmp_path, kind):
    path = str(tmp_path / "clean_songs.json")
    CleanStore(path).add(kind, "Gunna - Endless", "no explicit copy found", song_id="abc")

    entries = CleanStore(path).listed(kind)

    assert entries == [{"name": "Gunna - Endless", "reason": "no explicit copy found", "song_id": "abc",
                        "at": entries[0]["at"]}]


def test_the_lists_are_kept_apart(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.add("swapped", "A")
    store.add("unsure", "A")

    assert [e["name"] for e in store.listed("swapped")] == ["A"]
    assert store.listed("only_clean") == []
    assert store.listed("couldnt") == []


def test_an_unknown_kind_is_refused(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))

    with pytest.raises(ValueError):
        store.add("nonsense", "A")
    with pytest.raises(ValueError):
        store.listed("nonsense")
    assert not os.path.exists(str(tmp_path / "clean_songs.json"))


def test_a_list_is_newest_first(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    for name in ("A", "B", "C"):
        store.add("unsure", name)

    assert [e["name"] for e in store.listed("unsure")] == ["C", "B", "A"]


def test_adding_a_name_again_moves_it_to_the_front_with_the_new_reason(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.add("couldnt", "A", "first reason", song_id="old")
    store.add("couldnt", "B", "other")

    store.add("couldnt", "A", "second reason")

    assert [(e["name"], e["reason"]) for e in store.listed("couldnt")] == [("A", "second reason"), ("B", "other")]
    assert "song_id" not in store.listed("couldnt")[0]


def test_an_entry_says_when_it_was_added_by_the_clock_the_store_was_given(tmp_path):
    now = [1763000000]
    store = CleanStore(str(tmp_path / "clean_songs.json"), clock=lambda: now[0])
    store.add("swapped", "A")
    now[0] += 60
    store.add("swapped", "B")

    assert [(e["name"], e["at"]) for e in store.listed("swapped")] == [("B", 1763000060), ("A", 1763000000)]
    # Kept in the file, so a reload still says when.
    assert CleanStore(str(tmp_path / "clean_songs.json")).listed("swapped")[1]["at"] == 1763000000


def test_an_entry_is_stamped_with_the_wall_clock_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(clean_store.time, "time", lambda: 1763000123.7)
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.add("unsure", "A")

    assert store.listed("unsure")[0]["at"] == 1763000123.7


def test_a_time_the_caller_gave_is_kept(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"), clock=lambda: 1763000000)
    store.add("couldnt", "A", "why", at=1700000000)

    assert store.listed("couldnt")[0]["at"] == 1700000000


def test_adding_a_name_again_stamps_the_new_time(tmp_path):
    now = [1763000000]
    store = CleanStore(str(tmp_path / "clean_songs.json"), clock=lambda: now[0])
    store.add("couldnt", "A", "first")
    now[0] += 3600
    store.add("couldnt", "A", "second")

    assert [e["at"] for e in store.listed("couldnt")] == [1763003600]


def test_an_entry_saved_before_entries_had_times_is_left_without_one(tmp_path):
    # So it counts as old, as the weekly note counts only what has an "at" within its week.
    path = str(tmp_path / "clean_songs.json")
    with open(path, "w") as file:
        json.dump({"lists": {"swapped": [{"name": "Old", "reason": ""}]}}, file)

    store = CleanStore(path, clock=lambda: 1763000000)

    assert store.listed("swapped") == [{"name": "Old", "reason": ""}]
    store.add("swapped", "New")
    assert [e.get("at") for e in store.listed("swapped")] == [1763000000, None]


def test_an_entry_can_be_dropped_by_its_song_id(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    store.add("couldnt", "A", "why", song_id="a1")
    store.add("couldnt", "B", "why", song_id="b1")
    store.add("swapped", "A", "", song_id="a1")

    store.discard("couldnt", "a1")
    store.discard("couldnt", "nobody")

    assert [e["name"] for e in CleanStore(path).listed("couldnt")] == ["B"]
    assert [e["name"] for e in CleanStore(path).listed("swapped")] == ["A"]
    with pytest.raises(ValueError):
        store.discard("nonsense", "a1")


def test_the_same_name_can_be_on_two_lists(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.add("swapped", "A", "one")
    store.add("couldnt", "A", "two")

    assert store.listed("swapped")[0]["reason"] == "one"
    assert store.listed("couldnt")[0]["reason"] == "two"


def test_a_list_keeps_the_newest_200(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    for number in range(205):
        store.add("only_clean", f"Song {number}")

    entries = CleanStore(path).listed("only_clean")

    assert len(entries) == 200
    assert entries[0]["name"] == "Song 204"
    assert entries[-1]["name"] == "Song 5"
    assert len(store.listed("swapped")) == 0


def test_a_swap_survives_a_reload(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    CleanStore(path).save_swap(swap())

    saved = CleanStore(path).swaps()

    # JSON has no tuples, so a Held's sidecars come back as lists, and the same before a reload.
    assert saved[0]["held"]["sidecars"] == [["Gunna/Album/01 - Endless.lrc", "Gunna/Album/01 - Endless.lrc"]]
    assert saved == CleanStore(path).swaps()
    assert saved[0]["held"]["original"] == "Gunna/Album/01 - Endless.mp3"


def test_a_swap_reads_the_same_before_and_after_a_reload(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    store.save_swap(swap())

    assert store.swaps() == CleanStore(path).swaps()


def test_saving_a_swap_with_the_same_key_replaces_it(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.save_swap(swap("a", tries=1))
    store.save_swap(swap("b"))

    store.save_swap(swap("a", tries=2))

    assert [(s["key"], s["tries"]) for s in store.swaps()] == [("a", 2), ("b", 1)]


def test_ending_a_swap_forgets_only_that_one(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    store.save_swap(swap("a"))
    store.save_swap(swap("b"))

    store.end_swap("a")
    store.end_swap("never-there")

    assert [s["key"] for s in CleanStore(path).swaps()] == ["b"]


def test_a_swap_handed_back_cant_change_the_store(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    given = swap()
    store.save_swap(given)

    given["held"]["original"] = "changed"
    store.swaps()[0]["held"]["held"] = "changed too"

    assert store.swaps()[0]["held"]["original"] == "Gunna/Album/01 - Endless.mp3"
    assert store.swaps()[0]["held"]["held"] == "Gunna/Album/01 - Endless.mp3"


def test_a_swap_without_a_key_is_refused(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))

    with pytest.raises(ValueError):
        store.save_swap({"name": "A"})


def test_the_checks_progress_survives_a_reload(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    state = {"running": True, "done": 112, "total": 398, "current": "Gunna - Endless",
             "queue": ["a", "b", "c"], "last_finished": 1763236800.0}
    CleanStore(path).save_check(state)

    assert CleanStore(path).check() == state


def test_the_check_handed_back_cant_change_the_store(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.save_check({"queue": ["a"]})

    store.check()["queue"].append("b")

    assert store.check() == {"queue": ["a"]}


def test_the_schedules_record_survives_a_reload(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    record = {"night_done": "2026-11-17", "week_sent": "2026-W47", "note_at": 1763371800.0}
    CleanStore(path).save_schedule(record)

    assert CleanStore(path).schedule() == record


def test_a_new_store_has_no_schedule_record(tmp_path):
    assert CleanStore(str(tmp_path / "clean_songs.json")).schedule() == {}


def test_the_schedule_handed_back_cant_change_the_store(tmp_path):
    store = CleanStore(str(tmp_path / "clean_songs.json"))
    store.save_schedule({"night_done": "2026-11-17"})

    store.schedule()["night_done"] = "2026-11-18"

    assert store.schedule() == {"night_done": "2026-11-17"}


def test_schedule_fields_of_the_wrong_type_are_dropped_on_loading(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"schedule": {"night_done": 20261117, "week_sent": "2026-W47", "note_at": True},
                                "check": {"done": 3}}))

    store = CleanStore(str(path))

    assert store.schedule() == {"week_sent": "2026-W47"}
    assert store.check() == {"done": 3}


def test_a_schedule_that_isnt_a_dictionary_starts_empty(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"schedule": ["2026-11-17"], "check": {"done": 3}}))

    store = CleanStore(str(path))

    assert store.schedule() == {}
    assert store.check() == {"done": 3}


def test_every_part_is_kept_together(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    store.set_verdict("abc", 1, UPDATED, Verdict("clean", 5, 0), "X")
    store.add("swapped", "X")
    store.save_swap(swap())
    store.save_check({"done": 1})
    store.save_schedule({"night_done": "2026-11-17"})

    again = CleanStore(path)

    assert again.verdict("abc") is not None
    assert again.listed("swapped")
    assert again.swaps()
    assert again.check() == {"done": 1}
    assert again.schedule() == {"night_done": "2026-11-17"}


def test_the_file_is_readable_by_this_user_only(tmp_path):
    path = tmp_path / "clean_songs.json"
    CleanStore(str(path)).add("swapped", "A")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_corrupt_file_starts_empty_and_is_kept_aside(tmp_path, caplog):
    path = tmp_path / "clean_songs.json"
    path.write_text("{not json")

    with caplog.at_level(logging.WARNING):
        store = CleanStore(str(path))

    assert store.swaps() == []
    assert store.listed("swapped") == []
    assert (tmp_path / "clean_songs.json.bad").read_text() == "{not json"
    assert not path.exists()
    assert "clean_songs.json" in caplog.text


def test_a_second_corrupt_file_doesnt_overwrite_the_first_kept_aside(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text("first")
    CleanStore(str(path))
    path.write_text("second")

    CleanStore(str(path))

    assert (tmp_path / "clean_songs.json.bad").read_text() == "first"
    assert [p.read_text() for p in tmp_path.glob("clean_songs.json.bad.*")] == ["second"]


def test_a_file_that_isnt_a_dictionary_is_corrupt(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text("[1, 2]")

    store = CleanStore(str(path))

    assert store.check() == {}
    assert (tmp_path / "clean_songs.json.bad").exists()


def test_a_part_of_the_wrong_type_starts_empty_without_losing_the_rest(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"verdicts": [], "lists": {"swapped": "x", "unsure": [{"name": "A", "reason": ""}, 7]},
                                "swaps": {"a": 1}, "check": {"done": 3}}))

    store = CleanStore(str(path))

    assert store.verdict("a") is None
    assert store.listed("swapped") == []
    assert store.listed("unsure") == [{"name": "A", "reason": ""}]
    assert store.swaps() == []
    assert store.check() == {"done": 3}


def test_a_swap_that_isnt_a_dictionary_is_dropped_on_loading(tmp_path):
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"swaps": [swap("a"), "junk", {"no": "key"}]}))

    assert [s["key"] for s in CleanStore(str(path)).swaps()] == ["a"]


def test_a_store_which_cant_be_saved_raises_and_keeps_its_state(tmp_path, monkeypatch):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    store.save_swap(swap("a"))

    def refuse(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(clean_store.storage, "save_json", refuse)

    with pytest.raises(OSError):
        store.save_swap(swap("b"))
    with pytest.raises(OSError):
        store.add("swapped", "A")

    assert [s["key"] for s in store.swaps()] == ["a"]
    assert store.listed("swapped") == []
    monkeypatch.undo()
    assert [s["key"] for s in CleanStore(path).swaps()] == ["a"]


def test_an_unwritable_file_leaves_the_old_one_whole(tmp_path, monkeypatch):
    path = tmp_path / "clean_songs.json"
    store = CleanStore(str(path))
    store.save_swap(swap("a"))
    before = path.read_text()

    # The save itself fails part way, after the new file was begun beside the old one.
    def disk_full(descriptor):
        raise OSError("disk full")
    monkeypatch.setattr(storage.os, "fsync", disk_full)

    with pytest.raises(OSError):
        store.save_check({"done": 3})

    monkeypatch.undo()
    assert path.read_text() == before
    assert store.check() == {}
    assert sorted(os.listdir(tmp_path)) == ["clean_songs.json"]


def test_a_check_that_cant_be_made_json_leaves_the_old_file_whole(tmp_path):
    path = tmp_path / "clean_songs.json"
    store = CleanStore(str(path))
    store.save_swap(swap("a"))
    before = path.read_text()

    with pytest.raises(TypeError):
        store.save_check({"bad": object()})

    assert path.read_text() == before and store.check() == {}


def test_a_file_that_cant_be_read_is_neither_set_aside_nor_written_over(tmp_path, monkeypatch, caplog):
    # Unreadable for a while (a permissions slip, a disk error), not corrupt: the swaps in it
    # say where songs are held, so starting empty must never replace it.
    path = tmp_path / "clean_songs.json"
    path.write_text(json.dumps({"swaps": [swap("a")]}))
    before = path.read_text()

    def unreadable(*args, **kwargs):
        raise PermissionError("Permission denied")
    monkeypatch.setattr(clean_store.storage, "load_json", unreadable)

    with caplog.at_level(logging.ERROR):
        store = CleanStore(str(path))

    assert store.swaps() == [] and "clean_songs.json" in caplog.text
    with pytest.raises(OSError):
        store.save_swap(swap("b"))
    with pytest.raises(OSError):
        store.add("swapped", "A")
    assert path.read_text() == before
    assert sorted(os.listdir(tmp_path)) == ["clean_songs.json"]


def test_a_corrupt_file_that_cant_be_set_aside_isnt_written_over(tmp_path, monkeypatch):
    path = tmp_path / "clean_songs.json"
    path.write_text("{not json")

    def refuse(*args, **kwargs):
        raise OSError("read-only")
    monkeypatch.setattr(clean_store.os, "rename", refuse)
    store = CleanStore(str(path))

    with pytest.raises(OSError):
        store.add("swapped", "A")
    assert path.read_text() == "{not json"


def test_verdicts_and_check_fields_of_the_wrong_type_are_dropped_on_loading(tmp_path):
    path = tmp_path / "clean_songs.json"
    good = {"size": 1, "updated": UPDATED, "name": "A", "verdict": {"kind": "clean", "expected": 5, "heard": 0,
                                                                     "reason": ""}}
    path.write_text(json.dumps({
        "verdicts": {"a": good, "b": "junk", "c": [1], "d": {"size": 1, "verdict": "clean"}},
        "check": {"running": "yes", "paused": False, "done": "3", "total": 5, "current": 7, "queue": None,
                  "last_finished": 1763000000.0, "error": None}}))

    store = CleanStore(str(path))

    assert store.verdict("a") == good
    assert [store.verdict(key) for key in "bcd"] == [None, None, None]
    assert not store.known("b", 1, UPDATED)
    assert store.check() == {"paused": False, "total": 5, "queue": None, "last_finished": 1763000000.0,
                             "error": None}


def test_two_threads_adding_at_once_lose_nothing(tmp_path):
    path = str(tmp_path / "clean_songs.json")
    store = CleanStore(path)
    start = threading.Barrier(2)

    def add(prefix):
        start.wait()
        for number in range(60):
            store.add("unsure", f"{prefix} {number}")
            store.set_verdict(f"{prefix}{number}", number, UPDATED, Verdict("clean", 5, 0), "X")

    threads = [threading.Thread(target=add, args=(prefix,)) for prefix in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    again = CleanStore(path)
    assert len(again.listed("unsure")) == 120
    assert all(again.known(f"{p}{n}", n, UPDATED) for p in ("A", "B") for n in range(60))


def test_it_writes_through_the_shared_atomic_save(tmp_path, monkeypatch):
    saved = []
    real = storage.save_json
    monkeypatch.setattr(clean_store.storage, "save_json", lambda path, data, indent=None: (saved.append(path),
                                                                                           real(path, data, indent)))

    CleanStore(str(tmp_path / "clean_songs.json")).add("swapped", "A")

    assert saved == [str(tmp_path / "clean_songs.json")]
