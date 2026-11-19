"""
Last Change Test
Checks the one playlist or calendar change Nova keeps for "undo that" is given once, only
for ten minutes, and not once anything else has been done - with a clock of the test's
own, so nothing here waits.
"""

from voice_assistant.undo import NOTHING_TO_UNDO, SOMETHING_ELSE_SINCE, TOO_LATE, UNDO_CHANGE_KEPT, LastChange


def _undone():
    """An undo which works."""
    return {"success": True, "message": "Undone."}


def test_nothing_kept_says_theres_nothing_to_undo():
    assert LastChange().take() == NOTHING_TO_UNDO


def test_the_change_kept_is_given_once():
    change = LastChange()
    change.keep(_undone)
    assert change.take() is _undone
    assert change.take() == NOTHING_TO_UNDO


def test_a_change_kept_too_long_is_too_late():
    now = [1000.0]
    change = LastChange(clock=lambda: now[0])
    change.keep(_undone)
    now[0] += UNDO_CHANGE_KEPT + 1
    assert change.take() == TOO_LATE
    # Said once, as it's let go of then.
    assert change.take() == NOTHING_TO_UNDO

    # Ten minutes to the second is still in time.
    change.keep(_undone)
    now[0] += UNDO_CHANGE_KEPT
    assert change.take() is _undone


def test_something_else_since_leaves_the_change_alone():
    change = LastChange()
    change.keep(_undone)
    change.note_other()
    assert change.take() == SOMETHING_ELSE_SINCE
    # Left where it was, not undone, so it's said the same again.
    assert change.take() == SOMETHING_ELSE_SINCE


def test_something_else_with_nothing_kept_changes_nothing():
    change = LastChange()
    change.note_other()
    assert change.take() == NOTHING_TO_UNDO


def test_a_new_change_replaces_the_last():
    def other():
        return {"success": True, "message": "Other."}

    change = LastChange()
    change.keep(_undone)
    change.keep(other)
    assert change.take() is other
    assert change.take() == NOTHING_TO_UNDO


def test_a_change_after_something_else_is_the_last_thing_done_again():
    change = LastChange()
    change.keep(_undone)
    change.note_other()
    change.keep(_undone)
    assert change.take() is _undone


def test_the_sentences_are_the_ones_the_design_gives():
    assert UNDO_CHANGE_KEPT == 600.0
    assert NOTHING_TO_UNDO == "There's nothing to undo."
    assert SOMETHING_ELSE_SINCE == "The last thing I did wasn't a playlist or calendar change, so I've left it."
    assert TOO_LATE == "That was too long ago to undo."
