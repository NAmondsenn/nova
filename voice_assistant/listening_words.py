"""
Listening Words Module.

The text Claude answers questions about what the user has listened to from: "what have I
listened to most this week?", "who's my top artist this year?", "when do I listen?".

Nova gives Claude plain sentences built from the latest snapshot of SoulSync's stats (see
listening.py, which keeps it and knows when there isn't one), not SoulSync's JSON: the listening
time already worked out in hours and minutes, the change as a whole percentage, the hour as a
person says it, so that Claude never has to convert a figure or guess one. Every number comes
from SoulSync; nothing is counted here. Only describe asks the snapshot anything, and it never
waits on SoulSync, as it reads what the background refresh last kept.

SoulSync's answer is only checked for the keys it must have when it is read (see
Listening.refresh), so the rest is read defensively here: an entry without a name, or a figure
which isn't a number, is passed over rather than raising, as an update to its :latest image
could change what it gives and a question must still get an answer.
"""

import logging
from datetime import date
from typing import Dict, List, Optional

from voice_assistant.google_calendar import _ordinal
from voice_assistant.listening import WINDOW_WORDS, WINDOWS, ListeningUnavailable

# Logger setup
logger = logging.getLogger(__name__)

# What a question can ask about, the first being the one when it isn't said.
ABOUT = ("summary", "artists", "songs", "albums", "genres", "when")

# How many of each top list are said: the summary's short one, and the lists asked for outright.
SUMMARY_ARTISTS = 3
TOP_LIST = 5
TOP_GENRES = 3

# What the period before a window is called, for "up 34% on the 7 days before".
BEFORE_WORDS = {"7d": "the 7 days before", "30d": "the 30 days before", "12m": "the 12 months before"}

# The days of SoulSync's week grid and clock.peak.weekday, which count as SQLite's strftime('%w') does:
# 0 is Sunday, 1 Monday ... 6 Saturday (not Python's weekday(), where Monday is 0). Hours are local time.
_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# What is said when the period or the sort of answer asked for isn't one of those offered, as the
# model can give anything.
NOT_A_PERIOD = "I can only look at the last week, month, year or all time."
# What is said when the snapshot has no usable answer for the window, which a refresh which worked always gives.
NOT_READ = "I haven't got those listening stats."
NOT_AN_ABOUT = "I can tell you your totals, top artists, songs, albums and genres, and when you listen."


def describe(listening, period: Optional[str], about: Optional[str]) -> Dict:
    """
    Says what the user has listened to, from the latest snapshot of SoulSync's stats.

    Args:
        listening: Listening, which gives the snapshot - or what it couldn't.
        period: The window, as a key of WINDOWS - "week", "month", "year" or "all" - or None for the week.
        about: What to say of it: "summary", "artists", "songs", "albums", "genres" or "when", or None for
            the summary.

    Returns:
        {"success": True, "message": the facts to reply from}, or {"success": False, "message": a sentence
        to say} if there is nothing to answer from, or the question wasn't one of those offered.
    """
    period = "week" if period is None else period
    about = "summary" if about is None else about
    if not isinstance(period, str) or period not in WINDOWS:
        return {"success": False, "message": NOT_A_PERIOD}
    if not isinstance(about, str) or about not in ABOUT:
        return {"success": False, "message": NOT_AN_ABOUT}
    try:
        snapshot = listening.require()
    except ListeningUnavailable as e:
        return {"success": False, "message": str(e)}

    window = WINDOWS[period]
    answer = snapshot.windows.get(window)
    if not isinstance(answer, dict):
        # Not one of the four a refresh always reads, so the snapshot isn't one Nova made.
        logger.warning("The listening snapshot has no answer for a window")
        return {"success": False, "message": NOT_READ}
    words = WINDOW_WORDS[window]

    plays = _dict(answer.get("overview")).get("total_plays")
    if not _number(plays):
        logger.warning("The listening snapshot has no number of plays for a window")
        return {"success": False, "message": NOT_READ}
    if plays <= 0:
        # Nothing was played, so there is nothing to list, whatever is asked, and nothing is made up.
        return {"success": True, "message": _nothing(window, words)}

    sayers = {"summary": _summary, "artists": _artists, "songs": _songs, "albums": _albums, "genres": _genres,
              "when": _when}
    return {"success": True, "message": sayers[about](answer, window, words)}


def _nothing(window: str, words: str) -> str:
    """What is said of a window with no plays in it."""
    return "You haven't listened to anything yet." if window == WINDOWS["all"] else \
        f"You haven't listened to anything in {words}."


def _summary(answer: Dict, window: str, words: str) -> str:
    """The plays, listening time, artists and change in a window, with its top three artists."""
    overview = _dict(answer.get("overview"))
    plays = int(overview["total_plays"])
    parts = [f"{_count(plays, 'play')}"]
    time_listened = _hours_and_minutes(overview.get("total_time_ms"))
    if time_listened:
        parts.append(f"{time_listened} of listening")
    if _number(overview.get("unique_artists")):
        parts.append(_count(int(overview["unique_artists"]), "artist"))
    message = f"In {words}, you had " + ", ".join(parts) + "."

    change = _change(plays, _dict(answer.get("previous")).get("total_plays"))
    if change is not None and window in BEFORE_WORDS:
        message += (f" That's {change} on {BEFORE_WORDS[window]}." if change != "the same"
                    else f" That's the same as {BEFORE_WORDS[window]}.")
    top = _named(answer.get("top_artists"), SUMMARY_ARTISTS)
    if top:
        message += " Top artists: " + ", ".join(top) + "."
    return message


