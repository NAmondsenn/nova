"""
Football Watch Module
What Nova says by itself about a followed team's match, from decision 7 of docs/decisions/0014.

Two pure parts: FootballChoices, which keeps which announcements are switched on for which
team and what was last seen of each match, and events, which says what a change in a match
means. The third is FootballWatcher, the thread which reads Home Assistant (see football.py
for what it says and when it can't be trusted), works out with events what is due, and says it.

Neither FootballChoices nor events reads the clock or Home Assistant: events is given the time
and the match, so it says the same in any time zone, and says only what the match shows - the
names and scores Home Assistant gave, never a scorer or a result worked out from anything else.
"""

import copy
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.choices import ChoiceStore
from voice_assistant.football import Football, FootballUnavailable, Match, Reading, _is_whole

logger = logging.getLogger(__name__)

# What can be announced, in the order the page lists the switches.
KINDS = ("starting", "goals", "half_time", "second_half", "full_time")

# Seconds between looks at the followed teams' matches, and minutes before kick-off from which a match
# is said to be starting, where the settings give neither.
CHECK_EVERY = 30
KICKOFF_MINUTES = 15

# Seconds after kick-off that what was seen of a match is kept: nothing is said of one so long over.
KEEP_FOR = 86400

# Seconds old what was last seen of a match may be and still be compared with. After longer,
# whatever changed meanwhile isn't announced: Nova was restarted, or Home Assistant or that team's
# data was down, and a goal an hour old isn't news.
GAP = 300

# Seconds a lower score has to hold, from the first reading which showed it, before it is said to be a goal ruled
# out. Two readings a minute apart can both carry one old copy of the match from ESPN, so a second look at the
# score going down is no proof of it.
RULED_OUT_AFTER = 120

# The phases of a match which has started, as the Contract has them. One Home Assistant's side has
# added since is one whose meaning isn't known, so it counts as none.
_PHASES = ("first_half", "half_time", "second_half", "extra_time", "penalties", "full_time", "off")

# The order a match goes through its states, and, bar "off", its phases. A look which shows an
# earlier one than the last is an old copy of the match (see events).
_MATCH_STATES = ("pre", "in", "post")
_PHASE_ORDER = ("first_half", "half_time", "second_half", "extra_time", "penalties", "full_time")


