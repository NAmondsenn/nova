"""
Config Module.

The assistant's settings: the repository's config.yaml - every setting, with its
default and a comment saying what it's for - with this install's own settings file
on top (~/.config/nova/config.yaml, see paths.py), which only names what's
different here, setting by setting.

The install's file is checked against config.yaml as it's read. A setting config.yaml
doesn't have - "silence_treshold" - or one of the wrong kind - "silence_duration:
quick" - stops the assistant starting, with a message naming the file, the setting
and, for a misspelling, what was probably meant. Both used to be ignored without a
word, leaving the default quietly in force.
"""

import difflib
import logging
import math
import os
import re
import threading
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import yaml

from voice_assistant import paths

logger = logging.getLogger(__name__)

# Settings which only make sense within a range, or as one of a few words - checked
# beyond being the right kind.
BETWEEN_0_AND_1 = {("thresholds", "vad_threshold"), ("thresholds", "silence_threshold"), ("llm", "temperature")}
CHOICES = {
    ("music", "player"): ("navidrome", "spotify"),
    ("satellite", "trigger"): ("wake_word", "push_to_talk"),
    ("journeys", "distance_unit"): ("miles", "km"),
    ("computer", "search_site"): ("google", "duckduckgo", "bing"),
    ("computer", "screen_orb_place"): ("bottom centre", "bottom left", "bottom right",
                                       "top centre", "top left", "top right"),
    ("computer", "screen_orb_size"): ("small", "medium", "large"),
}
# Battery marks are percentages, and how many each kind of device has - the settings page
# allows no other number. The battery watcher can't check every 0 seconds or call a
# reading old after -5, nor can the football watcher, or say a match is starting 0 minutes
# before it does, and the journey watch can't look every 0 minutes, start 0 minutes before, or call
# 0 minutes of change a move, nor traffic heavy at 0 minutes over.
MARKS = {("batteries", "mobile_marks"): 1, ("batteries", "other_marks"): 2}
POSITIVE = {("batteries", "check_every"), ("batteries", "stale_after"),
            ("football", "check_every"), ("football", "stale_after"), ("football", "kickoff_minutes"),
            ("journeys", "check_every_minutes"), ("journeys", "watch_minutes"), ("journeys", "change_minutes"),
            ("journeys", "heavy_minutes"), ("computer", "mic_mute_minutes")}


class Limit(NamedTuple):
    """What a setting may be: its form, and for a number, its range."""
    form: str  # "number", "time", "day", "address", "topic", "topics", "playlist", "host" or "path".
    low: Optional[float] = None
    high: Optional[float] = None
    whole: bool = False


