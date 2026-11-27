"""
Conversation Module.

Decides how to answer what the user said: whether it answers a question the
assistant asked, stops something ringing, is an everyday command handled locally or
goes to Claude, which actions to run, and how they're confirmed - a sound, or words,
and whether to listen for an answer afterwards. Or, when Claude takes it for speech from
a film playing in the room, that nothing is done at all.

This used to be tangled up with the recording and playback, with its
ordering rules held in comments and instance flags, so none of it could be tested
without a microphone - or reused for anything which hears the user another way, like
a desktop push-to-talk or a satellite. Now each satellite's session (see session.py)
turns speech into text and the reply back into sound, and everything in between is
here, in plain Python: text in, Reply out.
"""

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union

from voice_assistant.actions import (LET_ME_THINK, PC_NOT_IN_A_ROUTINE, Action, ActionExecutor, ActionResult, Choice,
                                     confirm)
from voice_assistant.clock_ringing import Where, heard_in
from voice_assistant.devices import told_claude
from voice_assistant.intents import told_to_stop
from voice_assistant.journeys import told_claude as journey_told_claude
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.timeline import Timeline

# Logger setup
logger = logging.getLogger(__name__)

# What was going off at the moment the wake word fired, when nothing was.
NOTHING_RINGING = {"insistent": False, "insistent_entries": [], "reminder": None, "reminder_sounding": False}

# What start() sets afresh for each request: what was ringing as it began, how it began, and the
# questions asked in it - kept aside by aside().
TURN = ("ringing", "talked_over", "pressed", "follow_ups", "reminder_offered", "reminder_answer_pending",
        "asked_first")

# Seconds to wait for Home Assistant's lights before asking Claude without them. Every
# request to Claude includes the lights, so a Home Assistant which is slow or down must
# cost roughly this much at most, rather than the five seconds anything else waits -
# "what's the weather" shouldn't take longer because the lights couldn't be read.
LIGHTS_TIMEOUT = 1.5

# Said when Claude takes what it heard for speech from what's playing, while it was told
# nothing plays - as when nothing was caught at all.
NOT_CAUGHT = "Sorry, I didn't catch that."

# The whole of what may be said back to "Do you want me to download it?" to do it, and to leave it. Not
# Clock.answer_to_offer, which takes "stop", "cancel" or "thanks" for a yes - harmless for a
# reminder, but for a download it'd start one - and "yes" with a request after it ("yes, play
# music") for one too. Anything else is a fresh request, and nothing is pressed.
AGREED = {"yes", "yeah", "yep", "yup", "yes please", "do it", "go ahead", "go on", "sure", "ok do it",
          "okay do it"}
DECLINED = {"no", "nope", "nah", "no thanks", "no thank you", "don't", "dont", "don't do it", "no don't",
            "stop", "cancel", "cancel that", "cancel it", "leave it", "no leave it", "never mind", "nevermind",
            "not now", "no not now"}

# What Claude is told with a request begun over its last reply - by the voice, the wake word or a press - as
# nothing else in its history says the reply was stopped part way, so it might build on words never heard.
TALKED_OVER_NOTE = ("The user began speaking before your last reply had finished, so they may not have heard "
                    "the end of it.")

# The tools of an open conversation's own phrases, whose local rules are matched even in the
# answer to a question while one is on - as at any other turn of it, where "that's all" ends it,
# and "think harder" moves it up to the bigger model.
CONVERSATION_TOOLS = ["end_conversation", "use_bigger_model", "use_quick_model"]


@dataclass
class Reply:
    """
    What the assistant does in answer to one thing the user said.

    Attributes:
        say: What to say out loud, or nothing.
        sounds: Sounds to play first, as (file name, volume category) pairs.
        shown: A line to show without saying it - what a chime stood for, when the
            chime is switched off and nothing needs saying.
        listen_again: Whether the assistant asked something, and should listen for
            the answer without the wake word.
        after: Things to do once the reply has been said, each giving {"success",
            "message"} - the PC's sleep, so it isn't put to sleep mid-sentence.
        claude_answered: Whether Claude answered it, rather than Nova's own rules -
            for which model an open conversation notes answered it.
        not_for_me: Whether Claude took what was heard for speech from what's playing, so
            nobody answered it - which an open conversation counts towards its end.
    """
    say: str = ""
    sounds: List[Tuple[str, str]] = field(default_factory=list)
    shown: str = ""
    listen_again: bool = False
    after: List[Callable[[], Dict]] = field(default_factory=list)
    claude_answered: bool = False
    not_for_me: bool = False


