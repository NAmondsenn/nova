"""
Local Intents Module.

Handles the handful of everyday commands which don't need a language model - "set a
timer for ten minutes", "pause", "skip", "turn it up", "what time is it" - without
going to Claude at all.

Every request used to go to Claude first, so even these paid for a round trip to
the cloud, and when the internet dropped, timers and the music controls stopped
working altogether. The patterns here are deliberately strict: only a whole request
which is exactly one of these is handled locally. Anything else - "set a timer for
ten minutes and play Drake", "pause it in a bit" - still goes to Claude as before.

After all of those, a plain request for something of Home Assistant's - "pause the
TV", "run goodnight" - goes straight to its Assist, so Nova's own commands always
come first.
"""

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from voice_assistant.actions import Action
from voice_assistant.clock import spoken_clock_time
from voice_assistant.computers import ComputerHooks
from voice_assistant.lists import names_list
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.wake_phrase import DEFAULT_WAKE_WORD

# Logger setup
logger = logging.getLogger(__name__)

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}

UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}

# Said around a request without changing it - "could you pause the music please".
POLITE_START = r"^(?:please |can you |could you |would you |will you )+"
POLITE_END = r"(?: please| thanks| thank you)+$"

# A light's name - "kitchen", "living room" - of up to three words, none of them
# "and", so "turn off the timer and the lights" is left to Claude rather than all
# sent to Home Assistant as if it were one light.
LIGHT = r"(?:all )?(?:the |my )?(?:(?!and\b)[a-z']+ ){0,3}(?:lights?|lamps?)"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Where the activity rules have the activity - "what time do I have work?". Each of the
# user's calendars but their own is one, by its name, so the words are Google's, put in
# here at each match - see LocalIntents._activity_rule. Until then, any words will do.
ACTIVITY = r"(?P<activity>[a-z0-9' ]+?)"
# The bare shapes before an activity - "when's sunset?", "am I at home?" - in which an
# activity's first word alone is an everyday question, not "Sunset yoga" or "Home group",
# unless a day follows.
BARE_BEFORE_ACTIVITY = r"(?:when is|when's|am i)(?: at| in)? "

# A place said in a journey question - "work", "the Metrocentre", "my gran's" - of one to four
# words, as said. None starts with a digit or is a word that begins something else: a time
# ("to be at work for 9"), a day ("for work tomorrow", "on Monday", "this afternoon", "right
# now") or a "from", "with" or "via". Those are left to Claude, who has the arrival time and
# the day to take them, rather than made part of a place sent to Google. And the first word is
# never "it", "there", "that" or "here", which say a place without naming it.
JOURNEY_WORD = (r"(?!(?:at|by|before|for|be|in|on|from|with|via|now|right|today|tonight|tomorrow|this|next|"
                + "|".join(WEEKDAYS) + r")\b)[a-z][a-z']*")
JOURNEY_PLACE = rf"(?P<place>(?!(?:it|there|that|here)\b){JOURNEY_WORD}(?: {JOURNEY_WORD}){{0,3}})"
# How a journey is made, by the verb or noun said, in the words the journey tool has them.
JOURNEY_MODES = {"drive": "driving", "walk": "walking", "cycle": "cycling"}
JOURNEY_BY = r"(?:by (?:bus|train|public transport)|on public transport)"

# The devices a battery is asked about by - the kind words batteries.find knows. A
# device with a name of its own ("the S25 Ultra") is left to Claude.
BATTERY_DEVICE = "mac|phone|tablet|laptop|computer"
# And the devices found or rung by - those the Home Assistant app is on.
FOUND_DEVICE = "phone|tablet|mac|laptop"
# The PC, as ha_guard.PC_LOCK has it - see pc.py.
PC = r"(?:my |the )?(?:pc|computer)"

# Moving the music to another satellite - "play it on my Pi", "move this to the PC", "switch to the
# Mac". The thing moved is optional, and "on" is only a way to say where after "play": "switch on my
# PC" is the PC's power, which comes further down, whether or not "pc" names a satellite. Where it goes
# is of up to four words, taken only when the brain says they name a satellite, so "switch to the next
# song" and "change it to 50" are left to the rules after, or Claude.
MOVED_THING = r"(?:(?:it|this|that|the music|music|the song|this song) )?"
MOVE_TO = rf"(?:(?:move|switch|change|swap|transfer|take|play) {MOVED_THING}(?:to|onto|over to)|play {MOVED_THING}on)"
MOVE_WHERE = r"(?P<where>[a-z0-9']+(?: [a-z0-9']+){0,3})"

# A web address said with its dots - "bbc.co.uk", "my-site.com" - as written in the request
# itself, since the rules see it without them. The last part is letters, so "open 3.5" isn't one.
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")
# Where a computer is, said after "on": "open Fortnite on the PC".
ON_WHERE = rf"(?: on {MOVE_WHERE})?"
# What's muted when he says the sound is - Windows' at the PC, the music at the Pi (0034) - and the
# microphone, Nova's listening, which plain "mute" is.
SOUND = r"(?:sound|audio|volume|music)"
MIC = r"(?:the |my )?(?:mic|microphone)"
# Words which stand for something said before, so are never searched for, or closed, as they are.
PRONOUNS = {"it", "that", "this"}
# The window in front, as "close this" says it.
FRONT_WINDOW = r"this|that|this window|the window|it"

# A thing of Home Assistant's for Assist - "the TV", "AdGuard protection", "movie
# night" - of one to four words, none of them "and", "then" or "plus", so "turn off
# the TV and the lamp" is left to Claude rather than sent as one thing. "The" or "my"
# is never the name itself, so "set my volume to 40" isn't the volume of something
# called "my".
THING_WORD = r"(?!(?:and|then|plus)\b)[a-z0-9']+"
THING = rf"(?:all the |the |my )?(?P<thing>(?!(?:the|my)\b){THING_WORD}(?: {THING_WORD}){{0,3}})"

# Words for Nova's own things, which Assist mustn't be given in their place - "turn
# off the timer" is Nova's timer, not a Home Assistant helper of that name. Left to
# Claude, which knows Nova's tools.
NOVAS_OWN = {"timer", "timers", "alarm", "alarms", "reminder", "reminders", "music", "song", "songs", "track",
             "tracks", "playlist", "volume", "notification", "notifications", "microphone", "mic", "mute",
             "shuffle", "repeat", "spotify", "nova", "weather", "forecast", "calendar", "playback", "navidrome",
             "chime", "wake", "quiet", "battery", "warnings"}

# What only the conversation so far could say the meaning of - "turn it off", "turn
# the other one off", "open the first one" - which Assist, with no idea what was
# meant, can't be given.
PRONOUNS = {"it", "this", "that", "them", "these", "those", "one", "ones", "both", "other", "others", "rest",
            "same", "again", "first", "last"}

