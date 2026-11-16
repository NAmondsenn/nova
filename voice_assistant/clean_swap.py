"""
Clean Swap Module
Hears a song against its lyrics to judge it clean or explicit (Ears), for a download and for
the library check - decision 0019, sections 1 to 3.

Ears fetches what it needs from Navidrome - the file's size and time, the lyrics, the sound -
and gives the sound to the listening worker, then keeps the verdict with the file's size and
time, so a song is only heard again once its file changes. Every failure to hear a song is
"not judged" with a plain reason, never a guess: a song judged clean gets moved out of the
library.
"""

import logging
import os
import threading
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import requests

from voice_assistant import music
from voice_assistant.clean_ear import Thresholds, Verdict, count_swears, judge
from voice_assistant.clean_store import CleanStore
from voice_assistant.listener import ListenError, Listener, decode
from voice_assistant.music_folder import MusicFolderError
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary
from voice_assistant.nemotron import RATE

logger = logging.getLogger(__name__)

# Fewer words than this heard in a whole song means the listening went wrong, not that the
# song is clean: a clean edit still has its other words - Gunna's clean "endless" gave 321 -
# and judging near-silence would call any song clean, and swap it for nothing.
HEARD_ENOUGH = 30

# How long the worker is given for a song: three times its length and a minute. Nemotron took
# about 35 s for a two-minute song on two of pve2's cores, so this is only for a stuck worker.
TIMEOUT_TIMES = 3
TIMEOUT_MORE = 60

# Which of several copies' verdicts says most of a download, best first: one copy heard
# explicit (or with nothing to bleep) is the download, whatever the others are; a copy only
# judged clean when no other copy could be anything else.
BEST_FIRST = ("explicit", "too_few", "unsure", "not_judged", "clean")


def song_name(song: Dict) -> str:
    """How a song is named on the page's lists: "Gunna - endless", or its title alone."""
    title, artist = song.get("title") or "", song.get("artist") or ""
    return f"{artist} - {title}" if artist else title


def listening_library(config) -> Callable[[], Optional[NavidromeLibrary]]:
    """
    Makes the one NavidromeLibrary that listening and swapping share - Navidrome limits how
    often its own API can be signed in to, and a library signs in once and keeps its token -
    made again only when the settings it's made from change.

    Args:
        config: Configs loaded from config.yaml - navidrome.url and navidrome.username, read
            each time, with NAVIDROME_PASSWORD.

    Returns:
        What gives the library, or None while Navidrome isn't set up.
    """
    lock = threading.Lock()
    kept: Dict[str, object] = {}

    def library() -> Optional[NavidromeLibrary]:
        if music._missing(config, "navidrome"):
            return None
        settings = config.section("navidrome")
        made_from = (settings["url"].strip(), settings["username"].strip(), os.getenv("NAVIDROME_PASSWORD"))
        with lock:
            if kept.get("made_from") != made_from:
                kept["made_from"] = made_from
                kept["library"] = NavidromeLibrary(*made_from)
            return kept["library"]

    return library