class Conversation:
    """The rules for answering the user, from one wake word to the end of any follow-ups."""

    def __init__(self, clock, intents, llm, actions, music, chime_enabled: Union[bool, Callable[[], bool]] = True,
                 max_follow_ups: int = 3, routines=None, satellite: Optional[str] = None,
                 playing: Optional[Callable[[], List[str]]] = None, progress: Optional[Callable[[], None]] = None,
                 room: Optional[Callable[[], List[str]]] = None, here: Optional[Callable[[], str]] = None,
                 conversing: Optional[Callable[[], bool]] = None,
                 say_first: Optional[Callable[[str], None]] = None):
        """
        Args:
            clock: The Clock, for what's ringing and for dismissing it.
            intents: The LocalIntents, for the everyday commands handled without Claude.
            llm: The LLMHandler, for everything else.
            actions: The ActionExecutor, which runs what was asked for.
            music: The MusicHold, whose pause has to land before anything else is
                sent to the music player.
            chime_enabled: Whether a success is confirmed with the chime - or a function
                giving that, asked at each use so a setting saved mid-session is used
                from the next answer.
            max_follow_ups: How many times in a row the assistant may ask a question
                and listen for the answer without the wake word, so it can't hold
                the microphone indefinitely.
            routines: The user's Routines, or None, for the words which start one.
            satellite: The satellite the user is talking to, whose ringing - and its
                room's - alone they can hear, and so stop - or None for anything
                ringing anywhere.
            playing: Gives the names of what's playing in the user's room - see
                Session.playing_here - or None for nothing ever playing.
            progress: Called with nothing as a turn gets on - once for each of Claude's
                replies, and each step of a routine - so a routine of several slow steps
                isn't taken for one which is stuck (see Session.moved_at). Never while
                waiting on anything, so one which is stuck still stops calling it.
            room: Gives every satellite in the user's room, theirs first - see
                Brain.room_of - or None for a room of its own. What rings on any of
                them is heard, and stopped, from here.
            here: Says which satellite the user is speaking through, for Claude - see
                Session._here - or None to say nothing of it.
            conversing: Says whether an open conversation is on at the user's satellite -
                see OpenConversation - in which every reply is listened after: so a question
                is listened for however many came before it, and the conversation's own
                phrases are heard even in its answer. None for never.
            say_first: Says something to the user and returns once it's been said, or None to say nothing -
                for "Let me think about that." before the slow request to the bigger model.
        """
        self.clock = clock
        self.intents = intents
        self.llm = llm
        self.actions = actions
        self.music = music
        self._chime_enabled = chime_enabled
        self.max_follow_ups = max_follow_ups
        self.routines = routines
        self.satellite = satellite
        self.playing = playing
        self.progress = progress or (lambda: None)
        self.room = room
        self.here = here
        self.conversing = conversing or (lambda: False)
        self.say_first = say_first
        self.start()

    def _heard_here(self) -> Where:
        """What rings where the user can hear it, to ask the clock about: their satellite's room."""
        return heard_in(self.room() if self.room is not None else [self.satellite])

    @property
    def chime_enabled(self) -> bool:
        """Whether a success is confirmed with the chime, as it is set now."""
        return self._chime_enabled() if callable(self._chime_enabled) else self._chime_enabled

    def start(self, ringing: Optional[Dict] = None, talked_over: bool = False, pressed: bool = False):
        """
        Starts a new conversation, as the wake word is heard.

        Args:
            ringing: What was audibly going off at that moment - Clock.ringing_state(),
                read before the conversation quietens anything, since what matters is
                what the user could hear when they started talking.
            talked_over: True when the wake word was said over the assistant's last
                reply, which was stopped there and then.
            pressed: True when it began on a press of a key, which is deliberate - so
                nothing said in it is taken for speech from what's playing.
        """
        self.ringing = ringing or dict(NOTHING_RINGING)
        self.talked_over = talked_over
        self.pressed = pressed
        # Counted per conversation, not per question, so a long back and forth still
        # ends after the cap is reached.
        self.follow_ups = 0
        # A reminder is only offered up once per conversation, however many
        # follow-up questions it runs to. The answer to that offer is only expected
        # within the same conversation, so a question left unanswered doesn't colour
        # the next one.
        self.reminder_offered = False
        self.reminder_answer_pending = False
        # What a yes to the question just asked would do - setting the reminder to leave, after
        # "Want a reminder?" - or, for a question with set answers, what each would do
        # ("The mic or the audio?"), or None. Only ever the very next thing said in the same
        # conversation answers it, so a new wake word drops it.
        self.asked_first: Union[Action, Choice, None] = None
        # Earlier conversations are only remembered for a few minutes, so this
        # morning's "play that one" doesn't colour tonight's "yes".
        self.llm.forget_stale_history()
        if talked_over:
            self.llm.note(TALKED_OVER_NOTE)

    @contextmanager
    def aside(self):
        """
        Starts afresh, as start() does, for a routine run by itself during an open conversation - and puts
        back what the conversation's own turn had once it's done, so a question Nova had just asked there
        is still answered by what the user says next, and what was ringing as the turn began still is.
        """
        kept = {name: getattr(self, name) for name in TURN}
        self.start()
        try:
            yield
        finally:
            for name, value in kept.items():
                setattr(self, name, value)

    def handle(self, text: str, follow_up: bool = False, timeline: Optional[Timeline] = None,
               in_routine: bool = False) -> Reply:
        """
        Works out the answer to one thing the user said.

        Args:
            text: What they said, as transcribed, without the wake word.
            follow_up: True when it answers a question the assistant just asked.
            timeline: The Timeline to mark each stage on, if it's being timed.
            in_routine: True for a step of a routine, said for the user - see run_routine.

        Returns:
            The Reply.
        """
        timeline = timeline or Timeline()

        # Answering "Want a reminder?" or "Do you want me to download it?" - read before
        # anything else, the follow-up window's going straight to Claude included, and only
        # once: whatever's said next drops it. Only a yes runs it, and as Nova's own action,
        # never the model's. A question with set answers ("The mic or the audio?") runs the
        # one said back.
        asked_first, self.asked_first = self.asked_first, None
        if asked_first is not None:
            chosen, answer = _answer_to(asked_first, text) if follow_up else (None, None)
            if answer is not None:
                timeline.note(outcome="asked_first_answer")
                result = self._run([Action(chosen.tool, chosen.params)])[0] if answer else None
                said = result.say if result is not None else "Okay, I'll leave it."
                logger.info(f"{chosen.tool} {'run' if answer else 'not run'} on the user's answer")
                # Kept for Claude even when nothing's said now - "Asked SoulSync for Gooba. It
                # usually takes a few minutes..." - as the API refuses an exchange with no reply.
                self.llm.remember(text, said)
                if result is None:
                    return Reply(say=said)
                if result.ok and result.confirm == "later":
                    # "Gooba requested." comes once SoulSync has it.
                    return Reply()
                # As if asked for outright: the mic's own sound, rather than "Microphone off.", and
                # at the PC its shutdown pressed once the chime has played.
                sounds, messages = confirm([result], self.chime_enabled)
                after = [result.after_said] if result.ok and result.after_said else []
                return Reply(say=" ".join(messages), sounds=sounds, after=after)
            # Anything else is a fresh request, and nothing is done.

        # Answering the assistant's own "shall I turn that reminder off?". Checked
        # before anything else bar that, since "no" here means leave it alone, while "no"
        # said to something ringing means make it stop - opposite outcomes from the
        # same word, told apart only by the question having just been asked.
        if self.reminder_answer_pending:
            self.reminder_answer_pending = False
            timeline.note(outcome="reminder_answer")
            answer = self.clock.answer_to_offer(text)
            if answer is False:
                logger.info("Reminder left running at the user's request")
                return Reply(say="Okay, I'll leave it.")
            if answer:
                self.clock.dismiss(satellite=self._heard_here())
                logger.info("Reminder dismissed after being offered")
                return self._chime("Okay.")
            # Anything else is a fresh request, so it falls through and is handled
            # normally - the reminder simply carries on.

        # Anything short said while a timer is going off means "make it stop", so it's
        # handled here rather than going to the model, making it quicker and easier
        # to silence.
        here = self._heard_here()
        if self.clock.is_ringing(satellite=here) and self.clock.looks_like_dismissal(text):
            result = self.clock.dismiss(satellite=here)
            timeline.note(outcome="dismissed_locally")
            logger.info("Dismissed by voice without asking the model")
            return self._chime(result["message"])

        # "Hey Nova, stop" or "Hey Nova, okay" over a reply only means stop talking,
        # which the wake word already did. Taken as a command, "stop" would pause the
        # music as well - which was only quiet while the assistant was talking. In an open
        # conversation, where being told to stop ends it, so is "stop talking": over a reply
        # each only stops the reply, and it's said again in the quiet to end it.
        if self.talked_over and not follow_up and (self.clock.looks_like_dismissal(text)
                                                   or self.conversing() and told_to_stop(text)):
            timeline.note(outcome="stopped_talking")
            logger.info("Told to stop talking, so said nothing more")
            return Reply()

        # One of the user's routines, said by its own words or asked for by name - see
        # routines.py. Not as one of a routine's own steps, which would start it again.
        routine = None if follow_up or in_routine or self.routines is None else self.routines.called(text)
        if routine:
            timeline.note(outcome="routine")
            return self.run_routine(routine, timeline=timeline)

        # A timer or alarm going off is silenced by the user saying anything at all,
        # since talking to the assistant means they've heard it. Read as the request
        # began - the wake word, or the start of a turn of an open conversation -
        # rather than now, so one which came due while it was being answered isn't
        # silenced along with it: until a reply is over it waits unheard, as
        # announcements wait for a request to end, and in an open conversation for
        # its next gap.
        heard_ringing = self.ringing.get("insistent_entries", [])

        # The everyday commands which don't need a language model are handled straight
        # away without going to Claude, so they still work offline. Not the answer to
        # a question the assistant asked, though, which needs the conversation so far
        # to make sense of. See intents.py.
        # Bar one whose tool's answer is the user's alone - "where's my phone?" - which can't be
        # the answer to anything Nova asks, and which Claude, once asked, would hear. And in an
        # open conversation, its own phrases, as the user may end it in answer to anything.
        if follow_up:
            only = ActionExecutor.private_tools() + (CONVERSATION_TOOLS if self.conversing() else [])
            result = self.intents.match(text, only=only)
        else:
            result = self.intents.match(text)
        local = result is not None
        claude_answered = not local
        # "Think harder", by the user's words: the conversation moves up, and what is asked of the bigger
        # model is answered as any other of Claude's answers is, from here.
        moved_up = local and any(action.tool == "use_bigger_model" for action in result["actions"])
        earlier = None
        if moved_up:
            asked = self._asked_of_the_bigger_model(text, result["actions"][0])
            if asked is None:
                return Reply(say="Okay.")
            text, earlier = asked
            local, claude_answered = False, True
            # The wait for the slower model isn't silence, as when Claude moves up by itself.
            if self.say_first is not None and not in_routine:
                try:
                    self.say_first(LET_ME_THINK)
                except Exception as e:
                    logger.warning(f"Could not say a line first: {type(e).__name__}")
            result = self._ask_claude(text, in_routine=in_routine)
            # A turn nobody answered leaves the exchange it was to replace as it was.
            if earlier is not None and (not result.get("success") or result.get("not_for_me")):
                self.llm.put_back(earlier)
                # Its reply, talked over, still stands, so Claude is told so after all - see
                # _asked_of_the_bigger_model.
                if self.talked_over:
                    self.llm.note(TALKED_OVER_NOTE)
        elif local:
            timeline.note(local_intent=True)
        else:
            result = self._ask_claude(text, in_routine=in_routine)
        timeline.mark("llm_done")
        # Before anything is run or done with the turn: speech from a film has nothing ringing
        # silenced, as the user said nothing, nor kept to send to the phone.
        if result.get("not_for_me"):
            return self._not_for_me(result["playing"], timeline, in_routine)
        actions = result.get("actions", [])

        # Every action asked for is run, in order - "pause the music and set a ten
        # minute timer" is two of them.
        action_results = self._run(actions, in_routine=in_routine)

        # Home Assistant's Assist only knows so many sentences. "Dim the lights" isn't
        # one of them, though Claude can set a brightness - so what Assist matched
        # nothing in is asked of Claude, as if no local command had matched. Only
        # remembered once that's known, or the request would be in the model's history
        # twice: here, and again from its own answer.
        if local and all(action_result.understood for action_result in action_results):
            stand_ins = _stand_ins(actions, action_results)
            self.llm.remember(text, next(iter(stand_ins.values()), result["response"]))
        elif local:
            logger.info("Home Assistant didn't understand that, so asking Claude")
            timeline.note(local_intent=False)
            claude_answered = True
            result = self._ask_claude(text, in_routine=in_routine)
            timeline.mark("llm_done")
            if result.get("not_for_me"):
                return self._not_for_me(result["playing"], timeline, in_routine)
            actions = result.get("actions", [])
            action_results = self._run(actions, in_routine=in_routine)
        response_text = result["response"]
        if actions:
            # Marked here rather than as each run finishes, so Assist's failed attempt
            # can't leave a time from before Claude was asked if Claude's answer has no
            # actions of its own.
            timeline.mark("action_done")
            timeline.note(actions=", ".join(action.tool for action in actions))

        # Done after the action has run, so a request which was itself about the timer
        # ("cancel the timer") still finds it ringing and dismisses it, rather than
        # finding it already silent and deleting a pending one instead.
        if heard_ringing:
            self.clock.silence_insistent(heard_ringing)

        # An action either confirms itself - the music changing, or a sound - or has
        # its message said in place of the model's own reply, which for a tool call is
        # usually just "Okay.". See confirm().
        sounds, messages = confirm(action_results, self.chime_enabled, fallback=response_text)
        if action_results:
            response_text = " ".join(messages)
        after = [r.after_said for r in action_results if r.ok and r.after_said]

        # A reminder is left alone by all of that - it isn't insistent enough to count
        # as heard just because the user happened to speak. One caught in the quiet gap
        # between its rounds is easy to forget is even pending, so once the actual
        # request is dealt with the user is asked about it. One that was
        # mid-announcement isn't: it's loud and obvious, so it can be acknowledged
        # outright rather than needing an offer. Not tacked onto a question the model
        # is already asking, which would leave two to answer at once - and added last,
        # so it trails the answer rather than interrupting it.
        # Nor in a routine, whose next step would be taken for the answer.
        reminder = self.ringing.get("reminder")
        if (reminder and not in_routine and not self.ringing.get("reminder_sounding") and not self.reminder_offered
                and self.clock.is_ringing(satellite=self._heard_here()) and not response_text.rstrip().endswith("?")):
            self.reminder_offered = True
            self.reminder_answer_pending = True
            response_text = f"{response_text} Also, your {reminder} reminder is still going. Shall I turn it off?".strip()

        if not response_text:
            # A download asked for says nothing until SoulSync has it, but Claude is still
            # told what its call did - "Asked SoulSync for X. It usually takes..." - rather
            # than "Done.", which it took for the download finished.
            if result.get("success") and any(r.ok and r.confirm == "later" for r in action_results):
                self.llm.record_reply(text, "", _tool_answers(actions, action_results))
            logger.info(f"Action confirmed itself: {[r.say for r in action_results]}", extra=TRANSCRIPT)
            return Reply(sounds=sounds, after=after, claude_answered=claude_answered)

        # Kept for "send that to my phone" - unless this was that, or the next one would
        # send "Sent.".
        if not any(action.tool == "send_to_phone" for action in actions):
            self.actions.last_answer = (text, response_text)

        # The model's history only has its own text for this turn - usually "Okay." when
        # it called a tool - so it's told what's actually being said. Otherwise a
        # question an action asked ("Which one?") would be missing when the answer
        # comes back on the next turn. Only what's spoken is recorded: an action
        # confirmed by a chime or by the music changing keeps the model's own "Okay.".
        # What each action said goes with it, as the tools' own answers.
        # Where the phone is - a private answer - is the user's alone, whoever asked for it:
        # Claude's history goes to Anthropic with every later request, so it only has that it
        # was said, in place of the words. Claude needs no place to answer anything it's asked.
        if result.get("success"):
            stand_ins = _stand_ins(actions, action_results)
            recorded = response_text
            for index, stand_in in stand_ins.items():
                if action_results[index].say:
                    recorded = recorded.replace(action_results[index].say, stand_in)
            self.llm.record_reply(text, recorded, _tool_answers(actions, action_results))

        # If the assistant asked something, it listens straight away for the answer -
        # but only so many times in a row, unless in an open conversation, which listens
        # after every reply anyway.
        listen_again = response_text.rstrip().endswith("?") and (self.follow_ups < self.max_follow_ups
                                                                 or self.conversing())
        if listen_again:
            self.follow_ups += 1
            # A question one of the actions asks first, answered by what's said next. Not one
            # nobody's listening for an answer to, nor one asked in a routine, whose next step
            # would be taken for the answer.
            if not in_routine:
                self.asked_first = _only_question(response_text, action_results)
        return Reply(say=response_text, sounds=sounds, listen_again=listen_again, after=after,
                     claude_answered=claude_answered)

    def _asked_of_the_bigger_model(self, text: str, move: Action) -> Optional[Tuple[str, Optional[Dict]]]:
        """
        Moves the conversation up to the bigger model, and says what it is to be asked.

        Args:
            text: What the user said, e.g. "think harder about the route to Leeds".
            move: The use_bigger_model action the user's words matched, whose "rest" is what came after
                them.

        Returns:
            None when it has moved up and that is all - the user said it alone and the last exchange
            can't be asked again: it ran something, which would be run twice, or it was a local command
            or phrase the user gave, which isn't a request to answer, or there is none; or it couldn't be
            moved up. Else the request to ask: the whole of what the user said when they said more, as
            Claude copes with the words before it, with None; or the last request when the user said it
            alone and Claude answered it without running anything, with that exchange - taken out of the
            history, to be replaced by the answer.
        """
        if not self._run([Action("use_bigger_model")])[0].ok:
            return None
        if move.params.get("rest"):
            return text, None
        if not self.llm.history or not self.llm.history[-1].get("plain"):
            return None
        # The reply the user may have talked over is the one the answer replaces, so they've missed
        # nothing of it - and noted, it would head the request asked again, whose reply before it
        # nobody talked over.
        if self.talked_over:
            self.llm.unnote(TALKED_OVER_NOTE)
        earlier = self.llm.take_back()
        return earlier["user"], earlier

    def _ask_claude(self, text: str, in_routine: bool = False) -> Dict:
        """
        Asks Claude to answer what the user said, along with what's going on around it.

        Args:
            text: What they said.
            in_routine: True for a step of a routine - the user's own words for it, never
                something heard, so Claude isn't told what's playing.

        Returns:
            What LLMHandler.process_query gives - with 'playing', what Claude was told
            plays in the room, when it took what it heard for speech from that.
        """
        # Sent along with what's currently on the clock, so a mangled label like
        # "walk the frogs" can still be matched to the real "walk the dogs" reminder.
        timers = "; ".join(self.clock.describe_all()) or None
        # And the lights, with the room Nova is in, so "dim the lights" means that
        # room's. Left out if Home Assistant isn't there, or is too slow to wait for.
        home = self.actions.home
        lights = (home.describe_lights(timeout=LIGHTS_TIMEOUT) if home else "") or None
        # The tools afresh, so a calendar added in Google is named to Claude from the next
        # request - the API's cache only changes when they do. The tools only read from are
        # the same executor's, at the same time, so none is ever run as an action.
        self.llm.tools = self.actions.tool_schemas()
        self.llm.read_tools = set(self.actions.read_tools())
        # Read once, as the request is built, so what Claude is told plays is what decides
        # whether its not_for_me counts - a film which stops while it thinks changes neither.
        # Nothing for a press, which no film can make, so it's never dropped as one.
        playing = [] if in_routine or self.pressed or self.playing is None else list(self.playing())
        # What Claude reads, such as home_status, goes back to it to answer from.
        result = self.llm.process_query(text, context=timers, lights=lights, playing=playing,
                                        read=lambda action: self.actions.execute(action).say,
                                        # Nobody spoke a routine's steps, so no device was spoken through.
                                        device=self.here() if self.here and not in_routine else None)
        self.progress()
        if result.get("not_for_me"):
            result["playing"] = playing
        return result

    def _not_for_me(self, playing: List[str], timeline: Timeline, in_routine: bool = False) -> Reply:
        """
        The reply when Claude took what it heard for speech from what's playing - a line
        from a film, heard as the wake word and a request.

        Args:
            playing: What Claude was told plays in the room.
            timeline: The Timeline the turn is marked on.
            in_routine: True for a step of a routine, whose turn stays noted as the routine.

        Returns:
            Nothing at all while something played, so a film is never answered, and the
            music comes back as after any reply with nothing to say - else NOT_CAUGHT, as
            Claude had no grounds for it.
        """
        if not playing:
            # It counts how often Claude takes what it heard for a film with nothing playing,
            # which tunes the prompt. A routine's step wasn't heard - it's the user's own words.
            if not in_routine:
                timeline.note(outcome="not_for_me_unfounded")
            logger.info("Claude took that for speech from what's playing, with nothing playing, "
                        "so it wasn't caught")
            return Reply(say=NOT_CAUGHT, not_for_me=True)
        timeline.note(outcome="not_for_me")
        logger.info("Taken as speech from what's playing, so ignored")
        return Reply(not_for_me=True)

    def _run(self, actions: List[Action], in_routine: bool = False) -> List[ActionResult]:
        """
        Runs the actions asked for, in order.

        Args:
            actions: The Actions, if any.
            in_routine: True for a step of a routine, which never runs a tool that needs the user
                there - see ActionExecutor.routine_refusals - but says why in its place, as a
                routine may be running by itself.

        Returns:
            The ActionResult of each.
        """
        if not actions:
            return []
        # Music and volume actions talk to the music player too, so the pause has
        # to have landed first.
        self.music.wait()
        refused = ActionExecutor.routine_refusals() if in_routine else {}
        results = [ActionResult(False, refused[action.tool]) if action.tool in refused else self.actions.execute(action)
                   for action in actions]
        if not in_routine:
            return results
        # Shutting down or restarting the PC asks first in a conversation, and a routine's question
        # is never answered (see handle), so it's left undone and said so rather than asked.
        return [ActionResult(False, PC_NOT_IN_A_ROUTINE)
                if result.ok and isinstance(result.ask_first, Action) and result.ask_first.tool == "press_pc_button"
                else result for result in results]

    def run_routine(self, routine: Dict, timeline: Optional[Timeline] = None) -> Reply:
        """
        Runs a routine's steps in turn, each as if the user had said it, and says what
        they say, together - after any sounds, each played once. A question a step
        asks isn't listened for an answer to, as nobody asked it.

        Args:
            routine: The routine, as Routines has it.
            timeline: The Timeline to mark each stage on, if it's being timed.

        Returns:
            The Reply.
        """
        logger.info(f"Running the {routine['name']} routine", extra=TRANSCRIPT)
        said, shown, sounds, after = [], [], [], []
        for step in routine["steps"]:
            reply = self.handle(step, timeline=timeline, in_routine=True)
            self.progress()
            said.append(reply.say)
            shown.append(reply.shown)
            sounds.extend(sound for sound in reply.sounds if sound not in sounds)
            after.extend(reply.after)
        return Reply(say=" ".join(part for part in said if part), sounds=sounds,
                     shown=" ".join(part for part in shown if part), after=after)

    def _chime(self, message: str) -> Reply:
        """A success confirmed by the chime - or, with it switched off, only shown."""
        if self.chime_enabled:
            return Reply(sounds=[("success_chime.wav", "general")])
        return Reply(shown=message)


