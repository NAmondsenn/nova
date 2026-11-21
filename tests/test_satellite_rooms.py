"""Tests for what the brain keeps of each satellite's kind and room."""

import json
import logging

import pytest

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


def test_called_is_kept_across_a_restart(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    rooms.set_area("pc", "Study")
    rooms.set_called("pc", ["Desk", "the computer"])

    for kept in (rooms, _rooms(tmp_path)):
        assert kept.called("pc") == ["Desk", "the computer"]
        # What it costs the others nothing.
        assert kept.push_to_talk("pc") is True and kept.machine("pc") == "pc" and kept.area("pc") == "Study"
    # The next hello, and the room being found, keep the names.
    rooms.seen("pc", True, "pc", True)
    rooms.seen("pc", False, "pc", False)
    rooms.set_area("pc", "Office")
    assert _rooms(tmp_path).called("pc") == ["Desk", "the computer"]
    # And no names clears them.
    rooms.set_called("pc", [])
    assert _rooms(tmp_path).called("pc") == []


def test_called_defaults_to_none(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True)
    rooms.set_area("study", "Study")
    assert rooms.called("pc") == [] and rooms.called("study") == [] and rooms.called("ghost") == []


def test_a_file_from_before_has_no_names(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": "Study", "machine": "pc", "saves_notes": True}}))
    rooms = _rooms(tmp_path)
    assert rooms.called("pc") == []
    rooms.set_called("pc", ["Desk"])
    assert _rooms(tmp_path).called("pc") == ["Desk"] and _rooms(tmp_path).machine("pc") == "pc"


def test_odd_names_in_the_file_are_none_not_a_lost_file(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": "Study", "called": "Desk"},
        "mac": {"push_to_talk": True, "area": None, "called": ["Desk", 5]},
        "pi": {"push_to_talk": False, "area": None, "called": None}}))
    rooms = _rooms(tmp_path)
    assert rooms.area("pc") == "Study"
    for name in ("pc", "mac", "pi"):
        assert rooms.called(name) == []


