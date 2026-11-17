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
them, in the playlist Nova made, public, so his own Navidrome user sees it. Nothing new leaves it
as it is.

Nova only ever writes the playlist it made itself, found by the id it recorded when it made it
and never by name: Nova signs in to Navidrome as the same user who may own all of his own
playlists, so a playlist of this name which he made himself must never be taken for Nova's. The
first look, or one after he deleted Nova's, makes a new playlist under the setting's name and
records its id; a changed name does the same, and leaves the old playlist for him to delete.
Nothing else is ever touched. The record in the data folder holds that id and the name it was
made under, and the day last done, so a restart doesn't make it twice; Navidrome not set up or
not answering leaves the day to the next look.

What he listens to is never logged: what is said is counts, never a song.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import requests

from voice_assistant import storage
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary, normalise

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
            store_path: The record of the playlist Nova made (its id and the name it was made
                under) and the day last made, new_music.json in the data folder.
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

    def _record(self) -> Dict:
        """The record: day, id and name, as many as are in it - empty if there is none or it can't be read."""
        try:
            record = storage.load_json(self._path, {})
        except (OSError, ValueError):
            # Read as not made, and with no playlist recorded: a new one is made, never one found by name.
            logger.warning("The new-music record couldn't be read, so today's playlist is made again")
            return {}
        return record if isinstance(record, dict) else {}

    def playlist_id(self, name: str) -> Optional[str]:
        """
        The id of the playlist Nova made under this name, as recorded, for "find me something
        new" to play - or None if it hasn't made one under this name yet.
        """
        record = self._record()
        key = normalise(name)
        return record.get("id") if key and record.get("name") == key and isinstance(record.get("id"), str) else None

    def look(self):
        """One look: makes today's playlist if it's due and not made yet."""
        now = self._clock()
        # The local time as the system's zone has it - 6 in the morning is the user's, not UTC's.
        today = datetime.fromtimestamp(now)
        day = today.date().isoformat()
        record = self._record()
        if today.hour < FROM_HOUR or record.get("day") == day:
            return
        settings = self._settings() or {}
        name = str(settings.get("playlist") or "").strip()
        library = self._library()
        if not name or library is None:
            return
        key = normalise(name)
        if not key:
            # Only punctuation or emoji: nothing to tell its playlist by, so none is made.
            logger.info("The new-music playlist's name has no letters or digits, so it isn't made")
            return
        days = _days(settings.get("days"))
        try:
            songs = library.arrived_songs(now - days * 86400, MOST_SONGS)
            made = False
            if songs:
                # Only the one Nova made, by its id, and only under this name: a new name is a new playlist.
                playlist_id = record.get("id") if record.get("name") == key else None
                if not playlist_id or library.playlist_with_id(playlist_id) is None:
                    # New, or the one he deleted. Its id is kept before it is filled, so a failure part way
                    # fills this one at the next look, not another.
                    playlist_id = library.make_playlist(name)
                    made = True
                    record = {**record, "id": playlist_id, "name": key}
                    storage.save_json(self._path, record)
                library.replace_playlist(playlist_id, songs)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            # Only the kind of error: its words can hold the request's address, and with it the signed-in token.
            logger.info(f"The new-music playlist wasn't made ({type(e).__name__}), will try again at the next look")
            return
        if songs:
            logger.info(f"{'Made' if made else 'Replaced'} the new-music playlist with {len(songs)} songs "
                        f"from the last {days} days")
        else:
            logger.info(f"Nothing new in the last {days} days, so the new-music playlist is left as it was")
        storage.save_json(self._path, {**record, "day": day})
