"""
Clean Swap Module
Hears a song against its lyrics to judge it clean or explicit (Ears), and swaps a clean copy
for an explicit one (Swapper), for a download and for the library check - decision 0019,
sections 1 to 4.

Ears fetches what it needs from Navidrome - the file's size and time, the lyrics, the sound -
and gives the sound to the listening worker, then keeps the verdict with the file's size and
time, so a song is only heard again once its file changes. Every failure to hear a song is
"not judged" with a plain reason, never a guess: a song judged clean gets moved out of the
library.

The Swapper sets the clean copy aside in the music folder's .nova-held, has Navidrome and
SoulSync forget it, asks SoulSync for it again and hears what comes, a set number of times.
The user must never end up without a copy of a song he had: whatever fails, the copy last set
aside goes back where it was, and nothing is ever deleted. Each step is saved before it's
taken, so a restart carries a swap on, or puts the song back.
"""

import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, replace
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import requests

from voice_assistant import music
from voice_assistant.clean_ear import Thresholds, Verdict, count_swears, judge
from voice_assistant.clean_store import CleanStore
from voice_assistant.downloads import NAVIDROME_ERRORS, POLL_EVERY, SCAN_AFTER, Wanted, song_limit, swap_said
from voice_assistant.listener import ListenError, Listener, decode
from voice_assistant.music_folder import HELD, HELD_GONE, NEW_COPY, PUT_BACK, Held, MusicFolder, MusicFolderError
from voice_assistant.navidrome import BRACKETED, NavidromeError, NavidromeLibrary
from voice_assistant.nemotron import RATE
from voice_assistant.soulsync import NOT_FORGOTTEN, SoulSync, SoulSyncAdmin, SoulSyncError

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

# Seconds Navidrome is given, once asked to scan, to stop listing a copy set aside. Its
# watcher usually sees the move within seconds.
DROP_WAIT = 120

# How many more times SoulSync is asked for a song which came down clean, when the settings
# don't say - as config.yaml has it.
SWAP_TRIES = 2

# Why a swap couldn't be made, as the page's list and the download's sentence give it:
# "... and I couldn't swap it: Navidrome still lists it."
NO_ADMIN = "SoulSync admin profile isn't set up"
NO_SOULSYNC = "SoulSync isn't set up"
NO_NAVIDROME = "Navidrome isn't set up"
STILL_LISTED = "Navidrome still lists it"
NOT_IN_TIME = "SoulSync didn't bring another copy in time"
NOT_SAVED = "Nova couldn't save the swap's progress"
NAVIDROME_UNASKED = "Navidrome couldn't be asked"
WENT_WRONG = "something went wrong part way, so Nova carries it on later"
# A restart came between moving the copy and saving where it went: never guessed at.
WHERE_NOT_SAVED = f"set aside, but where wasn't saved - look in {HELD}"

# The steps of a swap, saved as each starts, so a restart carries it on from there: moving the
# copy into .nova-held, waiting for Navidrome to stop listing it, having SoulSync forget it,
# asking SoulSync for it again, and watching for what comes.
SETTING_ASIDE = "setting_aside"
DROPPING = "dropping"
FORGETTING = "forgetting"
FETCHING = "fetching"
WATCHING = "watching"

# Which list each way a swap ends goes on, if any: an unsure copy is listed as it's heard.
LISTS = {"swapped": "swapped", "only_clean": "only_clean", "couldnt": "couldnt", "not_judged": "couldnt"}


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


class Outcome(str):
    """
    How a swap ended - "swapped", "only_clean", "unsure", "not_judged" or "couldnt" - or
    "stopped", when Nova stopped part way and the swap is carried on at its next start. It's
    the word itself, so it compares as one, with why in its reason: Outcome("couldnt",
    "Navidrome still lists it") == "couldnt".
    """

    reason: str

    def __new__(cls, kind: str, reason: str = "") -> "Outcome":
        outcome = super().__new__(cls, kind)
        outcome.reason = reason
        return outcome


STOPPED = Outcome("stopped")

# What a swap needs: the music folder, SoulSync with its key, SoulSync's admin profile and
# Navidrome.
Parts = Tuple[MusicFolder, SoulSync, SoulSyncAdmin, NavidromeLibrary]


def _album(song: Dict) -> Optional[str]:
    """
    A song's album as SoulSync is asked to prefer it, without its version notes: SoulSync's
    tagging adds "(clean)" to an album which isn't, and a candidate on "ASTROWORLD" would
    never match "ASTROWORLD (clean)".
    """
    album = " ".join(BRACKETED.sub(" ", song.get("album") or "").split())
    return album or None


