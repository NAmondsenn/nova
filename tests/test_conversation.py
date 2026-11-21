"""
Conversation Test
Drives the rules for answering the user with text alone - no microphone, speaker,
Whisper or Claude - using fakes for the clock, local commands, Claude and the
actions, so each rule can be checked on its own.
"""

import logging
from types import SimpleNamespace

import pytest

from voice_assistant import network
from voice_assistant.actions import Action, ActionExecutor, ActionResult
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import Clock
from voice_assistant.conversation import LIGHTS_TIMEOUT, Conversation, Reply
from voice_assistant.downloads import NOT_SET_UP, ON_ITS_WAY, Downloads
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.intents import LocalIntents
from voice_assistant.llm import LLMHandler
from voice_assistant.open_conversation import OpenConversation
from voice_assistant.pc import PCPower
from voice_assistant.playlist_changes import PlaylistChanges
from voice_assistant.timeline import Timeline


class FakeClock:
    """Stands in for the Clock: what's ringing, and the word rules it really uses."""

    looks_like_dismissal = staticmethod(Clock.looks_like_dismissal)
    answer_to_offer = staticmethod(Clock.answer_to_offer)

    def __init__(self, ringing=False, timers=(), on=None):
        self.ringing = ringing
        self.timers = list(timers)
        self.dismissed = 0
        self.silenced = []
        # The satellite it rings on - or None, for wherever it's asked about.
        self.on = on

    def _heard(self, satellite):
        """Whether what rings is heard on satellite - a name, a room of them, or None for anywhere."""
        if self.on is None or satellite is None:
            return True
        return self.on in satellite if isinstance(satellite, list) else self.on == satellite

    def is_ringing(self, satellite=None):
        self.asked_about = satellite
        return self.ringing and self._heard(satellite)

    def dismiss(self, satellite=None):
        self.dismissed += 1
        self.dismissed_on = satellite
        if self._heard(satellite):
            self.ringing = False
        return {"success": True, "message": "Okay, stopped."}

    def silence_insistent(self, entries):
        self.silenced.append(entries)

    def describe_all(self):
        return list(self.timers)


class FakeIntents:
    def __init__(self, handles=None):
        self.handles = handles or {}

    def match(self, text, only=None):
        handled = self.handles.get(text)
        if handled and only is not None and not all(action.tool in only for action in handled["actions"]):
            return None
        return handled


