"""
Local Intents Test
Checks which everyday commands are handled without Claude, and - just as much -
which are left to it.
"""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from voice_assistant import intents as intents_module
from voice_assistant.actions import Action
from voice_assistant.clock_words import ClockWords
from voice_assistant.computers import ComputerHooks, Computers
from voice_assistant.intents import LocalIntents, calendar_days
from voice_assistant.open_conversation import OpenConversation


# Stands in for Home Assistant, which the Assist shapes need to be there at all.
HOME = object()


# The activities' words, as GoogleCalendar.activity_words gives them for a Work and a Uni calendar.
WORK_AND_UNI = {"work": "work", "works": "work", "working": "work", "shift": "work", "shifts": "work",
                "uni": "uni", "unis": "uni", "university": "uni", "lecture": "uni", "lectures": "uni",
                "class": "uni", "classes": "uni"}


def _actions(text, clock=None, home=HOME, activities=lambda: WORK_AND_UNI):
    """The actions a request is handled with locally, or None if it's left to Claude."""
    result = LocalIntents(clock=clock, home=home, activities=activities).match(text)
    return None if result is None else result["actions"]


@pytest.mark.parametrize("text, seconds", [
    ("Set a timer for ten minutes.", 600),
    ("set a timer for 10 minutes", 600),
    ("Timer for 30 seconds", 30),
    ("Set a 5 minute timer.", 300), ("Set of one minute timer,", 60),
    ("Start a timer for twenty five minutes, please.", 1500),
    ("Set a timer for an hour.", 3600),
    ("Set a timer for half an hour.", 1800),
    ("Could you set a timer for 2 hours?", 7200),
])
def test_timers_are_set_without_claude(text, seconds):
    # Every request used to go to Claude first, so a timer couldn't even be set
    # with the internet down.
    assert _actions(text) == [Action("set_timer", {"duration_seconds": seconds})]


@pytest.mark.parametrize("text, command", [
    # A bare "stop" is the music too - anything ringing has already been dismissed by then.
    ("Pause.", "pause"), ("Pause the music.", "pause"), ("Stop the music.", "pause"), ("Stop.", "pause"),
    ("Skip.", "skip"), ("Next song.", "skip"), ("Skip this song.", "skip"),
    ("Previous song.", "previous"), ("Go back.", "previous"),
    # How Moonshine heard "stop" and "skip" said on their own.
    ("Soap.", "pause"), ("So.", "pause"), ("Scap.", "skip"),
    # How Nemotron, hearing it again said twice, heard Piper's "Pause." on its own.
    ("Cause.", "pause"), ("Paws.", "pause"),
])
def test_playback_controls_are_handled_without_claude(text, command):
    assert _actions(text) == [Action("control_playback", {"action": command})]


def test_a_number_is_only_read_from_plain_digits():
    # "²" is a digit to str.isdigit, but not a number int() reads.
    assert intents_module._number("12") == 12 and intents_module._number("²") is None


@pytest.mark.parametrize("text, command", [
    # "Nova, pause." said in one breath, as Nemotron heard it.
    ("Novopause.", "pause"), ("Novapause.", "pause"), ("Novaskip.", "skip"), ("Novopause, please.", "pause"),
    # With the rest of the wake word before it.
    ("Hey Novopause.", "pause"), ("Hey, Novaskip.", "skip"),
])
def test_the_wake_word_run_into_a_command_is_the_command(text, command):
    assert _actions(text) == [Action("control_playback", {"action": command})]


@pytest.mark.parametrize("text", [
    # Words of their own which start the same way - Claude's, as ever.
    "Novel.", "Novacane.", "Novas.", "Nova.", "Novo.",
    # Only a lone word is tried, so a request run on from it is Claude's to work out.
    "Novopause the music.", "Hey Novopause the music.", "Hey Novel.", "Hey hey Novopause.",
])
def test_words_starting_like_the_wake_word_arent_taken_for_it(text):
    assert _actions(text) is None


def test_its_the_wake_word_set_which_is_run_into_a_command():
    jarvis = LocalIntents(wake_word="Hey Jarvis")
    assert jarvis.match("Jarvispause.")["actions"] == [Action("control_playback", {"action": "pause"})]
    assert jarvis.match("Hey Jarvispause.")["actions"] == [Action("control_playback", {"action": "pause"})]
    assert jarvis.match("Novopause.") is None and jarvis.match("Hey Novopause.") is None


@pytest.mark.parametrize("text", ["Resume.", "Play.", "Play the music.", "Unpause."])
def test_resuming_plays_whatever_was_playing(text):
    assert _actions(text) == [Action("play_music")]


@pytest.mark.parametrize("text, change", [
    ("Turn it up.", {"direction": "up"}), ("Turn it down.", {"direction": "down"}),
    ("Volume up.", {"direction": "up"}), ("Louder.", {"direction": "up"}), ("Quieter.", {"direction": "down"}),
    ("Volume 40.", {"level": 40}), ("Set the volume to 70 percent.", {"level": 70}),
])
def test_volume_is_changed_without_claude(text, change):
    assert _actions(text) == [Action("set_volume", change)]


@pytest.mark.parametrize("text", [
    # More than one thing, or something extra - Claude runs both or works it out.
    "Set a timer for ten minutes and play Drake.",
    "Pause it in five minutes.",
    "Play Drake.",
    "Turn up the heating.",
    "What's the time in Tokyo?",
    "Set a timer.",
    "Set a timer for a few minutes.",
    "Volume 400.",
    "Remind me to check the oven in ten minutes.",
])
def test_anything_else_is_left_to_claude(text):
    assert _actions(text) is None


def test_the_time_and_date_are_answered_without_claude(monkeypatch):
    class Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, 19, 36)

    monkeypatch.setattr(intents_module, "datetime", Evening)
    assert LocalIntents().match("What time is it?") == {"response": "It's 7:36 PM.", "actions": [],
                                                        "success": True}
    assert LocalIntents().match("What's the date today?")["response"] == "It's Monday the 19th of October."


@pytest.mark.parametrize("timers, handled", [(1, True), (0, False), (2, False)])
def test_the_timer_is_only_cancelled_locally_when_theres_just_one(timers, handled):
    clock = SimpleNamespace(timer_count=lambda: timers)
    assert (_actions("Cancel the timer.", clock=clock) is not None) is handled


@pytest.mark.parametrize("text, day", [
    ("What's the weather?", "today"), ("What's the weather like tomorrow?", "tomorrow"),
    ("What will the weather be like today?", "today"), ("What's the forecast for tomorrow?", "tomorrow"),
])
def test_the_weather_is_asked_for_without_claude(text, day):
    assert _actions(text) == [Action("get_weather", {"day": day})]


@pytest.mark.parametrize("text, command, on, words, brightness", [
    ("Turn off the kitchen lights.", "turn off the kitchen lights", False, "the kitchen lights", None),
    ("Switch the lamp on.", "switch the lamp on", True, "the lamp", None),
    ("Living room lights on.", "living room lights on", True, "living room lights", None),
    ("Lights off, please.", "lights off", False, "lights", None),
    ("Set the bedroom lamp to 30 percent.", "set the bedroom lamp to 30 percent", True, "the bedroom lamp", 30),
    # Off, as Home Assistant takes 0% - so Nova's own lights don't switch it on at 1%.
    ("Set the garage light to 0 percent.", "set the garage light to 0 percent", False, "the garage light", 0),
])
def test_the_lights_go_straight_to_home_assistant(text, command, on, words, brightness):
    # With what Nova's own lights need, should the guard refuse the sentence.
    assert _actions(text) == [Action("control_home", {"command": command, "on": on, "words": words,
                                                      "brightness": brightness})]


@pytest.mark.parametrize("text, direction, words", [
    ("Dim the lights.", "down", "the lights"),
    ("Brighten the lamp.", "up", "the lamp"),
    ("Dim the bedroom lights.", "down", "the bedroom lights"),
    ("Dim the main light.", "down", "the main light"),
    ("brighten all the lights, please", "up", "all the lights"),
    # With the word that often goes with each, which isn't a light's name.
    ("Brighten up the lights.", "up", "the lights"), ("Dim down the bedroom lamp.", "down", "the bedroom lamp"),
])
def test_dimming_is_done_without_claude(text, direction, words):
    assert _actions(text) == [Action("dim_lights", {"direction": direction, "words": words})]


@pytest.mark.parametrize("text", ["Dim the timer and the lights.", "Dim the screen.", "Dim the lights by half.",
                                  "Brighten my day.", "Brighten up the lights a bit.", "Dim."])
def test_dimming_leaves_what_isnt_a_light_to_claude(text):
    assert _actions(text) is None


def test_setting_a_light_to_a_level_still_goes_to_assist():
    assert _actions("Set the lamp to 30 percent.") == [Action("control_home", {
        "command": "set the lamp to 30 percent", "on": True, "words": "the lamp", "brightness": 30})]