def _copy(song: Dict, rel: str) -> Dict:
    """The copy of a song being swapped, as it's saved: its Navidrome id, names and file."""
    return {"id": song["id"], "title": song.get("title") or "", "artist": song.get("artist") or "", "rel": rel}


def _held(saved: Dict) -> Held:
    """A Held as it was saved, its sidecars made pairs again - a JSON round trip makes them lists."""
    return Held(saved["original"], saved["held"], [tuple(pair) for pair in saved.get("sidecars") or []])


class Swapper:
    """Swaps a song heard clean for an explicit copy, one step saved at a time."""

    def __init__(self, config, ears: Ears, folder_factory: Callable[[], Optional[MusicFolder]],
                 soulsync_factory: Callable[[], Optional[SoulSync]],
                 admin_factory: Callable[[], Optional[SoulSyncAdmin]],
                 library_factory: Callable[[], Optional[NavidromeLibrary]], store: CleanStore,
                 say: Callable[[Optional[str], str], None], now: Callable[[], float] = time.time,
                 wait: Optional[Callable[[float], bool]] = None):
        """
        Args:
            config: Configs loaded from config.yaml - clean_check.swap_tries, and the song time
                limit under music, read as each swap needs them.
            ears: Hears each copy SoulSync brings.
            folder_factory: Gives the MusicFolder from the settings as they are now.
            soulsync_factory: Gives the SoulSync to ask with the key, or None if it isn't set up.
            admin_factory: Gives Nova's SoulSyncAdmin, or None if it isn't set up.
            library_factory: Gives the NavidromeLibrary, or None if it isn't set up.
            store: Where each swap's progress and outcome are kept.
            say: Says how a download's swap carried on after a restart ended, given the
                satellite which asked - as Downloads says a download's outcome.
            now: The wall clock, in seconds - a swap's time limit outlives a restart.
            wait: Waits the seconds given between looks, and says whether Nova is stopping -
                by default the wait of the event stop sets, which it cuts short.
        """
        self.config = config
        self.ears = ears
        self._folder = folder_factory
        self._soulsync = soulsync_factory
        self._admin = admin_factory
        self._library = library_factory
        self.store = store
        self._say = say
        self._now = now
        self._stopping = threading.Event()
        self._wait = wait or self._stopping.wait
        # The keys of the swaps running now, which resume leaves alone: it reads the saved
        # swaps on a thread of its own as Nova starts, when a download's may be under way.
        self._running: Set[str] = set()
        self._running_lock = threading.Lock()
        # What was last logged of the swaps left for later, so it isn't logged again and again.
        self._left_for_later: Optional[str] = None

    def stop(self):
        """Ends every swap at its next step, as Nova stops. Each is saved, to be carried on at the next start."""
        self._stopping.set()

    def swap(self, song: Dict, wanted: Wanted, satellite: Optional[str], quiet: bool) -> Outcome:
        """
        Swaps a copy of a song heard clean: sets it aside, has Navidrome and SoulSync forget it,
        asks SoulSync for it again and hears what comes - up to clean_check.swap_tries more
        times while each comes down clean.

        Args:
            song: The clean copy, as Navidrome lists it, with its 'id', 'title', 'artist' and 'album'.
            wanted: The song as SoulSync is to be asked for it - for a download, the title asked
                for and SoulSync's artist - and as it's named if a restart says how it ended.
            satellite: The satellite which asked for the download, or None.
            quiet: True for the library check, whose swaps say nothing, even after a restart.

        Returns:
            "swapped" for an explicit copy (or one with nothing to bleep); "only_clean" when every
            try came down clean, the last kept; "unsure" or "not_judged" for a copy kept as it
            couldn't be judged either way; "couldnt", with its reason, when it was put back or
            never moved; or "stopped", as Nova stops. Each but "stopped" is on the page's lists.
        """
        name = song_name(song)
        parts = self._parts()
        if isinstance(parts, str):
            return self._list(name, song["id"], "couldnt", parts)
        try:
            rel = parts[3].song_file(song["id"])["path"]
        except NavidromeError as e:
            return self._list(name, song["id"], "couldnt", str(e))
        swap = {"key": uuid.uuid4().hex, "stage": SETTING_ASIDE, "song_id": song["id"], "name": name,
                "named": wanted.name, "satellite": satellite, "quiet": quiet, "title": wanted.title,
                "artist": wanted.artist, "album": _album(song), "copy": _copy(song, rel), "held": [], "tries": 0}
        # Saved before anything moves, so a restart part way never loses where a song went -
        # and if it can't be saved, nothing moves.
        with self._running_lock:
            self._running.add(swap["key"])
        try:
            if not self._save(swap):
                return self._list(name, song["id"], "couldnt", NOT_SAVED)
            logger.info(f"Swapping {name}, which was heard clean")
            return self._carry_on(swap, parts)
        finally:
            with self._running_lock:
                self._running.discard(swap["key"])

    def resume(self, begin: Optional[Callable[[Dict], None]] = None,
               end: Optional[Callable[[Dict], None]] = None):
        """
        Carries on each swap Nova was part way through when it stopped: one watching for a copy
        goes on watching while its time limit lasts, and is put back once it's past; one moved
        but not yet fetched for goes on, as does one about to set aside a later try's copy. One
        whose copy moved before where it went was saved puts back the copy set aside before it,
        if there is one, and says where to look. Without the music folder they're all left for
        later, as nothing can be put back. Swaps running now are left alone.

        Args:
            begin: Told of each saved swap as it's carried on, e.g. for Downloads to count
                a download's as still being got.
            end: Told of it once it's over, or Nova stops.
        """
        with self._running_lock:
            saved = [swap for swap in self.store.swaps() if swap.get("key") not in self._running]
            self._running.update(swap.get("key") for swap in saved)
        try:
            self._resume_all(saved, begin, end)
        finally:
            with self._running_lock:
                self._running.difference_update(swap.get("key") for swap in saved)

    def _resume_all(self, saved: List[Dict], begin: Optional[Callable[[Dict], None]],
                    end: Optional[Callable[[Dict], None]]):
        """Carries on the saved swaps given, one at a time - see resume."""
        if not saved:
            return
        folder = self._folder()
        problem = folder.usable() if folder is not None else "no music folder is set"
        if problem:
            # Once until the reason changes: resume runs as each download starts.
            left = f"{len(saved)} swap{'' if len(saved) == 1 else 's'} left for later, as {problem}"
            if left != self._left_for_later:
                logger.warning(left)
                self._left_for_later = left
            return
        self._left_for_later = None
        logger.info(f"Carrying on {len(saved)} swap{'' if len(saved) == 1 else 's'} Nova was part way through")
        for swap in saved:
            if self._stopping.is_set():
                return
            self._told(begin, swap)
            try:
                outcome = self._resume(swap, folder)
            except Exception:
                # One swap's surprise mustn't keep the others from being carried on.
                logger.exception(f"Carrying on the swap of {swap.get('name')} failed")
                continue
            finally:
                self._told(end, swap)
            self._tell(swap, outcome)

    @staticmethod
    def _told(whom: Optional[Callable[[Dict], None]], swap: Dict):
        """Tells resume's begin or end of a swap; what they do wrong is only logged."""
        if whom is None:
            return
        try:
            whom(swap)
        except Exception:
            logger.exception(f"Telling of the swap of {swap.get('name')} failed")

    def _resume(self, swap: Dict, folder: MusicFolder) -> Optional[Outcome]:
        """One saved swap, carried on or put back - None if it ended with nothing to say."""
        stage = swap.get("stage")
        if stage == SETTING_ASIDE:
            rel = (swap.get("copy") or {}).get("rel")
            in_place = bool(rel) and os.path.lexists(os.path.join(folder.root, rel))
            if not swap.get("held"):
                if in_place:
                    # Stopped before the first copy moved: nothing to carry on or put back.
                    self._end_saved(swap)
                    logger.info(f"The swap of {swap.get('name')} hadn't moved anything, so it's ended")
                    return None
                return self._end(swap, "couldnt", WHERE_NOT_SAVED)
            if not in_place:
                # A later try's copy moved before where it went was saved: never guessed at, but the
                # copy set aside before it goes back, so the library isn't left without the song.
                return self._put_back(swap, (folder, self._soulsync(), None, self._library()), WHERE_NOT_SAVED)
            # A later try's copy, not moved yet: the swap carries on from that try.
        # Watching needs no admin profile; every other step leads to forgetting a copy, which does.
        parts = self._parts(admin_needed=stage != WATCHING)
        if isinstance(parts, str):
            if stage == SETTING_ASIDE:
                # The copy this try brought is still in the library; the earlier ones stay held.
                return self._end(swap, "couldnt", parts)
            # Can't be carried on, but the copy can still go back.
            return self._put_back(swap, (folder, self._soulsync(), None, self._library()), parts,
                                  unwish=stage == WATCHING)
        return self._carry_on(swap, parts)

    def _tell(self, swap: Dict, outcome: Optional[Outcome]):
        """Says how a download's swap carried on after a restart ended. The library check's say nothing."""
        if outcome is None or outcome == STOPPED or swap.get("quiet", True):
            return
        try:
            self._say(swap.get("satellite"), swap_said(swap.get("named") or swap.get("name") or "", outcome))
        except Exception:
            logger.exception(f"How the swap of {swap.get('name')} ended couldn't be said")

    def _parts(self, admin_needed: bool = True) -> Union[Parts, str]:
        """
        What a swap needs, or why it can't be made - nothing is moved without all of it.

        Args:
            admin_needed: False for a swap only watching, which can go on without the admin
                profile; the admin is then None if it isn't set up, and a copy that needs
                forgetting isn't moved.
        """
        folder = self._folder()
        problem = folder.usable() if folder is not None else "no music folder is set"
        if problem:
            return problem
        admin = self._admin()
        if admin is None and admin_needed:
            return NO_ADMIN
        soulsync = self._soulsync()
        if soulsync is None:
            return NO_SOULSYNC
        library = self._library()
        if library is None:
            return NO_NAVIDROME
        return folder, soulsync, admin, library

    def _carry_on(self, swap: Dict, parts: Parts) -> Outcome:
        """
        Runs a swap on from its saved step. Something unforeseen leaves it saved as it is, to
        be carried on later, rather than acted on half understood.
        """
        try:
            return self._run(swap, parts)
        except Exception:
            logger.exception(f"Swapping {swap['name']} failed part way; it's carried on at Nova's next start")
            return self._list(swap["name"], swap.get("song_id"), "couldnt", WENT_WRONG)

    def _run(self, swap: Dict, parts: Parts) -> Outcome:
        """Each step of a swap in turn, from the one saved, until it ends or Nova stops."""
        folder, soulsync, admin, library = parts
        while not self._stopping.is_set():
            stage = swap["stage"]
            if stage == SETTING_ASIDE:
                if admin is None:
                    # It would need forgetting before another try: left where it is.
                    return self._end(swap, "couldnt", NO_ADMIN)
                try:
                    held = folder.set_aside(swap["copy"]["rel"])
                except MusicFolderError as e:
                    # Nothing of this copy moved: set_aside puts back what it moved. Copies set
                    # aside by earlier tries stay held, as clean as this one.
                    return self._end(swap, "couldnt", e.reason)
                swap["held"].append(asdict(held))
                self._next(swap, DROPPING)
            elif stage == DROPPING:
                dropped = self._dropped(library, swap["copy"])
                if dropped is None:
                    break
                if not dropped:
                    return self._put_back(swap, parts, STILL_LISTED)
                self._next(swap, FORGETTING)
            elif stage == FORGETTING:
                if admin is None:
                    return self._put_back(swap, parts, NO_ADMIN)
                copy = swap["copy"]
                try:
                    reason = None if admin.forget(soulsync, copy["title"], copy["artist"], copy["rel"]) \
                        else NOT_FORGOTTEN
                except SoulSyncError as e:
                    reason = e.reason or "SoulSync couldn't be asked"
                if reason:
                    return self._put_back(swap, parts, reason)
                if swap.get("pending"):
                    # Another clean copy the same try brought, set aside in its turn.
                    swap["copy"] = swap["pending"].pop(0)
                    self._next(swap, SETTING_ASIDE)
                else:
                    self._next(swap, FETCHING)
            elif stage == FETCHING:
                ended = self._fetch(swap, soulsync, library, parts)
                if ended:
                    return ended
            elif stage == WATCHING:
                new = self._watch(library, swap)
                if new is None:
                    break
                if not new:
                    return self._put_back(swap, parts, NOT_IN_TIME, unwish=True)
                ended = self._judge(swap, library, new)
                if ended:
                    return ended
            else:
                return self._put_back(swap, parts, f"its saved step, {stage!r}, wasn't one Nova knows")
        return STOPPED

    def _fetch(self, swap: Dict, soulsync: SoulSync, library: NavidromeLibrary, parts: Parts) -> Optional[Outcome]:
        """Asks SoulSync for the song again, saved as watching first - None once it's asked for."""
        try:
            request = soulsync.find_song(swap["title"], swap["artist"], swap["album"])
        except SoulSyncError as e:
            # Nothing was put on the wishlist, so there's nothing to take off it.
            return self._put_back(swap, parts, e.reason or "SoulSync couldn't be asked")
        # Looked for by the title asked for and SoulSync's artist, as a download is - and only
        # what isn't listed now counts.
        before = self._listed(library, Wanted(swap["title"], request.artist, "track"))
        if before is None:
            # Without it, a copy already there could be taken for the one asked for.
            return self._put_back(swap, parts, NAVIDROME_UNASKED)
        swap.update(stage=WATCHING, tries=swap["tries"] + 1, track_id=request.track.get("id"),
                    sought_artist=request.artist, before=sorted(before), asked_at=self._now(),
                    limit=song_limit(self.config))
        # Before asking, so a restart which comes before the answer still takes it off the wishlist.
        self._save(swap)
        try:
            soulsync.request_song(request)
        except SoulSyncError as e:
            return self._put_back(swap, parts, e.reason or "SoulSync couldn't be asked", unwish=True)
        logger.info(f"Asked SoulSync for {swap['name']} again, try {swap['tries']}")
        return None

    def _judge(self, swap: Dict, library: NavidromeLibrary, new: List[Dict]) -> Optional[Outcome]:
        """
        Hears the copies SoulSync brought and ends the swap by what they are - or, when they're
        all clean with tries to spare, sets each of them aside in turn, and returns None to
        fetch again.
        """
        verdict, copy = self.ears.hear_copies(new, download=not swap["quiet"])
        if self._stopping.is_set():
            # Heard as the worker was closed: not judged, but only because Nova is stopping.
            return STOPPED
        if verdict.kind in ("explicit", "too_few"):
            return self._end(swap, "swapped", "")
        if verdict.kind == "unsure":
            # Kept, never swapped; Ears has listed it.
            return self._end(swap, "unsure", f"heard {verdict.heard} of its {verdict.expected} swears")
        if verdict.kind != "clean":
            return self._end(swap, "not_judged", verdict.reason,
                             listed=f"couldn't check the copy SoulSync brought: {verdict.reason}")
        if swap["tries"] > self._tries():
            # The last copy stays in the library; the earlier ones stay held.
            return self._end(swap, "only_clean", f"{swap['tries']} copies came down clean")
        # Clean is only ever the verdict when every copy heard was clean: each goes, the one
        # given first.
        copies = [copy] + [other for other in new if other.get("id") != copy.get("id")]
        try:
            files = [_copy(clean, library.song_file(clean["id"])["path"]) for clean in copies]
        except NavidromeError as e:
            return self._end(swap, "couldnt", f"couldn't find the clean copy SoulSync brought: {e}")
        swap.update(stage=SETTING_ASIDE, copy=files[0], pending=files[1:])
        if not self._save(swap):
            return self._end(swap, "couldnt", NOT_SAVED)
        return None

    def _tries(self) -> int:
        """How many more times SoulSync is asked for a clean song, from the settings as they are now."""
        tries = self.config.section("clean_check").get("swap_tries")
        return SWAP_TRIES if tries is None else int(tries)

    def _dropped(self, library: NavidromeLibrary, copy: Dict) -> Optional[bool]:
        """
        Asks Navidrome to scan, then whether it has stopped listing a copy set aside, every
        POLL_EVERY seconds for DROP_WAIT.

        Returns:
            True once it has, False if it still does after DROP_WAIT, None if Nova is stopping.
        """
        library.start_scan()
        started = self._now()
        while not self._wait(POLL_EVERY):
            listed = self._listed(library, Wanted(copy["title"], copy["artist"] or None, "track"))
            if listed is not None and copy["id"] not in listed:
                return True
            if self._now() - started >= DROP_WAIT:
                return False
        return None

    def _watch(self, library: NavidromeLibrary, swap: Dict) -> Optional[List[Dict]]:
        """
        Looks for a new copy in Navidrome every POLL_EVERY seconds, as a download is watched,
        until its time limit from when SoulSync was asked - with one scan in time for the last
        look, as SoulSync never says it's done.

        Returns:
            The new copies, or [] once the limit has passed without any; None if Nova is stopping.
        """
        sought = Wanted(swap["title"], swap.get("sought_artist"), "track")
        before = set(swap.get("before") or ())
        scanned = False
        while not self._wait(POLL_EVERY):
            listed = self._found(library, sought)
            new = [song for song in listed or [] if song.get("id") not in before]
            if new:
                return new
            waited = self._now() - swap["asked_at"]
            if waited >= swap["limit"]:
                return []
            if not scanned and waited >= swap["limit"] - SCAN_AFTER - POLL_EVERY:
                scanned = True
                library.start_scan()
        return None

    def _listed(self, library: NavidromeLibrary, sought: Wanted) -> Optional[Set[str]]:
        """The ids of the copies of a song Navidrome lists, or None if it couldn't be asked."""
        found = self._found(library, sought)
        return None if found is None else {song.get("id") for song in found}

    @staticmethod
    def _found(library: NavidromeLibrary, sought: Wanted) -> Optional[List[Dict]]:
        """The copies of a song Navidrome lists, or None if it couldn't be asked this time."""
        try:
            return library.find_songs(sought.title, sought.artist)
        except NAVIDROME_ERRORS as e:
            NavidromeLibrary._failure_message(e)
            logger.warning(f"Couldn't look for {sought.name} in Navidrome this time, so the swap waits on")
            return None

    def _put_back(self, swap: Dict, parts, reason: str, unwish: bool = False) -> Outcome:
        """
        Ends a swap which couldn't go on: takes its request off SoulSync's wishlist, if it made
        one, so nothing lands later beside the copy put back; puts back the copy it last set
        aside; and asks Navidrome to scan for it.

        Args:
            swap: The swap.
            parts: The music folder, SoulSync, its admin and Navidrome - SoulSync or Navidrome
                None if they aren't set up, when that part is left out.
            reason: Why it couldn't go on.
            unwish: Whether SoulSync was asked for it, so it comes off the wishlist.

        Returns:
            "couldnt", with the reason and what became of the copy set aside.
        """
        folder, soulsync, _admin, library = parts
        if unwish and swap.get("track_id"):
            if soulsync is not None:
                soulsync.unwish(swap["track_id"])
            else:
                logger.warning(f"{swap['name']} couldn't be taken off SoulSync's wishlist, as it isn't set up")
        put = folder.put_back(_held(swap["held"][-1])) if swap.get("held") else None
        if library is not None:
            library.start_scan()
        if put in (None, PUT_BACK):
            said = reason
        elif put == NEW_COPY:
            said = (f"{reason}, and a new file is where it was, so both are kept: the new file, "
                    f"and the old copy in {HELD}")
        elif put == HELD_GONE:
            said = f"{reason}, and the copy set aside has gone from {HELD}"
        else:
            said = f"{reason}, and it couldn't be put back ({put}), so it's still in {HELD}"
        if put not in (None, PUT_BACK):
            logger.warning(f"Putting {swap['name']} back: {put}")
        return self._end(swap, "couldnt", said)

    def _next(self, swap: Dict, stage: str):
        """Moves a swap on to its next step, saved."""
        swap["stage"] = stage
        self._save(swap)

    def _save(self, swap: Dict) -> bool:
        """
        Saves a swap's progress. Once a copy has moved a failure is only logged, and the swap
        carries on: stopping would leave the song held with less known about it.

        Returns:
            Whether it was saved.
        """
        try:
            self.store.save_swap(swap)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Couldn't save the swap of {swap['name']} at {swap['stage']}: {type(e).__name__}")
            return False
        return True

    def _end_saved(self, swap: Dict):
        """Forgets a swap which is over. If that can't be saved, the next start finds it ended as it is."""
        try:
            self.store.end_swap(swap["key"])
        except OSError as e:
            logger.error(f"Couldn't forget the finished swap of {swap.get('name')}: {type(e).__name__}")

    def _end(self, swap: Dict, kind: str, reason: str, listed: Optional[str] = None) -> Outcome:
        """
        Ends a swap: forgets its progress and puts it on the page's list for how it ended.

        Args:
            swap: The swap.
            kind: How it ended.
            reason: Why, as the outcome gives it.
            listed: The reason as the page's list gives it, if not the same.
        """
        self._end_saved(swap)
        logger.info(f"The swap of {swap['name']} ended {kind}" + (f": {reason}" if reason else ""))
        if kind in LISTS:
            self._list(swap["name"], swap.get("song_id"), LISTS[kind], listed or reason)
        return Outcome(kind, reason)

    def _list(self, name: str, song_id: Optional[str], kind: str, reason: str) -> Outcome:
        """Puts a song on one of the page's lists; a list that can't be saved is logged."""
        try:
            self.store.add(kind, name, reason, song_id=song_id)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Couldn't list {name} as {kind}: {type(e).__name__}")
        return Outcome(kind, reason)