def agreed(text: str) -> Optional[bool]:
    """
    Whether what was said back to a question that asks first - "Do you want me to download it?" - agreed.

    Args:
        text: What the user said, e.g. "Nova, yes please."

    Returns:
        True for the whole of it a plain yes (AGREED), False for a plain no (DECLINED), else
        None - after its punctuation and quote marks, a "nova" at the start and a "please" at
        the end are taken off.
    """
    said = _plain_answer(text)
    if said in AGREED:
        return True
    if said in DECLINED:
        return False
    return None


def _answer_to(asked: Union[Action, Choice], text: str) -> Tuple[Optional[Action], Optional[bool]]:
    """
    What the answer to a question asked first does.

    Args:
        asked: The Action a yes runs, or a Choice of answers.
        text: What the user said back.

    Returns:
        (the Action to run or not, True to run it, False for a no) - or (None, None) when it isn't
        an answer, so it's a fresh request. A Choice has no no: only an answer it knows counts.
    """
    if isinstance(asked, Choice):
        chosen = asked.chosen(_plain_answer(text))
        return (chosen, True) if chosen is not None else (None, None)
    answer = agreed(text)
    return (asked, answer) if answer is not None else (None, None)


def _plain_answer(text: str) -> str:
    """
    An answer to a question tidied to its words: lower case, without its punctuation and quote
    marks, a "nova" at the start or a "please" at the end.
    """
    # Digits and other scripts stay words, so "Yes, 10." - most likely in ten minutes - isn't a
    # plain yes. Only the quote marks around a word go, so "'yes'" is one and "don't" stays.
    text = text.lower().replace("\u2018", "'").replace("\u2019", "'")
    words = [word.strip("'") for word in re.sub(r"[^\w' ]", " ", text).split()]
    words = [word for word in words if word]
    if words[:1] == ["nova"]:
        words = words[1:]
    if words[-1:] == ["please"]:
        words = words[:-1]
    return " ".join(words)