def _is_number(value) -> bool:
    """Whether a value is a number - not a bool, which Python counts as one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_waiting(waiting) -> bool:
    """
    Whether something is the note of a lower score waiting for a later reading to confirm it, as _goals
    makes it: the two scores, each a whole number or None, and when Home Assistant wrote the reading
    which first showed them - {"home": 1, "away": None, "reported": 1800000000.0}.
    """
    return (isinstance(waiting, dict) and set(waiting) == {"home", "away", "reported"}
            and all(waiting[key] is None or _is_whole(waiting[key]) for key in ("home", "away"))
            and _is_number(waiting["reported"]))


def _is_record(record) -> bool:
    """
    Whether something read from the file has the shape of events' record. One which hasn't (the
    file was edited by hand) is better taken for a match never seen than made to fail every look.
    """
    if not isinstance(record, dict):
        return False
    try:
        return (
            record["state"] in _MATCH_STATES
            and (record["phase"] is None or isinstance(record["phase"], str))
            and all(record[key] is None or _is_whole(record[key]) for key in ("home", "away"))
            and (record["waiting"] is None or _is_waiting(record["waiting"]))
            and isinstance(record["starting"], bool)
            and _is_number(record["at"])
            and _is_number(record["kickoff"])
        )
    except KeyError:
        return False


def _check_kind(kind: str):
    """
    Raises:
        ValueError: If the kind isn't one of KINDS, with a sentence the page can show.
    """
    if kind not in KINDS:
        raise ValueError(f"There's no '{kind}' switch.")


def _apart_from_when(record: Dict) -> Dict:
    """A record without "at", the time of the look: all there is to tell two looks apart but when they were."""
    return {key: value for key, value in record.items() if key != "at"}


class FootballChoices:
    """
    Which announcements are on for each followed team, and what was last seen of each match.

    Both are kept in one file, so a restart neither forgets a switch nor says a match's
    announcements twice or makes up for ones it missed. A team Home Assistant stops following
    keeps its switches, in case it comes back. The watcher's thread remembers what it has seen
    while the settings page's thread changes the switches, so every read and change goes through
    the store's lock: a save from one can't lose the other's change.

    The file holds each match as it was when last written. A look at a match changes its record
    only in when it was made, almost always, so that is kept in memory and the file is written
    only when something else changes - not every 30 seconds for every fixture, all day.
    """

    def __init__(self, path: str):
        """
        Args:
            path: The file to keep the switches and the matches seen in, e.g. football.json.
        """
        self._store = ChoiceStore(path, {"teams": {}, "seen": {}})
        self._lock = self._store.lock
        # The newest record of each match looked at since Nova started, "at" and all. The store's
        # are what the file holds, which can be older in "at" alone, or - after a failed save - in more.
        self._newest: Dict[str, Dict] = {}

    @property
    def _state(self) -> Dict:
        """The store's live state, which is only read under the lock and never changed in place."""
        return self._store.state

    def switches(self, team_id: str) -> Dict[str, bool]:
        """
        Says which announcements are on for a team.

        Args:
            team_id: ESPN's id for the team.

        Returns:
            Every kind, each on unless it was switched off. A team nothing was ever changed
            for has them all on.
        """
        with self._lock:
            entry = self._state["teams"].get(str(team_id))
            chosen = entry.get("switches") if isinstance(entry, dict) else None
            if not isinstance(chosen, dict):
                chosen = {}
            # Only a real switch counts: True == 1, and a hand-edited "no" is no choice at all.
            return {kind: chosen[kind] if isinstance(chosen.get(kind), bool) else True for kind in KINDS}

    def enabled(self, team_id: str, kind: str) -> bool:
        """
        Says whether one kind of announcement is on for a team.

        Args:
            team_id: ESPN's id for the team.
            kind: One of KINDS.

        Returns:
            Whether it is on - it is, until switched off.

        Raises:
            ValueError: If the kind isn't one of KINDS.
        """
        _check_kind(kind)
        return self.switches(team_id)[kind]

    def set(self, team_id: str, name: str, kind: str, on: bool):
        """
        Switches one kind of announcement on or off for a team, and saves.

        Args:
            team_id: ESPN's id for the team, which the switches are kept by.
            name: The team's name, kept beside them so that the file can be read by hand.
            kind: One of KINDS.
            on: Whether it is to be said.

        Raises:
            ValueError: If the kind isn't one of KINDS, with a sentence the page can show.
                Nothing is changed then.
            OSError: If it couldn't be saved. Nothing is changed then either.
        """
        _check_kind(kind)
        team_id = str(team_id)
        with self._lock:
            def change(state: Dict):
                entry = state["teams"].get(team_id)
                if not isinstance(entry, dict):
                    entry = state["teams"][team_id] = {}
                if not isinstance(entry.get("switches"), dict):
                    entry["switches"] = {}
                entry["name"] = name
                entry["switches"][kind] = bool(on)
            self._store.change(change)

    def _held(self, match_id: str):
        """The newest record there is of a match, whatever shape it is in: the one looked at, else the file's."""
        return self._newest.get(match_id, self._state["seen"].get(match_id))

    def seen(self, match_id: str) -> Optional[Dict]:
        """
        Gives what was last seen of a match.

        Args:
            match_id: The match's id: ESPN's, or for a match from BBC Sport its id beginning "bbc:".

        Returns:
            A deep copy of the record events() gave last, as it holds a dict, or None if the match was
            never seen, or what the file holds for it isn't a record. After a restart that is the one the
            file holds, which is old in "at", so that the first look says nothing of what changed meanwhile.
        """
        with self._lock:
            record = self._held(str(match_id))
            return copy.deepcopy(record) if _is_record(record) else None

    def remember(self, match_id: str, record: Dict):
        """
        Keeps what was seen of a match. It is kept in memory at once, and saved only if it
        differs from the record last saved in something other than "at". A save which fails
        keeps it in memory alone, so the match isn't announced again every look - only after
        a restart - and is tried again at the next look, which is when it can be.

        Args:
            match_id: The match's id: ESPN's, or for a match from BBC Sport its id beginning "bbc:".
            record: What events() gave. It is copied, all the way down, so changing it later changes nothing here.

        Raises:
            TypeError: If the record has something JSON can't hold. Nothing is changed then.
        """
        match_id = str(match_id)
        record = copy.deepcopy(record)
        with self._lock:
            saved = self._state["seen"].get(match_id)
            if not (_is_record(saved) and _apart_from_when(saved) == _apart_from_when(record)):
                try:
                    self._store.change(lambda state: state["seen"].__setitem__(match_id, copy.deepcopy(record)))
                except OSError as e:
                    logger.warning(f"Couldn't save what was seen of a match: {e}")
            self._newest[match_id] = record

    def forget_before(self, kickoff: float):
        """
        Drops the matches which kicked off before a time, as nothing is ever said of one so
        long over, and the file would grow with every match for ever. One whose record isn't
        readable goes too.

        Args:
            kickoff: Epoch seconds. A match which kicked off at this time is kept.
        """
        with self._lock:
            old = []
            for match_id in {*self._state["seen"], *self._newest}:
                record = self._held(match_id)
                if not _is_record(record) or record["kickoff"] < kickoff:
                    old.append(match_id)
            for match_id in old:
                self._newest.pop(match_id, None)
            saved = [match_id for match_id in old if match_id in self._state["seen"]]
            if saved:
                def drop(state: Dict):
                    for match_id in saved:
                        del state["seen"][match_id]
                self._store.keep(drop, "forgotten matches")