def test_called_gives_a_copy(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.set_called("pc", ["Desk"])
    rooms.called("pc").append("Other")
    assert rooms.called("pc") == ["Desk"]
    names = ["Desk"]
    rooms.set_called("pc", names)
    names.append("Other")
    assert rooms.called("pc") == ["Desk"]


def test_called_for_a_satellite_not_yet_seen_makes_its_entry(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.set_called("pc", ["Desk"])

    stored = json.loads((tmp_path / "satellite_rooms.json").read_text())
    # As seen makes it, so an old brain can read the file, and push_to_talk starts as False.
    assert stored["pc"]["push_to_talk"] is False and stored["pc"]["area"] is None
    assert stored["pc"]["called"] == ["Desk"]
    assert rooms.push_to_talk("pc") is False and rooms.machine("pc") is None and rooms.saves_notes("pc") is False
    # The satellite's hello then fills in the rest and keeps the names.
    rooms.seen("pc", True, "pc", True)
    again = _rooms(tmp_path)
    assert again.called("pc") == ["Desk"] and again.push_to_talk("pc") is True and again.machine("pc") == "pc"


def test_an_old_brain_can_still_read_a_file_with_names(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    rooms.set_called("pc", ["Desk"])
    rooms.set_called("new", ["Other"])

    stored = json.loads((tmp_path / "satellite_rooms.json").read_text())
    assert set(stored) == {"pc", "new"}
    for each in stored.values():
        assert isinstance(each["push_to_talk"], bool)
        assert each["area"] is None or isinstance(each["area"], str)


def test_set_called_raises_when_it_cant_be_saved_and_keeps_what_it_had(tmp_path, monkeypatch):
    from voice_assistant import satellite_rooms as rooms_module

    rooms = _rooms(tmp_path)
    rooms.set_called("pc", ["Desk"])

    def refuse(path, data, indent=None):
        raise OSError("disk full")

    monkeypatch.setattr(rooms_module, "save_json", refuse)
    with pytest.raises(OSError):
        rooms.set_called("pc", ["Other"])
    with pytest.raises(OSError):
        rooms.set_called("new", ["Other"])
    # The page is told it failed, so it mustn't also be doing what it was refused.
    assert rooms.called("pc") == ["Desk"] and rooms.called("new") == []


def test_opens_things_default_on_and_saved(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    # On until he says otherwise, for one nobody has seen too.
    assert rooms.opens_things("pc") is True and rooms.opens_things("ghost") is True
    rooms.set_opens_things("pc", False)
    for kept in (rooms, _rooms(tmp_path)):
        assert kept.opens_things("pc") is False
    # The next hello keeps it.
    rooms.seen("pc", False, "pc", False)
    assert _rooms(tmp_path).opens_things("pc") is False
    rooms.set_opens_things("pc", True)
    assert _rooms(tmp_path).opens_things("pc") is True


def test_app_names_and_the_pc_are_saved_and_default_to_none(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, "pc", True)
    assert rooms.app_names("pc") == {} and rooms.pc_device("pc") is None and rooms.app_names("ghost") == {}
    rooms.set_app_names("pc", {"gta": "Grand Theft Auto V Enhanced"})
    rooms.set_pc_device("pc", "desk")
    for kept in (rooms, _rooms(tmp_path)):
        assert kept.app_names("pc") == {"gta": "Grand Theft Auto V Enhanced"} and kept.pc_device("pc") == "desk"
    rooms.set_pc_device("pc", None)
    assert _rooms(tmp_path).pc_device("pc") is None


def test_a_file_from_before_opens_things_and_odd_values_are_read_as_the_defaults(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": None, "machine": "pc"},
        "mac": {"push_to_talk": True, "area": None, "opens_things": "no", "app_names": ["x"], "pc_device": 4}}))
    rooms = _rooms(tmp_path)
    assert rooms.opens_things("pc") is True and rooms.app_names("pc") == {} and rooms.pc_device("pc") is None
    assert rooms.opens_things("mac") is True and rooms.app_names("mac") == {} and rooms.pc_device("mac") is None


def test_can_approve_is_on_only_for_a_pc_or_mac_set_up_first(tmp_path):
    rooms = _rooms(tmp_path)
    # Saying it approves, with nothing else set up to: the first way, so on.
    for name, machine in (("pc", "pc"), ("mac", "mac"), ("pi", "pi"), ("bare", None)):
        rooms.seen(name, True, machine=machine, approves=True, guarded=False)
    assert [rooms.can_approve(n) for n in ("pc", "mac", "pi", "bare")] == [True, True, False, False]
    assert rooms.can_approve("ghost") is False
    # Stored, not worked out from the machine, so it outlasts a restart as it was.
    assert _rooms(tmp_path).can_approve("pc") is True
    # A PC which never said it approves has no switch on.
    rooms.seen("older", True, machine="pc")
    assert rooms.can_approve("older") is False


def test_a_pc_first_saying_it_approves_once_a_way_is_set_up_is_off(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=True)
    assert rooms.can_approve("pc") is False and _rooms(tmp_path).can_approve("pc") is False
    # Unless told, it's taken as guarded: never on by mistake.
    rooms.seen("mac", True, machine="mac", approves=True)
    assert rooms.can_approve("mac") is False
    # Once stored, a later hello leaves it as it was, either way.
    rooms.set_can_approve("pc", True)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=True)
    assert rooms.can_approve("pc") is True
    rooms.seen("mac", True, machine="mac", approves=True, guarded=False)
    assert rooms.can_approve("mac") is False


def test_a_satellite_which_becomes_a_pc_is_stored_afresh(tmp_path):
    rooms = _rooms(tmp_path)
    # A Pi he switched on, whose hello now says it's a PC: anything with its password could say so.
    rooms.seen("study", True, machine="pi", approves=True, guarded=False)
    rooms.set_can_approve("study", True)
    rooms.seen("study", True, machine="pc", approves=True, guarded=True)
    assert rooms.can_approve("study") is False
    # The same, with nothing set up yet, is the first way, so on.
    rooms.seen("den", True, machine="linux", approves=True, guarded=False)
    rooms.seen("den", True, machine="mac", approves=True, guarded=False)
    assert rooms.can_approve("den") is True


def test_approving_is_each_kept_satellite_which_approves_and_may(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=False)
    rooms.seen("mac", True, machine="mac", approves=True, guarded=True)
    rooms.seen("pi", True, machine="pi")
    rooms.set_can_approve("pi", True)
    assert rooms.approving(["pc", "mac", "pi", "ghost"]) == ["pc"]
    # Only those which may connect are looked at.
    assert rooms.approving(["mac", "pi"]) == []


def test_can_approve_switch_outlasts_a_restart(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=False)
    rooms.seen("pi", True, machine="pi")
    rooms.set_can_approve("pc", False)
    rooms.set_can_approve("pi", True)
    again = _rooms(tmp_path)
    assert again.can_approve("pc") is False and again.can_approve("pi") is True


def test_approves_is_what_it_last_said(tmp_path):
    rooms = _rooms(tmp_path)
    assert rooms.approves("pc") is False
    rooms.seen("pc", True, machine="pc", approves=True)
    assert rooms.approves("pc") is True and _rooms(tmp_path).approves("pc") is True
    rooms.seen("pc", True, machine="pc")
    assert rooms.approves("pc") is False


def test_forget_approval_follows_the_first_set_up_rule_and_is_saved(tmp_path):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=False)
    rooms.forget_approval("pc", guarded=True)
    assert rooms.can_approve("pc") is False and _rooms(tmp_path).can_approve("pc") is False
    rooms.forget_approval("pc", guarded=False)
    assert rooms.can_approve("pc") is True and _rooms(tmp_path).can_approve("pc") is True
    # Removed, it's forgotten entirely, so its next hello stores it afresh.
    rooms.forget_approval("pc", None)
    again = _rooms(tmp_path)
    assert again.approves("pc") is False and again.can_approve("pc") is False
    again.seen("pc", True, machine="pc", approves=True, guarded=True)
    assert again.can_approve("pc") is False
    # One never seen is left for its hello.
    rooms.forget_approval("nowhere", guarded=False)
    assert "nowhere" not in json.loads((tmp_path / "satellite_rooms.json").read_text())


def test_forget_approval_raises_when_it_cant_be_saved_but_is_off_in_memory(tmp_path, monkeypatch):
    rooms = _rooms(tmp_path)
    rooms.seen("pc", True, machine="pc", approves=True, guarded=False)

    def refuse(path, data, indent=None):
        raise OSError("disk full")

    monkeypatch.setattr("voice_assistant.satellite_rooms.save_json", refuse)
    with pytest.raises(OSError):
        rooms.forget_approval("pc", guarded=True)
    assert rooms.can_approve("pc") is False


def test_an_older_file_reads_with_no_switch(tmp_path):
    (tmp_path / "satellite_rooms.json").write_text(json.dumps({
        "pc": {"push_to_talk": True, "area": None, "machine": "pc"},
        "odd": {"push_to_talk": True, "area": None, "machine": "pi", "approves": "yes", "can_approve": "no"}}))
    rooms = _rooms(tmp_path)
    # No switch stored is no, until its hello stores one.
    assert rooms.approves("pc") is False and rooms.can_approve("pc") is False
    assert rooms.approves("odd") is False and rooms.can_approve("odd") is False
    rooms.seen("pc", True, machine="pc", approves=True, guarded=False)
    assert rooms.can_approve("pc") is True