# Design 0009's table: what each setting on the settings page may hold, so a value which
# would silently do nothing (a silence of 30 seconds, a quiet hour of "11pm") is refused
# when it's saved, and at start, instead of being found out later.
LIMITS: Dict[Tuple[str, str], Limit] = {
    ("ntfy", "quiet_from"): Limit("time"),
    ("ntfy", "quiet_until"): Limit("time"),
    ("navidrome", "url"): Limit("address"),
    ("soulsync", "url"): Limit("address"),
    ("home_assistant", "url"): Limit("address"),
    ("ntfy", "url"): Limit("address"),
    ("ntfy", "phone"): Limit("topic"),
    ("ntfy", "reminders"): Limit("topic"),
    ("ntfy", "restarts"): Limit("topic"),
    ("ntfy", "alerts"): Limit("topics"),
    ("thresholds", "silence_duration"): Limit("number", 0.2, 3),
    # Under 6 dB the dips between one word and the next would count as the voice dropping away, and over
    # 40 dB hardly any sound is quiet enough to.
    ("thresholds", "press_voice_drop_db"): Limit("number", 6, 40),
    # Nemotron's words only move once each 0.56 seconds of audio it works through, so under 1.2 a single
    # stretch of its work without a new word - an ordinary pause between two words - could end the
    # recording, and over 5 it would hardly ever end one before the quiet or the longest request had.
    ("thresholds", "press_no_words_seconds"): Limit("number", 1.2, 5),
    ("thresholds", "recording_timeout"): Limit("number", 5, 30, False),
    # Below 0.3 almost anything wakes Nova, and above 0.95 hardly anything does.
    ("thresholds", "wake_word_confidence"): Limit("number", 0.3, 0.95),
    ("thresholds", "media_wake_word_confidence"): Limit("number", 0.3, 0.95),
    # 0 is no check, and above 0.95 hardly any wake would pass it.
    ("thresholds", "voice_check"): Limit("number", 0, 0.95),
    ("conversation", "history_max_age"): Limit("number", 0, 3600, True),
    # Under a minute Nova would give up before SoulSync had answered once, and a wait of
    # more than an hour for a song, or four for an album, is a download which has stalled.
    ("music", "song_download_minutes"): Limit("number", 1, 60, True),
    ("music", "album_download_minutes"): Limit("number", 1, 240, True),
    # Under 10 seconds would only hammer Home Assistant. A look at least once a minute keeps inside
    # football_watch.GAP, the five minutes what was last seen of a match may be old and still be compared
    # with - past it only a match starting would ever be said - and inside the shortest time before
    # kick-off that a match is said to be starting, so that no match's start falls between two looks.
    ("football", "check_every"): Limit("number", 10, 60, True),
    # Home Assistant's scores are up to a minute behind ESPN's, so under 2 minutes football would be called
    # old while it is as fresh as it gets. Over GAP, data which had stood still would be taken for fresh,
    # and a goal long since scored said as if it were news.
    ("football", "stale_after"): Limit("number", 120, 300, True),
    # Under 2 minutes a look every minute could miss the time before kick-off altogether, and over 2 hours
    # is long before a match is near.
    ("football", "kickoff_minutes"): Limit("number", 2, 120, True),
    # The PC's own microphone, muted by Nova, comes back on by itself after this - at least a
    # minute, and at most a day, past which it's a microphone forgotten about.
    ("computer", "mic_mute_minutes"): Limit("number", 1, 1440, True),
    # Approvals. One approval may cover what he asks for up to an hour after it, and 0 asks every time; a
    # longer window is a door left open. A wait under a minute is too short to find the phone, and one over
    # ten minutes leaves the action hanging long after he has moved on. A download of 1 album needing
    # approval would make every album ask; over 20 is one which never would.
    ("approvals", "trust_minutes"): Limit("number", 0, 60, True),
    ("approvals", "wait_minutes"): Limit("number", 1, 10, True),
    ("approvals", "album_count"): Limit("number", 2, 20, True),
    # Journeys. A buffer of over an hour, or a reminder over half an hour ahead, is a slip. A look every minute
    # would spend the month's requests in days, and one over half an hour would miss the traffic changing. A
    # watch under 10 minutes would hardly look before the time to leave, and over 2 hours would ask Google about
    # traffic which is hours from mattering. A move of under 2 minutes is the forecast's own noise.
    ("journeys", "buffer_minutes"): Limit("number", 0, 60, True),
    ("journeys", "reminder_minutes"): Limit("number", 0, 30, True),
    ("journeys", "check_every_minutes"): Limit("number", 2, 30, True),
    ("journeys", "watch_minutes"): Limit("number", 10, 120, True),
    ("journeys", "change_minutes"): Limit("number", 2, 30, True),
    # Heavy is a share of the clear time as well as minutes, so a long drive isn't called heavy for a few minutes.
    ("journeys", "heavy_minutes"): Limit("number", 1, 60, True),
    ("journeys", "heavy_percent"): Limit("number", 0, 100, True),
    # 0 asks Google nothing. Google's free month is 5,000, so more is a bill.
    ("journeys", "monthly_requests"): Limit("number", 0, 5000, True),
    ("mqtt", "host"): Limit("host"),
    ("mqtt", "port"): Limit("number", 1, 65535, True),
    # Not a port below 1024, which a user's own program can't listen on.
    ("satellite", "press_port"): Limit("number", 1024, 65535, True),
    ("voice", "speed"): Limit("number", 0.75, 1.5),
    # 0 is NPM's listener switched off.
    ("settings_page", "proxy_port"): Limit("number", 0, 65535, True),
    # 0 is the clock displays' server switched off.
    ("display", "port"): Limit("number", 0, 65535, True),
    # 0 is no spending alert, and above 100 pounds a day the alert would never be heard.
    ("usage", "daily_limit"): Limit("number", 0, 100),
    # Nothing near 0, and not 10 pounds to a dollar either: a number outside this is a slip,
    # and every pound shown on the dashboard would be wrong by it.
    ("usage", "pounds_per_dollar"): Limit("number", 0.1, 10),
    # Dollars per million tokens, and per 1,000 searches. Claude's cost nothing like 1,000.
    ("usage", "input_price"): Limit("number", 0, 1000),
    ("usage", "output_price"): Limit("number", 0, 1000),
    ("usage", "search_price"): Limit("number", 0, 1000),
    # The month the yearly Wrapped is made on the 1st of. Empty is no link in its notification.
    ("wrapped", "yearly_month"): Limit("number", 1, 12, True),
    ("wrapped", "link"): Limit("address"),
    # Decision 0022. 0 tries is a clean song left as it is, and more than 5 would be asking SoulSync again
    # and again for a version which is not coming. A song with fewer than 1 swear has nothing to hear, and
    # past 20 hardly any song would be judged. A share is a part of the song's swears, so from 0 to 1.
    # The music folder is a full path, or empty for none: "music" would be wherever Nova happened to start.
    # The SoulSync profile is free text, and has no limit, as Navidrome's user has none.
    ("clean_check", "swap_tries"): Limit("number", 0, 5, True),
    ("clean_check", "lyric_minimum"): Limit("number", 1, 20, True),
    ("clean_check", "explicit_share"): Limit("number", 0, 1),
    ("clean_check", "clean_share"): Limit("number", 0, 1),
    ("clean_check", "music_folder"): Limit("path"),
    # The nightly check's time, and the weekly note's day and time (decision 0022, section 8). The day is kept as
    # written, "Monday" or "monday", and read in any capitals.
    ("clean_check", "nightly_time"): Limit("time"),
    ("clean_check", "weekly_day"): Limit("day"),
    ("clean_check", "weekly_time"): Limit("time"),
    # 0 is no following. Over 50 reaches artists played a handful of times a year, whose every release
    # would download.
    ("new_music", "follow_top_artists"): Limit("number", 0, 50, True),
    # Two months of arrivals is already more than the playlist's 500 songs most weeks.
    ("new_music", "days"): Limit("number", 1, 60, True),
    # Nova tells its playlist from the others by the words of its name, so a name of only punctuation, emoji
    # or letters outside A to Z has none to go by.
    ("new_music", "playlist"): Limit("playlist"),
}

