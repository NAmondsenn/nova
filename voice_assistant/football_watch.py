"""
Football Watch Module
What Nova says by itself about a followed team's match, from decision 7 of docs/decisions/0014.

Two pure parts: FootballChoices, which keeps which announcements are switched on for which
team and what was last seen of each match, and events, which says what a change in a match
means. The thread which reads Home Assistant and speaks them is the watcher's (see
football.py for what Home Assistant says and when it can't be trusted).

Nothing here reads the clock or Home Assistant: events is given the time and the match,
so it says the same in any time zone, and says only what the match shows - the names and
scores Home Assistant gave, never a scorer or a result worked out from anything else.
"""

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from voice_assistant.choices import ChoiceStore
from voice_assistant.football import Match, _is_whole

logger = logging.getLogger(__name__)

# What can be announced, in the order the page lists the switches.
KINDS = ("starting", "goals", "half_time", "second_half", "full_time")

# Seconds old what was last seen of a match may be and still be compared with. After longer,
# whatever changed meanwhile isn't announced: Nova was restarted, or Home Assistant or that team's
# data was down, and a goal an hour old isn't news.
GAP = 300

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


def _is_scores(scores) -> bool:
    """Whether something is a pair of scores, each a whole number or None: {"home": 1, "away": None}."""
    return (isinstance(scores, dict) and set(scores) == {"home", "away"}
            and all(goals is None or _is_whole(goals) for goals in scores.values()))


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
            and (record["waiting"] is None or _is_scores(record["waiting"]))
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
            match_id: ESPN's id for the match.

        Returns:
            A copy of the record events() gave last, or None if the match was never seen, or
            what the file holds for it isn't a record. After a restart that is the one the file
            holds, which is old in "at", so that the first look says nothing of what changed meanwhile.
        """
        with self._lock:
            record = self._held(str(match_id))
            return dict(record) if _is_record(record) else None

    def remember(self, match_id: str, record: Dict):
        """
        Keeps what was seen of a match. It is kept in memory at once, and saved only if it
        differs from the record last saved in something other than "at". A save which fails
        keeps it in memory alone, so the match isn't announced again every look - only after
        a restart - and is tried again at the next look, which is when it can be.

        Args:
            match_id: ESPN's id for the match.
            record: What events() gave. It is copied, so changing it later changes nothing here.

        Raises:
            TypeError: If the record has something JSON can't hold. Nothing is changed then.
        """
        match_id = str(match_id)
        record = dict(record)
        with self._lock:
            saved = self._state["seen"].get(match_id)
            if not (_is_record(saved) and _apart_from_when(saved) == _apart_from_when(record)):
                try:
                    self._store.change(lambda state: state["seen"].__setitem__(match_id, dict(record)))
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


def _goals(before: Dict, match: Match) -> Tuple[Optional[Event], Dict]:
    """
    Says what a change in the score means.

    A score which has gone down is not said at once, as it may be an old copy of the match
    which Home Assistant gave for a look: the higher score is kept, and the lower one noted as
    waiting. If the next look shows that same lower score it is said, as a goal ruled out;
    if it shows anything else the note is dropped and that look is compared as usual.

    Args:
        before: What was last seen of the match.
        match: The match as it is.

    Returns:
        The event, or None if there is nothing to say, and the scores for the record to keep:
        {"home", "away", "waiting"}. A goal for either side is said, so that the score heard is
        never behind. A goal which went in as another was ruled out is said as the goal, as
        that is the news and the score shows the rest.
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
        if before["waiting"] == here:
            return Event("goals", f"Goal ruled out. {score}"), taken
        return None, {"home": before["home"], "away": before["away"], "waiting": here}
    return None, taken


def _full_time(match: Match) -> str:
    """
    Says that a match is over, and how it ended where that was after extra time or on penalties.

    Args:
        match: A match which has reached full time.

    Returns:
        The sentence. The winner of a shoot-out is named with its own score first, however the
        sides were listed; one Home Assistant didn't name isn't worked out from the penalties.
    """
    text = "Full time, after extra time." if match.extra_time else "Full time."
    text += " " + _score(match)
    if match.shootout:
        home_goals, away_goals = match.shootout
        if match.winner == "home":
            text += f" {match.home} won {home_goals}-{away_goals} on penalties."
        elif match.winner == "away":
            text += f" {match.away} won {away_goals}-{home_goals} on penalties."
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
    before: Optional[Dict], match: Match, now: float, lead: float, followed: Set[str]
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
    but the time of the look; a score which has gone down waits for a second look (see _goals);
    and a look with no known phase doesn't take the record's away. A match which is off says nothing.

    Args:
        before: What was last seen of the match (the record this gave last time), or None.
        match: The match as it is.
        now: The time, in epoch seconds. Given, not read, so that this says the same in any time zone.
        lead: Seconds before kick-off to say that it is starting from.
        followed: The ids of the followed teams in the match.

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
    if recent and phase != "off":
        kept_phase = phase or before["phase"]
        if before["state"] != "post":
            goals, scores = _goals(before, match)
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