def test_the_lights_with_something_else_are_left_to_claude():
    # Sent whole, Home Assistant would take "the timer and the" for a light's name.
    assert _actions("Turn off the timer and the lights.") is None


@pytest.mark.parametrize("text", [
    "Pause the TV.", "Turn off AdGuard protection.", "Switch the fan on.", "Next track on the TV.",
    "Set the TV volume to 40.", "Run goodnight.", "Activate movie night.", "Close the blinds.",
    # "In" and a number are only a delay together - "in five minutes", "at six".
    "Turn off the TV in the kitchen.", "Turn on fan 2.",
    # "One" is as often a name's number, and "second" an order, as either points back or
    # says when.
    "Turn on fan one.", "Turn on BBC One.", "Activate the second scene.", "Turn the second fan off.",
])
def test_assists_own_sentences_go_to_assist(text):
    # Sent whole, on the wider rule - see HomeAssistant.command.
    request = text.lower().rstrip(".?")
    assert _actions(text) == [Action("control_home", {"command": request, "wider": True})]


@pytest.mark.parametrize("text", [
    "Is the fan on?", "Is the garage door open?", "Is the patio closed?",
    # The light rule doesn't take "is" for part of a light's name.
    "Is the lamp on?",
])
def test_a_question_for_assist_says_its_a_question(text):
    # So a refused one goes to Claude, which can read it with home_status, rather than
    # being refused - see ActionExecutor.control_home.
    request = text.lower().rstrip(".?")
    assert _actions(text) == [Action("control_home", {"command": request, "wider": True, "question": True})]


@pytest.mark.parametrize("text", [
    "Turn off my alarm.", "Turn off the timer.", "Turn on shuffle.", "Turn on repeat.",
    "Turn it off.", "Pause it.", "Is it on?", "Turn off the music.", "Turn off notifications.",
    "Turn off the TV and the lamp.", "Turn up the heating.",
    # "My" isn't a name, so this isn't "the volume" of something called "my".
    "Set my volume to 40.",
    # Nova's own, by more of their words.
    "Pause playback.", "Pause Navidrome.", "Turn off the chime.", "Turn off the wake word.", "Turn off quiet hours.",
    "Turn on battery alerts.", "Turn off the warnings.",
    # Words that only point back, to something the conversation so far says.
    "Turn the other one off.", "Turn the same one on.", "Turn both off.", "Switch the rest off.", "Run again.",
    "Run the last one.", "Open the first one.", "Turn one off.", "Turn off the second one.", "Open the third one.",
    "Turn on the one in the kitchen.", "Turn off the fourth one.", "Turn on another one.", "Run the previous one.",
    "Turn off the second.",
    # Two things at once, however they're joined.
    "Turn off the TV plus the fan.", "Turn off the TV then the fan.",
    # When, rather than what - a delay is Claude's.
    "Pause the TV in five minutes.", "Pause the TV for a bit.", "Turn off the TV in ten minutes.",
    "Turn off the oven at six.", "Turn on the heating until ten.", "Turn off the light in five minutes.",
    "Run the dishwasher tonight.", "Turn on the heating by 7.", "Turn on the oven for half an hour.",
    "Pause the TV in a second.", "Turn off the fan in 30 seconds.", "Pause the TV a second.",
    "Pause the TV one second.", "Pause the TV in second.", "Turn off the fan for second.",
    # For the user, not a thing of Home Assistant's.
    "Run me a bath.", "Open us a window.",
    # The shapes with nothing above yet for what they mustn't catch.
    "Previous track on Spotify.", "Run the timer.", "Activate it.", "Open it.", "Close that.",
])
def test_what_the_assist_rule_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict. Nova's own things, words Assist can't know the
    # meaning of, a delay, or two things at once, are Claude's.
    assert _actions(text) is None


def FOLLOWED():
    """Stands in for Football.names: the teams the user follows."""
    return ["Stevenage", "Liverpool", "South Shields"]


@pytest.mark.parametrize("text", [
    "Is Liverpool playing?", "Is the football on?", "Is Stevenage on?", "Is South Shields playing?",
    "Is the Liverpool game on?", "Pause the football.",
])
def test_football_is_never_assists(text):
    # Left to Claude, which has get_football - Assist would take "Liverpool" for a media player.
    result = LocalIntents(home=HOME, football=FOLLOWED).match(text)
    assert result is None


def test_other_things_still_go_to_assist_with_teams_followed():
    result = LocalIntents(home=HOME, football=FOLLOWED).match("Is the fan on?")
    assert result["actions"] == [Action("control_home", {"command": "is the fan on", "wider": True, "question": True})]


def test_a_team_nobody_follows_is_assists_to_turn_down():
    # Assist matches nothing in it, so it reaches Claude the usual way - see Conversation.handle.
    assert LocalIntents(home=HOME, football=FOLLOWED).match("Is Arsenal playing?") is not None


def test_a_team_is_only_heard_as_whole_words():
    # "Liverpool" isn't "Liverpoolish", so a thing which only starts with a team's name is still Assist's.
    result = LocalIntents(home=HOME, football=lambda: ["Liverpool"]).match("Is the liverpoolish lamp on?")
    assert result["actions"][0].tool == "control_home"


def test_the_teams_are_only_asked_for_once_a_request_has_an_assist_shape():
    asked = []
    intents = LocalIntents(home=HOME, football=lambda: asked.append("names") or ["Liverpool"])
    assert intents.match("What time is it?") is not None
    assert intents.match("Turn it off.") is None
    assert not asked
    intents.match("Is the fan on?")
    assert asked == ["names"]


@pytest.mark.parametrize("text, followed", [
    # Said in plain letters or the accented ones, as the speech recogniser may write either.
    ("Is Atletico Madrid playing?", "Atlético Madrid"), ("Is Atlético Madrid playing?", "Atlético Madrid"),
    ("Is Bodo Glimt playing?", "Bodø/Glimt"), ("Is Bodø Glimt playing?", "Bodø/Glimt"),
    ("Is Koln playing?", "Köln"), ("Is Lodz playing?", "Łódź"), ("Is Atletico Madrid playing?", "ATLÉTICO MADRID"),
])
def test_a_team_is_heard_with_its_accents_or_without(text, followed):
    assert LocalIntents(home=HOME, football=lambda: [followed]).match(text) is None


def test_a_team_is_heard_by_any_name_it_is_known_by():
    # Football.known_as gives the short name ESPN has, the full name and the entry of the box.
    def known_as():
        return ["Nott'm Forest", "Nottingham Forest", "Forest"]

    assert LocalIntents(home=HOME, football=known_as).match("Is Nottingham Forest playing?") is None
    assert LocalIntents(home=HOME, football=known_as).match("Is Forest on?") is None


def test_the_football_calendar_is_still_heard():
    # "Football" is only left out of Assist's things, not of the calendars' words, so a
    # calendar of that name keeps working.
    games = {"football": "football"}
    result = LocalIntents(home=HOME, football=FOLLOWED, activities=lambda: games).match("When's football tomorrow?")
    assert result["actions"][0].tool == "get_activity"


def test_a_question_isnt_taken_for_a_light_sentence():
    # "Are the lights" isn't a light's name, which Nova's own lights would switch on
    # if the guard refused the sentence.
    assert _actions("Are the lights on?") is None


@pytest.mark.parametrize("text", [
    "Pause the TV.", "Is the post office open?", "Is Tesco open?", "Is the pharmacy closed?", "Is the football on?",
    "Run through my day.", "Open YouTube.", "Open the settings.", "Turn on the news.",
])
def test_without_home_assistant_nothing_is_taken_for_assist(text):
    # Otherwise "is the post office open?" would get "Home Assistant isn't set up yet",
    # and never reach Claude.
    assert _actions(text, home=None) is None


@pytest.mark.parametrize("text, action", [
    ("Pause.", Action("control_playback", {"action": "pause"})),
    ("Next track.", Action("control_playback", {"action": "skip"})),
    ("Set the volume to 40.", Action("set_volume", {"level": 40})),
])
def test_novas_own_commands_still_come_before_assist(text, action):
    assert _actions(text) == [action]


@pytest.mark.parametrize("text", ["What have I got on Thursday?", "What's on my calendar for Thursday?",
                                  "Am I busy on Thursday?", "What do I have planned for Thursday?",
                                  "Do I have anything on Thursday?"])