_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# A "day" setting's days, as they're compared: in small letters, Monday first, as datetime's weekday() counts.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9.:-]*")

# Sections an install's file may still have from when the Pi heard for itself, before
# the brain did - see docs/decisions/0006. Nothing reads them now, but a Pi's own
# settings often have them, and refusing them stopped the satellite and the brain both
# starting, so they're let through with a warning instead. Any other section config.yaml
# doesn't have is still a mistake.
RETIRED = ("speech_to_text", "moonshine", "whisper")

# Each setting which has left config.yaml, and where it's set now. An install's file
# often still has one - the brain's own had weather.location once the weather was Home
# Assistant's - and refusing it would stop Nova starting over a setting nothing reads,
# so it's let through, and a section holding only these, with a warning saying so.
REMOVED: Dict[Tuple[str, str], str] = {
    ("weather", "location"): "the weather is Home Assistant's own, for its home location",
    ("home_assistant", "calendars"): "every calendar in Google's own list is read now",
    ("home_assistant", "activities"): "activities are your calendars in Google now",
}


class ConfigError(ValueError):
    """A settings file which can't be used as it is."""


def _read(path: str) -> Dict:
    """A YAML file's settings, or a ConfigError saying why it can't be read."""
    try:
        with open(path) as file:
            data = yaml.safe_load(file)
    # A number of more than 4,300 digits is a ValueError, from Python rather than PyYAML -
    # and so is a file which isn't UTF-8, saved in Latin-1 by an editor, say, as its
    # UnicodeDecodeError is one.
    except (yaml.YAMLError, ValueError) as e:
        raise ConfigError(f"{path} isn't valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should be sections of settings, like the config.yaml it's on top of")
    return data