def _artists(answer: Dict, window: str, words: str) -> str:
    return _top_list(answer.get("top_artists"), "artists", words)


def _songs(answer: Dict, window: str, words: str) -> str:
    return _top_list(answer.get("top_tracks"), "songs", words)


def _albums(answer: Dict, window: str, words: str) -> str:
    return _top_list(answer.get("top_albums"), "albums", words)


def _top_list(entries, what: str, words: str) -> str:
    """The top five of a list, e.g. "Your top songs in the last 7 days: Song A by Artist One (12 plays), ..."."""
    top = _named(entries, TOP_LIST)
    if not top:
        return f"SoulSync has no top {what} for {words}."
    return f"Your top {what} in {words}: " + ", ".join(top) + "."


def _genres(answer: Dict, window: str, words: str) -> str:
    """The top three genres of a window, with the share of the plays each had."""
    genres = answer.get("genres")
    said = []
    for entry in (genres if isinstance(genres, list) else []):
        entry = _dict(entry)
        name = entry.get("genre")
        if isinstance(name, str) and name.strip() and _number(entry.get("percentage")):
            said.append(f"{name.strip()} ({round(entry['percentage'])}%)")
        if len(said) == TOP_GENRES:
            break
    if not said:
        return f"SoulSync has no genres for {words}."
    return f"Your top genres in {words}: " + ", ".join(said) + "."


def _when(answer: Dict, window: str, words: str) -> str:
    """The weekday and hour of the most plays, the longest streak and the busiest day of a window."""
    sentences = []
    peak = _dict(_dict(answer.get("clock")).get("peak"))
    weekday, hour = peak.get("weekday"), peak.get("hour")
    if _whole(weekday) and 0 <= weekday < len(_WEEKDAYS) and _whole(hour) and 0 <= hour < 24:
        sentences.append(f"You listen most on {_WEEKDAYS[weekday]}s, around {_hour_said(hour)}.")
    rhythm = _dict(answer.get("rhythm"))
    longest = rhythm.get("longest_streak")
    if _number(longest) and longest > 0:
        sentences.append(f"Your longest streak is {_count(int(longest), 'day')} in a row.")
    busiest = _dict(rhythm.get("busiest_day"))
    day = _date(busiest.get("date"))
    if day and _number(busiest.get("plays")):
        sentences.append(f"Your busiest day was {day.strftime('%A')} the {_ordinal(day.day)} of "
                         f"{day.strftime('%B')}, with {_count(int(busiest['plays']), 'play')}.")
    if not sentences:
        return f"SoulSync has nothing on when you listen for {words}."
    return f"In {words}: " + " ".join(sentences)


def _named(entries, most: int) -> List[str]:
    """
    The first entries of a list of artists, songs or albums which have a name and a number of plays,
    as "Song A by Artist One (12 plays)" - the artist only where it has one.

    Args:
        entries: SoulSync's list. Its cached lists count in play_count.
        most: How many to give at most.
    """
    said = []
    for entry in (entries if isinstance(entries, list) else []):
        entry = _dict(entry)
        name, plays = entry.get("name"), entry.get("play_count")
        if not (isinstance(name, str) and name.strip() and _number(plays)):
            continue
        artist = entry.get("artist")
        by = f" by {artist.strip()}" if isinstance(artist, str) and artist.strip() else ""
        said.append(f"{name.strip()}{by} ({_count(int(plays), 'play')})")
        if len(said) == most:
            break
    return said


def _change(plays: int, before) -> Optional[str]:
    """
    The change in plays against the period before, as a whole percentage - "up 34%", "down 20%" or
    "the same" - or None if there is no period before, or it had no plays to change from.
    """
    if not _number(before) or before <= 0:
        return None
    percent = round((plays - before) / before * 100)
    if percent == 0:
        return "the same"
    return f"up {percent}%" if percent > 0 else f"down {-percent}%"


def _hours_and_minutes(ms) -> Optional[str]:
    """Milliseconds as it is said, e.g. "36 hours 46 minutes", "1 hour" or "45 minutes" - None if it isn't known."""
    if not _number(ms) or ms < 0:
        return None
    hours, minutes = divmod(int(ms) // 60_000, 60)
    parts = ([_count(hours, "hour")] if hours else []) + ([_count(minutes, "minute")] if minutes or not hours else [])
    return " ".join(parts)


def _hour_said(hour: int) -> str:
    """An hour of the day as a person says it: "midnight", "9am", "midday", "6pm"."""
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "midday"
    return f"{hour % 12}{'am' if hour < 12 else 'pm'}"


def _count(number: int, what: str) -> str:
    """A number of something, e.g. "1 play" or "596 plays"."""
    return f"{number} {what}" if number == 1 else f"{number} {what}s"


def _dict(found) -> Dict:
    """What SoulSync gave if it is a dict, else an empty one, so a key can be asked of it whatever it was."""
    return found if isinstance(found, dict) else {}


def _number(found) -> bool:
    """Whether something SoulSync gave is a number - and not True or False, which Python counts as one."""
    return isinstance(found, (int, float)) and not isinstance(found, bool)


def _whole(found) -> bool:
    """Whether something SoulSync gave is a whole number, not a bool."""
    return isinstance(found, int) and not isinstance(found, bool)


def _date(found) -> Optional[date]:
    """A day as SoulSync writes it, "2026-11-13", or None if it isn't one."""
    try:
        return date.fromisoformat(found)
    except (TypeError, ValueError):
        return None