def test_the_calendar_is_read_without_claude(text, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_calendar", {"start": "2026-10-22", "days": 1})]


@pytest.mark.parametrize("day, today, expected", [
    (None, "2026-10-20", ("2026-10-20", 1)),
    ("tomorrow", "2026-10-20", ("2026-10-21", 1)),
    ("tuesday", "2026-10-20", ("2026-10-20", 1)),
    ("monday", "2026-10-20", ("2026-10-26", 1)),
    ("this weekend", "2026-10-21", ("2026-10-24", 2)),
    ("the weekend", "2026-10-24", ("2026-10-24", 2)),
    ("this weekend", "2026-10-25", ("2026-10-25", 1)),
])
def test_a_named_day_is_the_next_one(day, today, expected):
    from datetime import date
    first, days = calendar_days(day, date.fromisoformat(today))
    assert (first.isoformat(), days) == expected


@pytest.mark.parametrize("text, activity, start", [
    ("What time do I have work tomorrow?", "work", "2026-10-21"),
    ("When do I finish work today?", "work", "2026-10-20"),
    ("Am I working on Saturday?", "work", "2026-10-24"),
    ("What time is work?", "work", "2026-10-20"),
    ("What time do I have uni tomorrow?", "uni", "2026-10-21"),
    ("When do I finish uni today?", "uni", "2026-10-20"),
    ("Have I got lectures on Friday?", "uni", "2026-10-23"),
])
def test_work_and_uni_are_read_from_the_calendar_without_claude(text, activity, start, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_activity", {"activity": activity, "start": start, "days": 1})]


@pytest.mark.parametrize("text, activity, count", [
    ("When do I next have work?", "work", 1),
    ("When's my next shift?", "work", 1),
    ("What are my next three shifts?", "work", 3),
    ("When is my next lecture?", "uni", 1),
])
def test_the_next_time_is_asked_for_without_claude(text, activity, count, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_activity", {"activity": activity, "start": "2026-10-20", "count": count})]


def test_without_the_calendar_work_and_uni_are_left_to_claude():
    for text in ("What time do I have work tomorrow?", "When's my next shift?", "Am I working on Saturday?"):
        assert _actions(text, activities=None) is None
        assert _actions(text, activities=dict) is None


# Calendars named like Nova's own things - Claude, which has both, is asked about them.
NOVAS_WORDS = {"timer": "timer", "timers": "timer", "music": "music", "musics": "music", "shopping": "shopping",
               "shoppings": "shopping", "notifications": "notifications", "phone": "phone", "phones": "phone"}


@pytest.mark.parametrize("text, action", [
    ("Set a timer for ten minutes.", Action("set_timer", {"duration_seconds": 600})),
    ("Pause the music.", Action("control_playback", {"action": "pause"})),
    ("Add milk to my shopping list.", Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                             "command": "add milk to my shopping list"})),
    ("What's on my shopping list?", Action("read_list", {"list": "my shopping list"})),
    ("Have I got any notifications?", Action("read_notifications")),
    ("Where's my phone?", Action("find_device", {"device": "phone", "ring": False})),
])
def test_a_calendar_named_like_novas_own_command_never_takes_it_over(text, action):
    result = LocalIntents(home=HOME, ntfy=FakeNtfy(), activities=lambda: NOVAS_WORDS).match(text)
    assert result["actions"] == [action]


@pytest.mark.parametrize("text", ["When's my next timer?", "What time do I have music tomorrow?",
                                  "Have I got notifications today?", "Am I at phone tomorrow?"])
def test_a_calendar_named_as_one_of_novas_own_things_is_claudes(text):
    assert _actions(text, activities=lambda: NOVAS_WORDS) is None
    # Shopping isn't one of Nova's own things, only a list's name.
    assert _actions("When's shopping tomorrow?", activities=lambda: NOVAS_WORDS)[0].params["activity"] == "shopping"


# Notifications

class FakeNtfy:
    """Stands in for ntfy, saying whether an alert was just said out loud."""

    def __init__(self, just_said=False):
        self.just = just_said

    def just_said(self):
        return self.just


@pytest.mark.parametrize("said, tool", [
    ("Any notifications?", "read_notifications"),
    ("Have I got any new notifications?", "read_notifications"),
    ("What are my notifications?", "read_notifications"),
    ("Send that to my phone.", "send_to_phone"),
    ("Can you send it to my phone please?", "send_to_phone"),
    ("Text me that.", "send_to_phone"),
])
def test_notification_commands_are_handled_on_the_pi(said, tool):
    assert [action.tool for action in LocalIntents(ntfy=FakeNtfy()).match(said)["actions"]] == [tool]


def test_tell_me_more_is_only_about_an_alert_just_said():
    assert LocalIntents(ntfy=FakeNtfy(just_said=True)).match("Tell me more.")["actions"][0].tool == "tell_me_more"
    # Otherwise it's a follow-up to whatever was being talked about, which is Claude's.
    assert LocalIntents(ntfy=FakeNtfy(just_said=False)).match("Tell me more.") is None


def test_without_ntfy_notification_commands_go_to_claude():
    for said in ("Any notifications?", "Send that to my phone.", "Tell me more."):
        assert LocalIntents().match(said) is None


@pytest.mark.parametrize("text, params", [
    ("How's my Mac's battery?", {"device": "mac"}),
    ("what's my phone on", {"device": "phone"}),
    ("What's the tablet's battery?", {"device": "tablet"}),
    ("Battery levels.", {}),
    ("How are my batteries?", {}),
])
def test_battery_questions_are_answered_locally(text, params):
    assert _actions(text) == [Action("get_batteries", params)]


@pytest.mark.parametrize("text", [
    "What's on my phone",
    "What's on tonight",
    "Play battery by Metallica",
    "Set an alarm for 7",
    "How's my battery",
    "My phone's battery is flat",
    "What's the battery of the car",
    "Battery levels and the weather",
    "Turn off my phone",
])
def test_what_the_battery_rules_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict. "What's on tonight" is the calendar's, and the rest isn't a battery question.
    actions = _actions(text)
    assert not actions or all(action.tool != "get_batteries" for action in actions)


def test_the_phone_is_found_and_rung_without_claude():
    assert _actions("Where's my phone?") == [Action("find_device", {"device": "phone", "ring": False})]
    assert _actions("Where is the tablet?") == [Action("find_device", {"device": "tablet", "ring": False})]
    assert _actions("Find my phone.") == [Action("find_device", {"device": "phone", "ring": True})]
    assert _actions("Ring my tablet.") == [Action("find_device", {"device": "tablet", "ring": True})]
    assert _actions("Ring the Mac, please.") == [Action("find_device", {"device": "mac", "ring": True})]
    # Still the battery, and anything less plain is Claude's.
    assert _actions("What's my phone on?") == [Action("get_batteries", {"device": "phone"})]
    assert _actions("Where's my phone charger?") is None
    assert _actions("Find my phone and turn off the lamp.") is None
    # Without Home Assistant it's still Nova's, which says so, rather than Claude's.
    assert _actions("Where's my phone?", home=None) == [Action("find_device", {"device": "phone", "ring": False})]


@pytest.mark.parametrize("text", [
    "Find my phone number",
    "Ring my mum",
    "Ring my phone in five minutes",
    "Where's the Mac's charger?",
    "Where's my phone case",
    "Where is my tablet's charger",
    "Find my keys",
    "Where's the TV remote?",
    "Find the laptop bag",
    "Ring the doorbell",
])
def test_what_the_find_device_rules_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict - only the device itself, said plainly.
    actions = _actions(text)
    assert not actions or all(action.tool != "find_device" for action in actions)


@pytest.mark.parametrize("text, device", [
    ("Send that to my Mac.", "mac"), ("Put it on the tablet.", "tablet"), ("Text this to my laptop", "laptop"),
    ("Send it to my MacBook.", "macbook"), ("Put that on my iPad please.", "ipad"),
])
def test_sending_to_the_mac_and_tablet_is_done_without_claude(text, device):
    assert _actions(text) == [Action("send_to_phone", {"device": device})]
    # Without Home Assistant there is nothing to send it through.
    assert _actions(text, home=None) is None


def test_the_send_to_device_rule_comes_before_assists_shapes():
    rules = LocalIntents(home=HOME)._rules
    sending = [n for n, (pattern, _) in enumerate(rules) if "(?P<device>mac|macbook|laptop|tablet|ipad)" in pattern]
    assisting = [n for n, (_, handle) in enumerate(rules) if getattr(handle, "__name__", "") in ("_assist", "_ask")]
    assert len(sending) == 1 and assisting and sending[0] < min(assisting)


@pytest.mark.parametrize("text", [
    "Send that to my Mac mini in the loft",
    "Put it on my iPad's list",
    "Put it on the tablet in the kitchen",
    "Send that to my mum",
    "Send the photo to my Mac",
    "Put a film on the tablet",
    "Send that to my Mac and my phone",
])
def test_what_the_send_to_device_rule_mustnt_catch_is_left_alone(text):
    actions = _actions(text)
    assert not actions or all(action.tool != "send_to_phone" for action in actions)


def test_the_find_device_rules_come_before_assists_shapes():
    # So "find my phone" is never a thing of Home Assistant's, should Assist ever learn a "find" shape.
    rules = LocalIntents(home=HOME)._rules
    finding = [n for n, (pattern, _) in enumerate(rules) if "(?P<device>phone|tablet|mac|laptop)" in pattern]
    assisting = [n for n, (_, handle) in enumerate(rules) if getattr(handle, "__name__", "") in ("_assist", "_ask")]
    assert len(finding) == 2 and assisting and max(finding) < min(assisting)


# Lists - see lists.py

@pytest.mark.parametrize("text, item, said, command", [
    ("Put milk on my shopping list.", "milk", "my shopping list", "put milk on my shopping list"),
    # Assist matches this with no list, so it isn't sent, and Nova adds it - see add_to_list.
    ("Add milk to notes.", "milk", "notes", "add milk to notes"),
    ("Add milk to the todo list, please.", "milk", "the todo list", "add milk to the todo list"),
])
def test_one_thing_is_added_without_claude(text, item, said, command):
    assert _actions(text) == [Action("add_to_list", {"item": item, "list": said, "command": command})]


@pytest.mark.parametrize("text", [
    "Add eggs, milk and bread to my shopping list.",
    # The comma said is lost with the punctuation, but still counts.
    "Add eggs, milk, bread to my list.",
    # One thing or two? Claude keeps it whole, or splits it.
    "Add salt and vinegar crisps to my list.",
    "Put eggs,milk on my list.",
])
def test_several_things_go_to_claude(text):
    assert _actions(text) is None


@pytest.mark.parametrize("text, tool", [
    ("Add ten minutes to the timer.", None), ("Add the dentist to my calendar.", None),
    ("Put the kettle on.", None), ("Put it on my phone.", "send_to_phone"), ("What's on tomorrow?", "get_calendar"),
    # Nova's own things and the devices, by any of their words.
    ("Add an hour to my alarm.", None), ("Add this to my reminders.", None), ("Put lunch in my diary.", None),
    ("Put the meeting on my schedule.", None), ("Put Drake on my iPad.", None), ("Put a film on the tablet.", None),
    ("Add a note to my agenda.", None), ("What's on my phone?", None), ("What's on the laptop?", None),
    ("What's on my Mac?", None),
    # Only pointing back, to something the conversation so far says.
    ("Add it to my shopping list.", None), ("Put that on my list.", None), ("Tick that off.", None),
    ("Take it off my list.", None), ("I've got it.", None), ("I have got this.", None), ("Cross this off my list.", None),
    # Not a list's name, nor a thing to add.
    ("Read me a story.", None), ("Read me the news.", None), ("Take the bins out.", None),
    # Two requests at once.
    ("Add milk to my list and play Drake.", None), ("Tick off milk on my list then the eggs.", None),
])
def test_what_the_list_rules_mustnt_catch_is_left_alone(text, tool):
    # 0003: local rules stay strict.
    found = LocalIntents(home=HOME, ntfy=SimpleNamespace(just_said=lambda: False)).match(text)
    assert (found and [action.tool for action in found["actions"]]) == ([tool] if tool else None)


@pytest.mark.parametrize("text, action", [
    ("What's on my shopping list?", Action("read_list", {"list": "my shopping list"})),
    ("Read me my todo list.", Action("read_list", {"list": "my todo list"})),
    ("Tick off milk.", Action("tick_off_list", {"item": "milk", "list": None, "command": "tick off milk"})),
    ("Tick milk off.", Action("tick_off_list", {"item": "milk", "list": None, "command": "tick milk off"})),
    ("Tick off milk on my shopping list.", Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "tick off milk on my shopping list"})),
    ("Take milk off my shopping list.", Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "take milk off my shopping list", "exact": False})),
    ("Cross the eggs off the shopping list.", Action("tick_off_list", {
        "item": "the eggs", "list": "the shopping list", "command": "cross the eggs off the shopping list",
        "exact": False})),
    # Without the word "list", only the thing itself - "take the day off work" is about the world.
    ("Take the day off work.", Action("tick_off_list", {
        "item": "the day", "list": "work", "command": "take the day off work", "exact": True})),
    ("I've got the milk.", Action("tick_off_list", {"item": "milk", "list": None, "exact": True})),
])
def test_a_list_is_read_and_things_ticked_off_without_claude(text, action):
    assert _actions(text) == [action]