@dataclass(frozen=True)
class Event:
    """
    One thing to say about a match.

    Attributes:
        kind: Which switch it is under, one of KINDS.
        message: The sentence to say, in full names.
    """

    kind: str
    message: str


def _phase_of(match: Match) -> Optional[str]:
    """A match's phase if it is one of the Contract's, or None - which is what an unknown one is."""
    return match.phase if match.phase in _PHASES else None


def _score(match: Match) -> str:
    """Says a match's score, e.g. "Notts County 0, Stevenage 1." - with none given yet as 0."""
    return f"{match.home} {match.home_score or 0}, {match.away} {match.away_score or 0}."


def _minutes(count: int) -> str:
    """Says a number of minutes, e.g. "1 minute" or "15 minutes"."""
    return f"{count} minute" if count == 1 else f"{count} minutes"


def _starting(match: Match, now: float, lead: float, followed: Set[str]) -> Optional[Event]:
    """
    Says that a match which hasn't started is about to.

    Args:
        match: The match.
        now: The time, in epoch seconds.
        lead: Seconds before kick-off to say it from.
        followed: The ids of the followed teams in the match.

    Returns:
        The event, or None unless the match is still to be played - no phase, so not one
        called off, and no clock, so not one ESPN calls delayed - and kicks off within the lead.
    """
    left = match.kickoff.timestamp() - now
    if match.state != "pre" or _phase_of(match) or match.clock or not 0 < left <= lead:
        return None
    # The followed team first, as it is the one that was asked about: only if just the away side
    # is followed does the order change, so that two followed teams keep the home side first.
    first, second = match.home, match.away
    if match.away_id in followed and match.home_id not in followed:
        first, second = second, first
    return Event("starting", f"{first} play {second} in {_minutes(math.ceil(left / 60))}.")


