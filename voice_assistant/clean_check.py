"""
Clean Check Module
The library check (decision 0019, section 5): every song in Navidrome heard against its lyrics,
one at a time, in the background, and each one heard clean swapped quietly for an explicit copy.

Only songs which are new, or whose file has changed since they were last judged, are heard. A
song whose lyrics have too few swears is passed over without being listened to - Ears reads the
lyrics first. A download's hearing goes ahead: the check waits while one is being heard.

The check says nothing aloud and sends nothing to the phone; how it went is on the settings
page. Its progress is saved after each song, so a restart carries it on from the song it was
on, and the page's Stop ends it once the song it's on is done.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from voice_assistant.clean_store import KINDS, CleanStore
from voice_assistant.clean_swap import Ears, Swapper, song_name
from voice_assistant.downloads import NAVIDROME_ERRORS, Wanted, _without_clean_note
from voice_assistant.music_folder import MusicFolder
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary

logger = logging.getLogger(__name__)

# How many albums Navidrome is asked for at a time - the most getAlbumList2 gives.
PAGE = 500

# Seconds between looks at whether a download is still being heard, while the check waits.
BUSY_POLL = 5

# What's kept of each song still to do: enough to hear it (its lyrics are looked up on LRCLIB
# by title, artist and length) and to ask SoulSync for it again.
KEPT = ("id", "title", "artist", "artists", "album", "duration")

NO_NAVIDROME = "Navidrome isn't set up, so the library can't be checked."
WENT_WRONG = "something went wrong part way"


class CleanCheck:
    """Checks the whole library for clean songs, one at a time, on a thread of its own."""

    def __init__(self, ears: Ears, swapper: Swapper, store: CleanStore,
                 library_factory: Callable[[], Optional[NavidromeLibrary]],
                 folder_factory: Callable[[], Optional[MusicFolder]] = lambda: None,
                 wait: Optional[Callable[[float], bool]] = None, now: Callable[[], float] = time.time):
        """
        Args:
            ears: Hears each song - the brain's own, so the check and the downloads take turns.
            swapper: Swaps a song heard clean.
            store: Where the check's progress, and the page's lists, are kept.
            library_factory: Gives the NavidromeLibrary, or None if it isn't set up.
            folder_factory: Gives the MusicFolder from the settings as they are now, for how
                many songs are held.
            wait: Waits the seconds given while a download is heard, and says whether the
                check is stopping - by default the wait of the event stop and close set.
            now: The wall clock, in seconds - for when the last check finished.
        """
        self._ears = ears
        self._swapper = swapper
        self._store = store
        self._library = library_factory
        self._folder = folder_factory
        self._now = now
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        # The page's Stop: the check ends after the song it's on, and isn't carried on.
        self._stop_asked = threading.Event()
        # Nova stopping: the check ends as soon as it can, and is carried on at the next start.
        self._closing = threading.Event()
        # Cuts a wait for a download short, for either of them.
        self._woken = threading.Event()
        self._wait = wait or self._woken.wait
        self._progress = self._saved()

    def start(self) -> Dict:
        """
        Starts a check of the whole library, in the background.

        Returns:
            {"success", "message"} - refused while a check is running, without Navidrome, or as
            Nova stops.
        """
        if self._library() is None:
            return {"success": False, "message": NO_NAVIDROME}
        with self._lock:
            if self._thread is not None:
                return {"success": False, "message": "The library check is already running."}
            if self._closing.is_set():
                return {"success": False, "message": "Nova is stopping, so the library can't be checked now."}
            self._progress = {"running": True, "done": 0, "total": 0, "current": None, "queue": None,
                              "last_finished": self._progress.get("last_finished"), "error": None}
            self._save()
            self._go()
        logger.info("Checking the library for clean songs")
        return {"success": True, "message": "Checking the library."}

    def stop(self) -> Dict:
        """
        Ends the check once the song it's on is done - the page's Stop. It isn't carried on at
        the next start.

        Returns:
            {"success", "message"}.
        """
        with self._lock:
            if self._thread is None:
                return {"success": False, "message": "The library check isn't running."}
            self._stop_asked.set()
            self._woken.set()
        logger.info("The library check will stop after this song")
        return {"success": True, "message": "The library check will stop after this song."}

    def close(self):
        """
        Ends the check as Nova stops, kept as it is to be carried on at the next start. A song
        cut short by the listening worker closing is heard again then.
        """
        self._closing.set()
        self._woken.set()

    def resume(self):
        """Carries on a check a restart stopped, from its saved queue - or from listing the library again."""
        with self._lock:
            if self._thread is not None or not self._progress.get("running") or self._closing.is_set():
                return
            logger.info(f"Carrying on the library check, {self._progress.get('done', 0)} of "
                        f"{self._progress.get('total', 0)} songs done")
            self._go()

    def state(self) -> Dict:
        """
        Returns:
            How the check is going, for the settings page: running, done, total, current (the
            song being heard, "Gunna - endless", or None), last_finished (seconds since the
            epoch, or None), error (why the last check ended early, or None), the four lists -
            swapped, only_clean, unsure and couldnt, newest first - and held, how many songs
            wait in the music folder's .nova-held (0 if the folder isn't usable).
        """
        with self._lock:
            progress = dict(self._progress)
        state = {"running": bool(progress.get("running")), "done": progress.get("done", 0),
                 "total": progress.get("total", 0), "current": progress.get("current"),
                 "last_finished": progress.get("last_finished"), "error": progress.get("error")}
        for kind in KINDS:
            state[kind] = self._store.listed(kind)
        state["held"] = self._held()
        return state

    def _held(self) -> int:
        """How many songs are in .nova-held, or 0 if the music folder isn't usable."""
        try:
            folder = self._folder()
            if folder is None or folder.usable():
                return 0
            return folder.held_count()
        except OSError as e:
            logger.warning(f"Couldn't count the songs held: {type(e).__name__}")
            return 0

    def _saved(self) -> Dict:
        """The progress last saved, or none."""
        saved = self._store.check()
        if saved.get("running") and not isinstance(saved.get("queue"), (list, type(None))):
            logger.warning("The saved library check's queue wasn't a list, so it's listed again")
            saved["queue"] = None
        return saved

    def _go(self):
        """Runs the check on a thread of its own. Called with the lock held."""
        self._stop_asked.clear()
        self._woken.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="clean-check")
        self._thread.start()

    def _run(self):
        try:
            self._check()
        except Exception:
            # Ended rather than left running, or each start would carry the same failure on.
            logger.exception("The library check failed")
            self._end(error=WENT_WRONG)
        finally:
            with self._lock:
                self._thread = None

    def _check(self):
        """Lists the songs to hear, if they aren't yet, then hears each in turn."""
        library = self._library()
        if library is None:
            self._end(error=NO_NAVIDROME)
            return
        if self._progress.get("queue") is None:
            try:
                queue = self._list(library)
            except NAVIDROME_ERRORS as e:
                # Only the kind of error, never the error itself, which can hold the signed address.
                why = f": {e}" if isinstance(e, NavidromeError) else f" ({type(e).__name__})"
                logger.warning(f"Couldn't list the songs in Navidrome{why}")
                self._end(error=f"couldn't list the songs in Navidrome{why}")
                return
            if queue is None:
                self._ended()
                return
            self._update(queue=queue, total=len(queue))
            logger.info(f"The library check has {len(queue)} songs to hear")
        while self._progress["queue"]:
            if self._ending() or not self._downloads_heard():
                break
            song = self._progress["queue"][0]
            with self._lock:
                self._progress["current"] = song_name(song)
            if not self._one(song):
                break
            self._update(queue=self._progress["queue"][1:], done=self._progress.get("done", 0) + 1)
        self._ended()

    def _list(self, library: NavidromeLibrary) -> Optional[List[Dict]]:
        """
        Every song in Navidrome not judged already with the file it has now, album by album,
        each song once.

        Returns:
            The songs, as KEPT keeps them, or None if the check is stopping.

        Raises:
            requests.RequestException, NavidromeError or ValueError if Navidrome couldn't be asked.
        """
        songs, seen, offset = [], set(), 0
        while True:
            albums = library._call("getAlbumList2", type="alphabeticalByName", size=PAGE,
                                   offset=offset).get("albumList2", {}).get("album") or []
            for album in albums:
                if self._ending():
                    return None
                for song in library._album_songs(album["id"]):
                    if song.get("id") in seen:
                        continue
                    seen.add(song.get("id"))
                    if not self._known(library, song):
                        songs.append({key: song[key] for key in KEPT if key in song})
            if len(albums) < PAGE:
                return songs
            offset += PAGE

    def _known(self, library: NavidromeLibrary, song: Dict) -> bool:
        """
        Whether a song was judged with the file it has now. One whose file Navidrome won't give
        is heard anyway, which says why it can't be judged. A song which only came down clean is
        known: the copy left has its own verdict.
        """
        try:
            file = library.song_file(song["id"])
        except NavidromeError:
            return False
        return self._store.known(song["id"], file["size"], file["updated"])

    def _downloads_heard(self) -> bool:
        """Waits while a download is being heard. False if the check is stopping meanwhile."""
        while self._ears.busy_with_download():
            if self._wait(BUSY_POLL) or self._ending():
                return False
        return not self._ending()

    def _one(self, song: Dict) -> bool:
        """
        Hears one song, and swaps it quietly if it's clean.

        Returns:
            Whether it's done with - False if Nova is stopping and it's to be heard again.
        """
        name = song_name(song)
        try:
            verdict = self._ears.hear(song)
            if self._closing.is_set() and verdict.kind in ("not_judged", "clean"):
                # Cut short as the worker closed - or heard clean, but with no time to swap it.
                return False
            if verdict.kind == "clean":
                # Asked for by its title without the clean note, as SoulSync names the explicit copy.
                wanted = Wanted(_without_clean_note(song.get("title") or ""), song.get("artist") or None, "track")
                outcome = self._swapper.swap(song, wanted, None, quiet=True)
                if outcome == "stopped" and self._closing.is_set():
                    # Heard again at the next start: by then its saved swap has been carried on,
                    # and the song set aside isn't there to hear - or, if nothing had moved yet,
                    # ended, and the song is swapped afresh.
                    return False
                logger.info(f"The library check's swap of {name} ended {outcome}"
                            + (f": {outcome.reason}" if outcome.reason else ""))
        except Exception:
            # One song's surprise mustn't end the check.
            logger.exception(f"The library check couldn't check {name}")
        return True

    def _ending(self) -> bool:
        return self._stop_asked.is_set() or self._closing.is_set()

    def _ended(self):
        """
        Ends the check as it stood: kept running, to carry on, if Nova is stopping; otherwise
        finished, or stopped by the page's Stop.
        """
        if self._closing.is_set():
            self._save()
            logger.info("The library check stopped as Nova stops, and carries on at its next start")
        elif self._stop_asked.is_set():
            self._end()
            logger.info("The library check was stopped")
        else:
            self._end(finished=True)
            logger.info(f"The library check finished: {self._progress.get('done', 0)} songs heard")

    def _end(self, error: Optional[str] = None, finished: bool = False):
        """Saves the check as over, with why it ended early or when it finished."""
        changes = {"running": False, "current": None, "queue": None, "error": error}
        if finished:
            changes["last_finished"] = self._now()
        self._update(**changes)

    def _update(self, **changes):
        """Changes the progress, and saves it."""
        with self._lock:
            self._progress = {**self._progress, **changes}
            self._save()

    def _save(self):
        """
        Saves the progress, called with the lock held. A failure is only logged: the check goes
        on, and at worst a restart hears a song or two again.
        """
        try:
            self._store.save_check(self._progress)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Couldn't save the library check's progress: {type(e).__name__}")