@pytest.mark.parametrize("text", [
    "I've got a headache.", "Add milk to my shopping list.", "What's on my shopping list?", "Tick off milk.",
    "Take milk off my shopping list.",
])
def test_without_home_assistant_the_list_rules_leave_it_to_claude(text):
    # "I've got a headache" is a sentence about the world, which a brain without Home
    # Assistant would otherwise answer with "Lists need Home Assistant."
    assert _actions(text, home=None) is None


@pytest.mark.parametrize("text, item, said", [
    # The last " to " or " on " is the list's, so a thing can have one too.
    ("Add a trip to Ikea to my list.", "a trip to ikea", "my list"),
    ("Put a letter to the council on my list.", "a letter to the council", "my list"),
    # "To do" is a list's name, never where the thing goes.
    ("Add milk to my to do list.", "milk", "my to do list"),
    ("Add milk to my to-do list.", "milk", "my to do list"),
    ("Put the radio on my shopping list.", "the radio", "my shopping list"),
    # Without commas, three words can't be told from one thing of three words.
    ("Add eggs milk bread to my list.", "eggs milk bread", "my list"),
])
def test_a_thing_is_taken_up_to_the_last_to_or_on(text, item, said):
    command = re.sub(r"[^a-z0-9' ]", " ", text.lower()).strip()
    assert _actions(text) == [Action("add_to_list", {"item": item, "list": said, "command": " ".join(command.split())})]


@pytest.mark.parametrize("text", [
    # Only pointing back, by any of the words for it.
    "Add them to my shopping list.", "Put all of those on my list.", "Add these to my list.", "Tick them off.",
    "Tick both off my list.", "I've got them.", "Take those off my shopping list.", "Add the other one to my list.",
    # Several things, joined other ways.
    "Add eggs plus milk to my list.", "Add eggs as well as milk to my list.",
    # Possessives of Nova's own things and the devices.
    "Add a note to my phone's notes.", "Put lunch on my calendar's day.",
    # Ticking off from what isn't a list.
    "Tick milk off my calendar.", "Tick it off my list.", "Tick milk off my list and the eggs.",
])
def test_what_else_the_list_rules_mustnt_catch_is_left_alone(text):
    assert _actions(text) is None


@pytest.mark.parametrize("text", [
    # When, not a list - Claude adds it to the calendar.
    "Put the dentist on Thursday at 3.", "Add the dentist to my calendar on Thursday at 3.",
    "Put lunch with Sam on tomorrow.", "Add a haircut to Friday at 10am.", "Put the MOT on the 3rd of March.",
    "Add dinner with Sam to next week.", "Put football on today at 6 pm.", "Take the day off work tomorrow.",
    "Put the plumber on at nine.", "Put tea on at 5pm.", "Add yoga to Mondays.",
])
def test_a_day_or_time_in_a_lists_words_is_left_to_claude(text):
    assert _actions(text) is None


def test_a_list_named_after_a_day_is_still_one_said_with_list():
    assert _actions("Add the bins to my Monday jobs list.") == [Action("add_to_list", {
        "item": "the bins", "list": "my monday jobs list", "command": "add the bins to my monday jobs list"})]


def test_a_thing_is_ticked_off_a_list_said_after_it():
    assert _actions("Tick milk off my shopping list.") == [Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "tick milk off my shopping list"})]


def test_ive_got_only_ticks_off_an_exact_fit_and_never_asks_assist():
    # "I've got the flu" is a sentence about the world - only "flu" itself on a list is
    # ticked off, and Assist, which has no such sentence, is never sent it.
    assert _actions("I've got the flu.") == [Action("tick_off_list", {"item": "flu", "list": None, "exact": True})]


@pytest.mark.parametrize("text, item", [
    ("Add one egg to my list.", "one egg"), ("Add the last of the bread to my list.", "the last of the bread"),
    ("Add that cake to my list.", None), ("Add the other one to my list.", None),
])
def test_a_thing_is_only_left_to_claude_when_it_points_back(text, item):
    # "That" always points back; "one" or "last" only when they're all the thing is.
    expected = None if item is None else [Action("add_to_list", {
        "item": item, "list": "my list", "command": text.lower().rstrip(".")})]
    assert _actions(text) == expected


@pytest.mark.parametrize("text", [
    "Add the last song to Gym.", "Take the song before off car.", "Add this song to gym.",
    "Add the previous track to my gym list.", "Put the last song on my car playlist.",
    "Add the song before that to chill.", "Take the last track off my gym list.", "Add the last song to my playlist.",
    # Said by what it is doing or where it is, not as a song.
    "Add what's playing to Gym.", "Add what is playing to Gym.", "Add the one playing to Gym.",
    "Add the one before to Gym.", "Take the one before off Gym.",
])
def test_a_song_pointed_at_is_never_put_on_a_to_do_list(text):
    # A song isn't a thing for a list: it's Claude's, which has the playlists.
    assert _actions(text) is None


