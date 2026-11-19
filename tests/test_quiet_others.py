"""
Tests for quiet_others.py: which satellites stop listening for the wake word while
Nova speaks on another.
"""

from voice_assistant.quiet_others import QuietOthers


def test_the_others_go_quiet_while_one_speaks_and_it_doesnt():
    quiet = QuietOthers()
    assert sorted(quiet.update(["pc", "bedroom", "kitchen"], "pc", True)) == [("bedroom", True), ("kitchen", True)]
    assert sorted(quiet.update(["pc", "bedroom", "kitchen"], "pc", False)) == [("bedroom", False), ("kitchen", False)]


def test_each_is_told_only_when_its_own_state_changes():
    quiet = QuietOthers()
    connected = ["pc", "bedroom"]
    assert quiet.update(connected, "pc", True) == [("bedroom", True)]
    # Saying it again changes nothing.
    assert quiet.update(connected, "pc", True) == []
    # Both speaking: each is quiet for the other.
    assert quiet.update(connected, "bedroom", True) == [("pc", True)]
    # One stops: the one still speaking hears again, the other stays quiet.
    assert quiet.update(connected, "pc", False) == [("bedroom", False)]
    assert quiet.update(connected, "bedroom", False) == [("pc", False)]


def test_a_satellite_which_joins_while_another_speaks_is_told():
    quiet = QuietOthers()
    assert quiet.update(["pc"], "pc", True) == []
    assert quiet.update(["pc", "bedroom"]) == [("bedroom", True)]


def test_one_which_goes_stops_counting_and_is_forgotten():
    quiet = QuietOthers()
    assert quiet.update(["pc", "bedroom"], "pc", True) == [("bedroom", True)]
    # The PC goes mid-reply: the bedroom hears again.
    assert quiet.update(["bedroom"], "pc", False) == [("bedroom", False)]
    # Back while the bedroom speaks: told as if new.
    assert quiet.update(["bedroom"], "bedroom", True) == []
    assert quiet.update(["pc", "bedroom"]) == [("pc", True)]


def test_one_satellite_alone_is_never_told_anything():
    quiet = QuietOthers()
    assert quiet.update(["bedroom"], "bedroom", True) == []
    assert quiet.update(["bedroom"], "bedroom", False) == []
