"""Tests for what the brain keeps of each satellite's kind and room."""

import json
import logging

from voice_assistant.satellite_rooms import SatelliteRooms


def _rooms(tmp_path):
    return SatelliteRooms(str(tmp_path / "satellite_rooms.json"))


def test_kind_and_area_survive_a_restart(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True)
    rooms.seen("bedroom", False)
    rooms.set_area("pc", "Study")

    again = _rooms(tmp_path)
    assert again.push_to_talk("pc") is True and again.push_to_talk("bedroom") is False
    assert again.area("pc") == "Study" and again.area("bedroom") is None


def test_a_satellite_never_seen_is_always_on_and_in_no_room(tmp_path):
    rooms = _rooms(tmp_path)
    assert rooms.push_to_talk("ghost") is False and rooms.area("ghost") is None


def test_an_unknown_area_is_a_room_of_its_own(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.set_area("pi", "Study")
    # Neither has been found in a room, so they aren't taken to share one.
    assert rooms.room_of("pc", ["pi", "pc", "kitchen"]) == ["pc"]
    assert rooms.room_of("kitchen", ["pi", "pc", "kitchen"]) == ["kitchen"]


def test_room_of_lists_the_same_area_first_self(tmp_path):
    rooms = _rooms(tmp_path)
    for name, area in (("pi", "Study"), ("pc", "Study"), ("kitchen", "Kitchen"), ("laptop", "Study")):
        rooms.set_area(name, area)
    assert rooms.room_of("pc", ["pi", "pc", "kitchen", "laptop"]) == ["pc", "pi", "laptop"]
    assert rooms.room_of("kitchen", ["pi", "pc", "kitchen", "laptop"]) == ["kitchen"]


def test_always_on_in_room_skips_push_to_talk_and_self(tmp_path):
    rooms = _rooms(tmp_path)
    for name, pressed in (("pc", True), ("laptop", True), ("pi", False), ("other", False)):
        rooms.seen(name, pressed)
        rooms.set_area(name, "Study")
    rooms.seen("kitchen", False)
    rooms.set_area("kitchen", "Kitchen")
    everyone = ["laptop", "kitchen", "pi", "pc", "other"]

    assert rooms.always_on_in_room("pc", everyone) == "pi"
    # The satellite asking is never the answer, even when it is always on.
    assert rooms.always_on_in_room("pi", everyone) == "other"
    assert rooms.always_on_in_room("kitchen", everyone) is None
    # Nothing is said of a room which isn't known.
    assert rooms.always_on_in_room("nowhere", everyone) is None


def test_none_keeps_the_known_area(tmp_path):
    # Home Assistant being down, or not having the device yet, isn't the satellite leaving its room.
    rooms = _rooms(tmp_path)
    rooms.set_area("pc", "Study")
    rooms.set_area("pc", None)
    assert rooms.area("pc") == "Study" and _rooms(tmp_path).area("pc") == "Study"


def test_a_new_kind_replaces_the_old_and_keeps_the_area(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", False)
    rooms.set_area("pc", "Study")
    rooms.seen("pc", True)
    assert _rooms(tmp_path).push_to_talk("pc") is True and _rooms(tmp_path).area("pc") == "Study"


def test_a_file_which_cant_be_read_starts_empty_and_is_replaced(tmp_path, caplog):
    (tmp_path / "satellite_rooms.json").write_text("not json")
    with caplog.at_level(logging.ERROR):
        rooms = _rooms(tmp_path)
    assert rooms.push_to_talk("pc") is False and "Could not read the satellites' rooms" in caplog.text
    rooms.seen("pc", True)
    assert _rooms(tmp_path).push_to_talk("pc") is True


def test_seen_keeps_machine_and_notes_across_a_restart(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    rooms.seen("pi", False, "pi")
    rooms.seen("bedroom", False)
    rooms.set_area("pc", "Study")

    for kept in (rooms, _rooms(tmp_path)):
        assert kept.machine("pc") == "pc" and kept.saves_notes("pc") is True
        assert kept.machine("pi") == "pi" and kept.saves_notes("pi") is False
        assert kept.machine("bedroom") is None and kept.saves_notes("bedroom") is False
        # Neither field costs the others theirs.
        assert kept.push_to_talk("pc") is True and kept.area("pc") == "Study"
    assert rooms.machine("ghost") is None and rooms.saves_notes("ghost") is False


def test_a_new_hello_replaces_the_machine_and_notes(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    # The satellite's latest word is the truth, e.g. its Downloads folder has gone, or it's an older program.
    rooms.seen("pc", True, "pc", False)
    assert _rooms(tmp_path).saves_notes("pc") is False
    rooms.seen("pc", True)
    assert _rooms(tmp_path).machine("pc") is None


def test_seen_only_saves_when_something_changed(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    path = tmp_path / "satellite_rooms.json"
    path.write_text("{}")
    rooms.seen("pc", True, "pc", True)
    assert path.read_text() == "{}"
    rooms.seen("pc", True, "mac", True)
    assert json.loads(path.read_text())["pc"]["machine"] == "mac"


def test_file_from_before_is_read(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": "Study"}, "bedroom": {"push_to_talk": False, "area": None}}))
    rooms = _rooms(tmp_path)
    assert rooms.push_to_talk("pc") is True and rooms.area("pc") == "Study"
    assert rooms.machine("pc") is None and rooms.saves_notes("pc") is False
    # And it takes what the satellite says next without losing the room.
    rooms.seen("pc", True, "pc", True)
    assert _rooms(tmp_path).area("pc") == "Study" and _rooms(tmp_path).machine("pc") == "pc"


def test_odd_machine_or_notes_in_the_file_are_unknown_not_a_lost_file(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": "Study", "machine": 5, "saves_notes": "yes"},
        "mac": {"push_to_talk": True, "area": None, "machine": "toaster", "saves_notes": 1}}))
    rooms = _rooms(tmp_path)
    assert rooms.area("pc") == "Study"
    for name in ("pc", "mac"):
        assert rooms.machine(name) is None and rooms.saves_notes(name) is False


def test_an_old_brain_can_still_read_the_new_file(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    rooms.seen("bedroom", False, "pi")
    rooms.set_area("pc", "Study")
    rooms.set_area("study", "Study")

    stored = json.loads((tmp_path / "satellite_rooms.json").read_text())
    assert set(stored) == {"pc", "bedroom", "study"}
    # What the brain before this one checks of each entry, or it throws the whole file away.
    for each in stored.values():
        assert isinstance(each["push_to_talk"], bool)
        assert each["area"] is None or isinstance(each["area"], str)