class FakeLLM:
    """Stands in for Claude, answering each request with the next reply given."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked, self.remembered, self.recorded, self.tool_answers = [], [], [], []
        self.contexts, self.lights, self.playing = [], [], []
        # What Claude reads before answering, and what it's told - see LLMHandler.process_query.
        self.to_read, self.read = [], []
        # The tools it's offered, and those of them it only reads from - as each request found them.
        self.tools, self.read_tools, self.offered = [], set(), []
        # What Claude was told of the device spoken through, for each request.
        self.devices = []
        # The exchanges it keeps, which each answer it gives adds to, as the real one's do.
        self.history = []

    def forget_stale_history(self):
        pass

    def process_query(self, text, context=None, lights=None, read=None, playing=(), device=None):
        self.devices.append(device)
        self.asked.append(text)
        self.contexts.append(context)
        self.lights.append(lights)
        self.playing.append(list(playing))
        self.offered.append((list(self.tools), set(self.read_tools)))
        self.read += [read(action) for action in self.to_read]
        reply = self.replies.pop(0)
        if reply.get("success") and not reply.get("not_for_me"):
            self.history.append({"user": text, "assistant": reply["response"], "plain": not reply["actions"]})
        return reply

    def remember(self, text, reply):
        self.remembered.append((text, reply))
        # Not Claude's answer, so not "plain" - see LLMHandler.process_query.
        self.history.append({"user": text, "assistant": reply})

    def record_reply(self, text, spoken, tool_answers=()):
        self.recorded.append((text, spoken))
        self.tool_answers.append(list(tool_answers))


class FakeActions:
    def __init__(self, results=None, home=None):
        self.results = results or {}
        self.home = home
        self.run = []
        self.params = []

    def execute(self, action):
        self.run.append(action.tool)
        self.params.append(action.params)
        return self.results.get(action.tool, ActionResult(True, "Done."))

    def tool_schemas(self):
        return []

    def read_tools(self):
        return ["home_status"]


class FakeHome:
    """Stands in for Home Assistant: the lights it describes, and how long it was given to read them."""

    def __init__(self, lights):
        self.lights = lights
        self.timeouts = []

    def describe_lights(self, timeout=None):
        self.timeouts.append(timeout)
        return self.lights


class FakeMusic:
    def __init__(self):
        self.waited = 0

    def wait(self):
        self.waited += 1


def _answer(text, *actions):
    return {"response": text, "actions": list(actions), "success": True}


# Claude's answer when what it heard was speech from what's playing - see LLMHandler.process_query.
NOT_FOR_ME = {"response": "", "actions": [], "success": True, "not_for_me": True}


def _conversation(clock=None, intents=None, llm=None, actions=None, ringing=None, **settings):
    made = Conversation(clock or FakeClock(), intents or FakeIntents(), llm or FakeLLM(), actions or FakeActions(),
                        FakeMusic(), **settings)
    made.start(ringing)
    return made


# Answering a question

def test_a_plain_answer_is_said_and_remembered():
    llm = FakeLLM(_answer("Paris."))
    reply = _conversation(llm=llm).handle("what's the capital of France")
    assert (reply.say, reply.sounds, reply.listen_again) == ("Paris.", [], False)
    assert llm.recorded == [("what's the capital of France", "Paris.")]


def test_claude_reads_home_status_rather_than_it_being_said():
    # What home_status reads is for Claude to answer from, not to be said as it is.
    llm = FakeLLM(_answer("Sunset's at 5:52 PM."))
    llm.to_read = [Action("home_status", {"about": "sunset"}, from_model=True)]
    actions = FakeActions({"home_status": ActionResult(
        True, "Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52")})
    reply = _conversation(llm=llm, actions=actions).handle("when's sunset?")
    assert llm.read == ["Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52"]
    assert (actions.run, actions.params) == (["home_status"], [{"about": "sunset"}])
    assert reply.say == "Sunset's at 5:52 PM."
    assert llm.recorded == [("when's sunset?", "Sunset's at 5:52 PM.")]


class FakeCalendar:
    """Stands in for GoogleCalendar: the activity calendars Google listed last."""

    def __init__(self, *names):
        self.names = list(names)

    def activity_calendars(self):
        return {name.lower(): {"id": f"calendar-{name.lower()}", "name": name, "primary": False, "writable": True}
                for name in self.names}


def test_claude_is_told_the_activities():
    calendar = FakeCalendar("Uni", "Work")
    actions = ActionExecutor(calendar=calendar)
    llm = FakeLLM(_answer("Okay."), _answer("Okay."))
    conversation = _conversation(llm=llm, actions=actions)

    def told():
        tools, read_tools = llm.offered[-1]
        [activity] = [tool for tool in tools if tool["name"] == "get_activity"]
        # The tools only read from are the same executor's, at the same time - so none is run as an action.
        assert read_tools == set(actions.read_tools()) and "home_status" in read_tools
        return activity

    conversation.handle("when have I got gym")
    activity = told()
    assert activity["description"].endswith("The user's activities: Uni and Work.")
    said = activity["input_schema"]["properties"]["activity"]["description"].lower()
    assert "work" not in said and "uni" not in said

    # A calendar added in Google is named from the next request - in order, so a list
    # Google gives in another order doesn't change the tools, and the API's cache.
    calendar.names.append("Gym")
    conversation.handle("when have I got gym")
    assert told()["description"].endswith("The user's activities: Gym, Uni and Work.")


def test_a_local_command_never_reaches_claude():
    llm = FakeLLM()
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("pause")
    assert llm.asked == [] and llm.remembered == [("pause", "Okay.")]
    assert reply.sounds == [("success_chime.wav", "general")] and reply.say == ""


def test_where_my_phone_is_never_reaches_claude_when_asked_locally():
    # Claude's history goes to Anthropic with every later request, so a place said for a
    # local "where's my phone?" is kept from it - it only hears that it was said.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    intents = FakeIntents({"where's my phone": _answer("Okay.", Action("find_device", {"device": "phone",
                                                                                       "ring": False}))})
    actions = FakeActions({"find_device": ActionResult(True, where, private=True)})
    llm = FakeLLM()
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("where's my phone")
    assert reply.say == where and llm.asked == []
    assert llm.remembered == [("where's my phone", "I said where your phone is.")]
    assert llm.recorded == [("where's my phone", "I said where your phone is.")]
    assert llm.tool_answers == [["I said where your phone is."]]



def test_where_a_device_is_never_reaches_claude_even_when_claude_asked():
    # Claude needs no place to answer anything it's asked, so its own call is kept from it too -
    # beside anything else the turn said, which it keeps.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    llm = FakeLLM(_answer("Okay.", Action("find_device", {"device": "phone"}, from_model=True),
                          Action("set_timer", {"duration_seconds": 600}, from_model=True)))
    actions = FakeActions({"find_device": ActionResult(True, where, private=True),
                           "set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    reply = _conversation(llm=llm, actions=actions).handle("where did I leave my phone, and set a ten minute timer")
    assert reply.say == f"{where} Timer set for 10 minutes."
    assert llm.recorded == [("where did I leave my phone, and set a ten minute timer",
                             "I said where your phone is. Timer set for 10 minutes.")]
    assert llm.tool_answers == [["I said where your phone is.", "Timer set for 10 minutes."]]
    assert "Example Road" not in str((llm.recorded, llm.tool_answers, llm.remembered))


def test_a_journey_from_where_the_user_is_never_reaches_claude_but_its_question_does():
    # How far the user is from a place says where they are. Claude is told it was said, and the question
    # asked, so it can carry the answer to "When do you need to be there?".
    said = "The drive to Work is 24 minutes, 6.4 miles. When do you need to be there?"
    llm = FakeLLM(_answer("Okay.", Action("journey", {"place": "work"}, from_model=True)))
    actions = FakeActions({"journey": ActionResult(True, said, private=True)})
    reply = _conversation(llm=llm, actions=actions).handle("how long to get to work from here")
    assert reply.say == said
    told = "I said how long the journey takes from where you are. When do you need to be there?"
    assert llm.recorded == [("how long to get to work from here", told)]
    assert llm.tool_answers == [[told]]


def test_where_my_phone_is_still_found_locally_when_nova_was_listening_for_an_answer():
    # After "Anything else?", "where's my phone?" can't be its answer, so it's still Nova's own -
    # and Claude, which isn't asked, never hears where.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    intents = LocalIntents(home=object())
    actions = FakeActions({"find_device": ActionResult(True, where, private=True)})
    llm = FakeLLM(_answer("Okay."))
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("Where's my phone?", follow_up=True)
    assert reply.say == where and llm.asked == [] and actions.run == ["find_device"]
    assert llm.remembered == [("Where's my phone?", "I said where your phone is.")]
    assert "Example Road" not in str((llm.recorded, llm.tool_answers))
    # Anything else in the window is still the conversation's, so "stop" isn't the music's.
    _conversation(intents=intents, llm=llm, actions=actions).handle("pause", follow_up=True)
    assert llm.asked == ["pause"]


def test_the_answer_to_a_question_isnt_taken_for_a_local_command():
    # "Stop" answering "which timer?" needs the conversation so far to make sense of.
    llm = FakeLLM(_answer("Okay."))
    intents = FakeIntents({"stop": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, llm=llm).handle("stop", follow_up=True)
    assert llm.asked == ["stop"]


def test_actions_wait_for_the_music_to_have_paused_and_say_their_own_words():
    llm = FakeLLM(_answer("Okay.", Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    conversation = _conversation(llm=llm, actions=actions)
    reply = conversation.handle("set a ten minute timer")
    assert conversation.music.waited == 1 and reply.say == "Timer set for 10 minutes."


def test_what_each_action_said_is_kept_for_the_model():
    llm = FakeLLM(_answer("Okay.", Action("get_weather", {"day": "today"}), Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"get_weather": ActionResult(True, "It's 10 degrees."),
                           "set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    _conversation(llm=llm, actions=actions).handle("what's the weather, and set a ten minute timer")
    assert llm.tool_answers == [["It's 10 degrees.", "Timer set for 10 minutes."]]


def test_a_question_is_listened_for_but_only_so_many_times_in_a_row():
    llm = FakeLLM(*[_answer("Which one?")] * 3)
    conversation = _conversation(llm=llm, max_follow_ups=2)
    assert [conversation.handle("cancel it").listen_again for _ in range(3)] == [True, True, False]


def test_in_an_open_conversation_every_question_is_listened_for():
    # Every reply is listened after in one, so each question's answer is its answer, however many in a row.
    llm = FakeLLM(*[_answer("Which one?")] * 3)
    conversation = _conversation(llm=llm, max_follow_ups=2, conversing=lambda: True)
    assert [conversation.handle("cancel it").listen_again for _ in range(3)] == [True, True, True]


def test_in_an_open_conversation_its_end_is_heard_in_the_answer_to_a_question():
    # "That's all" said back to "Anything else?" is Nova's own to hear, as it is at any other turn.
    talk = OpenConversation(lambda: {"enabled": True})
    talk.start("pc")
    intents = LocalIntents(conversation=talk, satellite="pc")
    actions = ActionExecutor(conversation=talk, satellite="pc")
    llm = FakeLLM(_answer("Sorry?"))
    # Told it isn't in one, the answer to a question is Claude's, as it always was.
    _conversation(intents=intents, llm=llm, actions=actions, conversing=lambda: False).handle(
        "That's all.", follow_up=True)
    assert llm.asked == ["That's all."] and talk.on_at("pc")

    reply = _conversation(intents=intents, llm=llm, actions=actions, conversing=lambda: talk.on_at("pc")).handle(
        "That's all.", follow_up=True)
    assert reply.say == "Okay." and llm.asked == ["That's all."]
    assert not talk.on_at("pc")


def test_a_reply_says_whether_claude_answered_it():
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    conversation = _conversation(intents=intents, llm=FakeLLM(_answer("Paris.")))
    assert not conversation.handle("pause").claude_answered
    assert conversation.handle("what's the capital of France").claude_answered
    # As is one Home Assistant didn't understand, which Claude then answered.
    conversation, _, _ = _lights_assist_didnt_understand()
    assert conversation.handle("dim the lights").claude_answered


@pytest.mark.parametrize("playing", [["Bedroom TV"], []])
def test_a_reply_says_claude_took_it_for_speech_from_whats_playing(playing):
    reply = _conversation(llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying(playing)).handle("turn the lights off")
    assert reply.not_for_me and not reply.claude_answered
    assert not _conversation(llm=FakeLLM(_answer("Paris."))).handle("what's the capital of France").not_for_me


# What Home Assistant doesn't understand

def _lights_assist_didnt_understand(llm=None, home=None, **settings):
    """A conversation in which "dim the lights" goes to Assist, which matches nothing, and so to Claude."""
    intents = FakeIntents({"dim the lights": _answer("Okay.", Action("control_home", {"command": "dim the lights"}))})
    actions = FakeActions({"control_home": ActionResult(False, "Sorry, I couldn't understand that", understood=False),
                           "set_lights": ActionResult(True, "Done.", confirm="chime")}, home=home)
    llm = llm or FakeLLM(_answer("Okay.", Action("set_lights", {"on": True, "brightness": 30})))
    return _conversation(intents=intents, llm=llm, actions=actions, **settings), llm, actions


def test_a_local_command_assist_didnt_understand_goes_to_claude():
    conversation, llm, actions = _lights_assist_didnt_understand()
    reply = conversation.handle("dim the lights")
    assert llm.asked == ["dim the lights"] and actions.run == ["control_home", "set_lights"]
    assert "couldn't understand" not in reply.say and reply.sounds == [("success_chime.wav", "general")]


def test_a_request_which_falls_through_to_claude_isnt_remembered_twice():
    # Claude keeps the exchanges it answers itself, so remembering the local command's
    # "Okay." as well would give it the same request twice.
    conversation, llm, _ = _lights_assist_didnt_understand()
    conversation.handle("dim the lights")
    assert llm.remembered == []


def test_what_claude_says_after_a_fall_through_is_kept_and_listened_for():
    llm = FakeLLM(_answer("Which room?"))
    conversation, llm, actions = _lights_assist_didnt_understand(llm=llm)
    reply = conversation.handle("dim the lights")
    assert (reply.say, reply.listen_again) == ("Which room?", True)
    assert llm.recorded == [("dim the lights", "Which room?")] and actions.last_answer == ("dim the lights", "Which room?")


def test_a_fall_through_inside_a_routine_is_answered_like_any_other_step():
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["dim the lights"]})
    conversation, llm, _ = _lights_assist_didnt_understand(routines=evening)
    reply = conversation.handle("evening")
    assert llm.asked == ["dim the lights"] and reply.sounds == [("success_chime.wav", "general")]


def test_what_an_action_leaves_until_after_its_said_goes_with_the_reply():
    def press():
        return {"success": True, "message": "Putting your PC to sleep."}

    assert ActionResult.from_reply({"success": True, "message": "x", "after_said": press}).after_said is press
    intents = FakeIntents({"go to sleep": _answer("Okay.", Action("pc_power", {"action": "sleep"}))})
    actions = FakeActions({"pc_power": ActionResult(True, "Putting your PC to sleep.", after_said=press)})
    conversation = _conversation(intents=intents, llm=FakeLLM(), actions=actions,
                                 routines=FakeRoutines({"name": "Night", "phrases": ["night"],
                                                        "steps": ["go to sleep"]}))
    reply = conversation.handle("go to sleep")
    assert (reply.say, reply.after) == ("Putting your PC to sleep.", [press])
    # A failure leaves nothing for after, and a routine carries its steps' along.
    failed = FakeActions({"pc_power": ActionResult(False, "Your PC's off.", after_said=press)})
    assert _conversation(intents=intents, llm=FakeLLM(), actions=failed).handle("go to sleep").after == []
    assert conversation.handle("night").after == [press]


def test_a_local_command_assist_understood_but_couldnt_do_stays_with_assist():
    # "Understood" is about Assist having matched a sentence, not about it having worked:
    # the lamp being unreachable isn't something Claude can do anything about.
    intents = FakeIntents({"lamp off": _answer("Okay.", Action("control_home", {"command": "lamp off"}))})
    actions = FakeActions({"control_home": ActionResult(False, "The lamp is unavailable")})
    llm = FakeLLM()
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("lamp off")
    assert llm.asked == [] and reply.say == "The lamp is unavailable"
    assert llm.remembered == [("lamp off", "Okay.")]


# What Claude is told about the lights

LAMP_OFF = "Nova is in the Bedroom. Lights: Bedroom Light (light.bedroom_light, Bedroom) off; Lamp (light.lamp, Bedroom) off"


def test_claude_is_told_about_the_lights_beside_the_timers():
    # Separately: the lights aren't a timer, so they aren't put in the list of timers.
    home = FakeHome(LAMP_OFF)
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes"] and llm.lights == [LAMP_OFF]
    assert "Nova is in the Bedroom" in llm.lights[0]


def test_claude_is_told_about_the_lights_with_no_timers_running():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(llm=llm, actions=FakeActions(home=FakeHome(LAMP_OFF))).handle("is it sunny")
    assert llm.contexts == [None] and llm.lights == [LAMP_OFF]


def test_claude_is_told_when_novas_room_isnt_known():
    told = "Nova's room isn't known. Lights: Lamp (light.lamp, Bedroom) off"
    llm = FakeLLM(_answer("Which room?"))
    _conversation(llm=llm, actions=FakeActions(home=FakeHome(told))).handle("dim the lights")
    assert llm.lights == [told]


def test_claude_goes_without_the_lights_when_they_cant_be_read():
    # describe_lights is "" when Home Assistant is down, or too slow to wait for.
    llm = FakeLLM(_answer("It's sunny."), _answer("It's sunny."))
    home = FakeHome("")
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    _conversation(llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes", None] and llm.lights == [None, None]


def test_without_home_assistant_claude_is_only_told_about_the_timers():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes"] and llm.lights == [None]


def test_a_slow_home_assistant_is_only_waited_for_a_moment_to_read_the_lights():
    # Every request to Claude asks for the lights, so waiting out Home Assistant's own
    # five seconds each time would slow every reply when it's down.
    home = FakeHome(LAMP_OFF)
    _conversation(llm=FakeLLM(_answer("It's sunny.")), actions=FakeActions(home=home)).handle("is it sunny")
    assert home.timeouts == [LIGHTS_TIMEOUT] and 0 < LIGHTS_TIMEOUT <= 2


def test_the_lights_are_only_read_when_claude_is_asked():
    home = FakeHome(LAMP_OFF)
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, actions=FakeActions(home=home)).handle("pause")
    assert home.timeouts == []


def test_a_fall_through_tells_claude_about_the_lights_too():
    home = FakeHome(LAMP_OFF)
    conversation, llm, _ = _lights_assist_didnt_understand(home=home)
    conversation.handle("dim the lights")
    assert llm.lights == [LAMP_OFF]


# Something ringing

def test_stop_while_ringing_is_handled_without_claude():
    clock, llm = FakeClock(ringing=True), FakeLLM()
    reply = _conversation(clock=clock, llm=llm).handle("Stop.")
    assert clock.dismissed == 1 and llm.asked == []
    assert reply.sounds == [("success_chime.wav", "general")]


def test_stop_heard_as_so_while_ringing_is_still_a_stop():
    # Heard over a ringing timer on the Pi, "So" went to Claude, which replied "Your
    # message just says 'So'" and asked what was wanted.
    clock, llm = FakeClock(ringing=True), FakeLLM()
    _conversation(clock=clock, llm=llm).handle("So")
    assert clock.dismissed == 1 and llm.asked == []


def test_mute_while_ringing_stops():
    # "Mute" is the microphone now (20 Nov), but said over something ringing it still stops it, as
    # "stop" does, before the local rules are asked - and over Nova talking, it only stops her.
    clock, llm, actions = FakeClock(ringing=True), FakeLLM(), FakeActions()
    intents = LocalIntents(names_satellite=lambda said: False)
    assert intents.match("Mute.")["actions"] == [Action("mute_microphone", {"muted": True})]
    reply = _conversation(clock=clock, intents=intents, llm=llm, actions=actions).handle("Mute.")
    assert clock.dismissed == 1 and actions.run == [] and llm.asked == []
    assert reply.sounds == [("success_chime.wav", "general")]

    conversation = _conversation(intents=intents, llm=llm, actions=actions)
    conversation.start(talked_over=True)
    assert conversation.handle("Mute.").say == "" and actions.run == []
    # With nothing ringing or being said, it's the mute.
    _conversation(intents=intents, llm=llm, actions=actions).handle("Mute.")
    assert actions.run == ["mute_microphone"] and actions.params == [{"muted": True}]


def test_only_whats_ringing_where_the_user_is_talking_is_stopped():
    # The kitchen's "stop" dismissed the bedroom's alarm, which couldn't be heard there.
    clock = FakeClock(ringing=True)
    _conversation(clock=clock, satellite="kitchen").handle("Stop.")
    assert clock.asked_about == "kitchen" and clock.dismissed_on == "kitchen"


def test_stop_at_the_pc_stops_the_pi_ringing():
    # A timer set at the PC rings on the Pi in the same room, and "stop" said at the
    # PC has to reach it.
    clock = FakeClock(ringing=True, on="bedroom")
    _conversation(clock=clock, satellite="pc", room=lambda: ["pc", "bedroom"]).handle("Stop.")
    assert clock.dismissed == 1 and clock.ringing is False
    assert clock.dismissed_on == ["pc", "bedroom"]


def test_without_the_chime_a_dismissal_is_shown_not_said():
    reply = _conversation(clock=FakeClock(ringing=True), chime_enabled=False).handle("Stop.")
    assert (reply.sounds, reply.shown, reply.say) == ([], "Okay, stopped.", "")


def test_talking_over_a_timer_silences_it_after_the_request_is_dealt_with():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's sunny."))
    ringing = {"insistent": True, "insistent_entries": ["timer 1"], "reminder": None, "reminder_sounding": False}
    _conversation(clock=clock, llm=llm, ringing=ringing).handle("what's the weather like")
    assert clock.silenced == [["timer 1"]] and clock.dismissed == 0


def test_a_quiet_reminder_is_offered_once_and_the_answer_heard():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Paris."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)

    reply = conversation.handle("what time is it")
    assert reply.say == "It's 7:36 PM. Also, your walk the dogs reminder is still going. Shall I turn it off?"
    assert reply.listen_again

    # "No" here means leave it, where said to something ringing it means stop.
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    assert clock.dismissed == 0
    # And it isn't offered again in the same conversation.
    assert "reminder" not in conversation.handle("what's the capital of France").say


def test_yes_to_the_reminder_offer_turns_it_off():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    assert conversation.handle("yes please", follow_up=True).sounds == [("success_chime.wav", "general")]
    assert clock.dismissed == 1


def test_a_new_conversation_forgets_an_unanswered_offer():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Okay, leaving it."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    clock.ringing = False
    conversation.start()
    conversation.handle("no")
    assert llm.asked[-1] == "no"


# Talked over

def test_stop_said_over_a_reply_only_stops_the_talking():
    # Taken as a command, "stop" paused the music too - which was only quiet while
    # the assistant was talking.
    llm = FakeLLM()
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    conversation = _conversation(intents=intents, llm=llm, actions=actions)
    conversation.start(talked_over=True)
    for said in ("Stop.", "Okay, thanks.", "Shut up.", "Soap.", "So."):
        reply = conversation.handle(said)
        assert (reply.say, reply.sounds, reply.listen_again) == ("", [], False)
    assert actions.run == [] and llm.asked == [] and llm.remembered == []


def test_a_request_said_over_a_reply_is_answered_as_usual():
    llm = FakeLLM(_answer("Paris."))
    conversation = _conversation(llm=llm)
    conversation.start(talked_over=True)
    assert conversation.handle("what's the capital of France").say == "Paris."


def test_stop_said_after_the_reply_has_finished_is_still_a_command():
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    _conversation(intents=intents, actions=actions).handle("Stop.")
    assert actions.run == ["control_playback"]


# Speech from what's playing

class FakePlaying:
    """Stands in for Session.playing_here: what plays in the room each time it's asked, in turn."""

    def __init__(self, *each):
        self.each = [list(names) for names in each]
        self.asked = 0

    def __call__(self):
        self.asked += 1
        return self.each.pop(0) if len(self.each) > 1 else self.each[0]