def _goals(before: Dict, match: Match, reported: Optional[float]) -> Tuple[Optional[Event], Dict]:
    """
    Says what a change in the score means.

    A score which has gone down is not said at once, as it may be an old copy of the match
    which Home Assistant gave: the higher score is kept, and the lower one noted as waiting,
    with when Home Assistant wrote the reading which first showed it. If a reading written
    RULED_OUT_AFTER seconds or more after that one still shows that same lower score it is said,
    as a goal ruled out; one in between which shows it changes nothing, so the time is always
    counted from the first. If a reading shows anything else the note is dropped and that
    reading is compared as usual. The same reading seen again confirms nothing, as Nova looks
    more often than Home Assistant writes the sensor, so the second look is usually the first
    one's reading; and nor does the next reading, a minute on, as two readings can both carry
    the one old copy ESPN gave.

    Args:
        before: What was last seen of the match.
        match: The match as it is.
        reported: Epoch seconds when Home Assistant wrote the reading the match is from, or None
            where that isn't known: a lower score is then never noted or confirmed.

    Returns:
        The event, or None if there is nothing to say, and the scores for the record to keep:
        {"home", "away", "waiting"}, where waiting is None or {"home", "away", "reported"}. A goal
        for either side is said, so that the score heard is never behind. A goal which went in as
        another was ruled out is said as the goal, as that is the news and the score shows the rest.
    """
    here = {"home": match.home_score, "away": match.away_score}
    taken = {**here, "waiting": None}
    home, away = match.home_score or 0, match.away_score or 0
    was_home, was_away = before["home"] or 0, before["away"] or 0
    home_up, away_up = home > was_home, away > was_away
    score = _score(match)
    if home_up and away_up:
        return Event("goals", f"Goals for both sides. {score}"), taken
    if home_up or away_up:
        return Event("goals", f"Goal for {match.home if home_up else match.away}. {score}"), taken
    if home < was_home or away < was_away:
        kept = {"home": before["home"], "away": before["away"]}
        waiting = before["waiting"]
        if waiting is not None and (waiting["home"], waiting["away"]) == (here["home"], here["away"]):
            if reported is not None and reported - waiting["reported"] >= RULED_OUT_AFTER:
                return Event("goals", f"Goal ruled out. {score}"), taken
            # The reading it was first seen on, one too soon after it, or one of unknown time: the note stays as
            # it was, with the first reading's time, which is what the two minutes are counted from.
            return None, {**kept, "waiting": waiting}
        return None, {**kept, "waiting": None if reported is None else {**here, "reported": reported}}
    return None, taken


def _full_time(match: Match) -> str:
    """
    Says that a match is over, and how it ended where that was after extra time or on penalties.

    Args:
        match: A match which has reached full time.

    Returns:
        The sentence. The winner of a shoot-out is named with its own score first, however the
        sides were listed; one Home Assistant didn't name isn't worked out from the penalties. The
        penalties are said in words ("won on penalties, 4 to 3"), as digits joined by a dash are
        read out as "dash".
    """
    text = "Full time, after extra time." if match.extra_time else "Full time."
    text += " " + _score(match)
    if match.shootout:
        home_goals, away_goals = match.shootout
        if match.winner == "home":
            text += f" {match.home} won on penalties, {home_goals} to {away_goals}."
        elif match.winner == "away":
            text += f" {match.away} won on penalties, {away_goals} to {home_goals}."
        else:
            text += f" The penalty shootout finished {match.home} {home_goals}, {match.away} {away_goals}."
    return text


def _is_old_copy(before: Dict, match: Match, phase: Optional[str]) -> bool:
    """
    Whether a look is an older copy of the match than the last one: Home Assistant's side now
    and then gives one, and said as it is it would be false, then said again when the match came back.

    Args:
        before: What was last seen of the match, no more than GAP seconds ago.
        match: The match as it is.
        phase: The match's phase if it is a known one, else None.

    Returns:
        Whether the look's state is earlier than the record's, its phase is earlier (one
        which isn't known, and "off", which is in no order, never is), or it has no score where
        the record had one. A match called off is never an old copy, whichever side of it is.
    """
    if phase == "off" or before["phase"] == "off":
        return False
    if _MATCH_STATES.index(match.state) < _MATCH_STATES.index(before["state"]):
        return True
    if phase in _PHASE_ORDER and before["phase"] in _PHASE_ORDER:
        if _PHASE_ORDER.index(phase) < _PHASE_ORDER.index(before["phase"]):
            return True
    return (before["home"] is not None and match.home_score is None) or (
        before["away"] is not None and match.away_score is None)


