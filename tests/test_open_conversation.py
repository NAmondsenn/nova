"""
Tests for open_conversation.py: whether a conversation is on, where, and the facts which
end it.
"""

from datetime import datetime

from voice_assistant import open_conversation
from voice_assistant.open_conversation import OpenConversation

SETTINGS = {"enabled": True, "idle_minutes": 5, "bigger_model": "claude-sonnet-5-5", "history_exchanges": 100}


class Clock:
    """A time to move by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(settings=None, ended=None):
    """Returns a conversation on a clock of its own, with the clock and the settings it reads."""
    clock = Clock()
    settings = dict(SETTINGS) if settings is None else settings
    return OpenConversation(lambda: settings, now=clock, ended=ended), clock, settings


def test_start_and_end():
    conversation, _, settings = make()
    assert conversation.satellite is None
    assert not conversation.on_at("pc")
    assert conversation.start("pc") == "started"
    assert conversation.satellite == "pc"
    assert conversation.on_at("pc")
    assert not conversation.on_at("bedroom")
    assert not conversation.on_at(None)
    assert conversation.start("pc") == "already here"
    assert conversation.start("bedroom") == "elsewhere"
    assert conversation.satellite == "pc"
    assert conversation.end("said") is True
    assert conversation.end("said") is False
    assert conversation.satellite is None
    settings["enabled"] = False
    assert conversation.start("pc") == "off"
    assert conversation.satellite is None


def test_enabled_is_read_as_it_is_set_now():
    conversation, _, settings = make()
    assert conversation.enabled
    settings["enabled"] = False
    assert not conversation.enabled
    # Missing means on, as start() has it.
    del settings["enabled"]
    assert conversation.enabled


def test_only_answered_turns_put_off_the_idle_end():
    conversation, clock, _ = make()
    assert conversation.idle_in() == float("inf")
    conversation.start("pc")
    assert conversation.idle_in() == 300
    clock.now += 100
    assert conversation.idle_in() == 200
    conversation.answered("hello", "hi", "claude-haiku-5-5")
    assert conversation.idle_in() == 300
    clock.now += 100
    conversation.not_for_nova()
    assert conversation.idle_in() == 200
    clock.now += 250
    assert conversation.idle_in() < 0


def test_three_turns_not_for_nova_in_a_row_end_it():
    conversation, _, _ = make()
    assert not conversation.not_for_nova()
    conversation.start("pc")
    assert [conversation.not_for_nova() for _ in range(open_conversation.NOT_FOR_ME_MOST)] == [False, False, True]
    # A turn Nova answered starts the count again.
    conversation.not_for_nova()
    conversation.answered("hello", "hi", "claude-haiku-5-5")
    assert [conversation.not_for_nova() for _ in range(3)] == [False, False, True]


def test_the_bigger_model_stays_until_moved_down_or_the_end():
    conversation, _, _ = make()
    conversation.start("pc")
    assert not conversation.bigger
    assert conversation.model("h") == "h"
    conversation.bigger = True
    assert conversation.model("h") == "claude-sonnet-5-5"
    conversation.answered("plan it", "here", "claude-sonnet-5-5")
    assert conversation.model("h") == "claude-sonnet-5-5"
    conversation.bigger = False
    assert conversation.model("h") == "h"
    conversation.bigger = True
    conversation.end("said")
    conversation.start("pc")
    assert not conversation.bigger
    assert conversation.model("h") == "h"


def test_the_bigger_model_is_the_one_set_now():
    conversation, _, settings = make()
    conversation.start("pc")
    conversation.bigger = True
    settings["bigger_model"] = "claude-opus-5-5"
    assert conversation.model("h") == "claude-opus-5-5"


def test_ended_gets_the_whole_conversation_once():
    given = []
    conversation, _, _ = make(ended=given.append)
    # Nothing is on, so nothing is noted or handed over.
    conversation.answered("lost", "lost", "claude-haiku-5-5")
    assert conversation.end("said") is False
    assert given == []
    conversation.start("pc")
    conversation.answered("one", "1", "claude-haiku-5-5")
    conversation.answered("two", "2", "claude-haiku-5-5")
    conversation.bigger = True
    conversation.answered("three", "3", "claude-sonnet-5-5")
    assert conversation.end("said") is True
    assert conversation.end("said") is False
    assert len(given) == 1
    assert [(e["user"], e["assistant"], e["model"]) for e in given[0]] == [
        ("one", "1", "claude-haiku-5-5"), ("two", "2", "claude-haiku-5-5"), ("three", "3", "claude-sonnet-5-5")]
    # Each says when, as a wall-clock time which reads back.
    assert all(isinstance(datetime.fromisoformat(e["at"]), datetime) for e in given[0])
    # A new conversation starts with none of it.
    conversation.start("pc")
    conversation.end("idle")
    assert given[1] == []


def test_ended_is_called_outside_the_lock_and_a_failure_doesnt_stop_the_end(caplog):
    def fail(exchanges):
        # Asking again from inside would wait for ever if the lock were still held.
        assert conversation.satellite is None
        raise RuntimeError("secret detail")

    conversation, _, _ = make(ended=fail)
    conversation.start("pc")
    assert conversation.end("said") is True
    assert conversation.satellite is None
    assert conversation.start("pc") == "started"
    # Logged by its type, as what it says may be what was said.
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_only_the_most_recent_exchanges_are_kept():
    given = []
    conversation, _, settings = make(ended=given.append)
    settings["history_exchanges"] = 3
    conversation.start("pc")
    for number in range(5):
        conversation.answered(str(number), "reply", "claude-haiku-5-5")
    conversation.end("said")
    assert [e["user"] for e in given[0]] == ["2", "3", "4"]


def test_a_changed_setting_is_used_at_once():
    conversation, _, settings = make()
    conversation.start("pc")
    assert conversation.idle_in() == 300
    settings["idle_minutes"] = 1
    assert conversation.idle_in() == 60


def test_a_late_move_up_after_the_end_doesnt_leave_the_bigger_model_on():
    conversation, _, _ = make()
    conversation.start("pc")
    conversation.end("idle")
    # A turn still being answered when the conversation ended.
    conversation.bigger = True
    assert not conversation.bigger
    assert conversation.model("h") == "h"
    conversation.start("pc")
    assert not conversation.bigger
    assert conversation.model("h") == "h"


def test_a_late_turn_from_another_satellite_isnt_noted_in_this_conversation():
    given = []
    conversation, clock, _ = make(ended=given.append)
    conversation.start("pc")
    clock.now += 100
    conversation.answered("old", "reply", "claude-haiku-5-5", satellite="pi")
    assert conversation.idle_in() == 200
    assert [conversation.not_for_nova(satellite="pi") for _ in range(3)] == [False, False, False]
    # Where it is on, they count as they do without saying where.
    assert [conversation.not_for_nova(satellite="pc") for _ in range(3)] == [False, False, True]
    conversation.answered("new", "reply", "claude-haiku-5-5", satellite="pc")
    assert conversation.idle_in() == 300
    conversation.end("said")
    assert [e["user"] for e in given[0]] == ["new"]


def test_an_end_for_another_satellite_ends_nothing():
    # The Pi going, say, just as a conversation starts at the PC.
    given = []
    conversation, _, _ = make(ended=given.append)
    conversation.start("pc")
    assert conversation.end("gone", satellite="pi") is False
    assert conversation.on_at("pc") and given == []
    assert conversation.end("gone", satellite="pc") is True
    assert conversation.satellite is None and given == [[]]