def test_a_thing_is_still_a_lists_if_its_only_like_a_song():
    assert _actions("Add milk to my shopping list.") == [Action("add_to_list", {
        "item": "milk", "list": "my shopping list", "command": "add milk to my shopping list"})]
    # A song's name isn't a pointer at the song, and nor is a song which isn't the one playing.
    for item in ("a new song", "the song of the summer", "track pants"):
        assert _actions(f"Add {item} to my wish list.") == [Action("add_to_list", {
            "item": item, "list": "my wish list", "command": f"add {item} to my wish list"})]
    assert _actions("Take the milk off my shopping list.") is not None


# The PC - see pc.py

@pytest.mark.parametrize("text, action", [
    ("Turn on my PC.", "on"), ("Switch on the computer.", "on"), ("Turn the PC on.", "on"),
    ("Switch my computer on.", "on"), ("Wake my PC.", "on"), ("Wake up the computer.", "on"),
    ("Turn off the PC.", "off"), ("Switch off my computer.", "off"), ("Turn my PC off.", "off"),
    ("Switch the computer off.", "off"), ("Shut down my PC.", "off"), ("Shut the computer down.", "off"),
    ("Restart my PC.", "restart"), ("Reboot the computer.", "restart"),
    ("Put my PC to sleep.", "sleep"), ("Put the computer to sleep.", "sleep"),
    ("Turn off my PC's screen.", "screen_off"), ("Turn off the computer monitor.", "screen_off"),
    ("Lock my PC.", "lock"), ("Lock the computer.", "lock"),
    ("Is my PC on?", "status"), ("Is the computer on?", "status"),
])
def test_the_pc_is_worked_without_claude(text, action):
    # "Lock my PC" is the PC's own, not Assist's; "is my PC on?" comes before Assist's
    # "is X on", and "put my PC to sleep" before the lists' "put X to Y".
    assert _actions(text) == [Action("pc_power", {"action": action})]
    # Without Home Assistant too, which says the PC needs it.
    assert _actions(text, home=None) == [Action("pc_power", {"action": action})]


@pytest.mark.parametrize("text, tools", [
    # Still the lights, and Assist's phone.
    ("Turn on the PC lights.", ["control_home"]), ("Turn off my phone.", ["control_home"]),
    # Claude's - the music, two requests at once, a delay, an alarm, a battery or unlocking.
    ("Restart the music.", None), ("Shut down the PC and play Drake.", None),
    ("Shut down my PC in ten minutes.", None), ("Wake me up at 7.", None), ("Is my PC's battery on?", None),
    ("Is my PC on charge?", None), ("Unlock my PC.", None),
])
def test_what_the_pc_rules_mustnt_catch_is_left_alone(text, tools):
    actions = _actions(text)
    assert (None if actions is None else [action.tool for action in actions]) == tools


# Moving the music to another satellite

# The words the brain says name a satellite, as Brain.names_satellite says them of the Pi, the PC and the Mac.
SATELLITE_WORDS = {"pc", "the pc", "my pi", "mac"}


def _moves(text, names=SATELLITE_WORDS.__contains__, **others):
    """The actions a request is handled with when the brain knows which words name a satellite, or None."""
    result = LocalIntents(home=HOME, names_satellite=names, **others).match(text)
    return None if result is None else result["actions"]


@pytest.mark.parametrize("text, where", [
    ("Play it on my Pi.", "my pi"), ("Move this to the PC.", "the pc"), ("Switch to the PC.", "the pc"),
    ("Change it to the PC.", "the pc"), ("Move the music over to the PC.", "the pc"),
    ("Transfer the song to the PC.", "the pc"), ("Take it to my Pi.", "my pi"),
    # The rest of the verbs, objects and joiners.
    ("Swap the music to Mac.", "mac"), ("Play the music onto the PC.", "the pc"),
    ("Play this on the PC.", "the pc"), ("Switch this song over to my Pi.", "my pi"),
    ("Could you move that to the PC please?", "the pc"),
])
def test_move_phrases(text, where):
    assert _moves(text) == [Action("move_music", {"to": where})]


@pytest.mark.parametrize("text", ["Switch to the next song.", "Change it to 50.", "Play it on repeat.",
                                  "Move this to the kitchen."])
def test_move_words_that_name_no_satellite_fall_through(text):
    # Left to the rules after, or to Claude, as before the move rule.
    actions = _moves(text)
    assert actions is None or all(action.tool != "move_music" for action in actions)


@pytest.mark.parametrize("text, action", [
    ("Send it to my Mac.", Action("send_to_phone", {"device": "mac"})),
    ("Put that on my Mac.", Action("send_to_phone", {"device": "mac"})),
    ("Put it on my phone.", Action("send_to_phone")),
])
def test_send_and_put_phrases_still_send_the_reply(text, action):
    # Even though "mac" names a satellite: only "play it on", and the move verbs, are a move.
    assert _moves(text, ntfy=FakeNtfy()) == [action]


def test_bare_play_still_resumes():
    assert _moves("Play.") == [Action("play_music")]
    assert _moves("Play the music.") == [Action("play_music")]


@pytest.mark.parametrize("text, action", [("Switch on the PC.", "on"), ("Switch on my PC.", "on"),
                                          ("Turn on the PC.", "on")])
def test_switch_on_the_pc_is_still_power(text, action):
    # "On" only means somewhere to play after "play" - and "pc" names a satellite here.
    assert _moves(text) == [Action("pc_power", {"action": action})]


# Volume and mute on the device named, else the one spoken to (0034)

@pytest.mark.parametrize("text, change", [
    ("Volume up on the PC.", {"direction": "up", "where": "the pc"}),
    ("Turn it down on my Pi.", {"direction": "down", "where": "my pi"}),
    ("Turn the music up on the PC.", {"direction": "up", "where": "the pc"}),
    ("Turn the PC down.", {"direction": "down", "where": "the pc"}),
    ("PC volume up.", {"direction": "up", "where": "pc"}),
    ("Louder on the PC.", {"direction": "up", "where": "the pc"}),
    ("Quieter on my Pi.", {"direction": "down", "where": "my pi"}),
    ("Set the volume to 30 on the PC.", {"level": 30, "where": "the pc"}),
    ("Set the PC volume to 30.", {"level": 30, "where": "pc"}),
    ("PC volume 40.", {"level": 40, "where": "pc"}),
    ("Set my Pi to 30.", {"level": 30, "where": "my pi"}),
    ("Set the PC to 30 percent.", {"level": 30, "where": "the pc"}),
])
def test_volume_on_a_device_named(text, change):
    assert _moves(text) == [Action("set_volume", change)]


@pytest.mark.parametrize("text, change", [
    ("Mute the sound.", {"mute": True}), ("Unmute the sound.", {"mute": False}), ("Mute the audio.", {"mute": True}),
    ("Unmute the music.", {"mute": False}), ("Mute the volume.", {"mute": True}),
    ("Turn the sound off.", {"mute": True}), ("Turn the audio on.", {"mute": False}),
    ("Turn off the sound.", {"mute": True}),
    ("Mute the sound on the PC.", {"mute": True, "where": "the pc"}),
    ("Unmute the audio on my Pi.", {"mute": False, "where": "my pi"}),
    ("Turn the sound off on the PC.", {"mute": True, "where": "the pc"}),
])
def test_sound_mute_needs_the_word(text, change):
    # Sound is muted only when he says sound or audio (or the music, or the volume): 0034's mute.
    assert _moves(text) == [Action("set_volume", change)]


@pytest.mark.parametrize("text, change", [
    ("Mute.", {"muted": True}), ("Unmute.", {"muted": False}), ("Stop listening.", {"muted": True}),
    ("Start listening.", {"muted": False}), ("Mute the mic.", {"muted": True}),
    ("Mute the microphone.", {"muted": True}), ("Unmute the microphone.", {"muted": False}),
    ("Turn off the mic.", {"muted": True}), ("Turn the microphone on.", {"muted": False}),
    # Never Assist's, which once had "turn off the microphone" left to Claude.
    ("Turn off the microphone.", {"muted": True}),
    ("Mute the PC.", {"muted": True, "where": "the pc"}), ("Unmute my Pi.", {"muted": False, "where": "my pi"}),
    ("Stop listening on my Pi.", {"muted": True, "where": "my pi"}),
    ("Mute the mic on my Pi.", {"muted": True, "where": "my pi"}),
])
def test_mute_means_the_microphone(text, change):
    # Plain "mute", the mic and "stop listening" turn Nova's listening off, here or on the device named.
    assert _moves(text) == [Action("mute_microphone", change)]


def test_mute_without_names_satellite_is_still_the_microphone():
    assert _actions("Mute.") == [Action("mute_microphone", {"muted": True})]
    assert _actions("Mute the sound.") == [Action("set_volume", {"mute": True})]
    # A device can't be told without the brain.
    assert _actions("Stop listening on my Pi.") is None