def _kind(value: Any) -> str:
    """The kind of value a setting holds, as said in an error."""
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "words, in quotes"
    if isinstance(value, list):
        return "a list"
    return "settings"


def _guess(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def marks_problem(marks: Any, count: int, who: str) -> Optional[str]:
    """
    Says what is wrong with a list of battery marks, if anything - the one rule for
    config.yaml's and the settings page's alike, so the page can always save what
    config.yaml starts it with.

    Args:
        marks: The marks, e.g. [15, 5].
        count: How many there must be - one for a mobile device, two for anything else.
        who: What they belong to, to start the sentence with, e.g. "MacBook Pro" or
            "config.yaml: 'batteries.mobile_marks'".

    Returns:
        A sentence saying what is wrong, or None if they are fine: the right number of
        whole numbers from 1 to 99 (0% would never be reached, and 100 or more would
        warn about a full battery), all different.
    """
    if not isinstance(marks, list):
        return f"{who} needs {'one mark' if count == 1 else 'two marks'}, in a list, not {marks!r}."
    if len(marks) != count:
        return f"{who} needs {'one mark' if count == 1 else 'two marks'}, not {len(marks)}."
    if any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 99 for m in marks):
        return "A mark must be a whole number from 1 to 99."
    if len(set(marks)) != len(marks):
        return "The two marks must be different."
    return None


def limit_problem(section: str, name: str, value: Any) -> Optional[str]:
    """
    Says whether a setting makes sense - the one rule for config.yaml's start and the
    settings page's saving alike.

    Args:
        section: The setting's section, e.g. "thresholds".
        name: The setting, e.g. "silence_duration".
        value: What it holds.

    Returns:
        The end of a sentence, e.g. "should be from 0.2 to 3", or None if it is fine
        or has no limit.
    """
    limit = LIMITS.get((section, name))
    if limit is None:
        return None
    if limit.form == "number":
        if limit.whole:
            message = f"should be a whole number from {limit.low:g} to {limit.high:g}"
        else:
            message = f"should be from {limit.low:g} to {limit.high:g}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return message
        # json.loads (the settings page's) and YAML both accept infinity and NaN, which int()
        # raises on. Only a float is tested: isfinite() raises on an int too big for a float.
        if (isinstance(value, float) and not math.isfinite(value)) or (limit.whole and value != int(value)):
            return message
        return None if limit.low <= value <= limit.high else message
    if limit.form == "time":
        ok = isinstance(value, str) and _TIME.fullmatch(value)
        return None if ok else "should be a time of day, from 00:00 to 23:59"
    if limit.form == "day":
        ok = isinstance(value, str) and value.strip().casefold() in WEEKDAYS
        return None if ok else "should be a day of the week, from Monday to Sunday"
    if limit.form == "address":
        ok = isinstance(value, str) and (value == "" or re.match(r"https?://.", value))
        return None if ok else "should be an address starting http:// or https://"
    if limit.form == "playlist":
        # Imported here: navidrome.py is heavy, and only a playlist's name needs it.
        from voice_assistant.navidrome import normalise
        ok = isinstance(value, str) and (value.strip() == "" or normalise(value) != "")
        return None if ok else "should have a letter from A to Z or a digit in it"
    if limit.form in ("topic", "topics"):
        topics = value if limit.form == "topics" else [value]
        ok = isinstance(topics, list) and all(
            isinstance(topic, str) and (_TOPIC.fullmatch(topic) or (limit.form == "topic" and topic == ""))
            for topic in topics)
        return None if ok else "should be an ntfy topic: letters, digits, hyphens and underscores"
    if limit.form == "path":
        ok = isinstance(value, str) and (value == "" or (os.path.isabs(value) and "\0" not in value))
        return None if ok else "should be a folder's full path, starting /"
    ok = isinstance(value, str) and _HOST.fullmatch(value)
    return None if ok else "should be a host name or address, without http://"


