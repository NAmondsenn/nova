"""
Clean Check Module
The library check (decision 0019, section 5): every song in Navidrome heard against its lyrics,
one at a time, in the background, and each one heard clean swapped quietly for an explicit copy.

Only songs which are new, or whose file has changed since they were last judged, are heard. A
song whose lyrics have too few swears is passed over without being listened to - Ears reads the
lyrics first. A song heard clean before whose swap couldn't be made is swapped again without
being heard; one which only came down clean is left. A download's hearing goes ahead: the check
waits while one is being heard.

The check says nothing aloud and sends nothing to the phone; how it went is on the settings
page. Its progress is saved after each song, so a restart carries it on from the song it was
on, and the page's Stop ends it once the song it's on is done. With Navidrome out of reach it
tries the same song again each minute for a while, then pauses, to carry on at the next start.
"""

import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Set

import requests

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

# With Navidrome out of reach - as after pve2 restarts, when the brain can be up before it - the
# same song, or the listing, is tried again this many times, this many seconds apart, before
# the check pauses, rather than every song being used up as not judged.
RETRIES = 10
RETRY_AFTER = 60

# A song not judged because Navidrome couldn't be reached, by the reason Ears gives: "couldn't
# reach Navidrome (ConnectionError)", or the library's "Couldn't ask Navidrome for the song's
# file: ConnectTimeout" - or "...: HTTPError 502" while it starts.
OUT_OF_REACH = re.compile(r"^(couldn't reach Navidrome \(|Couldn't ask Navidrome for [^:]+: "
                          r"(\w*(Connect|Timeout|Proxy)\w*|HTTPError 5\d\d)$)")

# What's kept of each song still to do: enough to hear it (its lyrics are looked up on LRCLIB
# by title, artist and length) and to ask SoulSync for it again - and "swap", for one heard
# clean before, which is swapped without being heard again.
KEPT = ("id", "title", "artist", "artists", "album", "duration")

# How a song ended for the check: done with, to be heard again at the next start as Nova
# stops, or not heard as Navidrome couldn't be reached.
DONE = "done"
AGAIN = "again"
UNREACHED = "unreached"

# Why a check ended early, as the page says it.
NO_NAVIDROME = "Navidrome isn't set up, so the library can't be checked."
WENT_WRONG = "Something went wrong part way through the check, so it stopped."
PAUSED = "Navidrome couldn't be reached, so the check stopped here and carries on next time."


