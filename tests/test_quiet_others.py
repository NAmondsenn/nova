"""
Tests for quiet_others.py: which satellites stop listening for the wake word while
Nova speaks on another.
"""

from voice_assistant import quiet_others
from voice_assistant.quiet_others import QuietOthers


def test_the_others_go_quiet_while_one_speaks_and_it_doesnt():
    quiet = QuietOthers()
    assert sorted(quiet.tell({"pc": True, "bedroom": False, "kitchen": False}, 0)) == [("bedroom", True),
                                                                                       ("kitchen", True)]
    assert sorted(quiet.tell({"pc": False, "bedroom": False, "kitchen": False}, 1)) == [("bedroom", False),
                                                                                        ("kitchen", False)]


def test_each_is_told_only_when_its_own_state_changes():
    quiet = QuietOthers()
    assert quiet.tell({"pc": True, "bedroom": False}, 0) == [("bedroom", True)]
    assert quiet.tell({"pc": True, "bedroom": False}, 1) == []
    # Both speaking: each is quiet for the other.
    assert quiet.tell({"pc": True, "bedroom": True}, 2) == [("pc", True)]
    # One stops: the one still speaking hears again, the other stays quiet.
    assert quiet.tell({"pc": False, "bedroom": True}, 3) == [("bedroom", False)]
    assert quiet.tell({"pc": False, "bedroom": False}, 4) == [("pc", False)]


def test_one_staying_quiet_is_told_again_now_and_then():
    quiet = QuietOthers()
    assert quiet.tell({"pc": True, "bedroom": False}, 0) == [("bedroom", True)]
    assert quiet.tell({"pc": True, "bedroom": False}, quiet_others.QUIET_RENEW - 1) == []
    assert quiet.tell({"pc": True, "bedroom": False}, quiet_others.QUIET_RENEW) == [("bedroom", True)]


def test_a_satellite_which_joins_while_another_speaks_is_told():
    quiet = QuietOthers()
    assert quiet.tell({"pc": True}, 0) == []
    assert quiet.tell({"pc": True, "bedroom": False}, 1) == [("bedroom", True)]


def test_a_new_connection_under_the_same_name_is_told_afresh():
    old, new = object(), object()
    quiet = QuietOthers()
    assert quiet.tell({"pc": True, old: False}, 0) == [(old, True)]
    assert quiet.tell({"pc": True, new: False}, 1) == [(new, True)]


def test_one_which_goes_stops_counting():
    quiet = QuietOthers()
    assert quiet.tell({"pc": True, "bedroom": False}, 0) == [("bedroom", True)]
    # The PC goes mid-reply: the bedroom hears again.
    assert quiet.tell({"bedroom": False}, 1) == [("bedroom", False)]


def test_one_satellite_alone_is_never_told_anything():
    quiet = QuietOthers()
    assert quiet.tell({"bedroom": True}, 0) == []
    assert quiet.tell({"bedroom": False}, 1) == []