def test_tv_volume_still_assist():
    # A satellite's name is checked first, then Home Assistant's thing.
    assert _moves("Set the TV volume to 20.") == [Action("control_home", {"command": "set the tv volume to 20",
                                                                          "wider": True})]
    assert _moves("Set the PC volume to 20.") == [Action("set_volume", {"level": 20, "where": "pc"})]
    # Words naming no satellite are never a device's volume: left to the rules after, or Claude.
    for text in ("Turn the heating down.", "Mute the TV.", "Volume up on the TV.", "Set the lamp to 30.",
                 "Mute the sound on the TV.", "Stop listening on the TV."):
        actions = _moves(text)
        assert actions is None or all(action.tool not in ("set_volume", "mute_microphone")
                                      for action in actions), text
    # Nor without a brain to name satellites.
    assert _actions("Volume up on the PC.") is None and _actions("Mute the PC.") is None


def test_without_names_satellite_nothing_moves():
    # A brain without satellites to name, as the tests of every other rule have, leaves these as they were.
    for text in ("Play it on my Pi.", "Move this to the PC.", "Switch to the PC."):
        actions = _actions(text)
        assert actions is None or all(action.tool != "move_music" for action in actions)


def _journey(place, mode, leave):
    return [Action("journey", {"place": place, "mode": mode, "leave": leave})]


@pytest.mark.parametrize("text, place", [
    ("What time do I leave for work?", "work"),
    ("What time should I leave to get to work?", "work"),
    ("When do I need to leave for uni?", "uni"),
    ("When should I set off for work?", "work"),
    ("What time do I have to leave to get to my gran's?", "my gran's"),
    ("Please, what time do I leave for work", "work"),
])
def test_when_to_leave_is_heard_without_claude(text, place):
    # The place is as said, "my" and "the" kept: the journeys module finds the zone itself.
    assert _actions(text) == _journey(place, "driving", True)
    # Without the calendar or Home Assistant too: Claude can't be asked for these.
    assert _actions(text, home=None, activities=None) == _journey(place, "driving", True)


@pytest.mark.parametrize("text, place, mode", [
    ("How long is the drive to work?", "work", "driving"),
    ("How long to drive to the Metrocentre?", "the metrocentre", "driving"),
    ("How long is the walk to uni?", "uni", "walking"),
    ("How long to walk to uni?", "uni", "walking"),
    ("How long does it take to walk to uni?", "uni", "walking"),
    ("How long to cycle to work?", "work", "cycling"),
    ("How long is the cycle to work?", "work", "cycling"),
    ("How long to get to work by bus?", "work", "transit"),
    ("How long does it take to get to work on public transport?", "work", "transit"),
    ("Please, how long does it take to drive to work", "work", "driving"),
])
def test_how_long_a_journey_takes_is_heard_without_claude(text, place, mode):
    assert _actions(text) == _journey(place, mode, False)
    assert _actions(text, home=None, activities=None) == _journey(place, mode, False)


@pytest.mark.parametrize("text", [
    # The calendar's, for an activity.
    "What time do I have work tomorrow?", "When do I finish uni today?",
    # The timers', the alarm's and the clock's - the existing answer wins.
    "How long is left on my timer?", "How long until my alarm?", "What time is it?",
    # No place, or no journey: Claude's.
    "When should I leave?", "How long to boil an egg?",
    # No way of travelling named: Claude's, who can tell work from sleep.
    "How long to get to work?", "How long does it take to get to my gran's?", "How long to get to sleep?",
    "How long to work on public transport?",
    # A day, or a "from" or "with", after the place: Claude's, who can take the day as the arrival.
    "What time do I leave for work tomorrow?", "What time do I leave for work today?",
    "When should I leave for work on Monday?", "When should I set off for my gran's this afternoon?",
    "How long is the drive to work right now?", "How long to drive to the Metrocentre from work?",
    "How long to walk to uni with the dog?",
    # Not a place at all - a thing said instead of one.
    "When should I leave for it?", "How long is the drive to there?",
    "What time should I leave to go to work?", "When do I need to leave to catch my train?",
    "When do I leave to get there?", "When should I head off to bed?",
    # A time said is left to Claude.
    "What time do I leave to be at work for 9?", "When do I need to leave to get to work for 9?",
])
def test_what_the_journey_rules_mustnt_catch_is_left_alone(text):
    # Whatever these gave before the journey rules - the calendar's answer or Claude - they
    # still give, and none is a journey.
    actions = _actions(text)
    assert actions is None or all(action.tool != "journey" for action in actions)


def test_what_a_local_rule_asks_for_is_logged_without_a_notes_text(caplog):
    # No local rule makes a note, as "make a note" is Claude's to word - but the line which says
    # what was handled shouldn't be a way to put a note's text in the log if one ever does.
    intents = LocalIntents()
    intents._rules.insert(0, ("jot", lambda found: LocalIntents._result(
        "Okay.", [Action("save_note", {"title": "Jot", "text": "The secret of the bins."})])))
    with caplog.at_level("INFO"):
        result = intents.match("Jot.")
    assert result["actions"][0].params["text"] == "The secret of the bins."
    assert "secret" not in caplog.text and "save_note" in caplog.text and "'title': 'Jot'" in caplog.text


# Opening, closing and searching on a computer, and the PC's own power words - see computers.py

# The PC's apps and open windows, as the brain last heard them.
PC_APPS = ["Fortnite", "Discord", "Steam", "YouTube", "Rocket League", "Overwatch", "Overwatch 2", "Google Chrome",
           "Google Drive", "Windows Security", "Windows PowerShell", "Music Maker"]
PC_WINDOWS = ["Discord", "Firefox"]


class ComputerBrain:
    """The parts of the brain ComputerHooks asks, with the PC the only computer, connected or not there."""

    def __init__(self, somewhere=True, apps=PC_APPS, aliases=None, system_volume=True):
        self.somewhere = somewhere
        # The PC's connection, which says whether it can reach Windows' sound and microphone.
        self.pc = SimpleNamespace(satellite=SimpleNamespace(system_volume=system_volume))
        self.computers = Computers()
        self.computers.set_apps("pc", [{"id": name, "name": name, "kind": "exe"} for name in apps])
        self.computers.set_windows("pc", PC_WINDOWS)
        self.rooms = SimpleNamespace(app_names=lambda name: dict(aliases or {}),
                                     machine=lambda name: "pc" if name == "pc" else "pi")
        self.names_satellite = SATELLITE_WORDS.__contains__

    def computer_meant(self, spoken_at, where):
        return ("pc", None) if where or self.somewhere else (None, None)

    def session_for(self, name):
        return self.pc if name == "pc" else SimpleNamespace(satellite=SimpleNamespace(system_volume=False))


def _computer(text, at_pc=True, somewhere=True, **brain):
    """The actions a request gives at the PC or the Pi, with the PC to open things on or not, or None."""
    hooks = ComputerHooks.for_satellite(ComputerBrain(somewhere, **brain), "pc" if at_pc else "pi")
    result = LocalIntents(home=HOME, names_satellite=SATELLITE_WORDS.__contains__, computer=hooks).match(text)
    return None if result is None else result["actions"]


def _not_computers(actions):
    """Whether actions, or None, have nothing opened, closed or searched on a computer."""
    return actions is None or all(not action.tool.endswith(("_on_computer", "_web_page")) for action in actions)


@pytest.mark.parametrize("text, name", [
    ("Open Fortnite.", "fortnite"), ("Launch Discord.", "discord"), ("Start Rocket League.", "rocket league"),
    ("Load up Steam.", "steam"), ("Could you open Overwatch please?", "overwatch"),
])
def test_open_app_at_pc(text, name):
    assert _computer(text) == [Action("open_on_computer", {"name": name})]


def test_open_on_pc_from_pi():
    assert _computer("Open Fortnite on the PC.", at_pc=False) == [
        Action("open_on_computer", {"name": "fortnite", "where": "the pc"})]
    # With the PC named, whatever is said is the PC's, for it to say it can't find it.
    assert _computer("Open the garage on the PC.", at_pc=False, somewhere=False) == [
        Action("open_on_computer", {"name": "the garage", "where": "the pc"})]
    # Words after "on" which name no satellite are part of what's opened, so match nothing here.
    assert all(action.tool != "open_on_computer" for action in _computer("Open Fortnite on the left.", at_pc=False))


def test_open_unknown_falls_through():
    # Not one of the PC's apps: on to Assist, as before.
    assert [action.tool for action in _computer("Open the garage.")] == ["control_home"]
    # And with no computer to open things on at all, even a known name falls through.
    assert all(action.tool != "open_on_computer" for action in _computer("Open Fortnite.", at_pc=False,
                                                                           somewhere=False))
    assert _actions("Open Fortnite.") != [Action("open_on_computer", {"name": "fortnite"})]


def test_open_domain():
    assert _computer("Open bbc.co.uk.") == [Action("open_web_page", {"url": "bbc.co.uk"})]
    assert _computer("Go to bbc.co.uk on the PC.", at_pc=False) == [
        Action("open_web_page", {"url": "bbc.co.uk", "where": "the pc"})]
    assert _computer("Go to my-site.com") == [Action("open_web_page", {"url": "my-site.com"})]
    # Nowhere to open it: Claude's.
    assert _computer("Go to bbc.co.uk.", at_pc=False, somewhere=False) is None