# "One" is a name's number as often as it points back - "turn on fan one", "BBC One" - so
# in a thing for Assist it only points back first ("the one in the kitchen", as "the"
# never reaches the thing), or after a word saying which of several: "the other one",
# "the second one". Such a word alone points back too: "turn off the second".
WHICH_OF_SEVERAL = {"other", "another", "first", "second", "third", "fourth", "fifth", "next", "previous", "last"}

# When, rather than what - "pause the TV in five minutes" is a delay, which only Claude
# can set. A time is a unit anywhere, or one of TIME_LEADS straight before a number, a
# unit, "a", "an" or "half" - so "the TV in the kitchen" and "fan 2" are still things.
# "Second" is an order as often as a unit, so it's only a unit after a number or one of
# TIME_LEADS - "in a second", "one second", "for second" - and "activate the second
# scene" is a thing too. "The light in second bedroom" is then Claude's, which is the
# safe way round.
TIME_UNITS = {"second", "seconds", "minute", "minutes", "hour", "hours", "bit", "while", "later", "tonight",
              "tomorrow", "o'clock"}
TIME_LEADS = {"in", "for", "at", "until", "after", "before", "by"}
# Who for, rather than what: "run me a bath" isn't a script called "me a bath".
NOT_A_THING = {"me", "us"}

# A list, as it's said - "my shopping list", "notes", "the uni-work list". Which list it is
# is up to lists.py, which finds it by the user's own words.
LISTWORDS = r"(?:my |the )?[a-z0-9' -]+?"

# The words for Nova's own things and the devices, which a list's words mustn't have -
# "add ten minutes to the timer", "put it on my phone" - left to the rules after, or Claude.
NOT_A_LIST = {"timer", "alarm", "reminder", "calendar", "schedule", "diary", "agenda", "phone", "tablet", "mac",
              "laptop", "ipad"}
# A connector before the list - the last one, so "a trip to Ikea" is one thing - but never
# the "to" of "to do", which is a list's name.
LIST_LEAD = r"(?:to|on|onto) (?!do\b)"


# The point-back words that never name a thing, wherever they are - "add that cake to my
# list" is the cake just talked about. The rest of PRONOUNS are only when they're all the
# thing is, so "one egg" and "the last of the bread" are things, but "the other one" isn't.
ALWAYS_POINTS_BACK = {"it", "this", "that", "them", "these", "those"}


# A song said only by where it is - "the last song", "the song before", "the one before", "the one
# playing" - is the one playing or just played, which a playlist is for, never a to-do list. Its words are
# the song's, which "one" is when a pointer is with it, and the pointers: "the song of the summer" and "a
# new song" are named, so still things for a list, and "one egg" is a thing.
SONG_WORDS = {"song", "track", "tune", "one"}
SONG_POINTERS = {"last", "previous", "before", "earlier", "prior", "current", "playing", "played", "next"}
# The song playing said as a question - "add what's playing to Gym".
WHATS_PLAYING = re.compile(r"(?:what(?:['\u2019]s| is)|whats) playing")


def points_back(item: str) -> bool:
    """
    Whether a thing said for a list only points back, to what the conversation so far says - or at a song
    by where it is, as "the last song" does.

    Args:
        item: The thing as it was said, e.g. "the other one".
    """
    words = item.split()
    rest = [word for word in words if word not in ("the", "my", "some")]
    return (bool(ALWAYS_POINTS_BACK & set(words)) or (bool(rest) and all(word in PRONOUNS for word in rest))
            or (bool(SONG_WORDS & set(rest)) and all(word in SONG_WORDS | SONG_POINTERS for word in rest))
            or WHATS_PLAYING.fullmatch(item) is not None)


def not_a_list(said: Optional[str]) -> bool:
    """
    Whether a list's words name one of Nova's own things or a device, e.g. "my phone's notes".

    Args:
        said: The list as it was said, or None.
    """
    # Singular, so "my reminders" is a reminder too - after any "'s", so "phone's" is a phone.
    return any(re.sub(r"s$", "", re.sub(r"'s$", "", word)) in NOT_A_LIST for word in (said or "").split())


# A day or a time in what was said for a list - "put the dentist on Thursday at 3" is
# the calendar, Claude's, not a list called "Thursday at 3". A list named after a day is
# still one with "list" said: "my Monday jobs list".
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
HOURS_SAID = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midnight"
WHEN_WORDS = re.compile(rf"\b(?:today|tonight|tomorrow|next week|(?:{'|'.join(WEEKDAYS)})s?|{'|'.join(MONTHS)}"
                        rf"|at (?:\d+|{HOURS_SAID})|\d+(?: \d+)? ?(?:am|pm|a m|p m))\b")


def names_a_day(said: Optional[str]) -> bool:
    """
    Whether a list's words hold a day or a time - and not the word "list" - e.g.
    "thursday at 3" in "put the dentist on Thursday at 3".

    Args:
        said: The list as it was said, or None.
    """
    return bool(said) and not names_list(said) and WHEN_WORDS.search(said) is not None


# A day asked about by name - anything less plain, like "next Thursday" (this week's
# or next week's?), is left to Claude.
DAY = r"(?P<day>today|tonight|tomorrow|this weekend|the weekend|" + "|".join(WEEKDAYS) + ")"


def _number(words: str) -> Optional[int]:
    """
    A number said as digits or words, e.g. "10", "ten", "twenty five".

    Returns:
        The number, or None if it isn't one - which keeps the request away from the
        local path rather than guessing at it.
    """
    if words.isdecimal():
        return int(words)
    parts = words.split()
    if len(parts) == 1:
        return NUMBER_WORDS.get(parts[0])
    if len(parts) == 2 and parts[0] in ("twenty", "thirty", "forty", "fifty") \
            and NUMBER_WORDS.get(parts[1], 10) < 10:
        return NUMBER_WORDS[parts[0]] + NUMBER_WORDS[parts[1]]
    return None


def _a_unit(word: str, before: str) -> bool:
    """Whether a word is a unit of time, said after another - see TIME_UNITS."""
    return word in TIME_UNITS and (word != "second" or before.isdecimal() or before in NUMBER_WORDS
                                   or before in TIME_LEADS)


def _says_a_time(words: List[str]) -> bool:
    """Whether a thing's words hold a time - "the TV in five minutes", "the oven at six"."""
    for before, word, after in zip([""] + words, words, words[1:] + [""]):
        if _a_unit(word, before):
            return True
        if word in TIME_LEADS and (after.isdecimal() or after in NUMBER_WORDS or _a_unit(after, word)
                                   or after == "half"):
            return True
    return False