def test_claude_is_told_whats_playing_in_the_room():
    llm = FakeLLM(_answer("It's sunny."), _answer("It's sunny."))
    conversation = _conversation(llm=llm, playing=FakePlaying(["Firefox", "Windows PC"], []))
    conversation.handle("is it sunny")
    conversation.handle("is it sunny")
    assert llm.playing == [["Firefox", "Windows PC"], []]


def test_without_anything_to_say_whats_playing_claude_is_told_nothing_plays():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(llm=llm).handle("is it sunny")
    assert llm.playing == [[]]


def test_speech_from_whats_playing_is_ignored_without_a_sound():
    llm = FakeLLM(NOT_FOR_ME)
    actions = FakeActions()
    conversation = _conversation(llm=llm, actions=actions, playing=FakePlaying(["Bedroom TV"]))
    timeline = Timeline()
    reply = conversation.handle("I'll never let go, Jack.", timeline=timeline)
    assert (reply.say, reply.sounds, reply.shown, reply.listen_again) == ("", [], "", False)
    assert timeline.notes["outcome"] == "not_for_me"
    # Nothing in the thread, and nothing kept to send to the phone.
    assert llm.remembered == [] and llm.recorded == [] and not hasattr(actions, "last_answer")
    assert actions.run == [] and conversation.music.waited == 0


