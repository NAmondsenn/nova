"""
Clean Ear Module
The rules which judge whether a song is clean or explicit from how many of its lyric swears
a speech model heard, from section 1 of decision 0022 (docs/decisions/0022-clean-songs-by-ear.md).

Pure: it is given the lyrics and what was heard, reads no file, network or clock, and says
the same in any time zone. Getting the lyrics, listening to the song and swapping a clean
copy are other modules' jobs.

The thresholds were set from a test on real songs, counting exactly as count_swears does
and listening as nemotron.py's streaming recogniser does, so the method is not to be
changed without setting them again.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# The shares last warned of, as the thresholds are read for every song heard.
_warned_of = None

# The words counted, in the lyrics and in what was heard alike: the fuck, shit, bitch, nigga,
# pussy, dick, hoe and ass families. \b stops "ass" matching inside "class" or "asshole", and
# "hoes?" matches "hoe" and "hoes" but not "hoedown".
SWEARS = re.compile(
    r"\b(fuck\w*|motherfuck\w*|shit\w*|bitch\w*|nigga\w*|nigger\w*|pussy|pussies|dick\w*|hoes?|ass)\b",
    re.IGNORECASE)

# The thresholds where config.yaml's clean_check section gives none.
LYRIC_MINIMUM = 3
EXPLICIT_SHARE = 0.35
CLEAN_SHARE = 0.15


def count_swears(text: str) -> int:
    """
    Counts the swears in some words, each time one is said.

    Args:
        text: Lyrics, or what a model heard of a song.

    Returns:
        How many matches there are, e.g. 2 for "what the fuck? Why these niggas".
    """
    return len(SWEARS.findall(text))


@dataclass
class Verdict:
    """What was made of a song: kind, how many swears its lyrics have, and how many were heard."""
    kind: str  # "explicit", "clean", "unsure", "too_few" or "not_judged".
    expected: int
    heard: int
    reason: str = ""  # Why it was not judged, e.g. "no lyrics".


@dataclass
class Thresholds:
    """How many swears a song's lyrics need to be judged, and the shares of them heard which decide it."""
    minimum: int = LYRIC_MINIMUM
    explicit_share: float = EXPLICIT_SHARE
    clean_share: float = CLEAN_SHARE

    @classmethod
    def from_config(cls, config) -> "Thresholds":
        """
        Reads the thresholds from the settings as they are now.

        Args:
            config: Nova's Config, with a clean_check section.

        Returns:
            The thresholds, with the defaults for any setting the section lacks or leaves
            blank. Shares that don't leave a gap between clean and explicit are both put back
            to their defaults, as clean_share 0.3 with explicit_share 0.2 puts a song heard at
            25% past both.
        """
        section = config.section("clean_check")

        def setting(name, default):
            value = section.get(name)
            return default if value is None else value

        explicit_share = setting("explicit_share", EXPLICIT_SHARE)
        clean_share = setting("clean_share", CLEAN_SHARE)
        if not clean_share < explicit_share:
            global _warned_of
            if _warned_of != (explicit_share, clean_share):
                _warned_of = (explicit_share, clean_share)
                logger.warning(f"clean_check.clean_share ({clean_share}) isn't below explicit_share "
                               f"({explicit_share}), so both are left at {CLEAN_SHARE} and {EXPLICIT_SHARE}")
            explicit_share, clean_share = EXPLICIT_SHARE, CLEAN_SHARE
        return cls(minimum=setting("lyric_minimum", LYRIC_MINIMUM), explicit_share=explicit_share,
                   clean_share=clean_share)


def judge(lyrics: Optional[str], transcript: Optional[str], thresholds: Thresholds) -> Verdict:
    """
    Judges a song by how many of its lyric swears were heard.

    Heard is not capped at the lyrics' count, since a model can mishear a word as a swear.
    Exactly at a threshold counts for that side, so a share of 35% is explicit and one of 15%
    is clean.

    Args:
        lyrics: The song's lyrics, or None if none were found.
        transcript: What the model heard of the song, or None if it was not listened to.
        thresholds: The settings to judge by.

    Returns:
        "not_judged" with no lyrics, or if they have enough swears and the song was not
        heard; "too_few" if the lyrics have fewer than the minimum (nothing to censor, so
        the song need not have been heard); else "explicit", "clean", or "unsure" between
        the two shares.
    """
    if lyrics is None:
        return Verdict("not_judged", 0, 0, "no lyrics")
    expected = count_swears(lyrics)
    if expected < thresholds.minimum:
        return Verdict("too_few", expected, 0)
    if transcript is None:
        return Verdict("not_judged", expected, 0, "not heard")
    heard = count_swears(transcript)
    if heard / expected >= thresholds.explicit_share:
        return Verdict("explicit", expected, heard)
    if heard / expected <= thresholds.clean_share:
        return Verdict("clean", expected, heard)
    return Verdict("unsure", expected, heard)