def _thing_points_back(words: List[str]) -> bool:
    """
    Whether a thing for Assist has a word in it which only the conversation so far could
    say the meaning of - "it in five minutes", "the other one", "the second" - but not
    "fan one" or "the second scene".

    Args:
        words: The thing's words, e.g. ["fan", "one"].
    """
    if len(words) == 1 and words[0] in WHICH_OF_SEVERAL:
        return True
    return any(word in PRONOUNS and (word != "one" or before == "" or before in WHICH_OF_SEVERAL)
               for before, word in zip([""] + words, words))


def calendar_days(day: Optional[str], today: date) -> Tuple[date, int]:
    """
    The days a plainly named day means.

    Args:
        day: e.g. "tomorrow", "thursday", "this weekend", or None for today.
        today: Today's date.

    Returns:
        (first day, how many days) - a weekday is the next one, or today if it's
        that day now, and the weekend is what's left of it.
    """
    if day in (None, "today", "tonight"):
        return today, 1
    if day == "tomorrow":
        return today + timedelta(days=1), 1
    if day in ("this weekend", "the weekend"):
        if today.weekday() == 6:
            return today, 1
        if today.weekday() == 5:
            return today, 2
        return today + timedelta(days=5 - today.weekday()), 2
    return today + timedelta(days=(WEEKDAYS.index(day) - today.weekday()) % 7), 1


def _tidied(text: str) -> str:
    """Words as the rules see them: lower case, letters, digits and apostrophes, single-spaced."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", text.lower())).strip()


# The letters which Unicode doesn't decompose into a plain letter and an accent, as they are written plain.
_UNACCENTED = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d"})


def _plain(text: str) -> str:
    """Text with its accents taken off, in lower case: "Atlético" is "atletico", "Bodø" is "bodo"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char)).translate(_UNACCENTED)


def _turned_off(found) -> bool:
    """
    Whether a mute rule's words turn something off - "mute", "turn the sound off", "stop
    listening" - rather than back on: "unmute", "turn it on", "start listening".
    """
    state = found.groupdict().get("state")
    if state:
        return state in ("off", "stop")
    return not found.groupdict().get("un")


def _ordinal(day: int) -> str:
    """A day of the month as it's said, e.g. 4 -> "4th"."""
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