def test_whats_playing_is_read_once_as_the_request_is_built():
    # Read again afterwards, a film stopped while Claude thought would have its line taken
    # for a request - or one started, a request taken for the film.
    playing = FakePlaying(["Bedroom TV"], [])
    reply = _conversation(llm=FakeLLM(NOT_FOR_ME), playing=playing).handle("I'll never let go, Jack.")
    assert playing.asked == 1 and reply == Reply(not_for_me=True)


def test_ignoring_speech_is_logged_without_the_words(caplog):
    caplog.set_level(logging.INFO)
    _conversation(llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying(["Bedroom TV"])).handle("I'll never let go, Jack.")
    assert "Taken as speech from what's playing, so ignored" in caplog.messages
    assert not any("let go" in message for message in caplog.messages)


def test_something_ringing_carries_on_when_speech_from_whats_playing_is_ignored():
    # The user didn't speak, so hasn't heard it.
    clock = FakeClock(ringing=True)
    ringing = {"insistent": True, "insistent_entries": ["timer 1"], "reminder": None, "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying(["Bedroom TV"]),
                                 ringing=ringing)
    assert conversation.handle("Fire!") == Reply(not_for_me=True)
    assert clock.silenced == [] and clock.dismissed == 0


def test_a_pressed_conversation_is_never_taken_for_speech_from_whats_playing():
    # A press is deliberate, so Claude is told nothing plays, for the whole conversation -
    # follow-ups too - and a not_for_me all the same is not caught, never ignored.
    llm = FakeLLM(_answer("Which room?"), NOT_FOR_ME, NOT_FOR_ME)
    playing = FakePlaying(["Bedroom TV"])
    conversation = _conversation(llm=llm, playing=playing)
    conversation.start(pressed=True)
    assert conversation.handle("turn the lights off").listen_again
    assert conversation.handle("the bedroom", follow_up=True).say == "Sorry, I didn't catch that."
    assert llm.playing == [[], []] and playing.asked == 0

    # The next conversation, begun by the wake word, is as before.
    conversation.start()
    assert conversation.handle("I'll never let go, Jack.") == Reply(not_for_me=True)
    assert llm.playing[-1] == ["Bedroom TV"]


def test_not_for_me_with_nothing_playing_is_taken_as_not_caught():
    llm = FakeLLM(NOT_FOR_ME)
    actions = FakeActions()
    reply = _conversation(llm=llm, actions=actions, playing=FakePlaying([])).handle("turn the lights off")
    assert (reply.say, reply.sounds, reply.listen_again) == ("Sorry, I didn't catch that.", [], False)
    assert actions.run == [] and llm.remembered == [] and llm.recorded == []


def test_not_for_me_without_anything_to_say_whats_playing_is_taken_as_not_caught():
    assert _conversation(llm=FakeLLM(NOT_FOR_ME)).handle("turn the lights off").say == "Sorry, I didn't catch that."


def test_an_unfounded_not_for_me_is_noted_on_the_timeline():
    # What the prompt is tuned by: how often Claude says "the film" with nothing playing.
    timeline = Timeline()
    _conversation(llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying([])).handle("turn the lights off", timeline=timeline)
    assert timeline.notes["outcome"] == "not_for_me_unfounded"


# What the llm would never give - kept here so a slip there still runs nothing.
NOT_FOR_ME_WITH_ACTIONS = {**NOT_FOR_ME, "actions": [Action("set_lights", {"on": False})]}


@pytest.mark.parametrize("playing", [["Bedroom TV"], []])
def test_not_for_me_runs_nothing_even_with_actions_beside_it(playing):
    actions = FakeActions()
    conversation = _conversation(llm=FakeLLM(NOT_FOR_ME_WITH_ACTIONS), actions=actions, playing=FakePlaying(playing))
    conversation.handle("turn the lights off, Jack")
    assert actions.run == [] and conversation.music.waited == 0


def test_not_for_me_after_a_fall_through_runs_nothing_even_with_actions_beside_it():
    conversation, llm, actions = _lights_assist_didnt_understand(llm=FakeLLM(NOT_FOR_ME_WITH_ACTIONS),
                                                                 playing=FakePlaying(["Bedroom TV"]))
    assert conversation.handle("dim the lights") == Reply(not_for_me=True)
    assert actions.run == ["control_home"]


def test_a_follow_up_gets_the_same_check():
    # Heard without the wake word, a follow-up is the likeliest to be the film's.
    llm = FakeLLM(_answer("Which room?"), NOT_FOR_ME)
    conversation = _conversation(llm=llm, playing=FakePlaying(["Bedroom TV"]))
    assert conversation.handle("turn the lights on").listen_again is True
    assert conversation.handle("Get out of the house!", follow_up=True) == Reply(not_for_me=True)
    assert llm.playing == [["Bedroom TV"], ["Bedroom TV"]]
    assert llm.recorded == [("turn the lights on", "Which room?")]


def test_a_fall_through_from_assist_gets_the_same_check():
    playing = FakePlaying(["Bedroom TV"])
    conversation, llm, actions = _lights_assist_didnt_understand(llm=FakeLLM(NOT_FOR_ME), playing=playing)
    assert conversation.handle("dim the lights") == Reply(not_for_me=True)
    assert actions.run == ["control_home"] and llm.remembered == [] and llm.recorded == []


@pytest.mark.parametrize("said", ["pause", "stop"])
def test_local_commands_never_reach_claude_while_something_plays(said):
    # Nova's own rules, so it's their pause and stop that are kept from Claude.
    llm = FakeLLM()
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    playing = FakePlaying(["Bedroom TV"])
    reply = _conversation(intents=LocalIntents(), llm=llm, actions=actions, playing=playing).handle(said)
    assert llm.asked == [] and (actions.run, actions.params) == (["control_playback"], [{"action": "pause"}])
    assert reply.sounds == [("success_chime.wav", "general")] and playing.asked == 0


def test_a_routines_steps_are_never_taken_for_whats_playing():
    # The user's own words for the routine, not something heard - so Claude isn't told
    # what's playing, and a not_for_me is taken as not caught rather than dropping a step.
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["is it sunny"]})
    llm = FakeLLM(NOT_FOR_ME)
    reply = _conversation(llm=llm, routines=evening, playing=FakePlaying(["Bedroom TV"])).handle("evening")
    assert llm.playing == [[]] and reply.say == "Sorry, I didn't catch that."


def test_a_routine_step_taken_for_whats_playing_leaves_the_turn_noted_as_the_routine():
    # not_for_me_unfounded counts what Claude heard, which a routine's step wasn't.
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["is it sunny"]})
    timeline = Timeline()
    _conversation(llm=FakeLLM(NOT_FOR_ME), routines=evening).handle("evening", timeline=timeline)
    assert timeline.notes["outcome"] == "routine"


# Sending to the phone

def test_whats_said_is_kept_for_sending_to_the_phone():
    actions = FakeActions()
    _conversation(llm=FakeLLM(_answer("Paris.")), actions=actions).handle("what's the capital of France")
    assert actions.last_answer == ("what's the capital of France", "Paris.")


