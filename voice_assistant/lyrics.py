"""
Lyrics Module
Finds a song's lyrics, for listening to it against them (decision 0019): Navidrome's first,
which it reads from the .lrc or .txt SoulSync saves beside a song, else LRCLIB's - a free,
public lyrics library, searched by title and artist.

The lyrics only say how many swears a song has, so a wrong song's lyrics would judge it
wrongly: LRCLIB's are only taken from a result with the song's title and artist, within
LRCLIB_WINDOW seconds of its length.
"""

import logging
import re
from typing import Dict, Optional, Tuple

import requests

from voice_assistant import network
from voice_assistant.navidrome import BRACKETED, NavidromeLibrary, normalise

# Logger setup
logger = logging.getLogger(__name__)

LRCLIB_SEARCH = "https://lrclib.net/api/search"

# Seconds to wait for LRCLIB, on the internet, before going without.
LRCLIB_TIMEOUT = 15

# How far, in seconds, LRCLIB's length for a song can be from Navidrome's and still be the
# same recording. Album and single versions differ by a second or two; a radio edit or a
# live take is usually further off, and has other words.
LRCLIB_WINDOW = 10

# Apostrophes, straight and curly, as tags and LRCLIB each have them.
APOSTROPHES = re.compile("['\u2019]")

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
        LRCLIB's lyrics for the result with the song's title and artist nearest its
        length, within LRCLIB_WINDOW seconds - None where there's none, or LRCLIB
        couldn't be asked.
        """
        length = song.get("duration")
        if not _is_number(length) or not network.online():
            return None
        # Its first artist alone: Navidrome joins several into one name - "Travis Scott •
        # Drake" - which LRCLIB has under "Travis Scott" alone.
        names = [entry.get("name") for entry in song.get("artists") or []
                 if isinstance(entry, dict) and entry.get("name")]
        artist = (names + [song.get("artist") or ""])[0]
        title = song.get("title") or ""
        # No artist would be "in" every artist LRCLIB names, and any song of the title would do.
        if not _plain(artist):
            return None
        try:
            response = self._session.get(LRCLIB_SEARCH, params={"track_name": title, "artist_name": artist},
                                         headers={"User-Agent": USER_AGENT}, timeout=LRCLIB_TIMEOUT)
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't search LRCLIB for lyrics: {type(e).__name__}")
            return None
        if not isinstance(results, list):
            return None
        near = [result for result in results
                if isinstance(result, dict) and _same_song(result, title, artist)
                and _is_number(result.get("duration")) and abs(result["duration"] - length) <= LRCLIB_WINDOW
                and isinstance(result.get("plainLyrics"), str) and result["plainLyrics"].strip()]
        if not near:
            return None
        return min(near, key=lambda result: abs(result["duration"] - length))["plainLyrics"]


def _same_song(result: Dict, title: str, artist: str) -> bool:
    """
    Whether an LRCLIB result is the song asked for by its names: the same title, or the
    same once bracketed parts are off both - "Gangsta's Paradise (feat. L.V.)" for
    "Gangsta's Paradise" - and an artist naming the song's first artist, as "Gunna & Lil
    Baby" names "Gunna". LRCLIB's search also finds "Endless Summer" for "Endless".
    """
    found = result.get("trackName")
    by = result.get("artistName")
    if not isinstance(found, str) or not isinstance(by, str):
        return False
    same_title = (_plain(found) == _plain(title)
                  or _plain(BRACKETED.sub(" ", found)) == _plain(BRACKETED.sub(" ", title)))
    return same_title and _plain(artist) in _plain(by)


def _plain(name: str) -> str:
    """
    A name as normalise has it, with apostrophes dropped first: normalise drops a curly
    one but spaces a straight one, so "Gangsta’s" and "Gangsta's" would differ.
    """
    return normalise(APOSTROPHES.sub("", name))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
