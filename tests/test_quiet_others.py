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


def test_a_same_room_satellite_stays_quiet_through_a_conversation():
    quiet = QuietOthers()
    # Nobody is speaking, but a conversation is on at the pc and the pi is in its room.
    assert quiet.tell({"pc": False, "pi": False}, 0, {"pi"}) == [("pi", True)]
    assert quiet.tell({"pc": False, "pi": False}, quiet_others.QUIET_RENEW - 1, {"pi"}) == []
    # Told again as the others are, so it never runs out of its own accord.
    assert quiet.tell({"pc": False, "pi": False}, quiet_others.QUIET_RENEW, {"pi"}) == [("pi", True)]
    # Still quiet while the pc speaks, and not told twice for it.
    assert quiet.tell({"pc": True, "pi": False}, quiet_others.QUIET_RENEW + 1, {"pi"}) == []
    # The conversation ends while the pc is still speaking: the pi stays quiet for that.
    assert quiet.tell({"pc": True, "pi": False}, quiet_others.QUIET_RENEW + 2) == []
    # Released once nothing is speaking either.
    assert quiet.tell({"pc": False, "pi": False}, quiet_others.QUIET_RENEW + 3) == [("pi", False)]


def test_a_conversation_ending_releases_the_satellite_at_once():
    quiet = QuietOthers()
    assert quiet.tell({"pc": False, "pi": False}, 0, {"pi"}) == [("pi", True)]
    assert quiet.tell({"pc": False, "pi": False}, 1) == [("pi", False)]


def test_another_rooms_satellite_isnt():
    quiet = QuietOthers()
    # The kitchen isn't in also_quiet, so it's told nothing while nobody speaks.
    assert quiet.tell({"pc": False, "pi": False, "kitchen": False}, 0, {"pi"}) == [("pi", True)]
    assert quiet.tell({"pc": False, "pi": False, "kitchen": False}, quiet_others.QUIET_RENEW, {"pi"}) == [
        ("pi", True)]
    # It is quiet for speech as ever.
    assert quiet.tell({"pc": True, "pi": False, "kitchen": False}, quiet_others.QUIET_RENEW + 1, {"pi"}) == [
        ("kitchen", True)]


def test_one_in_also_quiet_which_isnt_connected_is_left_out():
    quiet = QuietOthers()
    assert quiet.tell({"pc": False}, 0, {"pi"}) == []


def test_without_also_quiet_it_is_as_before():
    # The same scenario as the first test of all, once as it was called and once with none given.
    for call in (lambda speaking, now: quiet.tell(speaking, now),
                 lambda speaking, now: quiet.tell(speaking, now, frozenset())):
        quiet = QuietOthers()
        assert call({"pc": False, "pi": False}, 0) == []
        assert sorted(call({"pc": True, "pi": False, "kitchen": False}, 1)) == [("kitchen", True), ("pi", True)]
        assert sorted(call({"pc": False, "pi": False, "kitchen": False}, 2)) == [("kitchen", False), ("pi", False)]