def test_sending_it_doesnt_change_what_would_be_sent_next_time():
    actions = FakeActions({"send_to_phone": ActionResult(True, "Sent.")})
    actions.last_answer = ("what's the capital of France", "Paris.")
    intents = FakeIntents({"send that to my phone": _answer("Okay.", Action("send_to_phone"))})
    _conversation(intents=intents, actions=actions).handle("send that to my phone")
    assert actions.last_answer == ("what's the capital of France", "Paris.")


# Routines

class FakeRoutines:
    def __init__(self, *routines):
        self.routines = routines

    def called(self, text):
        return next((routine for routine in self.routines if text in routine["phrases"]), None)


MORNING = {"name": "Good morning", "phrases": ["good morning"],
           "steps": ["what's the weather", "pause", "turn on the lights"]}


def test_a_routines_steps_are_each_done_as_if_said_and_their_answers_said_together():
    llm = FakeLLM(_answer("It's sunny."), _answer("Okay.", Action("home_command", {"request": "turn on the lights"})))
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime"),
                           "home_command": ActionResult(True, "Turned on the lights", confirm="chime")})
    reply = _conversation(intents=intents, llm=llm, actions=actions, routines=FakeRoutines(MORNING)).handle("good morning")

    assert llm.asked == ["what's the weather", "turn on the lights"]
    assert actions.run == ["control_playback", "home_command"]
    # Two chimes would only be one sound twice.
    assert reply.say == "It's sunny." and reply.sounds == [("success_chime.wav", "general")]


def test_a_long_turn_says_its_getting_on_after_each_of_claudes_replies_and_each_step():
    # A routine of slow steps could outlast the watchdog's allowance, though every one
    # of them was getting on - see Session.moved_at. Once for each, and never while waiting.
    llm = FakeLLM(_answer("It's sunny."), _answer("Okay.", Action("home_command", {"request": "turn on the lights"})))
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    moves = []
    conversation = _conversation(intents=intents, llm=llm, routines=FakeRoutines(MORNING),
                                 progress=lambda: moves.append(list(llm.asked)))
    conversation.handle("good morning")

    # Claude's two replies, and the three steps - the second of which went no further than Nova.
    assert moves == [["what's the weather"], ["what's the weather"], ["what's the weather"],
                     ["what's the weather", "turn on the lights"], ["what's the weather", "turn on the lights"]]


def test_a_turn_handled_without_claude_doesnt_say_its_getting_on():
    moves = []
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, progress=lambda: moves.append(1)).handle("pause")
    assert moves == []


def test_a_routine_runs_a_home_assistant_script_through_assist():
    class RecordingActions(FakeActions):
        def execute(self, action):
            self.run.append(action)
            return ActionResult(True, "Done.", confirm="chime")

    actions = RecordingActions()
    goodnight = {"name": "Goodnight", "phrases": ["goodnight"], "steps": ["run goodnight"]}
    _conversation(intents=LocalIntents(home=object()), actions=actions, routines=FakeRoutines(goodnight)).handle("goodnight")
    assert actions.run == [Action("control_home", {"command": "run goodnight", "wider": True})]


def test_a_step_which_is_a_routines_own_words_doesnt_start_it_again():
    llm = FakeLLM(_answer("Going."))
    reply = _conversation(llm=llm, routines=FakeRoutines({"name": "Loop", "phrases": ["go"], "steps": ["go"]})).handle("go")
    assert llm.asked == ["go"] and reply.say == "Going."


def test_the_answer_to_a_question_isnt_taken_for_a_routine():
    llm = FakeLLM(_answer("Okay."))
    _conversation(llm=llm, routines=FakeRoutines(MORNING)).handle("good morning", follow_up=True)
    assert llm.asked == ["good morning"]


def test_a_question_a_step_asks_isnt_listened_for():
    llm = FakeLLM(_answer("Which one?"))
    routine = {"name": "Lights", "phrases": ["lights"], "steps": ["turn on a light"]}
    reply = _conversation(llm=llm, routines=FakeRoutines(routine)).handle("lights")
    assert reply.say == "Which one?" and not reply.listen_again


# Asking before shutting the PC down

class FakePCHome:
    """Stands in for Home Assistant with the desk PC on, noting each button pressed."""

    def __init__(self):
        entries = [Entry("button.desk_off", "mqtt", "desk", None, "desk_off", "Shutdown", (), False),
                   Entry("button.desk_again", "mqtt", "desk", None, "desk_again", "Restart", (), False),
                   Entry("sensor.desk_cpu", "mqtt", "desk", None, "desk_cpu", "CPU Load", (), False)]
        self.registry_read = Snapshot(entries={entry.entity_id: entry for entry in entries},
                                      devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}},
                                      areas={}, read_at=0.0)
        self.pressed = []

    def snapshot(self):
        return self.registry_read

    def states(self):
        return [{"entity_id": entity, "state": "12", "attributes": {}} for entity in self.registry_read.entries]

    def press(self, entity_id):
        self.pressed.append(entity_id)

    def describe_lights(self, timeout=None):
        return ""


class AskingActions(ActionExecutor):
    """The real actions, bar dim_lights, which asks "Which light?"."""

    # The tools are looked up on the class itself.
    tools = ActionExecutor.tools

    def execute(self, action):
        if action.tool == "dim_lights":
            return ActionResult(False, "Which light?")
        return super().execute(action)


def _pc_conversation(tmp_path, *replies):
    """A conversation with the real local rules and actions, and the PC on a fake Home Assistant."""
    home = FakePCHome()
    actions = AskingActions(home=home, pc=PCPower(home, ChoiceStore(str(tmp_path / "pcs.json"), {"wake": {}})))
    llm = FakeLLM(*replies)
    return _conversation(intents=LocalIntents(), llm=llm, actions=actions), home, llm


def test_shutting_down_asks_first_and_only_a_yes_does_it(tmp_path):
    conversation, home, llm = _pc_conversation(tmp_path)
    reply = conversation.handle("Shut down the PC.")
    assert (reply.say, reply.listen_again) == ("Shut down the PC?", True)
    assert home.pressed == []
    # Read before the follow-up window would send it to Claude.
    assert conversation.handle("yes", follow_up=True).say == "Shutting down the PC."
    assert home.pressed == ["button.desk_off"] and llm.asked == []

    conversation, home, llm = _pc_conversation(tmp_path)
    conversation.handle("Shut down the PC.")
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    # And once answered, a later yes is nobody's.
    conversation.handle("Restart the PC.")
    assert conversation.handle("leave it", follow_up=True).say == "Okay, I'll leave it."
    assert home.pressed == []

    # Anything else drops it, and is a fresh request - so a yes after that is Claude's.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("It's sunny. Anything else?"), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    assert conversation.handle("what's the weather", follow_up=True).say == "It's sunny. Anything else?"
    conversation.handle("yes", follow_up=True)
    assert home.pressed == [] and llm.asked == ["what's the weather", "yes"]


def test_claudes_shutdown_asks_first_too(tmp_path):
    conversation, home, llm = _pc_conversation(
        tmp_path, _answer("Okay.", Action("pc_power", {"action": "restart"}, from_model=True)))
    assert conversation.handle("could you reboot the thing on my desk").say == "Restart the PC?"
    assert home.pressed == []
    assert conversation.handle("go on", follow_up=True).say == "Restarting the PC."
    assert home.pressed == ["button.desk_again"]


def test_a_yes_in_a_new_conversation_presses_nothing(tmp_path):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    conversation.start()
    conversation.handle("yes")
    assert home.pressed == [] and llm.asked == ["yes"]

    # Nor does a yes said to any other question.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Shall I play some music?"), _answer("Okay."))
    conversation.handle("I'm bored")
    conversation.handle("yes", follow_up=True)
    assert home.pressed == []

    # Nor one said with no question listened for - the follow-ups had run out.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."))
    conversation.follow_ups = conversation.max_follow_ups
    assert not conversation.handle("Shut down the PC.").listen_again
    conversation.handle("yes")
    assert home.pressed == []


# Asking whether to set the reminder to leave

class FakeJourneys:
    """Journeys which offer a reminder for the drive to Work, noting what the yes sets."""

    PARAMS = {"place": "Work", "destination": "zone.work", "origin": "zone.home", "mode": "driving",
              "arrive": 2000.0, "leave_by": 1000.0, "watch": True}

    def __init__(self):
        self.reminded = []

    def ask(self, place, mode="driving", leave=False, arrive_at=None, satellite=None, now=None, day=None):
        return {"success": True, "message": "The drive to Work is 24 minutes, so leave by 8:15 AM. Want a reminder?",
                "ask_first": Action("set_leave_reminder", dict(self.PARAMS))}

    def remind(self, place, destination, origin, mode, arrive, leave_by, watch, satellite=None, now=None):
        self.reminded.append({"place": place, "destination": destination, "origin": origin, "mode": mode,
                              "arrive": arrive, "leave_by": leave_by, "watch": watch})
        return {"success": True, "message": "I'll remind you at 8:10 AM."}