def test_open_youtube_app_beats_site():
    assert _computer("Open YouTube.") == [Action("open_on_computer", {"name": "youtube"})]
    assert _computer("Go to youtube.com.") == [Action("open_on_computer", {"name": "youtube"})]
    # Only an app of that whole name, or his own name for one: Google Chrome isn't google.com.
    for text, url in (("Go to google.com.", "google.com"), ("Go to www.google.com.", "www.google.com"),
                      ("Open google.co.uk.", "google.co.uk")):
        assert _computer(text) == [Action("open_web_page", {"url": url})]
    assert _computer("Go to chrome.com.", aliases={"chrome": "Google Chrome"}) == [
        Action("open_on_computer", {"name": "chrome"})]


def test_only_a_whole_name_is_opened_or_closed_away_from_the_computer():
    # At the PC, the one app with every word said; at the Pi, only a whole name or his own.
    assert _computer("Open rocket.") == [Action("open_on_computer", {"name": "rocket"})]
    assert _not_computers(_computer("Open rocket.", at_pc=False))
    assert _computer("Open Rocket League.", at_pc=False) == [Action("open_on_computer", {"name": "rocket league"})]
    assert _computer("Open GTA.", at_pc=False, apps=["Grand Theft Auto V"], aliases={"gta": "Grand Theft Auto V"}) == [
        Action("open_on_computer", {"name": "gta"})]
    # Several it might be is left to Assist's shapes or Claude, whose tool asks which - even at the PC.
    assert _not_computers(_computer("Open Google."))
    assert _not_computers(_computer("Launch Google."))
    # A window's whole name, anywhere; the one window with every word, at the PC only.
    assert _computer("Close Discord.", at_pc=False) == [Action("close_on_computer", {"name": "discord"})]


@pytest.mark.parametrize("text", ["Close this.", "Close that.", "Close this window.", "Close the window.",
                                  "Close it."])
def test_close_this_at_pc_only(text):
    assert _computer(text) == [Action("close_on_computer", {"name": "this"})]
    # At the Pi, with no computer named, it isn't the PC's front window.
    actions = _computer(text, at_pc=False)
    assert actions is None or all(action.tool != "close_on_computer" for action in actions)
    assert _computer("Close this on the PC.", at_pc=False) == [
        Action("close_on_computer", {"name": "this", "where": "the pc"})]


@pytest.mark.parametrize("text", ["Quit it.", "Exit that.", "Quit this window.", "Exit the window.",
                                  "Quit it on the PC."])
def test_quit_or_exit_it_never_closes_the_window_in_front(text):
    # About something said before, as "quit" and "exit" are only for a program named.
    assert _not_computers(_computer(text))
    assert _not_computers(_computer(text, at_pc=False))


def test_close_open_window():
    assert _computer("Close Discord.") == [Action("close_on_computer", {"name": "discord"})]
    assert _computer("Quit Firefox.") == [Action("close_on_computer", {"name": "firefox"})]
    assert _computer("Exit Steam on the PC.", at_pc=False) == [
        Action("close_on_computer", {"name": "steam", "where": "the pc"})]
    # Not open: on to Assist.
    assert [action.tool for action in _computer("Close Steam.")] == ["control_home"]


@pytest.mark.parametrize("text, words, where", [
    ("Search for rocket league tips.", "rocket league tips", None),
    ("Google for cheap flights.", "cheap flights", None),
    ("Search google for the weather in Paris.", "the weather in paris", None),
    ("Google how tall is Everest.", "how tall is everest", None),
    ("Look up train times on Google.", "train times", None),
    ("Search for pizza on the PC.", "pizza", "the pc"),
    # "On" what names no satellite is part of the search.
    ("Search for restaurants on Deliveroo.", "restaurants on deliveroo", None),
])
def test_search_forms(text, words, where):
    params = {"words": words, **({"where": where} if where else {})}
    assert _computer(text, at_pc=where is None) == [Action("search_on_computer", params)]
    # With nowhere to search, and none named, Claude's.
    if where is None:
        assert _computer(text, at_pc=False, somewhere=False) is None


def test_a_plain_search_at_the_pi_is_claudes():
    # Claude's own web search, as before, unless the PC or Google is named.
    assert _not_computers(_computer("Search for rocket league tips.", at_pc=False))
    assert _computer("Search for pizza on the PC.", at_pc=False) == [
        Action("search_on_computer", {"words": "pizza", "where": "the pc"})]
    for text, words in (("Search Google for cheap flights.", "cheap flights"), ("Google for train times.", "train times"),
                        ("Google how tall is Everest.", "how tall is everest"),
                        ("Look up train times on Google.", "train times")):
        assert _computer(text, at_pc=False) == [Action("search_on_computer", {"words": words})]


@pytest.mark.parametrize("text", ["Search for it.", "Google that.", "Search Google for this.", "Look up it on Google.",
                                  "Search for it on the PC."])
def test_a_search_for_a_word_standing_for_something_else_is_claudes(text):
    assert _not_computers(_computer(text))
    assert _not_computers(_computer(text, at_pc=False))


def test_start_needs_a_whole_name_even_at_the_computer():
    # Music Maker is the one app with "music" in its name, but "start the music" is still the music's.
    assert _not_computers(_computer("Start the music."))
    assert _not_computers(_computer("Start rocket."))
    assert _computer("Open rocket.") == [Action("open_on_computer", {"name": "rocket"})]
    assert _computer("Load up rocket.") == [Action("open_on_computer", {"name": "rocket"})]
    assert _computer("Start Music Maker.") == [Action("open_on_computer", {"name": "music maker"})]
    assert _computer("Start GTA.", aliases={"gta": "Fortnite"}) == [Action("open_on_computer", {"name": "gta"})]


def test_fall_through_untouched():
    for at_pc in (True, False):
        assert _computer("Start a timer for 5 minutes.", at_pc) == [Action("set_timer", {"duration_seconds": 300})]
        for text in ("Open the garage.", "Close the garage door.", "Close the blinds."):
            assert [action.tool for action in _computer(text, at_pc)] == ["control_home"]
        assert _computer("Add milk to my shopping list.", at_pc)[0].tool == "add_to_list"
        # Words in many of the PC's names, but no one's whole name.
        assert [action.tool for action in _computer("Open the windows.", at_pc)] == ["control_home"]
        assert _computer("Play.", at_pc) == [Action("play_music")]
    assert _not_computers(_computer("Start the music.", at_pc=False))


@pytest.mark.parametrize("text, action", [
    ("Go to sleep.", "sleep"), ("Sleep.", "sleep"), ("Lock.", "lock"), ("Lock it.", "lock"),
    ("Lock the PC.", "lock"), ("Turn the screen off.", "screen_off"), ("Screen off.", "screen_off"),
    ("Turn off the screen.", "screen_off"), ("Shut down.", "off"), ("Shut it down.", "off"),
    ("Power off.", "off"), ("Restart.", "restart"), ("Reboot.", "restart"),
])
def test_power_words_only_whole(text, action):
    assert _computer(text) == [Action("pc_power", {"action": action})]
    # At the Pi, without the PC named, these are as they were.
    actions = _computer(text, at_pc=False)
    if text != "Lock the PC.":
        assert actions is None or all(action.tool != "pc_power" for action in actions)
    for longer in ("I'm going to sleep after this.", "Restart the song.", "Lock the front door.",
                   "Go to sleep in ten minutes.", "Restart it."):
        for at_pc in (True, False):
            actions = _computer(longer, at_pc)
            assert actions is None or all(action.tool != "pc_power" for action in actions)


def test_shut_down_bare_asks():
    # Through pc_power, whose question before shutting down is tested in test_pc - never press_pc_button.
    assert _computer("Shut down.") == [Action("pc_power", {"action": "off"})]
    assert _computer("Restart.") == [Action("pc_power", {"action": "restart"})]


# At the PC, "mute the mic" is Windows' own microphone, and a plain "mute" asks which (his rulings, 20 Nov).

@pytest.mark.parametrize("text, muted", [
    ("Mute mic.", True), ("Mute the mic.", True), ("Mute the microphone.", True), ("Mute my mic.", True),
    ("Unmute mic.", False), ("Unmute the microphone.", False), ("Turn the mic off.", True),
    ("Turn off the microphone.", True), ("Turn the mic on.", False), ("Turn on the mic.", False),
])
def test_the_mic_at_the_pc_is_its_own(text, muted):
    assert _computer(text) == [Action("mute_computer_mic", {"muted": muted})]


@pytest.mark.parametrize("text, muted", [("Mute.", True), ("Unmute.", False), ("Mute please.", True)])
def test_plain_mute_at_the_pc_asks_which(text, muted):
    assert _computer(text) == [Action("mute_which", {"muted": muted})]