def out_of_reach(error: Exception) -> bool:
    """Whether a request failed because Navidrome couldn't be reached, rather than answered."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(error, requests.HTTPError) and isinstance(status, int) and status >= 500


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
            wait: Waits the seconds given - for a download to be heard, or for Navidrome - and
                says whether the check is stopping: by default the wait of the event stop and
                close set.
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
        # Cuts a wait short, for either of them.
        self._woken = threading.Event()
        self._wait = wait or self._woken.wait
        self._progress = self._saved()

    def start(self) -> Dict:
        """
        Starts a check of the whole library, in the background - or carries on one paused as
        Navidrome couldn't be reached, from the song it was on.

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
            if self._progress.get("paused"):
                self._progress = {**self._progress, "running": True, "paused": False, "error": None}
                message = "Carrying on the library check."
            else:
                self._progress = {"running": True, "paused": False, "done": 0, "total": 0, "current": None,
                                  "queue": None, "last_finished": self._progress.get("last_finished"),
                                  "error": None}
                message = "Checking the library."
            self._save()
            self._go()
        logger.info(message)
        return {"success": True, "message": message}

    def stop(self) -> Dict:
        """
        Ends the check once the song it's on is done - the page's Stop. It isn't carried on at
        the next start. A check paused as Navidrome couldn't be reached is dropped, with its
        saved queue, so the next Start checks the whole library afresh.

        Returns:
            {"success", "message"}.
        """
        with self._lock:
            if self._thread is None and self._progress.get("paused"):
                self._progress = {**self._progress, "running": False, "paused": False, "current": None,
                                  "queue": None, "error": None}
                self._save()
                logger.info("The paused library check was dropped")
                return {"success": True, "message": "The paused check is dropped."}
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
        """
        Carries on a check a restart stopped, or one paused as Navidrome couldn't be reached:
        from its saved queue, or from listing the library again if it hadn't one yet.
        """
        with self._lock:
            progress = self._progress
            if self._thread is not None or self._closing.is_set() \
                    or not (progress.get("running") or progress.get("paused")):
                return
            self._progress = {**progress, "running": True, "paused": False, "error": None}
            logger.info(f"Carrying on the library check, {progress.get('done', 0)} of "
                        f"{progress.get('total', 0)} songs done")
            self._go()

    def state(self) -> Dict:
        """
        Returns:
            How the check is going, for the settings page: running; phase - "listing" while
            the songs are being listed, "checking" while they're heard, else None; done, total,
            current (the song being heard, "Gunna - endless", or None); last_finished (seconds
            since the epoch, or None); paused, when Navidrome couldn't be reached and the check
            carries on at the next start or Start; error (a sentence saying why the last check
            ended early, or None); the four lists - swapped, only_clean, unsure and couldnt,
            newest first - and held, how many songs wait in the music folder's .nova-held (0 if
            the folder isn't usable).
        """
        with self._lock:
            progress = dict(self._progress)
        running = bool(progress.get("running"))
        state = {"running": running,
                 "phase": ("listing" if progress.get("queue") is None else "checking") if running else None,
                 "done": progress.get("done", 0), "total": progress.get("total", 0),
                 "current": progress.get("current"), "last_finished": progress.get("last_finished"),
                 "paused": bool(progress.get("paused")), "error": progress.get("error")}
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
        if not isinstance(saved.get("queue"), (list, type(None))):
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
            queue = self._listed(library)
            if queue is None:
                return
            self._update(queue=queue, total=len(queue))
            logger.info(f"The library check has {len(queue)} songs to hear or swap")
        failures = 0
        while self._progress["queue"]:
            if self._ending() or not self._downloads_heard():
                break
            song = self._progress["queue"][0]
            with self._lock:
                self._progress["current"] = song_name(song)
            ended = self._one(song)
            if ended == AGAIN:
                break
            if ended == UNREACHED:
                if failures >= RETRIES:
                    self._pause(PAUSED)
                    return
                failures += 1
                logger.info(f"Navidrome couldn't be reached for {song_name(song)}, so it's tried again "
                            f"in {RETRY_AFTER} s ({failures} of {RETRIES})")
                if self._wait(RETRY_AFTER) or self._ending():
                    break
                continue
            failures = 0
            self._update(queue=self._progress["queue"][1:], done=self._progress.get("done", 0) + 1)
        self._ended()

    def _listed(self, library: NavidromeLibrary) -> Optional[List[Dict]]:
        """
        The songs to do, listed again each minute while Navidrome is out of reach, a set number
        of times - None if the check ended or paused meanwhile, as it's left saved.
        """
        failures = 0
        while True:
            try:
                queue = self._list(library)
            except NAVIDROME_ERRORS as e:
                # Only the kind of error, never the error itself, which can hold the signed address.
                if out_of_reach(e) and failures < RETRIES:
                    failures += 1
                    logger.info(f"Navidrome couldn't be reached to list the songs ({type(e).__name__}), so it's "
                                f"tried again in {RETRY_AFTER} s ({failures} of {RETRIES})")
                    if self._wait(RETRY_AFTER) or self._ending():
                        self._ended()
                        return None
                    continue
                why = str(e) if isinstance(e, NavidromeError) else type(e).__name__
                logger.warning(f"Couldn't list the songs in Navidrome: {why}")
                self._pause(PAUSED if out_of_reach(e) else
                            f"Navidrome couldn't list the songs ({why}), so the check stopped here and "
                            f"carries on next time.")
                return None
            if queue is None:
                self._ended()
            return queue

    def _list(self, library: NavidromeLibrary) -> Optional[List[Dict]]:
        """
        Every song in Navidrome not judged already with the file it has now, album by album,
        each song once - and those heard clean before whose swap couldn't be made.

        Returns:
            The songs, as KEPT keeps them, or None if the check is stopping.

        Raises:
            requests.RequestException, NavidromeError or ValueError if Navidrome couldn't be asked.
        """
        only_clean = self._only_clean()
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
                    todo = self._todo(library, song, only_clean)
                    if todo:
                        kept = {key: song[key] for key in KEPT if key in song}
                        songs.append({**kept, "swap": True} if todo == "swap" else kept)
            if len(albums) < PAGE:
                return songs
            offset += PAGE

    def _only_clean(self) -> Set[str]:
        """
        The ids of the songs which only came down clean: the copy swapped, and the copies kept,
        which SoulSync may have filed elsewhere, under another id.
        """
        ids = set()
        for entry in self._store.listed("only_clean"):
            ids.add(entry.get("song_id"))
            ids.update(entry.get("kept") or ())
        return ids

    def _todo(self, library: NavidromeLibrary, song: Dict, only_clean: Set[str]) -> Optional[str]:
        """
        What a song needs: "hear" if it's new or its file changed, "swap" if it was heard clean
        with this file and its swap couldn't be made, or None. One whose file Navidrome won't
        give is heard anyway, which says why it can't be judged.
        """
        try:
            file = library.song_file(song["id"])
        except NavidromeError:
            return "hear"
        if not self._store.known(song["id"], file["size"], file["updated"]):
            return "hear"
        kind = ((self._store.verdict(song["id"]) or {}).get("verdict") or {}).get("kind")
        if kind == "clean" and song["id"] not in only_clean:
            return "swap"
        return None

    def _being_swapped(self, song_id: str) -> bool:
        """
        Whether a saved swap - carried on after a restart, say - is handling the song: as the
        song it swaps, or a copy a later try brought.
        """
        for swap in self._store.swaps():
            pending = [copy.get("id") for copy in swap.get("pending") or () if isinstance(copy, dict)]
            if song_id in [swap.get("song_id"), (swap.get("copy") or {}).get("id"), *pending]:
                return True
        return False

    def _downloads_heard(self) -> bool:
        """Waits while a download is being heard. False if the check is stopping meanwhile."""
        while self._ears.busy_with_download():
            if self._wait(BUSY_POLL) or self._ending():
                return False
        return not self._ending()

    def _one(self, song: Dict) -> str:
        """
        Hears one song - unless it was heard clean before - and swaps it quietly if it's clean.

        Returns:
            DONE; AGAIN if Nova is stopping and it's to be heard again at the next start; or
            UNREACHED if Navidrome couldn't be reached to hear it.
        """
        name = song_name(song)
        try:
            if self._being_swapped(song["id"]):
                # Its swap's own outcome goes on the page's lists.
                logger.info(f"The library check leaves {name} to the swap already under way")
                return DONE
            if not song.get("swap"):
                verdict = self._ears.hear(song)
                if self._closing.is_set() and verdict.kind in ("not_judged", "clean"):
                    # Cut short as the worker closed - or heard clean, but with no time to swap it.
                    return AGAIN
                if verdict.kind == "not_judged" and OUT_OF_REACH.match(verdict.reason):
                    return UNREACHED
                if verdict.kind != "clean":
                    return DONE
            elif self._closing.is_set():
                return AGAIN
            # Asked for by its title without the clean note, as SoulSync names the explicit copy.
            wanted = Wanted(_without_clean_note(song.get("title") or ""), song.get("artist") or None, "track")
            outcome = self._swapper.swap({key: value for key, value in song.items() if key != "swap"},
                                         wanted, None, quiet=True)
            if outcome == "stopped" and self._closing.is_set():
                # Heard again at the next start: by then its saved swap has been carried on, and
                # the check leaves it to that - or, if nothing had moved yet, ended, and the
                # song is swapped afresh.
                return AGAIN
            logger.info(f"The library check's swap of {name} ended {outcome}"
                        + (f": {outcome.reason}" if outcome.reason else ""))
        except Exception:
            # One song's surprise mustn't end the check.
            logger.exception(f"The library check couldn't check {name}")
        return DONE

    def _ending(self) -> bool:
        return self._stop_asked.is_set() or self._closing.is_set()

    def _ended(self):
        """
        Ends the check as it stood: kept running, to carry on, if Nova is stopping; otherwise
        finished, or stopped by the page's Stop.
        """
        if self._closing.is_set():
            self._save_now()
            logger.info("The library check stopped as Nova stops, and carries on at its next start")
        elif self._stop_asked.is_set():
            self._end()
            logger.info("The library check was stopped")
        else:
            self._end(finished=True)
            logger.info(f"The library check finished: {self._progress.get('done', 0)} songs done")

    def _pause(self, error: str):
        """Pauses the check with its queue, to carry on at the next start, or Start."""
        self._update(running=False, paused=True, current=None, error=error)
        logger.warning(f"The library check paused: {error}")

    def _end(self, error: Optional[str] = None, finished: bool = False):
        """Saves the check as over, with why it ended early or when it finished."""
        changes = {"running": False, "paused": False, "current": None, "queue": None, "error": error}
        if finished:
            changes["last_finished"] = self._now()
        self._update(**changes)

    def _update(self, **changes):
        """Changes the progress, and saves it."""
        with self._lock:
            self._progress = {**self._progress, **changes}
            self._save()

    def _save_now(self):
        with self._lock:
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