def _journey_conversation(*replies):
    journeys = FakeJourneys()
    asking = Action("journey", {"place": "work", "leave": True, "arrive_at": "09:00"}, from_model=True)
    llm = FakeLLM(*(replies or (_answer("Okay.", asking),)))
    return _conversation(intents=LocalIntents(), llm=llm, actions=AskingActions(journeys=journeys)), journeys, llm


def test_a_yes_to_want_a_reminder_sets_it():
    conversation, journeys, llm = _journey_conversation()
    reply = conversation.handle("when should I leave for work to be there for nine")
    assert reply.say.endswith("Want a reminder?") and reply.listen_again
    assert journeys.reminded == []
    # Read before the follow-up window would send it to Claude.
    assert conversation.handle("yes please", follow_up=True).say == "I'll remind you at 8:10 AM."
    assert journeys.reminded == [FakeJourneys.PARAMS] and len(llm.asked) == 1


def test_a_no_to_want_a_reminder_leaves_it():
    conversation, journeys, llm = _journey_conversation()
    conversation.handle("when should I leave for work to be there for nine")
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    assert journeys.reminded == [] and len(llm.asked) == 1


@pytest.mark.parametrize("answer", ["yes", "Yes please.", "Nova, yeah.", "yep", "go ahead", "Go on.", "sure",
                                    "Do it.", "Okay, do it.", "ok do it please", "Yes.", "'yes'",
                                    "\u2018yes\u2019"])
def test_only_a_plain_yes_shuts_the_pc_down(tmp_path, answer):
    conversation, home, llm = _pc_conversation(tmp_path)
    conversation.handle("Shut down the PC.")
    assert conversation.handle(answer, follow_up=True).say == "Shutting down the PC."
    assert home.pressed == ["button.desk_off"]


@pytest.mark.parametrize("answer, said", [
    # Backing out - "stop" and "cancel" most of all, which a reminder's offer takes for a yes.
    ("stop", "Okay, I'll leave it."), ("Cancel.", "Okay, I'll leave it."), ("no thanks", "Okay, I'll leave it."),
    ("Never mind.", "Okay, I'll leave it."), ("don't", "Okay, I'll leave it."), ("Nope.", "Okay, I'll leave it."),
    ("leave it", "Okay, I'll leave it."),
    # Neither, so a fresh request, Claude's - nor a yes with more said after it.
    ("shut up", "Okay."), ("thanks", "Okay."), ("fine", "Okay."), ("okay thanks", "Okay."),
    ("yes, play music", "Okay."), ("yes in a minute", "Okay."), ("absolutely", "Okay."),
    # Digits and other scripts are words too - "Yes, 10." most likely means in ten minutes.
    ("Yes, 10.", "Okay."), ("Yes. 10:30", "Okay."), ("yes 10", "Okay."), ("yes \u4e0d\u8981", "Okay."),
])
def test_no_or_anything_else_said_back_presses_nothing(tmp_path, answer, said):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    assert conversation.handle(answer, follow_up=True).say == said
    assert home.pressed == []
    assert llm.asked == ([] if said.startswith("Okay, I'll") else [answer])
    # Either way, it's gone.
    conversation.handle("yes", follow_up=True)
    assert home.pressed == []


@pytest.mark.parametrize("actions", [
    # The PC's question isn't the whole reply...
    [Action("pc_power", {"action": "off"}, from_model=True), Action("dim_lights", {}, from_model=True)],
    [Action("dim_lights", {}, from_model=True), Action("pc_power", {"action": "off"}, from_model=True)],
    # ...or there are two to answer.
    [Action("pc_power", {"action": "off"}, from_model=True),
     Action("pc_power", {"action": "restart"}, from_model=True)],
])
def test_a_yes_to_more_than_the_pcs_question_presses_nothing(tmp_path, actions):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay.", *actions), _answer("Okay."))
    reply = conversation.handle("shut it all down")
    assert reply.listen_again and reply.say.count("?") == 2
    conversation.handle("yes", follow_up=True)
    assert home.pressed == [] and llm.asked == ["shut it all down", "yes"]


# Asking before downloading - see downloads.py

class NotInNavidrome:
    """Stands in for NavidromePlayer, which hasn't got anything it's asked to play."""

    name = "Navidrome"
    needs_internet = False

    def play(self, query=None, search_type=None):
        return {"success": False, "message": f"I couldn't find {query} on Navidrome.", "missing": True}


class NothingFound:
    """Stands in for Navidrome's library, which hasn't got anything either."""

    def find_songs(self, title, artist, also=None):
        return []

    def find_album_tracks(self, title, artist):
        return []


class HasGooba:
    """Stands in for Navidrome's library, which has Gooba by 6ix9ine."""

    def find_songs(self, title, artist, also=None):
        return [{"id": "1", "title": "Gooba", "artist": "6ix9ine", "album": "Gooba", "explicitStatus": "explicit"}]


class AskedDownloads(Downloads):
    """
    The real Downloads, noting each request a yes makes - (query, search_type, satellite) -
    and each download started - (its name, satellite) - and watching none.
    """

    def __init__(self, config, library=NothingFound):
        super().__init__(config, lambda satellite, message: None, soulsync=lambda: None, library=library)
        self.requested, self.started = [], []

    def request(self, query, search_type, satellite):
        self.requested.append((query, search_type, satellite))
        return super().request(query, search_type, satellite)

    def start(self, wanted, satellite):
        self.started.append((wanted.name, satellite))
        return True


@pytest.fixture
def download_settings(make_config, monkeypatch):
    """Downloading set up, with made-up settings."""
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    return make_config(soulsync={"url": "http://soulsync.local:8008"},
                       navidrome={"url": "http://navidrome.local:4533", "username": "nova"})


@pytest.fixture
def downloading(download_settings):
    """
    Makes a conversation on the kitchen satellite, with the real actions, Navidrome
    without anything asked for, and downloading set up - Claude asking to play Gooba,
    then answering each reply given.
    """
    def make(*replies):
        downloads = AskedDownloads(download_settings)
        actions = ActionExecutor(music=NotInNavidrome(), downloads=downloads, satellite="kitchen")
        llm = FakeLLM(_answer("Okay.", Action("play_music", {"query": "Gooba by 6ix9ine", "search_type": "track"},
                                              from_model=True)), *replies)
        return _conversation(llm=llm, actions=actions), downloads, llm

    return make


def test_yes_to_a_download_offer_requests_it_and_says_nothing_yet(downloading):
    conversation, downloads, llm = downloading()
    reply = conversation.handle("play Gooba by 6ix9ine")
    assert (reply.say, reply.listen_again) == ("I haven't got Gooba by 6ix9ine. Do you want me to download it?", True)
    assert downloads.requested == []

    reply = conversation.handle("yes please", follow_up=True)

    # "Gooba by 6ix9ine requested." comes from the watch, once SoulSync has it.
    assert (reply.say, reply.sounds, reply.listen_again) == ("", [], False)
    assert downloads.requested == [("Gooba by 6ix9ine", "track", "kitchen")]
    # Never empty, which the API refuses, and never sent to Claude.
    assert llm.remembered[-1] == ("yes please", f"Asked SoulSync for Gooba by 6ix9ine. {ON_ITS_WAY['track']}")
    assert llm.asked == ["play Gooba by 6ix9ine"]


def test_a_yes_which_cant_request_says_why(downloading, monkeypatch):
    conversation, downloads, llm = downloading()
    conversation.handle("play Gooba by 6ix9ine")
    # The key taken off under Keys before the answer came.
    monkeypatch.delenv("SOULSYNC_API_KEY")

    assert conversation.handle("yes", follow_up=True).say == NOT_SET_UP
    assert llm.remembered[-1] == ("yes", NOT_SET_UP)


@pytest.mark.parametrize("answer", ["thanks", "stop", "no", "okay thanks"])
def test_anything_but_a_plain_yes_to_a_download_offer_requests_nothing(downloading, answer):
    conversation, downloads, llm = downloading(_answer("You're welcome."), _answer("Okay."))
    conversation.handle("play Gooba by 6ix9ine")

    conversation.handle(answer, follow_up=True)

    assert downloads.requested == []
    # Either way, it's gone.
    conversation.handle("yes", follow_up=True)
    assert downloads.requested == []


def test_an_offer_left_unanswered_requests_nothing(downloading):
    conversation, downloads, llm = downloading(_answer("Okay."))
    conversation.handle("play Gooba by 6ix9ine")

    conversation.start()
    conversation.handle("yes", follow_up=True)

    assert downloads.requested == [] and llm.asked == ["play Gooba by 6ix9ine", "yes"]