def clashes(data: Dict) -> List[Tuple[str, str]]:
    """
    The settings which need each other, and are missing one - checked when the settings
    page saves, not at start, so a file which worked before still starts.

    Args:
        data: Every setting, by section.

    Returns:
        Each as (the setting to name, the sentence), e.g.
        [("ntfy.url", "ntfy's topics need its server.")].
    """
    found = []
    music, navidrome, ntfy = data.get("music") or {}, data.get("navidrome") or {}, data.get("ntfy") or {}
    if str(music.get("player", "")).lower() == "navidrome" and not navidrome.get("url"):
        found.append(("navidrome.url", "Navidrome as the player needs Navidrome's address."))
    if any(ntfy.get(name) for name in ("alerts", "phone", "reminders", "restarts")) and not ntfy.get("url"):
        found.append(("ntfy.url", "ntfy's topics need its server."))
    return found


def removed(settings: Dict) -> List[Tuple[str, str]]:
    """
    The settings in REMOVED an install's file still has.

    Args:
        settings: The install's settings.

    Returns:
        Each as (section, name), e.g. [("weather", "location")].
    """
    return [(section, name) for section, values in settings.items() if isinstance(values, dict)
            for name in values if (section, name) in REMOVED]


def emptied(settings: Dict, defaults: Dict) -> List[str]:
    """
    The sections an install's file still has, empty, once their removed settings are
    deleted - "weather:" on its own, after the warning said "weather.location" could go.

    Args:
        settings: The install's settings.
        defaults: config.yaml's.

    Returns:
        Each section's name, e.g. ["weather"].
    """
    homes = {section for section, _ in REMOVED}
    return [section for section, values in settings.items()
            if section in homes and section not in defaults and values in (None, {})]


def check(settings: Dict, defaults: Dict, path: str) -> List[str]:
    """
    Checks an install's settings against config.yaml's.

    Args:
        settings: The install's settings.
        defaults: config.yaml's.
        path: The install's file, for the message.

    Returns:
        The sections it has which are no longer used - see RETIRED - and so aren't
        checked, and are to be left out. The settings in REMOVED aren't checked either,
        nor the sections emptied of them.

    Raises:
        ConfigError: Naming the first setting which is wrong, and how.
    """
    retired = [section for section in settings if section in RETIRED and section not in defaults]
    empty = emptied(settings, defaults)
    for section, values in settings.items():
        if section in retired or section in empty:
            continue
        if section not in defaults:
            if isinstance(values, dict) and values and all((section, name) in REMOVED for name in values):
                continue
            raise ConfigError(f"{path}: there's no '{section}' section.{_guess(section, defaults)}")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: '{section}' should be settings under it, indented, like config.yaml's")
        for name, value in values.items():
            if (section, name) in REMOVED:
                continue
            known = defaults[section]
            if name not in known:
                raise ConfigError(f"{path}: '{section}.{name}' isn't a setting.{_guess(name, known)}")
            default = known[name]
            # YAML reads an unquoted 23:00 as the number 1380, so a time is told what's wrong
            # with it as a time, not as "words, in quotes".
            if (section, name) in LIMITS and LIMITS[(section, name)].form == "time" \
                    and value is not None and not isinstance(value, str):
                raise ConfigError(f"{path}: '{section}.{name}' {limit_problem(section, name, value)}, "
                                  f"not {value!r} - write it in quotes, like \"23:00\"")
            if default is not None and value is not None and _kind(value) != _kind(default):
                raise ConfigError(f"{path}: '{section}.{name}' should be {_kind(default)}, not {value!r}")
            if (section, name) in BETWEEN_0_AND_1 and value is not None and not 0 <= value <= 1:
                raise ConfigError(f"{path}: '{section}.{name}' should be between 0 and 1, not {value}")
            if (section, name) in MARKS:
                setting = f"{path}: '{section}.{name}'"
                problem = marks_problem(value, MARKS[(section, name)], setting)
                if problem:
                    # Only some of the sentences begin with who they're about.
                    raise ConfigError(problem if problem.startswith(setting) else f"{setting}: {problem}")
            if (section, name) in POSITIVE and not (isinstance(value, (int, float)) and value > 0):
                raise ConfigError(f"{path}: '{section}.{name}' should be a number above 0, not {value!r}")
            problem = limit_problem(section, name, value) if value is not None else None
            if problem:
                raise ConfigError(f"{path}: '{section}.{name}' {problem}, not {value!r}")
            choices = CHOICES.get((section, name))
            if choices and str(value).lower() not in choices:
                raise ConfigError(f"{path}: '{section}.{name}' should be one of {', '.join(choices)}, not {value!r}")
    return retired