class LocalIntents:
    """Recognises the everyday commands handled without Claude."""

    def __init__(self, clock=None, ntfy=None, home=None, activities: Optional[Callable[[], Dict[str, str]]] = None,
                 wake_word: str = DEFAULT_WAKE_WORD, football: Optional[Callable[[], List[str]]] = None,
                 names_satellite: Optional[Callable[[str], bool]] = None, computer: Optional[ComputerHooks] = None):
        """
        Args:
            clock: The Clock, to check there's exactly one timer before cancelling
                "the timer". Without it, that's left to Claude.
            ntfy: The user's notifications - see ntfy.py. Without it, "any
                notifications?" and the like are left to Claude.
            home: Home Assistant - see home_assistant.py. Without it, nothing is taken
                for one of Assist's own sentences, so "is the post office open?" is
                Claude's rather than "Home Assistant isn't set up yet".
            activities: Gives every word an activity is heard by, to the activity - see
                GoogleCalendar.activity_words. Asked at each match, so a calendar added
                in Google is heard from then on. Without it, the activity rules don't
                match, and Claude has them.
            football: Gives every name the teams the user follows are known by - see
                Football.known_as. Asked only once a request has the shape of one for
                Assist, so that a team is never taken for a thing of Home Assistant's.
                Without it, only the word "football" is left to Claude.
            wake_word: The wake word, e.g. "Hey Nova" - its last word, run into a command
                said in the same breath ("Novopause."), is taken off it, and so are the
                words before that ("Hey Novopause.").
            names_satellite: Says whether some words name a satellite - Brain.names_satellite.
                Without it, "play it on my Pi" and the like are left to Claude, as there's no
                telling "the PC" from "the next song".
            computer: Says which requests are a computer's to open, close or search, and whether
                this is a PC, whose bare power words are its own - see ComputerHooks. Without
                it, those are left to the rules after, or Claude.
        """
        self.clock = clock
        self.ntfy = ntfy
        self.home = home
        self.activities = activities
        self.football = football
        self.names_satellite = names_satellite
        self.computer = computer
        # How the wake word's last word starts a lone word it's been run into - and, for
        # "Nova", how Nemotron heard it so: "Nova, pause." said in one breath was "Novopause.".
        ending = (wake_word.split() or [""])[-1].lower()
        starts = [re.escape(ending)] + (["novo"] if ending == "nova" else [])
        # The wake word's other words may be heard before it, as they're said: "Hey Novopause.".
        before = _tidied(" ".join(wake_word.split()[:-1]))
        lead = rf"(?:{re.escape(before)} )?" if before else ""
        self._run_into = rf"{lead}(?:{'|'.join(starts)})(\w+)" if ending else None
        # The request as it was said, punctuation and all, while match looks at it - a
        # session's requests are matched one at a time. "Eggs, milk, bread" has commas,
        # which the rules never see.
        self._said = ""
        # Each pattern has to match the whole request, after politeness is taken off.
        self._rules = [
            # "Set of one minute timer" is how Moonshine heard "set a one minute timer"
            # with the network cable out - so it went to Claude, who couldn't be asked.
            (r"(?:(?:set|start) (?:me )?(?:a |an |of )?)?timer for (?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour)s?",
             self._timer),
            (r"(?:(?:set|start) (?:me )?(?:a |an |of )?)?(?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour) timer",
             self._timer),
            # "Soap" or "so", "scap" and "quiter" are how Moonshine heard "stop", "skip"
            # and "quieter" said on their own - see speech_to_text.py. "Cause" and
            # "paws" are how Nemotron, hearing it again said twice, heard "pause".
            (r"(?:pause|stop|soap|so|cause|paws)(?: the)?(?: music| song| spotify)?", self._playback("pause")),
            (r"(?:resume|unpause|play)(?: the)?(?: music| spotify)?|carry on playing", self._resume),
            (r"(?:skip|next|scap)(?: (?:this|the))?(?: song| track| one)?", self._playback("skip")),
            (r"(?:previous|last) (?:song|track)|go back(?: a (?:song|track))?", self._playback("previous")),
            (r"turn (?:it|the volume|the music) (?P<direction>up|down)|volume (?P<direction2>up|down)",
             self._volume_step),
            (r"louder", lambda match: self._volume({"direction": "up"})),
            (r"quieter|softer|quiter", lambda match: self._volume({"direction": "down"})),
            (r"(?:set (?:the )?)?volume (?:to )?(?P<level>\d{1,3})(?: percent)?", self._volume_level),
            # The same on a satellite he names - "volume up on the PC", "turn the Pi down", "set
            # the PC to 30", "PC volume 40" - taken only when the brain says the words name one,
            # so "set the TV volume to 20" is still Assist's and "turn the heating down" Claude's.
            (rf"turn (?:it|the volume|the music) (?P<direction>up|down) (?:on|for) {MOVE_WHERE}", self._volume_on),
            (rf"(?:turn )?(?:the )?volume (?P<direction>up|down) (?:on|for) {MOVE_WHERE}", self._volume_on),
            (rf"turn {MOVE_WHERE} (?P<direction>up|down)", self._volume_on),
            (rf"(?:the )?{MOVE_WHERE} volume (?P<direction>up|down)", self._volume_on),
            (rf"(?P<word>louder|quieter|softer) (?:on|for) {MOVE_WHERE}", self._volume_on),
            (rf"(?:set (?:the )?)?volume (?:to )?(?P<level>\d{{1,3}})(?: percent)? (?:on|for) {MOVE_WHERE}",
             self._volume_on),
            (rf"(?:set )?(?:the )?{MOVE_WHERE} volume (?:to )?(?P<level>\d{{1,3}})(?: percent)?",
             self._volume_on),
            (rf"set {MOVE_WHERE} to (?P<level>\d{{1,3}})(?: percent)?", self._volume_on),
            # Muting the sound needs the word - "mute the sound", "turn the audio off" - on the
            # satellite spoken to or one named (0034).
            (rf"(?P<un>un)?mute (?:the )?{SOUND}{ON_WHERE}", self._mute),
            # Only the sound or audio is turned off so: "turn the music off" isn't a mute.
            (rf"turn (?:the )?(?:sound|audio) (?P<state>off|on){ON_WHERE}", self._mute),
            (rf"turn (?P<state>off|on) the (?:sound|audio){ON_WHERE}", self._mute),
            # Plain "mute" is the microphone, Nova's listening: here, or on the device named. Said
            # over something ringing, or Nova talking, a bare "mute" never gets here: it stops
            # them, as "stop" does - see Conversation.handle. At a PC the mic said is its own, in
            # Windows, and a plain "mute" asks which (his rulings, 20 Nov) - see _microphone.
            (r"unmute (?:everything|all|both)", self._unmute_everything),
            (rf"(?P<un>un)?mute(?P<mic> {MIC})?{ON_WHERE}", self._microphone),
            (rf"(?P<un>un)?mute {MOVE_WHERE}", self._microphone),
            (rf"(?P<state>stop|start) listening{ON_WHERE}", self._microphone),
            (rf"turn (?P<mic>{MIC}) (?P<state>off|on){ON_WHERE}", self._microphone),
            (rf"turn (?P<state>off|on) (?P<mic>{MIC}){ON_WHERE}", self._microphone),
            (r"what(?:'s| is) the time|what time is it(?: now)?|tell me the time", self._time),
            (r"what(?:'s| is) (?:the date|today's date)(?: today)?|what day is it(?: today)?", self._date),
            (r"(?:cancel|stop|delete) (?:the|my) timer", self._cancel_only_timer),
            (r"what(?:'s| is| will) the weather(?: (?:going to )?be)?(?: like)?(?: (?P<day>today|tomorrow))?"
             r"|what(?:'s| is) the forecast(?: for)?(?: (?P<day2>today|tomorrow))?", self._weather),
            # Dimming has no sentence of Assist's own, so Nova steps the brightness
            # itself, from the lights' names in Home Assistant. "Brighten up the lights"
            # and "dim down the lamp" are said too, whose "up" and "down" aren't the name.
            (rf"(?P<direction>dim|brighten)(?:(?<=dim) down|(?<=brighten) up)? (?P<words>{LIGHT})", self._dim),
            # The lights go straight to Home Assistant, which works out which ones
            # from the words itself - and still works with the internet down. One rule
            # for each shape, so each can name its parts for Nova's own lights, which
            # switch them should the guard refuse the sentence. "Turn the lamp on"
            # comes before "lamp on", which would take "turn" for part of the name.
            (rf"(?:turn|switch) (?P<state>on|off) (?P<words>{LIGHT})", self._lights),
            (rf"(?:turn|switch) (?P<words>{LIGHT}) (?P<state>on|off)", self._lights),
            # Not "is the lamp on?" or "are the lights on?", whose "is" and "are" aren't part
            # of the name - Nova's own lights would switch them on if the guard refused it.
            # They're questions, the first for Assist's shape below.
            (rf"(?!(?:is|are)\b)(?P<words>{LIGHT}) (?P<state>on|off)", self._lights),
            (rf"set (?P<words>{LIGHT}) to (?P<brightness>\d{{1,3}})(?: percent)?", self._lights),
            # The calendar, for a day asked about by name. One rule each, since a
            # pattern can only name the day once.
            (rf"what(?:'s| is) (?:on )?(?:my )?(?:calendar|schedule|diary|agenda)(?:(?: (?:on|for))? {DAY})?",
             self._calendar),
            (rf"what(?:'s| is) (?:on|planned|happening)(?: (?:on|for))? {DAY}", self._calendar),
            (rf"what (?:have i|do i have)(?: got)?(?: on| planned| going on| happening)?(?: (?:on|for))? {DAY}",
             self._calendar),
            (rf"(?:am i|are we) (?:busy|free)(?: (?:on|for))? {DAY}", self._calendar),
            (rf"do i have anything(?: on| planned)?(?: (?:on|for))? {DAY}", self._calendar),
            # A journey - when to leave, and how long it takes - before the activity rules,
            # whose "when do I ..." would take "when do I need to leave for uni?" for an
            # activity. Only the main phrases, with no time said (see JOURNEY_PLACE): Claude
            # has the rest, with the same tool. A "how long" question is only taken when it
            # names a way of travelling, so "how long to get to work?" is Claude's, who can
            # tell work from "how long to get to sleep?".
            (rf"(?:what time|when) (?:do i|should i|shall i|do i need to|do i have to) "
             rf"(?:leave|set off|head off)(?: for| to get to) {JOURNEY_PLACE}",
             self._journey("driving", leave=True)),
            (rf"how long (?:is|will be) the (?P<verb>drive|walk|cycle) to {JOURNEY_PLACE}", self._journey()),
            (rf"how long (?:does it take |will it take )?to (?P<verb>drive|walk|cycle) to {JOURNEY_PLACE}",
             self._journey()),
            (rf"how long (?:does it take |will it take )?to get to {JOURNEY_PLACE} {JOURNEY_BY}",
             self._journey("transit")),
            # Work, uni and the like, from the calendar - "what time do I have work
            # tomorrow?". Without a day, it's today.
            # "When's gym tomorrow?" too.
            (rf"(?:(?:what time|when) (?:do i|am i|is)(?: have| start| finish| have to be at| got)?|when's) "
             rf"(?:at |in )?{ACTIVITY}(?:(?: on| for)? {DAY})?", self._activity_rule(self._activity)),
            (rf"(?:am i|do i have|have i got) (?:at |in )?{ACTIVITY}(?:(?: on| for)? {DAY})?",
             self._activity_rule(self._activity)),
            # When it's next - "when's my next shift?" is just the one, "what are my
            # next three shifts?" three.
            (rf"when (?:do i|am i) next (?:have |at |in |got )?{ACTIVITY}", self._activity_rule(self._next_activity)),
            (rf"(?:when|what)(?:'s| is) my next {ACTIVITY}", self._activity_rule(self._next_activity)),
            (rf"(?:when|what)(?:'re| are) my next (?P<count>[a-z0-9]+) {ACTIVITY}",
             self._activity_rule(self._next_activity)),
            # Notifications - see ntfy.py.
            (r"(?:are there |have i got |do i have )?any (?:new )?notifications|what(?:'s| are) my notifications"
             r"|read (?:me )?my notifications", self._notifications),
            (r"tell me more|what was that(?: about)?", self._tell_more),
            # The batteries - see batteries.py. A device is only the one word, so "what's on my
            # phone" or "how's my phone's screen" are never taken for a battery.
            (rf"(?:how(?:'s| is)|what(?:'s| is)) (?:the |my )(?P<device>{BATTERY_DEVICE})(?:'s)? battery"
             rf"(?: level| percentage)?|what(?:'s| is) (?:the |my )(?P<device2>{BATTERY_DEVICE}) (?:on|at)",
             self._batteries),
            (r"(?:what(?:'s| are| is) (?:the |my )?)?battery levels?|how are (?:all )?my batteries(?: doing)?",
             self._batteries),
            # Where the phone is, and ringing it - see devices.py. Only the one word, as for a
            # battery, so "where's my phone charger?" is Claude's.
            (rf"where(?:'s| is) (?:my |the )(?P<device>{FOUND_DEVICE})", self._find_device(False)),
            (rf"(?:find|ring) (?:my |the )(?P<device>{FOUND_DEVICE})", self._find_device(True)),
            # The music, moved to a satellite he names - before the sending of a reply to a device
            # below, whose "put it on my Mac" is the Mac's, not a satellite's.
            (rf"{MOVE_TO} {MOVE_WHERE}", self._move_music),
            (r"(?:send|text) (?:that|it|this) to (?:my phone|me)|text me (?:that|it)"
             r"|(?:send|put) (?:that|it|this) on my phone", self._send_to_phone),
            # The Mac or the tablet, through the Home Assistant app - only the device itself, said
            # plainly, so "put it on my iPad's list" is left alone.
            (r"(?:send|text|put) (?:that|it|this) (?:to|on) (?:my |the )(?P<device>mac|macbook|laptop|tablet|ipad)",
             self._send_to_device),
            # The PC - see pc.py. Before the lists, whose "put X to Y" would take "put my PC to
            # sleep" for adding to a list called sleep, and before Assist's shapes, so "lock my
            # PC" is never Assist's and "is my PC on?" is answered from HASS.Agent's entities.
            (rf"(?:turn|switch) on {PC}|(?:turn|switch) {PC} on|wake(?: up)? {PC}", self._pc("on")),
            (rf"(?:turn|switch) off {PC}|(?:turn|switch) {PC} off|shut down {PC}|shut {PC} down", self._pc("off")),
            (rf"(?:restart|reboot) {PC}", self._pc("restart")),
            (rf"put {PC} to sleep", self._pc("sleep")),
            (rf"turn off {PC}(?:'s)? (?:screen|monitor)", self._pc("screen_off")),
            (rf"lock {PC}", self._pc("lock")),
            (rf"is {PC} on", self._pc("status")),
            # Bare power words, said at the PC itself and only as the whole request - so "restart
            # the song" and "I'm going to sleep after this" never are. At the Pi, as they were.
            (r"(?:go to )?sleep", self._at_pc("sleep")),
            (r"lock(?: it| the pc| the computer| my pc| my computer)?", self._at_pc("lock")),
            (r"(?:turn )?(?:off )?the screen off|screen off|turn off the screen", self._at_pc("screen_off")),
            (r"shut down|shut it down|power off", self._at_pc("off")),
            (r"restart|reboot", self._at_pc("restart")),
            # Opening, closing and searching on a computer - see computers.py. Each is only a
            # computer's when the hooks say so, so "open the garage" and "close the blinds" go on to
            # Assist, below, and "start a timer" was a timer above. A web address first, so an app
            # of its name wins over it, then "close this" before any program called "this".
            (rf"(?:open|go to) (?P<site>[a-z0-9]+(?: [a-z0-9]+)+?){ON_WHERE}", self._open_site),
            # "Start" needs an app's whole name, or his own for one, so "start the music" at the PC
            # never opens the one app with "music" in its name.
            (rf"(?:open|launch|load up) (?P<app>.+?){ON_WHERE}", self._open_app(partly=True)),
            (rf"start (?P<app>.+?){ON_WHERE}", self._open_app(partly=False)),
            # Only "close" for the window in front: "quit it" or "exit that" is about something said before.
            (rf"close (?:{FRONT_WINDOW}){ON_WHERE}", self._close_this),
            (rf"(?:close|quit|exit) (?P<app>.+?){ON_WHERE}", self._close_app),
            # A plain "search for" is the computer's only at one, or with one named: at the Pi it's
            # Claude's own web search, as it was. With Google said, it's the computer's anywhere.
            (rf"search for (?P<words>.+?){ON_WHERE}", self._search(site_said=False)),
            (rf"(?:search google|google) for (?P<words>.+?){ON_WHERE}", self._search(site_said=True)),
            (rf"google (?P<words>.+?){ON_WHERE}", self._search(site_said=True)),
            (rf"look up (?P<words>.+?) on google{ON_WHERE}", self._search(site_said=True)),
            # The to-do lists - see lists.py. Assist is asked first for the adding and
            # ticking off, but only with its own list intents (see ActionExecutor), so
            # "take milk off my shopping list", which Assist reads as removing it, is only
            # ever ticked off.
            (rf"(?:add|put) (?P<item>.+) {LIST_LEAD}(?P<list>{LISTWORDS})", self._add_to_list),
            (rf"what(?:'s| is) on (?P<list>{LISTWORDS})", self._read_list),
            (rf"read (?:me )?(?P<list>{LISTWORDS} list)", self._read_list),
            (rf"tick (?!off )(?P<item>.+) off (?P<list>{LISTWORDS})", self._tick_off_list),
            (rf"tick (?:off )?(?P<item>.+?)(?: off)?(?: (?:on|from) (?P<list>{LISTWORDS}))?", self._tick_off_list),
            (rf"(?:take|cross) (?P<item>.+?) off (?P<list>{LISTWORDS})", self._take_off_list),
            # "I've got the milk" - and "I've got the flu", which only ticks off "flu" itself,
            # so a sentence about the world reaches Claude. Assist has no sentence like it.
            (r"i(?:'ve| have) got (?:the |some )?(?P<item>.+)", self._got),
            # Anything else of Home Assistant's - switches, media players, scripts and
            # scenes - goes to Assist, last, so that each of Nova's own commands is
            # matched first: "pause" is Nova's music, "pause the TV" Assist's. It's only
            # sent on the wider rule (see HomeAssistant.command), after the guard.
            (rf"(?:turn|switch) (?:on|off) {THING}", self._assist),
            (rf"(?:turn|switch) {THING} (?:on|off)", self._assist),
            (rf"(?:pause|resume|unpause) {THING}", self._assist),
            (rf"(?:next|previous) (?:track|song) on {THING}", self._assist),
            (rf"set (?:the )?{THING} volume to \d{{1,3}}(?: percent)?", self._assist),
            (rf"is {THING} (?:on|off|open|closed|playing|paused)", self._ask),
            (rf"(?:activate|run) {THING}", self._assist),
            (rf"(?:open|close) {THING}", self._assist),
        ]

    def match(self, text: str, only: Optional[Sequence[str]] = None) -> Optional[Dict]:
        """
        Handles a request locally, if it's one of the everyday commands.

        Args:
            text: What the user said.
            only: The tools a rule may give, or None for any - so a rule giving any other
                is passed over, e.g. ["find_device"] while Nova listens for an answer.

        Returns:
            A result shaped like LLMHandler.process_query's - a "response" and a list
            of "actions" - or None to leave the request to Claude.
        """
        request = _tidied(text)
        request = re.sub(POLITE_END, "", re.sub(POLITE_START, "", request)).strip()
        self._said = text

        for pattern, handle in self._rules:
            found = re.fullmatch(pattern, request)
            if not found:
                continue
            result = handle(found)
            if result is not None and only is not None and not (
                    result["actions"] and all(action.tool in only for action in result["actions"])):
                continue
            if result is not None:
                # Without a note's text, which is never logged - see actions.NOT_LOGGED.
                asked = [action.for_log() for action in result["actions"]]
                logger.info(f"Handled locally: '{text}'" + (f" (actions: {asked})" if asked else ""),
                            extra=TRANSCRIPT)
                return result

        # A lone word which is the wake word run into a command, tried as the command - only
        # kept if that's one in full, so "Novacane." is still Claude's. The wake word's
        # list of commands is the rules themselves, rather than a second one to keep.
        run_into = re.fullmatch(self._run_into, request) if self._run_into else None
        if run_into:
            result = self.match(run_into[1], only=only)
            if result is not None:
                logger.info(f"Took '{text}' for the wake word run into '{run_into[1]}'", extra=TRANSCRIPT)
            return result
        return None

    @staticmethod
    def _result(response: str, actions: Optional[List[Action]] = None) -> Dict:
        return {"response": response, "actions": actions or [], "success": True}

    def _timer(self, found) -> Optional[Dict]:
        amount = found["amount"]
        # "Half an hour" is the one fraction said often enough to be worth knowing.
        if amount in ("half an", "half a") and found["unit"] == "hour":
            seconds = 1800
        else:
            number = _number(amount)
            if number is None:
                return None
            seconds = number * UNIT_SECONDS[found["unit"]]
        return self._result("Okay.", [Action("set_timer", {"duration_seconds": seconds})])

    def _playback(self, command: str):
        return lambda found: self._result("Okay.", [Action("control_playback", {"action": command})])

    def _journey(self, mode: Optional[str] = None, leave: bool = False):
        """
        The handler for a journey question.

        Args:
            mode: How it's made, or None to take it from the rule's "verb" - drive, walk or cycle.
            leave: Whether it's when to leave that was asked, and not how long it takes.
        """
        def handle(found) -> Dict:
            how = mode or JOURNEY_MODES[found["verb"]]
            # All three keys, always: the place as said, the zones being found by the journeys module.
            return self._result("Okay.", [Action("journey", {"place": found["place"], "mode": how, "leave": leave})])
        return handle

    def _resume(self, found) -> Dict:
        # A play with nothing named carries on with whatever was playing.
        return self._result("Okay.", [Action("play_music")])

    def _move_music(self, found) -> Optional[Dict]:
        # Not a move at all unless the words name a satellite: "change it to 50" goes on to Claude.
        if self.names_satellite is None or not self.names_satellite(found["where"]):
            return None
        return self._result("Okay.", [Action("move_music", {"to": found["where"]})])

    def _volume(self, change: Dict) -> Dict:
        # Which volume is left to the executor, which turns the music down if it's
        # playing and the assistant's voice down if not.
        return self._result("Okay.", [Action("set_volume", change)])

    def _volume_step(self, found) -> Dict:
        return self._volume({"direction": found["direction"] or found["direction2"]})

    def _volume_level(self, found) -> Optional[Dict]:
        level = int(found["level"])
        return self._volume({"level": level}) if level <= 100 else None

    def _names_a_satellite(self, where: str) -> bool:
        return self.names_satellite is not None and self.names_satellite(where)

    def _volume_on(self, found) -> Optional[Dict]:
        # Only a satellite's: anything else falls through to the rules after. "The PC's
        # volume" is the PC's.
        where = re.sub(r"'s$", "", found["where"])
        if not self._names_a_satellite(where):
            return None
        groups = found.groupdict()
        if groups.get("level") is not None:
            level = int(groups["level"])
            if level > 100:
                return None
            change = {"level": level}
        elif groups.get("word"):
            change = {"direction": "up" if groups["word"] == "louder" else "down"}
        else:
            change = {"direction": groups["direction"]}
        return self._volume({**change, "where": where})

    def _mute(self, found) -> Optional[Dict]:
        where = found.groupdict().get("where")
        if where and not self._names_a_satellite(where):
            return None
        return self._volume({"mute": _turned_off(found), **({"where": where} if where else {})})

    def _unmute_everything(self, found) -> Optional[Dict]:
        # Only at a PC which can reach Windows' sound: its microphone and its sound both (his ruling,
        # 20 Nov). Elsewhere it falls through to the rules after.
        if self.computer is None or not self.computer.mic_here():
            return None
        return self._result("Okay.", [Action("unmute_everything")])

    def _microphone(self, found) -> Optional[Dict]:
        # Only a satellite's: "mute the TV" falls through to the rules after.
        groups = found.groupdict()
        where = groups.get("where")
        if where and not self._names_a_satellite(where):
            return None
        muted = _turned_off(found)
        # At a PC which can reach Windows' sound, with no device named: "mute the mic" is its own
        # microphone, and a plain "mute" could be that or the sound, so it asks. "Stop listening"
        # is still the room's always-on satellite's.
        if not where and self.computer is not None and self.computer.mic_here() \
                and groups.get("state") not in ("stop", "start"):
            return self._result("Okay.", [Action("mute_computer_mic" if groups.get("mic") else "mute_which",
                                                 {"muted": muted})])
        return self._result("Okay.", [Action("mute_microphone", {"muted": muted,
                                                                 **({"where": where} if where else {})})])

    def _time(self, found) -> Dict:
        now = datetime.now()
        return self._result(f"It's {spoken_clock_time(now.hour, now.minute)}.")

    def _date(self, found) -> Dict:
        now = datetime.now()
        return self._result(f"It's {now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B')}.")

    def _lights(self, found) -> Dict:
        # Setting a brightness switches a light on - bar 0%, which Home Assistant takes
        # as off, so Nova's own lights don't switch it on at 1% instead.
        brightness = found.groupdict().get("brightness")
        level = int(brightness) if brightness is not None else None
        on = found["state"] == "on" if level is None else level > 0
        return self._result("Okay.", [Action("control_home", {
            "command": found.string, "on": on, "words": found["words"], "brightness": level})])

    def _dim(self, found) -> Dict:
        direction = "down" if found["direction"] == "dim" else "up"
        return self._result("Okay.", [Action("dim_lights", {"direction": direction, "words": found["words"]})])

    def _assist(self, found, question: bool = False) -> Optional[Dict]:
        # A pronoun anywhere, not only alone, as in "pause it in five minutes" - which
        # isn't a thing called "it in five minutes" - bar "one" (see WHICH_OF_SEVERAL).
        words = found["thing"].split()
        if self.home is None or _says_a_time(words) or _thing_points_back(words) or any(
                word in NOVAS_OWN or word in NOT_A_THING for word in words):
            return None
        # Football is Claude's, which has get_football - Assist would take "Is Liverpool
        # playing?" for a media player. Checked here rather than put in NOVAS_OWN, which
        # also decides which calendars are heard (see _activity_words), so that a calendar
        # called Football keeps working.
        if "football" in words or self._names_a_team(words):
            return None
        asked = {"command": found.string, "wider": True, **({"question": True} if question else {})}
        return self._result("Okay.", [Action("control_home", asked)])

    def _names_a_team(self, words: List[str]) -> bool:
        """
        Whether the words have the name of a team the user follows, as whole words - "south
        shields" in "is the south shields game on", but not "liverpool" in "liverpoolish".
        Each name is tidied as a request is, and heard with its accents as well as without
        them: the speech recogniser may write "Atlético Madrid" either way, and tidied as it
        is "Atletico Madrid" is "atl tico madrid" only.

        Args:
            words: The thing a request is about, as the rules see it.

        Returns:
            True if one of the teams is in them, False without any, or without football.
        """
        if self.football is None:
            return False
        said = f" {' '.join(words)} "
        # Both forms of each name, as a request with its accents is split by _tidied in the same places.
        heard = {_tidied(form) for name in self.football() for form in (name, _plain(name))}
        return any(f" {tidy} " in said for tidy in heard if tidy)

    def _ask(self, found) -> Optional[Dict]:
        # Said to be a question, so that one the guard refuses - "is the garage door
        # open?" - goes to Claude, which can read it, rather than being refused.
        return self._assist(found, question=True)

    def _listed(self, found, tool: str, item: Optional[str] = None, **params) -> Optional[Dict]:
        """
        A list's action - None without Home Assistant, for a thing which only points back, or
        for list words naming one of Nova's own things or a device, a day or a time, or
        joining two requests.
        """
        said = found.groupdict().get("list")
        words = (said or "").split()
        if (self.home is None or not_a_list(said) or names_a_day(said) or {"and", "then", "plus"} & set(words)
                or (item is not None and points_back(item))):
            return None
        if item is None:
            params = {"list": said}
        else:
            # The request, for Assist to be asked first - unless the rule says otherwise.
            params = {"item": item, "list": said, **(params or {"command": found.string})}
        return self._result("Okay.", [Action(tool, params)])

    def _add_to_list(self, found) -> Optional[Dict]:
        # Several things at once - "eggs, milk and bread" - are Claude's, which splits
        # them, as is "salt and vinegar crisps", which it keeps whole.
        words = found["item"].split()
        # The thing as said, to see the commas between its words - but not one before
        # "please", as in "add milk to my list, please".
        as_said = re.search("[^a-z0-9']+".join(map(re.escape, words)), self._said.lower())
        if {"and", "plus"} & set(words) or " as well as " in f" {found['item']} " or as_said is None \
                or "," in as_said.group():
            return None
        return self._listed(found, "add_to_list", found["item"])

    def _read_list(self, found) -> Optional[Dict]:
        return self._listed(found, "read_list")

    def _tick_off_list(self, found, **params) -> Optional[Dict]:
        # "Tick milk off my calendar" is the shape before, which named no list - not a
        # thing called "milk off my calendar".
        if "off" in found["item"].split():
            return None
        return self._listed(found, "tick_off_list", found["item"], **params)

    def _take_off_list(self, found) -> Optional[Dict]:
        # "Take the day off work" is as everyday as "I've got the flu", so unless the words say
        # it's a list, only the thing itself is ticked off - never "Day rota" on a Work list.
        return self._tick_off_list(found, command=found.string, exact=not names_list(found["list"]))

    def _got(self, found) -> Optional[Dict]:
        return self._listed(found, "tick_off_list", found["item"], list=None, exact=True)

    def _calendar(self, found) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_calendar", {"start": first.isoformat(), "days": days})])

    def _activity_rule(self, handle: Callable) -> Callable:
        """
        An activity rule's handler, which matches the request again with the activities'
        words in the rule's ACTIVITY - the request having already matched with any words
        there, so a request of another shape never has the calendar's names asked for.
        It's None, for Claude, without the calendar, or when none of its activities is there.
        """
        def matched(found) -> Optional[Dict]:
            words = self._activity_words()
            if not words:
                return None
            pattern = found.re.pattern.replace(ACTIVITY, "(?P<activity>" + "|".join(
                re.escape(word) for word in sorted(words, key=len, reverse=True)) + ")")
            again = re.fullmatch(pattern, found.string)
            if not again:
                return None
            name = _tidied(words[again["activity"]]).split()
            first_word_alone = len(name) > 1 and again["activity"] == name[0]
            if (first_word_alone and not again.groupdict().get("day")
                    and re.fullmatch(BARE_BEFORE_ACTIVITY, found.string[:again.start("activity")])):
                return None
            return handle(again, words)
        return matched

    def _activity_words(self) -> Dict[str, str]:
        """
        The words the activities are heard by, without those for Nova's own things - a
        calendar called Timer or Music is Claude's to ask about, as Claude has both, so
        it never takes over "when's my next timer?". See ACTIVITY.
        """
        if self.activities is None:
            return {}
        own = NOVAS_OWN | NOT_A_LIST
        return {word: activity for word, activity in self.activities().items()
                if word and not any(part in own or part.rstrip("s") in own for part in word.split())}

    def _activity(self, found, words: Dict[str, str]) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_activity", {"activity": words[found["activity"]],
                                                              "start": first.isoformat(), "days": days})])

    def _next_activity(self, found, words: Dict[str, str]) -> Optional[Dict]:
        count = _number(found.groupdict().get("count") or "one")
        if not count:
            return None
        return self._result("Okay.", [Action("get_activity", {"activity": words[found["activity"]],
                                                              "start": date.today().isoformat(), "count": count})])

    def _weather(self, found) -> Dict:
        day = found["day"] or found["day2"] or "today"
        return self._result("Okay.", [Action("get_weather", {"day": day})])

    def _cancel_only_timer(self, found) -> Optional[Dict]:
        # Only when there's exactly one, so there's no question which was meant.
        if not self.clock or self.clock.timer_count() != 1:
            return None
        return self._result("Okay.", [Action("cancel_timer")])

    def _batteries(self, found) -> Dict:
        device = found.groupdict().get("device") or found.groupdict().get("device2")
        return self._result("Okay.", [Action("get_batteries", {"device": device} if device else {})])

    def _find_device(self, ring: bool):
        return lambda found: self._result("Okay.", [Action("find_device", {"device": found["device"], "ring": ring})])

    def _notifications(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("read_notifications")]) if self.ntfy else None

    def _tell_more(self, found) -> Optional[Dict]:
        # Only about an alert said a moment ago. Otherwise it's a follow-up to whatever
        # was being talked about - "tell me more" about the weather - which is Claude's.
        if not self.ntfy or not self.ntfy.just_said():
            return None
        return self._result("Okay.", [Action("tell_me_more")])

    def _send_to_device(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("send_to_phone", {"device": found["device"]})]) if self.home else None

    def _pc(self, action: str):
        return lambda found: self._result("Okay.", [Action("pc_power", {"action": action})])

    def _at_pc(self, action: str):
        """The handler for a bare power word, which is the PC's only when said at it."""
        def handle(found) -> Optional[Dict]:
            if self.computer is None or not self.computer.at_pc():
                return None
            return self._result("Okay.", [Action("pc_power", {"action": action})])
        return handle

    def _computer_words(self, found, name: str) -> Tuple[str, Optional[str]]:
        """
        What a request for a computer is about, and where.

        Args:
            found: The rule's match, with an optional "where".
            name: The group with what it's about, e.g. "app".

        Returns:
            (what, the words for where or None) - where only if they name a satellite, else
            they're part of what: "search for pizza on deliveroo" is all one search.
        """
        what, where = found[name], found["where"]
        if where and not (self.names_satellite is not None and self.names_satellite(where)):
            return f"{what} on {where}", None
        return what, where

    @staticmethod
    def _computer_action(tool: str, params: Dict, where: Optional[str]) -> Dict:
        return LocalIntents._result("Okay.", [Action(tool, {**params, **({"where": where} if where else {})})])

    def _open_site(self, found) -> Optional[Dict]:
        if self.computer is None:
            return None
        # Only a web address said with its dots, as the speech recogniser wrote it.
        sites = [site for site in DOMAIN.findall(self._said.lower()) if _tidied(site) == found["site"]]
        where = found["where"]
        if not sites or (where and not (self.names_satellite is not None and self.names_satellite(where))):
            return None
        site = sites[0]
        # His app of the same name wins: "go to youtube.com" opens his YouTube app, but "go to google.com"
        # isn't Google Chrome. Not with a computer named, where the page is opened.
        name = next((part for part in site.split(".") if part != "www"), site)
        if not where and self.computer.app_called(name):
            return self._computer_action("open_on_computer", {"name": name}, None)
        if not where and not self.computer.opens_somewhere():
            return None
        return self._computer_action("open_web_page", {"url": site}, where)

    def _open_app(self, partly: bool):
        """The handler for opening an app, which says whether the one name with every word said counts."""
        def handle(found) -> Optional[Dict]:
            if self.computer is None:
                return None
            app, where = self._computer_words(found, "app")
            return self._computer_action("open_on_computer", {"name": app}, where) \
                if self.computer.names_app(app, where, partly) else None
        return handle

    def _close_this(self, found) -> Optional[Dict]:
        # The window in front: the PC's when said at it, or a computer's when it's named.
        if self.computer is None:
            return None
        where = found["where"]
        if where:
            if self.names_satellite is None or not self.names_satellite(where):
                return None
        elif not self.computer.at_pc():
            return None
        return self._computer_action("close_on_computer", {"name": "this"}, where)

    def _close_app(self, found) -> Optional[Dict]:
        if self.computer is None:
            return None
        app, where = self._computer_words(found, "app")
        # "Quit it" or "exit the window" isn't a program called that.
        if re.fullmatch(FRONT_WINDOW, app.strip()):
            return None
        return self._computer_action("close_on_computer", {"name": app}, where) \
            if self.computer.names_window(app, where) else None

    def _search(self, site_said: bool):
        """The handler for a search, which says whether the search site was said ("search Google for")."""
        def handle(found) -> Optional[Dict]:
            if self.computer is None:
                return None
            words, where = self._computer_words(found, "words")
            # "Search for it" is about something said before, which only Claude knows.
            if words.strip() in PRONOUNS:
                return None
            if not where:
                if not self.computer.opens_somewhere():
                    return None
                if not site_said and not self.computer.opens_here():
                    return None
            return self._computer_action("search_on_computer", {"words": words}, where)
        return handle

    def _send_to_phone(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("send_to_phone")]) if self.ntfy else None
