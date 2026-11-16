"""
Lyrics Module
Finds a song's lyrics, for listening to it against them (decision 0019): Navidrome's first,
which it reads from the .lrc or .txt SoulSync saves beside a song, else LRCLIB's - a free,
public lyrics library, searched by title and artist.

The lyrics only say how many swears a song has, so a wrong song's lyrics would judge it
wrongly: LRCLIB's are only taken from a song within LRCLIB_WINDOW seconds of its length.
"""

import logging
from typing import Dict, Optional, Tuple

import requests

from voice_assistant import network
from voice_assistant.navidrome import NavidromeLibrary

# Logger setup
logger = logging.getLogger(__name__)

LRCLIB_SEARCH = "https://lrclib.net/api/search"

# Seconds to wait for LRCLIB, on the internet, before going without.
LRCLIB_TIMEOUT = 15

# How far, in seconds, LRCLIB's length for a song can be from Navidrome's and still be the
# same recording. Album and single versions differ by a second or two; a radio edit or a
# live take is usually further off, and has other words.
LRCLIB_WINDOW = 10

# LRCLIB asks every app to name itself and where it's from.
USER_AGENT = "Nova (https://github.com/NAmondsenn/nova)"


class Lyrics:
    """A song's lyrics, from Navidrome or else LRCLIB."""

    def __init__(self, library: NavidromeLibrary, session=None):
        """
        Args:
            library: The Navidrome to ask first.
            session: What LRCLIB is asked through - a requests.Session, or None for a new one.
        """
        self.library = library
        self._session = session or requests.Session()

    def for_song(self, song: Dict) -> Tuple[Optional[str], str]:
        """
        A song's lyrics, and where they came from.

        Args:
            song: The song as Navidrome lists it, with its 'id', 'title', 'artist' (and
                'artists', where Navidrome lists them) and 'duration' in seconds.

        Returns:
            The lyrics and "navidrome" or "lrclib" - or (None, "none") where neither has
            them, or LRCLIB couldn't be asked.

        Raises:
            NavidromeError: If Navidrome couldn't be asked. The song's sound comes from it
                too, so the song can't be listened to anyway.
        """
        found = self.library.lyrics(song["id"])
        if found:
            return found, "navidrome"
        found = self._from_lrclib(song)
        if found:
            return found, "lrclib"
        return None, "none"

    def _from_lrclib(self, song: Dict) -> Optional[str]:
        """
        LRCLIB's lyrics for the result nearest the song's length, within LRCLIB_WINDOW
        seconds - None where there's none, or LRCLIB couldn't be asked.
        """
        length = song.get("duration")
        if not _is_number(length) or not network.online():
            return None
        # Its first artist alone: Navidrome joins several into one name - "Travis Scott •
        # Drake" - which LRCLIB has under "Travis Scott" alone.
        artists = song.get("artists") or []
        artist = (artists[0].get("name") if isinstance(artists[0], dict) else None) if artists else None
        try:
            response = self._session.get(LRCLIB_SEARCH,
                                         params={"track_name": song.get("title") or "",
                                                 "artist_name": artist or song.get("artist") or ""},
                                         headers={"User-Agent": USER_AGENT}, timeout=LRCLIB_TIMEOUT)
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't search LRCLIB for lyrics: {type(e).__name__}")
            return None
        if not isinstance(results, list):
            return None
        near = [result for result in results
                if isinstance(result, dict) and _is_number(result.get("duration"))
                and abs(result["duration"] - length) <= LRCLIB_WINDOW
                and isinstance(result.get("plainLyrics"), str) and result["plainLyrics"].strip()]
        if not near:
            return None
        return min(near, key=lambda result: abs(result["duration"] - length))["plainLyrics"]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
