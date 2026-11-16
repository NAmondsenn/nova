"""
New Music Module.

Keeps Nova's own playlist in Navidrome of the music which has come in lately: the playlist
named by new_music.playlist on the settings page, "New Music" unless renamed. It is a
Nova-owned playlist - Nova makes it, and replaces what's in it each morning - and it is what
"find me something new" plays and what the user opens in his own player.

SoulSync's discovery playlists download songs he hasn't got, but never reach Navidrome
themselves: they only ever hold songs not yet in the library, and SoulSync makes no Navidrome
playlist of one which matches nothing there. So this gathers what arrived instead, whatever
brought it in.

NewMusicPlaylist, a thread, looks once an hour and once as it starts, and makes the playlist at
the first look of each day from 6 in the morning, after SoulSync's nightly run at 4: the songs
which came into Navidrome in the last new_music.days days, newest first, at most MOST_SONGS of
them, in the playlist of that name which belongs to the Navidrome user Nova signs in as -
made if it isn't there, its songs replaced if it is, and public, so his own Navidrome user sees
it. Nothing new leaves it as it is. No other playlist is ever touched. Done, the day is noted
in a small record in the data folder, so a restart doesn't make it twice; Navidrome not set up
or not answering leaves the day to the next look.

What he listens to is never logged: what is said is counts, never a song.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import requests

from voice_assistant import storage
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary

# Logger setup
logger = logging.getLogger(__name__)

# Seconds between the looks at whether today's playlist is due.
LOOK_EVERY = 3600

# The hour of the morning, local time, from which a day's playlist is made: after SoulSync's
# nightly run at 4 has downloaded what it found.
FROM_HOUR = 6

# How many days of new music the playlist holds when the setting isn't a whole number from 1
# to 60, and how many songs it holds at most - the newest.
DEFAULT_DAYS = 14
MOST_DAYS = 60
MOST_SONGS = 500


def _days(value) -> int:
    """The setting's days, or DEFAULT_DAYS if it isn't a whole number from 1 to MOST_DAYS."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MOST_DAYS:
        return value
    return DEFAULT_DAYS


class NewMusicPlaylist:
    """
    Looks once an hour, and once as it starts, at whether today's new-music playlist has been
    made, and makes it if not - see the module's docstring.
    """

    def __init__(self, library: Callable[[], Optional[NavidromeLibrary]], store_path: str,
                 settings: Callable[[], Dict], clock: Callable[[], float] = time.time):
        """
        Args:
            library: Gives a NavidromeLibrary from the settings as they are now, or None while
                Navidrome isn't set up - Downloads.library.
            store_path: The record of the day last made, new_music.json in the data folder.
            settings: Gives the new_music section of the settings as they are now, read at each
                look: playlist, the playlist's name, and days, how far back it reaches. A
                change is used at the next day's making.
            clock: Gives the current epoch time, only replaced by the tests.
        """
        self._library = library
        self._path = store_path
        self._settings = settings
        self._clock = clock
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the daemon thread, which looks at once and then every LOOK_EVERY seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="new-music")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a look already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.look()
            except Exception:
                logger.exception("Making the new-music playlist failed, will try again")
            self._stopping.wait(LOOK_EVERY)

    def _done_day(self) -> Optional[str]:
        """The day last made, e.g. "2026-11-16", or None."""
        try:
            record = storage.load_json(self._path, {})
        except (OSError, ValueError):
            # Read as not made: making it again only replaces it with the same.
            logger.warning("The new-music record couldn't be read, so today's playlist is made again")
            return None
        return record.get("day") if isinstance(record, dict) else None

    def look(self):
        """One look: makes today's playlist if it's due and not made yet."""
        now = self._clock()
        # The local time as the system's zone has it - 6 in the morning is the user's, not UTC's.
        today = datetime.fromtimestamp(now)
        day = today.date().isoformat()
        if today.hour < FROM_HOUR or self._done_day() == day:
            return
        settings = self._settings() or {}
        name = str(settings.get("playlist") or "").strip()
        library = self._library()
        if not name or library is None:
            return
        days = _days(settings.get("days"))
        try:
            songs = library.arrived_songs(now - days * 86400, MOST_SONGS)
            # Nothing new leaves the playlist as it was, rather than empty.
            there = library.replace_playlist(name, songs) if songs else None
        except (requests.RequestException, NavidromeError, ValueError) as e:
            # Only the kind of error: its words can hold the request's address, and with it the signed-in token.
            logger.info(f"The new-music playlist wasn't made ({type(e).__name__}), will try again at the next look")
            return
        if songs:
            logger.info(f"{'Replaced' if there else 'Made'} the new-music playlist with {len(songs)} songs "
                        f"from the last {days} days")
        else:
            logger.info(f"Nothing new in the last {days} days, so the new-music playlist is left as it was")
        storage.save_json(self._path, {"day": day})