class FakeClaude:
    """
    Stands in for the Anthropic client, so the real LLMHandler's rules are used: each
    request is answered with the next reply given - its text, then any tool calls, as
    (name, input) pairs.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.messages = self
        # Each request, as it was sent.
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        text, *calls = self.replies.pop(0)
        uses = [SimpleNamespace(type="tool_use", id=f"toolu_{index}", name=name, input=dict(params))
                for index, (name, params) in enumerate(calls)]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)] + uses,
                               stop_reason="tool_use" if calls else "end_turn")


GOOBA_TRACK = {"query": "Gooba by 6ix9ine", "search_type": "track"}
OFFERED_GOOBA = "I haven't got Gooba by 6ix9ine. Do you want me to download it?"


@pytest.fixture
def downloading_with_claude(download_settings, monkeypatch):
    """
    Makes a conversation as downloading does, but with the real LLMHandler, Claude
    answering each request with the next of the FakeClaude replies given.
    """
    monkeypatch.setattr(network, "online", lambda: True)

    def make(*replies, library=NothingFound):
        downloads = AskedDownloads(download_settings, library)
        actions = ActionExecutor(music=NotInNavidrome(), downloads=downloads, satellite="kitchen")
        llm = LLMHandler(api_key=None)
        llm.client = FakeClaude(*replies)
        return _conversation(llm=llm, actions=actions), downloads, llm

    return make


@pytest.mark.parametrize("answer", ["okay", "go on then", "fine"])
def test_anything_but_a_plain_yes_to_the_offer_only_asks_again_whatever_claude_calls(downloading_with_claude,
                                                                                  answer):
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("play_music", GOOBA_TRACK)),
                                                           ("Okay.", ("download_music", GOOBA_TRACK)))
    assert conversation.handle("play Gooba by 6ix9ine").say == OFFERED_GOOBA

    # Not a plain yes, so it's Claude's - which downloads on it.
    reply = conversation.handle(answer, follow_up=True)

    assert (reply.say, reply.listen_again) == (OFFERED_GOOBA, True)
    assert downloads.started == [] and downloads.requested == []
    # A plain yes to the question asked again requests it.
    assert conversation.handle("yes", follow_up=True).say == ""
    assert downloads.requested == [("Gooba by 6ix9ine", "track", "kitchen")]


def test_get_in_the_songs_name_doesnt_make_okay_a_yes(downloading_with_claude):
    get_lucky = {"query": "Get Lucky by Daft Punk", "search_type": "track"}
    offered = "I haven't got Get Lucky by Daft Punk. Do you want me to download it?"
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("play_music", get_lucky)),
                                                           ("Okay.", ("download_music", get_lucky)))
    assert conversation.handle("play Get Lucky by Daft Punk").say == offered

    reply = conversation.handle("okay", follow_up=True)

    assert (reply.say, reply.listen_again) == (offered, True)
    assert downloads.started == [] and downloads.requested == []
    assert conversation.handle("yes", follow_up=True).say == ""
    assert downloads.requested == [("Get Lucky by Daft Punk", "track", "kitchen")]


def test_a_yes_once_the_offer_has_lapsed_only_asks_again(downloading_with_claude):
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("play_music", GOOBA_TRACK)),
                                                           ("Okay.", ("download_music", GOOBA_TRACK)))
    conversation.handle("play Gooba by 6ix9ine")

    # Said with the wake word, after the follow-up window ran out.
    conversation.start()

    assert conversation.handle("yes").say == OFFERED_GOOBA
    assert downloads.started == [] and downloads.requested == []


def test_download_x_still_downloads_straight_away(downloading_with_claude):
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("download_music", GOOBA_TRACK)))

    assert conversation.handle("download Gooba by 6ix9ine").say == ""
    assert downloads.started == [("Gooba by 6ix9ine", "kitchen")]


def _tool_answers(request):
    """What Claude was told each earlier tool call said, in a request sent to it."""
    return [block["content"] for message in request["messages"] if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"]


def test_claude_is_told_a_download_is_only_asked_for(downloading_with_claude):
    # Live on 12 November, Claude was told only "Done.", and a minute later said the
    # download had finished - it landed five minutes after that.
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("download_music", GOOBA_TRACK)),
                                                           ("It's still on its way.",))

    # Nothing said now: "Gooba by 6ix9ine requested." comes once SoulSync has it.
    reply = conversation.handle("download Gooba by 6ix9ine")
    assert (reply.say, reply.sounds) == ("", [])
    assert downloads.started == [("Gooba by 6ix9ine", "kitchen")]

    conversation.handle("are you downloading any music right now?")

    asked_next = llm.client.requests[1]
    assert _tool_answers(asked_next) == [f"Asked SoulSync for Gooba by 6ix9ine. {ON_ITS_WAY['track']}"]
    # Claude's own words stay its reply, as the API refuses an empty one.
    assert asked_next["messages"][3] == {"role": "assistant", "content": "Okay."}


def test_claude_is_told_whats_already_got(downloading_with_claude):
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("download_music", GOOBA_TRACK)),
                                                           ("You're welcome.",), library=HasGooba)

    assert conversation.handle("download Gooba by 6ix9ine").say == "You've already got Gooba by 6ix9ine."
    conversation.handle("thanks")

    assert _tool_answers(llm.client.requests[1]) == ["You've already got Gooba by 6ix9ine."]
    assert downloads.started == []


def test_the_answer_to_what_to_download_still_downloads(downloading_with_claude):
    conversation, downloads, llm = downloading_with_claude(("Okay.", ("download_music", {})),
                                                           ("Okay.", ("download_music", GOOBA_TRACK)))
    reply = conversation.handle("download something")
    assert (reply.say, reply.listen_again) == ("What would you like me to download?", True)

    assert conversation.handle("Gooba by 6ix9ine", follow_up=True).say == ""
    assert downloads.started == [("Gooba by 6ix9ine", "kitchen")]


def test_the_chime_can_be_a_setting_read_at_each_answer():
    # The session passes a function reading the running settings, so a save changes the next answer.
    chime = [True]
    conversation = _conversation(chime_enabled=lambda: chime[0])
    assert conversation.chime_enabled is True
    chime[0] = False
    assert conversation.chime_enabled is False


def test_the_done_chime_is_switched_off_straight_away():
    chime = [True]
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    conversation = _conversation(intents=intents, actions=actions, chime_enabled=lambda: chime[0])
    assert conversation.handle("pause").sounds == [("success_chime.wav", "general")]
    chime[0] = False
    assert conversation.handle("pause").sounds == []


# Making the playlist offered for "add this to Jim"

class PlaylistNavidrome:
    """
    Stands in for NavidromePlayer, which has a song playing, before it another, and no playlists, noting
    what's made, what's put on one and what's deleted.
    """

    name = "Navidrome"
    needs_internet = False

    def __init__(self):
        self.songs = {"this": {"id": "s1", "title": "Endless", "artist": "Made Up Band"}, "last": None}
        self.made, self.added, self.deleted = [], [], []

    def song_at(self, which):
        return self.songs[which]

    def was_playing(self):
        return True

    def all_playlists(self):
        return []

    def make_playlist(self, name):
        self.made.append(name)
        return "p1"

    def add_songs(self, playlist_id, song_ids):
        self.added.append((playlist_id, song_ids))

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)


def test_a_yes_to_making_the_playlist_is_kept_for_undo():
    # Review Focus 4: a change made on a yes is the last thing done, so "undo that" takes it back.
    navidrome = PlaylistNavidrome()
    actions = ActionExecutor(music=navidrome, playlist_changes=PlaylistChanges(lambda: None, lambda: None))
    add = Action("add_to_playlists", {"playlists": ["Jim"]}, from_model=True)
    llm = FakeLLM(_answer("Okay.", add), _answer("Okay.", Action("undo_last_change", from_model=True)))
    conversation = _conversation(llm=llm, actions=actions)

    reply = conversation.handle("add this to Jim")
    assert (reply.say, reply.listen_again) == ("You haven't got a playlist called Jim. Want me to make it?", True)
    assert navidrome.made == []

    # The next song started before the yes: Jim is still made with the one which was asked about.
    navidrome.songs["this"] = {"id": "s2", "title": "Another", "artist": "Made Up Band"}
    reply = conversation.handle("yes", follow_up=True)
    assert reply.say == "Made Jim with Endless on it."
    assert navidrome.made == ["Jim"] and navidrome.added == [("p1", ["s1"])]
    assert llm.asked == ["add this to Jim"]

    assert conversation.handle("undo that").say == "Deleted Jim."
    assert navidrome.deleted == ["p1"]


# The device spoken through

def test_claude_is_told_which_device_it_is_spoken_through():
    llm = FakeLLM(_answer("Your PC."))
    _conversation(llm=llm, here=lambda: "You're speaking through Nathaniel's PC (push-to-talk).").handle(
        "what device are you")
    assert llm.devices == ["You're speaking through Nathaniel's PC (push-to-talk)."]


def test_a_routines_steps_are_never_said_to_come_through_a_device():
    # Nobody spoke them, so "you're speaking through" would be false.
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["is it sunny"]})
    llm = FakeLLM(_answer("Yes."))
    _conversation(llm=llm, routines=evening, here=lambda: "You're speaking through the bedroom Pi (wake word).").handle(
        "evening")
    assert llm.devices == [None]


# Moving up to the bigger model, and back, by his words

EARLIER = {"user": "help me plan my week", "assistant": "Sure, which days?", "plain": True}


def _bigger(rest=None):
    """The local rule's answer to "think harder", with what was said after it."""
    return _answer("Okay.", Action("use_bigger_model", {"rest": rest} if rest else {}))