class Ears:
    """Hears one song at a time against its lyrics, and keeps what it made of each."""

    def __init__(self, config, library_factory: Callable[[], Optional[NavidromeLibrary]], listener: Listener,
                 lyrics_factory: Callable, store: CleanStore):
        """
        Args:
            config: Configs loaded from config.yaml - the clean_check thresholds, read at each song.
            library_factory: Gives the NavidromeLibrary to ask, or None if it isn't set up - see
                listening_library.
            listener: The listening worker, whose lock is held across a whole song.
            lyrics_factory: Makes a song's Lyrics from the library - lyrics.Lyrics.
            store: Where each verdict is kept.
        """
        self.config = config
        self._library = library_factory
        self.listener = listener
        self._lyrics = lyrics_factory
        self.store = store
        # How many downloads are being heard, or waiting to be: the library check waits while
        # there are any, so a download is said sooner.
        self._downloads = 0
        self._count_lock = threading.Lock()

    def busy_with_download(self) -> bool:
        """Whether a download's song is being heard, or waiting for the worker."""
        with self._count_lock:
            return self._downloads > 0

    def hear(self, song: Dict, download: bool = False) -> Verdict:
        """
        Judges a song by ear, and keeps the verdict with its file's size and time.

        Args:
            song: The song as Navidrome lists it, with its 'id', 'title' and 'artist'.
            download: Whether it's a download's, which goes ahead of the library check.

        Returns:
            The Verdict. "not_judged" ones - no lyrics, or listening that failed - say why, and
            aren't kept, so the song is tried again.
        """
        if download:
            with self._count_lock:
                self._downloads += 1
        try:
            # Held from the file to the verdict, so the size and time kept are the file heard.
            with self.listener.lock:
                verdict, file = self._hear(song)
                self._keep(song, file, verdict)
                return verdict
        finally:
            if download:
                with self._count_lock:
                    self._downloads -= 1

    def hear_copies(self, copies: List[Dict], download: bool = False) -> Tuple[Verdict, Dict]:
        """
        Hears the copies of a song that landed, in turn, until one is explicit or has nothing to
        bleep - which is the download - and says which verdict says most of them.

        Args:
            copies: The new copies, as Navidrome lists them; at least one.
            download: Whether they're a download's.

        Returns:
            The verdict best for the download, by BEST_FIRST, and the copy it's of - the first
            of them, if every copy heard was clean.
        """
        best: Optional[Tuple[Verdict, Dict]] = None
        for copy in copies:
            verdict = self.hear(copy, download)
            if best is None or BEST_FIRST.index(verdict.kind) < BEST_FIRST.index(best[0].kind):
                best = (verdict, copy)
            if verdict.kind in ("explicit", "too_few"):
                break
        return best

    def _hear(self, song: Dict) -> Tuple[Verdict, Optional[Dict]]:
        """The verdict, and the song's file as Navidrome gave it - None if it couldn't."""
        library = self._library()
        if library is None:
            return Verdict("not_judged", 0, 0, "Navidrome isn't set up"), None
        file, lyrics = None, None
        thresholds = Thresholds.from_config(self.config)
        try:
            file = library.song_file(song["id"])
            lyrics, _source = self._lyrics(library).for_song(song)
            # Lyrics first, so a song with nothing to bleep is never listened to.
            if lyrics is None or count_swears(lyrics) < thresholds.minimum:
                return judge(lyrics, None, thresholds), file
            samples = decode(library.audio(song["id"]))
            heard = self.listener.transcribe(samples, timeout=TIMEOUT_TIMES * len(samples) / RATE + TIMEOUT_MORE)
        except (NavidromeError, ListenError) as e:
            return self._not_judged(lyrics, thresholds, str(e)), file
        except MusicFolderError as e:
            return self._not_judged(lyrics, thresholds, e.reason), file
        except requests.RequestException as e:
            # Its kind alone: the error itself can hold a signed address.
            return self._not_judged(lyrics, thresholds, f"couldn't reach Navidrome ({type(e).__name__})"), file
        if len(heard.split()) < HEARD_ENOUGH:
            return self._not_judged(lyrics, thresholds, "heard too little"), file
        return judge(lyrics, heard, thresholds), file

    @staticmethod
    def _not_judged(lyrics: Optional[str], thresholds: Thresholds, reason: str) -> Verdict:
        """Not judged, with why - and how many swears the lyrics have, once they're known."""
        if lyrics is None:
            return Verdict("not_judged", 0, 0, reason)
        return replace(judge(lyrics, None, thresholds), reason=reason)

    def _keep(self, song: Dict, file: Optional[Dict], verdict: Verdict):
        """
        Keeps a verdict with the file's size and time, and puts an unsure song on the page's
        list. A store that can't be saved to is logged: the verdict still stands for now.
        """
        name = song_name(song)
        logger.info(f"Heard {name}: {verdict.kind}, {verdict.heard} of {verdict.expected} swears"
                    + (f" ({verdict.reason})" if verdict.reason else ""))
        if verdict.kind == "not_judged" or file is None:
            return
        try:
            self.store.set_verdict(song["id"], file["size"], file["updated"], verdict, name)
            if verdict.kind == "unsure":
                self.store.add("unsure", name, f"heard {verdict.heard} of its {verdict.expected} swears",
                               song_id=song["id"])
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Couldn't keep what was heard of {name}: {type(e).__name__}")