class Config:
    """
    The settings, read once at startup and changed by the settings page as Nova runs.
    section() gives one part of them.
    """

    def __init__(self, path: str = None, defaults: str = None, quiet: bool = False):
        """
        Args:
            path: This install's settings file - ~/.config/nova/config.yaml unless
                given. It needn't exist: the defaults are used as they are.
            defaults: The file with every setting's default - the repository's
                config.yaml unless given.
            quiet: Not to log where the settings came from - for the settings page,
                which reads them every time it looks.

        Raises:
            ConfigError: If either file can't be used, saying why.
        """
        self.defaults_path = defaults or paths.DEFAULTS_FILE
        self.path = os.path.expanduser(path or paths.CONFIG_FILE)
        # Held while a change is made, so two at once can't each lose the other's - see apply.
        self._applying = threading.Lock()

        if not os.path.exists(self.defaults_path):
            raise FileNotFoundError(f"Config file not found at {self.defaults_path}")
        self.data = _read(self.defaults_path)
        if not self.data:
            raise ValueError(f"Config file at {self.defaults_path} is empty or invalid")

        # Only the settings it names are changed - "music: {player: ...}" leaves the
        # rest of config.yaml's music section as it is.
        if os.path.exists(self.path):
            settings = _read(self.path)
            retired = check(settings, self.data, self.path)
            gone = removed(settings)
            for name, values in settings.items():
                # A section holding only removed settings isn't config.yaml's any more.
                if name not in retired and name in self.data:
                    self.data[name] = {**self.data[name],
                                       **{key: value for key, value in values.items() if (name, key) not in gone}}
            if not quiet:
                logger.info(f"Settings loaded from {self.path}")
                if retired:
                    logger.warning(f"{self.path} still has the {', '.join(retired)} section"
                                   f"{'s' if len(retired) > 1 else ''}, which Nova no longer uses - "
                                   f"{'they' if len(retired) > 1 else 'it'} can be deleted")
                for section, name in gone:
                    logger.warning(f"{self.path}: '{section}.{name}' is no longer used - "
                                   f"{REMOVED[(section, name)]} - so it can be deleted")
                for section in emptied(settings, self.data):
                    where = "; ".join(where for (home, _), where in REMOVED.items() if home == section)
                    logger.warning(f"{self.path}: '{section}' is no longer used - {where} - so it can be deleted")

    def section(self, name):
        """
        Returns a specific section of the config such as 'audio' or 'llm' as a dict.
        Returns an empty dict if the section doesn't exist, so callers can fall back
        to their own defaults via .get() rather than crashing on optional sections.
        It's the settings as they are now - one read before a change keeps the old ones.
        """
        return self.data.get(name, {})

    def apply(self, changes: Dict[Tuple[str, str], Any]):
        """
        Changes settings as Nova runs - the settings page's, once they're saved.

        Every satellite's session reads the settings on its own thread while the page
        changes them on another, so nothing already given out is changed: each section
        changed is a new dict, in a copy of them all which is swapped in with one
        assignment. A reader sees all of a change or none of it - never a silence of
        1.2 seconds with the rest of the old thresholds half replaced.

        Args:
            changes: Each setting, as (section, name), to its new value, e.g.
                {("thresholds", "silence_duration"): 1.2}.
        """
        with self._applying:
            data = dict(self.data)
            for (section, name), value in changes.items():
                data[section] = {**data.get(section, {}), name: value}
            self.data = data
