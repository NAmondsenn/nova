"""Tests for what the brain keeps of each satellite's kind and room."""

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