@pytest.mark.parametrize("text, action", [
    # Windows' sound needs the word, as before.
    ("Mute the audio.", Action("set_volume", {"mute": True})),
    ("Mute the sound.", Action("set_volume", {"mute": True})),
    ("Unmute the sound.", Action("set_volume", {"mute": False})),
    # "Stop listening" is still the room's always-on Pi.
    ("Stop listening.", Action("mute_microphone", {"muted": True})),
    ("Start listening.", Action("mute_microphone", {"muted": False})),
    # A device named goes to that device.
    ("Mute the PC.", Action("mute_microphone", {"muted": True, "where": "the pc"})),
    ("Unmute my Pi.", Action("mute_microphone", {"muted": False, "where": "my pi"})),
    ("Mute the mic on my Pi.", Action("mute_microphone", {"muted": True, "where": "my pi"})),
])
def test_the_rest_of_mute_at_the_pc_is_unchanged(text, action):
    assert _computer(text) == [action]


@pytest.mark.parametrize("text, action", [
    ("Mute.", Action("mute_microphone", {"muted": True})),
    ("Mute the mic.", Action("mute_microphone", {"muted": True})),
    ("Unmute.", Action("mute_microphone", {"muted": False})),
])
def test_mute_at_the_pi_is_still_the_pis(text, action):
    assert _computer(text, at_pc=False) == [action]


def test_a_pc_which_cant_reach_windows_sound_mutes_as_before():
    # An older PC satellite, which never said it has system_volume, isn't asked of its microphone.
    assert _computer("Mute.", system_volume=False) == [Action("mute_microphone", {"muted": True})]
    assert _computer("Mute the mic.", system_volume=False) == [Action("mute_microphone", {"muted": True})]


# Starting and ending an open conversation - see open_conversation.py

START_PHRASES = ["let's talk", "let's have a conversation", "conversation mode", "start conversation mode",
                 "start a conversation"]
END_PHRASES = ["end conversation", "end the conversation", "end conversation mode", "stop the conversation",
               "that's all", "that's everything", "goodbye"]


def _talk(text, on_at=None, enabled=True, here="pc", **intents):
    """
    The actions a request gives at a satellite, with a conversation on at on_at or none, or None if it's left to
    Claude.
    """
    conversation = OpenConversation(lambda: {"enabled": enabled})
    if on_at:
        conversation.start(on_at)
    result = LocalIntents(conversation=conversation, satellite=here, **intents).match(text)
    return None if result is None else result["actions"]


@pytest.mark.parametrize("phrase", START_PHRASES)
def test_each_start_phrase_starts_one(phrase):
    assert _talk(phrase) == [Action("start_conversation")]
    # Any capitals, and the punctuation a transcript puts on it.
    assert _talk(phrase.capitalize() + ".") == [Action("start_conversation")]
    assert _talk(phrase.upper() + "!") == [Action("start_conversation")]
    # Also when one is on already, for the tool to say so.
    assert _talk(phrase, on_at="pc") == [Action("start_conversation")]


@pytest.mark.parametrize("phrase", END_PHRASES)
def test_each_end_phrase_ends_it(phrase):
    assert _talk(phrase, on_at="pc") == [Action("end_conversation")]
    assert _talk(phrase.capitalize() + ".", on_at="pc") == [Action("end_conversation")]
    assert _talk(phrase.upper() + "!", on_at="pc") == [Action("end_conversation")]


@pytest.mark.parametrize("text", [
    "That's all I can think of for the kitchen.", "Goodbye for now.", "End the conversation about the bins.",
    "Let's talk about the weather.", "Start a conversation with the neighbours.", "Let's talk later.",
])
def test_a_phrase_inside_a_sentence_doesnt_start_or_end_it(text):
    # Only the whole of what's said: the same words open longer requests, which are Claude's.
    assert _talk(text, on_at="pc") is None
    assert _talk(text) is None


def test_bye_alone_isnt_an_end_phrase():
    # The speech-to-text blanks a lone "bye" as a word imagined in noise, so it can't be told from one.
    assert _talk("Bye.", on_at="pc") is None
    assert _talk("Bye bye.", on_at="pc") is None


@pytest.mark.parametrize("phrase", END_PHRASES)
def test_end_phrases_mean_nothing_outside_a_conversation(phrase):
    # They go on to Claude as any other words would.
    assert _talk(phrase) is None
    # Nor when it's on at another satellite, which isn't the one he's talking to.
    assert _talk(phrase, on_at="bedroom") is None


@pytest.mark.parametrize("phrase", START_PHRASES)
def test_without_a_conversation_there_are_no_phrases(phrase):
    # As for every session which isn't given one.
    assert LocalIntents().match(phrase) is None


def test_switched_off_there_are_no_phrases():
    for phrase in START_PHRASES + END_PHRASES:
        assert _talk(phrase, enabled=False) is None
    # Switched off while one is on, it can still be ended by words, so the microphone is never left open.
    settings = {"enabled": True}
    conversation = OpenConversation(lambda: settings)
    conversation.start("pc")
    intents = LocalIntents(conversation=conversation, satellite="pc")
    assert intents.match("That's all.")["actions"] == [Action("end_conversation")]
    settings["enabled"] = False
    assert intents.match("That's all.")["actions"] == [Action("end_conversation")]
    assert intents.match("Let's talk.") is None


def test_the_phrases_are_the_conversations_before_any_rule_which_might_take_them():
    # An app called by the same words, which "start" would open.
    apps = PC_APPS + ["A Conversation", "Conversation Mode"]
    hooks = ComputerHooks.for_satellite(ComputerBrain(apps=apps), "pc")
    # Which it would, with no conversation to ask.
    assert _computer("Start a conversation.", apps=apps) == [Action("open_on_computer", {"name": "a conversation"})]
    conversation = OpenConversation(lambda: {"enabled": True})
    intents = LocalIntents(home=HOME, names_satellite=SATELLITE_WORDS.__contains__, computer=hooks,
                           conversation=conversation, satellite="pc")
    assert intents.match("Start a conversation.")["actions"] == [Action("start_conversation")]
    assert intents.match("Start conversation mode.")["actions"] == [Action("start_conversation")]
    assert intents.match("Conversation mode.")["actions"] == [Action("start_conversation")]


def test_the_end_phrases_arent_dismissals():
    # A timer going off while he ends a conversation: the dismissal shortcut, which Conversation.handle
    # tries before the rules, takes only words which are all dismissals or filler, and none of these are
    # - so the end phrase is the request, and saying anything silences what rang in the same turn.
    assert not any(ClockWords.looks_like_dismissal(phrase) for phrase in END_PHRASES + START_PHRASES)


# Moving an open conversation up to the bigger model, and back

BIGGER_PHRASES = ["think harder", "use the bigger model", "use the big model"]
QUICK_PHRASES = ["quick answers", "back to normal", "use the quick model"]


@pytest.mark.parametrize("phrase", BIGGER_PHRASES)
def test_each_phrase_for_the_bigger_model_moves_up(phrase):
    assert _talk(phrase, on_at="pc") == [Action("use_bigger_model")]
    assert _talk(phrase.capitalize() + ".", on_at="pc") == [Action("use_bigger_model")]
    assert _talk(phrase.upper() + "!", on_at="pc") == [Action("use_bigger_model")]
    assert _talk(phrase + " please", on_at="pc") == [Action("use_bigger_model")]


@pytest.mark.parametrize("phrase", BIGGER_PHRASES)
def test_the_bigger_model_may_be_asked_for_with_more_said(phrase):
    # What comes after is what's asked of it, by Conversation, which has the whole of what was said.
    assert _talk(f"{phrase.capitalize()} about the route to Leeds.", on_at="pc") == [
        Action("use_bigger_model", {"rest": "about the route to leeds"})]


@pytest.mark.parametrize("phrase", QUICK_PHRASES)
def test_each_phrase_for_quick_answers_goes_back(phrase):
    assert _talk(phrase, on_at="pc") == [Action("use_quick_model")]
    assert _talk(phrase.capitalize() + ".", on_at="pc") == [Action("use_quick_model")]
    assert _talk(phrase.upper() + "!", on_at="pc") == [Action("use_quick_model")]


@pytest.mark.parametrize("text", ["Quick answers about the weather.", "Back to normal programming.",
                                  "Use the quick model for that.", "I think harder than you.", "Think harderr."])
def test_going_back_is_the_whole_of_what_is_said(text):
    assert _talk(text, on_at="pc") is None


@pytest.mark.parametrize("phrase", BIGGER_PHRASES + QUICK_PHRASES + ["Think harder about the route to Leeds."])
def test_the_model_phrases_mean_nothing_outside_a_conversation(phrase):
    # They go on to Claude as any other words would.
    assert _talk(phrase) is None
    # Nor when it's on at another satellite, which isn't the one he's talking to.
    assert _talk(phrase, on_at="bedroom") is None
    assert LocalIntents().match(phrase) is None