def _only_question(reply: str, results: List[ActionResult]) -> Union[Action, Choice, None]:
    """
    What a yes (or, for a Choice, each answer) to the reply would do - only when exactly one result asks first and its
    question is the whole of what's asked: the reply's last sentence, with no other ending
    "?". "Do you want me to download it? Which light?" asks two things, so a yes does neither.

    Args:
        reply: The whole reply, as it's said.
        results: What each of the turn's actions said.

    Returns:
        The Action to run on a yes, or None.
    """
    asking = [result for result in results if result.ok and result.ask_first]
    reply = reply.rstrip()
    if len(asking) != 1 or not asking[0].say or not reply.endswith(asking[0].say):
        return None
    return None if "?" in reply[:-len(asking[0].say)] else asking[0].ask_first


def _stand_ins(actions: List[Action], results: List[ActionResult]) -> Dict[int, str]:
    """
    What Claude's history has in place of each private answer, by its action's place in the
    turn - e.g. {0: "I said where your phone is."}, or for a journey worked out from where the
    user is, that it was said and the question it asked (see journeys.told_claude).

    Args:
        actions: The actions the turn ran.
        results: What each said, in the same order.
    """
    return {index: journey_told_claude(result.say) if action.tool == "journey"
            else told_claude(str(action.params.get("device") or "phone"))
            for index, (action, result) in enumerate(zip(actions, results)) if result.private}


def _tool_answers(actions: List[Action], results: List[ActionResult]) -> List[str]:
    """
    What Claude's history has as each action's answer, in the order they ran - what it
    said, or a private answer's stand-in.

    Args:
        actions: The actions the turn ran.
        results: What each said, in the same order.
    """
    stand_ins = _stand_ins(actions, results)
    return [stand_ins.get(index, result.say) for index, result in enumerate(results)]