def events(
    before: Optional[Dict], match: Match, now: float, lead: float, followed: Set[str],
    reported: Optional[float] = None
) -> Tuple[List[Event], Dict]:
    """
    Works out what to announce about a match, from what was last seen of it and how it is now.

    Starting needs no record: it is said once, whether the match is first seen 15 minutes
    away or 4, or after a gap. Everything else is a change since the last look, so it needs a
    record of one no more than GAP seconds ago, of a match which wasn't yet over - and a match
    first seen in play is taken as it stands, with nothing said for the goals so far.

    Said unprompted, a false or repeated announcement is worse than one missed, so a look
    which can't be trusted says nothing: an old copy of the match (an earlier state or phase
    than the record's, or no score where it had one) is passed over, the record keeping all
    but the time of the look; a score which has gone down waits for a reading two minutes later (see _goals);
    and a look with no known phase doesn't take the record's away, however old the record. A match
    which is off says nothing, and one which comes back from off is taken as it stands.

    Args:
        before: What was last seen of the match (the record this gave last time), or None.
        match: The match as it is.
        now: The time, in epoch seconds. Given, not read, so that this says the same in any time zone.
        lead: Seconds before kick-off to say that it is starting from.
        followed: The ids of the followed teams in the match.
        reported: Epoch seconds when Home Assistant wrote the reading the match is from, which
            is what tells a later reading from the first one seen again, and how long a score
            which has gone down has held. Without it (None), a score which has gone down is never
            said to be a goal ruled out.

    Returns:
        The events in the order to say them - goals first, then one of half time, the second
        half or full time - and the record to keep: the match as it is now, {"home", "away",
        "waiting", "phase", "state", "starting", "at", "kickoff"}, with "at" always the time of
        this look. It keeps from before whether starting has been said, and the higher score
        and the phase where the look can't be trusted to take them.
    """
    phase = _phase_of(match)
    recent = before is not None and now - before["at"] <= GAP
    if recent and _is_old_copy(before, match, phase):
        return [], {**before, "at": now}

    said: List[Event] = []
    starting = bool(before and before["starting"])
    if not starting:
        event = _starting(match, now, lead, followed)
        if event:
            said.append(event)
            starting = True

    kept_phase = phase
    scores = {"home": match.home_score, "away": match.away_score, "waiting": None}
    if before is not None and phase != "off":
        # Whatever the age of the record: a phase Home Assistant can't say now doesn't undo the one it could.
        kept_phase = phase or before["phase"]
    # A match which was off had no scores to compare with, so what it shows on coming back is taken as it stands.
    if recent and phase != "off" and before["state"] != "post" and before["phase"] != "off":
        goals, scores = _goals(before, match, reported)
        if goals:
            said.append(goals)
        if phase != before["phase"]:
            score = _score(match)
            if phase == "half_time":
                said.append(Event("half_time", f"Half time. {score}"))
            elif phase == "second_half":
                said.append(Event("second_half", f"The second half has started. {score}"))
            elif phase == "full_time":
                said.append(Event("full_time", _full_time(match)))

    record = {
        **scores,
        "phase": kept_phase,
        "state": match.state,
        "starting": starting,
        "at": now,
        "kickoff": match.kickoff.timestamp(),
    }
    return said, record