def test_think_harder_alone_answers_the_last_turn_again():
    llm = FakeLLM(_answer("Monday, the gym; Tuesday, the library."))
    llm.history = [{"user": "hello", "assistant": "Hi.", "plain": True}, dict(EARLIER)]
    actions = FakeActions()
    said = []
    # What had been asked by the time it was said: the line comes before the slow request, not after.
    conversation = _conversation(intents=FakeIntents({"think harder": _bigger()}), llm=llm, actions=actions,
                                 say_first=lambda message: said.append((message, len(llm.asked))))
    reply = conversation.handle("think harder")
    assert said == [("Let me think about that.", 0)]
    # Moved up, then the last request asked again - not what he said - and answered as Claude's answers are.
    assert actions.run == ["use_bigger_model"] and llm.asked == ["help me plan my week"]
    assert (reply.say, reply.claude_answered) == ("Monday, the gym; Tuesday, the library.", True)
    assert llm.recorded == [("help me plan my week", "Monday, the gym; Tuesday, the library.")]
    # The new answer replaces that exchange, in the same place, rather than being kept beside it.
    assert llm.history == [{"user": "hello", "assistant": "Hi.", "plain": True},
                           {"user": "help me plan my week", "assistant": "Monday, the gym; Tuesday, the library.",
                            "plain": True}]


def test_think_harder_alone_with_nothing_said_before_only_moves_up():
    llm = FakeLLM()
    actions = FakeActions()
    said = []
    reply = _conversation(intents=FakeIntents({"think harder": _bigger()}), llm=llm, actions=actions,
                          say_first=said.append).handle("think harder")
    assert (reply.say, reply.claude_answered) == ("Okay.", False)
    # Nothing is waited for, so nothing is said first.
    assert actions.run == ["use_bigger_model"] and llm.asked == [] and said == []


@pytest.mark.parametrize("failed", [
    {"response": "Sorry, that didn't work.", "actions": [], "success": False}, NOT_FOR_ME])
def test_an_exchange_isnt_lost_when_asking_it_again_gave_no_answer(failed):
    llm = FakeLLM(failed)
    llm.history = [dict(EARLIER)]
    _conversation(intents=FakeIntents({"think harder": _bigger()}), llm=llm).handle("think harder")
    assert llm.history == [EARLIER]


def test_think_harder_with_more_asks_the_rest():
    llm = FakeLLM(_answer("Take the A1."))
    llm.history = [dict(EARLIER)]
    said = "Think harder about the route to Leeds."
    first = []
    conversation = _conversation(intents=FakeIntents({said: _bigger("about the route to leeds")}), llm=llm,
                                 say_first=lambda message: first.append((message, len(llm.asked))))
    reply = conversation.handle(said)
    assert first == [("Let me think about that.", 0)]
    # The whole of what he said, which Claude copes with, and what went before is left where it was.
    assert llm.asked == [said] and reply.say == "Take the A1."
    assert llm.recorded == [(said, "Take the A1.")]
    assert llm.history == [EARLIER, {"user": said, "assistant": "Take the A1.", "plain": True}]


def test_quick_answers_goes_back():
    talk = OpenConversation(lambda: {"enabled": True})
    talk.start("pc")
    talk.move_up("pc")
    llm = FakeLLM()
    conversation = _conversation(intents=LocalIntents(conversation=talk, satellite="pc"), llm=llm,
                                 actions=ActionExecutor(conversation=talk, satellite="pc"))
    reply = conversation.handle("Quick answers.")
    assert (reply.say, reply.claude_answered) == ("Okay, quick answers.", False)
    assert not talk.bigger and llm.asked == []


def test_thinking_harder_by_his_words_moves_the_conversation_up_even_when_it_may_not_by_itself():
    talk = OpenConversation(lambda: {"enabled": True, "switch_itself": False})
    talk.start("pc")
    llm = FakeLLM(_answer("Monday, the gym."))
    llm.history = [dict(EARLIER)]
    conversation = _conversation(intents=LocalIntents(conversation=talk, satellite="pc"), llm=llm,
                                 actions=ActionExecutor(conversation=talk, satellite="pc"))
    assert conversation.handle("Think harder.").say == "Monday, the gym."
    assert talk.bigger and llm.asked == ["help me plan my week"]


def test_the_model_phrases_are_heard_in_the_answer_to_a_question_while_one_is_on():
    talk = OpenConversation(lambda: {"enabled": True})
    talk.start("pc")
    intents = LocalIntents(conversation=talk, satellite="pc")
    actions = ActionExecutor(conversation=talk, satellite="pc")
    llm = FakeLLM(_answer("Sorry?"), _answer("Monday."))
    # Told it isn't in one, they are Claude's, as any answer to a question is.
    _conversation(intents=intents, llm=llm, actions=actions, conversing=lambda: False).handle(
        "Think harder.", follow_up=True)
    assert llm.asked == ["Think harder."] and not talk.bigger
    llm.history = [dict(EARLIER)]
    conversation = _conversation(intents=intents, llm=llm, actions=actions, conversing=lambda: talk.on_at("pc"))
    assert conversation.handle("Think harder.", follow_up=True).say == "Monday."
    assert talk.bigger and llm.asked == ["Think harder.", "help me plan my week"]
    reply = conversation.handle("Quick answers.", follow_up=True)
    assert (reply.say, talk.bigger) == ("Okay, quick answers.", False)


def _talking_at_the_pc(llm, **settings):
    """A conversation as the PC has it, with the real rules and the real executor, and the OpenConversation."""
    talk = OpenConversation(lambda: {"enabled": True, **settings})
    talk.start("pc")
    said = []
    conversation = _conversation(intents=LocalIntents(conversation=talk, satellite="pc"), llm=llm,
                                 actions=ActionExecutor(conversation=talk, satellite="pc"), say_first=said.append)
    return conversation, talk, said


def test_think_harder_alone_after_a_request_which_ran_something_only_moves_up():
    # Asking "add the gym to my calendar on Monday" again would add a second event.
    llm = FakeLLM(_answer("Okay.", Action("add_to_calendar", {"title": "Gym"}, from_model=True)), _answer("Monday."))
    conversation, talk, said = _talking_at_the_pc(llm)
    conversation.handle("add the gym to my calendar on Monday")
    assert llm.history[-1]["plain"] is False
    reply = conversation.handle("think harder")
    assert (reply.say, reply.claude_answered) == ("Okay.", False)
    assert llm.asked == ["add the gym to my calendar on Monday"] and said == []
    assert talk.bigger and talk.model("h") == "claude-sonnet-5-5"
    assert len(llm.history) == 1


def test_think_harder_alone_after_a_local_phrase_asks_nothing():
    llm = FakeLLM()
    conversation, talk, said = _talking_at_the_pc(llm)
    assert conversation.handle("Quick answers.").say == "Okay, quick answers."
    reply = conversation.handle("Think harder.")
    assert (reply.say, llm.asked, said) == ("Okay.", [], [])
    assert talk.bigger


def test_think_harder_alone_after_a_plain_answer_asks_it_again_and_replaces_it():
    llm = FakeLLM(_answer("It depends."), _answer("Monday, the gym."))
    conversation, talk, said = _talking_at_the_pc(llm)
    conversation.handle("which days should I go to the gym")
    reply = conversation.handle("think harder")
    assert reply.say == "Monday, the gym." and said == ["Let me think about that."]
    assert llm.asked == ["which days should I go to the gym"] * 2
    assert [turn["assistant"] for turn in llm.history] == ["Monday, the gym."]


def test_think_harder_alone_after_an_answer_which_only_read_something_asks_it_again():
    # What it read is not run again as an action: it is Claude's own reading, in its answer.
    llm = FakeLLM(_answer("Sunset's at 5:52 PM."), _answer("Sunset's at 5:52 PM, so leave by five."))
    llm.to_read = [Action("home_status", {"about": "sunset"}, from_model=True)]
    conversation, talk, said = _talking_at_the_pc(llm)
    conversation.handle("when's sunset?")
    assert llm.history[-1]["plain"] is True
    reply = conversation.handle("think harder")
    assert reply.say == "Sunset's at 5:52 PM, so leave by five." and llm.asked == ["when's sunset?"] * 2