def user_home(home) -> Optional[bool]:
    """
    Says whether the user is at home, as Home Assistant has it: by the first person it lists, as the
    battery watcher takes it for a device which is nobody's, there being one user.

    Args:
        home: Home Assistant, which has people().

    Returns:
        True if that person is home, False if they are anywhere else, None if Home Assistant couldn't
        say - it couldn't be asked, lists nobody, or has no state for the person: "unknown" or
        "unavailable" isn't away.
    """
    try:
        people = home.people()
        state = people[0]["state"] if people else None
    except HOME_FAILURES as e:
        logger.warning(f"Couldn't tell whether the user is home: {e!r}")
        return None
    if state in (None, "unknown", "unavailable"):
        return None
    return state == "home"


class FootballWatcher:
    """
    Looks at the followed teams' matches every 30 seconds or so, and says what is due: that a match
    is about to start, a goal, half time, the second half and full time (decision 7 of docs/decisions/0014).

    Each is said on the default satellite, but not in quiet hours and not while Home Assistant says
    the user is out. One which can't be said is dropped and logged, never held for later.
    """

    def __init__(
        self,
        football: Football,
        choices: FootballChoices,
        say: Callable[[str], bool],
        quiet: Callable[[], bool],
        home_now: Callable[[], Optional[bool]],
        settings: Callable[[], Dict],
    ):
        """
        Args:
            football: Reads the followed teams.
            choices: Which announcements are on for each team, and what was last seen of each match.
            say: Says a message on the default satellite, returning whether it was said - it isn't
                when no satellite is connected.
            quiet: Whether it is quiet hours now.
            home_now: Whether the user is at home - None if that can't be said. See user_home.
            settings: Gives the football section of the settings as they are now, read at each use so
                that a change on the settings page is used straight away. "check_every" is the seconds
                between looks, and "kickoff_minutes" the minutes before kick-off to say a match is
                starting; without them, CHECK_EVERY and KICKOFF_MINUTES.
        """
        self._football = football
        self._choices = choices
        self._say = say
        self._quiet = quiet
        self._home_now = home_now
        self._settings = settings
        self._down = False
        # The setting each warning of one which isn't usable was about, so that it is said once and not every look.
        self._warned: Dict[str, str] = {}
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def choices(self) -> FootballChoices:
        """The switches and the matches seen, which the settings page changes."""
        return self._choices

    def check(self, now: float):
        """
        Does one look: reads the followed teams, works out what is due for each match, and says it.

        If football can't be read, that is logged once until it can, and nothing else happens. A team
        whose data is stale is passed over, so that its matches' records keep the time of the last good
        look and a gap in looking is still a gap. A bug with one match is logged with its traceback
        and the look goes on with the rest.

        Args:
            now: The current epoch time.

        Raises:
            Exception: A bug in reading football (apart from it being unavailable), or in forgetting
                old matches. The thread's pass logs it, and the pass is over.
        """
        try:
            reading = self._football.read()
        except FootballUnavailable as e:
            # A line every 30 seconds for a night of Home Assistant being restarted would bury the log.
            if not self._down:
                reasons = [e.why] if e.why else []
                if e.__cause__ is not None:
                    reasons.append(repr(e.__cause__))
                why = f" ({'; '.join(reasons)})" if reasons else ""
                logger.warning(f"Football can't be read, so matches aren't being watched: {e}{why}")
            self._down = True
            return
        if self._down:
            logger.info("Football is back, matches are being watched again.")
            self._down = False
        lead = self._number("kickoff_minutes", KICKOFF_MINUTES) * 60
        asked: Dict[str, Optional[bool]] = {}
        for match, followed in self._matches(reading, now).values():
            try:
                before = self._choices.seen(match.id)
                due, record = events(before, match, now, lead, followed, reading.reported)
                # Kept before anything is said, so that what isn't said now is never said later.
                self._choices.remember(match.id, record)
                for event in due:
                    self._announce(event, followed, asked)
            except Exception:
                logger.exception(f"Watching the football match {match.id} failed")
        self._choices.forget_before(now - KEEP_FOR)

    def start(self):
        """Starts the daemon thread which looks every check_every seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="football")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a look already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            self._pass()
            self._stopping.wait(self._pause())

    def _pass(self):
        """One pass of the thread, in which no error may end the thread."""
        try:
            self.check(time.time())
        except Exception:
            logger.exception("Watching football failed, will try again")

    def _pause(self) -> float:
        """
        Gives the seconds to wait before the next look. Nothing it is given may end the thread.

        Returns:
            The check_every setting, or CHECK_EVERY if it isn't usable. Never longer than a wait can be.
        """
        try:
            return min(self._number("check_every", CHECK_EVERY), threading.TIMEOUT_MAX)
        except Exception:
            logger.exception("Couldn't read how often to look at football, using the default")
            return CHECK_EVERY

    def _number(self, key: str, default: float) -> float:
        """
        Reads one of the settings, afresh.

        Args:
            key: The setting in the football section, e.g. "check_every".
            default: What to use if the setting isn't usable.

        Returns:
            The setting, or the default if it isn't a number above zero: a bad one is warned of once,
            as it can't be put right from here, and would otherwise fill the log or, as a wait, hammer
            Home Assistant.
        """
        value = self._settings().get(key, default)
        if _is_number(value) and 0 < value < math.inf:
            self._warned.pop(key, None)
            return value
        if self._warned.get(key) != repr(value):
            self._warned[key] = repr(value)
            logger.warning(f"The football setting {key} is {value!r}, not a number above zero, so {default} is used.")
        return default

    @staticmethod
    def _matches(reading: Reading, now: float) -> Dict[str, Tuple[Match, Set[str]]]:
        """
        Gathers the matches to look at: each team's match in play or today, and its next fixture.

        Args:
            reading: What football.read() gave.
            now: The current epoch time.

        Returns:
            Each match once, by its id, with the ids of the followed teams in it. A team whose data is
            stale is no source of a match, so one it shares with a team whose data is good is looked
            at as that team has it - but it is still a followed team in the match, as the switches
            of both count (decision 7: said if either team's is on). A match which kicked off more
            than KEEP_FOR ago is left out: nothing is said of one so long over, and it would be
            remembered and forgotten again, with a save each time, at every look.
        """
        found: Dict[str, Tuple[Match, Set[str]]] = {}
        for team in reading.teams:
            if team.stale:
                continue
            for match in (team.now, team.next[0] if team.next else None):
                if match is not None and match.kickoff.timestamp() >= now - KEEP_FOR:
                    found.setdefault(match.id, (match, set()))[1].add(team.id)
        for match, followed in found.values():
            followed.update(team.id for team in reading.teams if team.id in (match.home_id, match.away_id))
        return found

    def _announce(self, event: Event, followed: Set[str], asked: Dict[str, Optional[bool]]):
        """
        Says one event if it should be, and logs why if it isn't.

        Args:
            event: What to say.
            followed: The ids of the followed teams in the match: it is said if any has that kind on.
            asked: Kept for the look, for _why_not_now.
        """
        if not any(self._choices.enabled(team_id, event.kind) for team_id in followed):
            why = "it is switched off for every followed team in the match"
        else:
            why = self._why_not_now(asked)
            if why is None and not self._say(event.message):
                why = "it couldn't be said"
        if why:
            logger.info(f'Not saying "{event.message}": {why}.')

    def _why_not_now(self, asked: Dict[str, Optional[bool]]) -> Optional[str]:
        """
        Says why nothing may be said just now, if so.

        Args:
            asked: What has been found out already at this look, which this adds to. Only where the
                user is: Home Assistant is asked once, and only if something is to be said. Quiet
                hours are asked each time, as a look with several announcements takes as long as
                saying them does, and may reach the night part way through.

        Returns:
            The reason - quiet hours, or Home Assistant saying the user is out (if it can't say, it
            may be said) - or None if it may be said.
        """
        if self._quiet():
            return "it is quiet hours"
        if "home" not in asked:
            asked["home"] = self._home_now()
        if asked["home"] is False:
            return "Home Assistant says the user is out"
        return None
