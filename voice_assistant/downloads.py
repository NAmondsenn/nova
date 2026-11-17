"""
Downloads Module.

Asks SoulSync for a song or album the user asked for - "download X", or a yes to the
offer made when "play X" finds nothing - and watches it until Navidrome has it, then
says how it went - on the satellite that asked, through the say it's given. Each
request is watched on a thread of its own, so waiting never holds up a conversation,
and several can wait at once.

SoulSync saying a download is done isn't enough: it's downloaded only once Navidrome
has it, and only what wasn't in Navidrome when it was asked for counts, so an old
clean copy isn't taken for the explicit one just asked for. Nothing here plays or
deletes a file. See docs/decisions/0012-music-fetched-by-voice.md.

What lands is heard before anything is said of it, and a clean copy is swapped for an
explicit one (clean_swap.py): its labels - "(clean)" on an album, Navidrome's explicit
mark - are never believed, as SoulSync's tagging gets them wrong both ways. What the
library already has is judged by what Nova heard of it, too. See
docs/decisions/0019-clean-songs-by-ear.md.
"""

import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple, Union

import requests

from voice_assistant import music
from voice_assistant.actions import Action
from voice_assistant.clean_ear import Verdict
from voice_assistant.clean_store import CleanStore
from voice_assistant.navidrome import (BRACKETED, CLEAN_VERSIONS, DASH, NavidromeError, NavidromeLibrary,
                                       _says_version, likeness, normalise, split_by)
from voice_assistant.soulsync import AlbumRequest, SongRequest, SoulSync, SoulSyncError

if TYPE_CHECKING:
    # Only for the types: clean_swap imports this module.
    from voice_assistant.clean_swap import Ears, Swapper

# Logger setup
logger = logging.getLogger(__name__)

# Seconds between asking SoulSync and Navidrome how a download is getting on. Both
# are on the local network, and a couple of small requests a poll are nothing to either.
POLL_EVERY = 15

# Seconds before the time limit, less a poll, at which Navidrome, still without a
# download, is asked to scan its folders - once. Navidrome finds new files itself, so
# this is only in case it hasn't. SoulSync's wishlist never says a song or album is
# done, so the scan is asked for this long and a poll before the time limit, and the
# last look sees what it found.
SCAN_AFTER = 60

NOT_SET_UP = ("Downloading music isn't set up. SoulSync's address goes in Settings, under Music, "
              "and its key under Keys.")

# Said after "I couldn't download X." when SoulSync queued none of an album - it only
# queues the tracks it hasn't got and isn't already getting - and Navidrome had none
# of it, or only a clean version: see _none_queued.
NOTHING_QUEUED = "SoulSync says it already has every track, or is already getting them."

# Said for "download" everything by an artist, which can mean dozens of albums - the user
# said a whole discography "could go wrong".
WHOLE_ARTIST = "I only download songs and albums, not a whole artist."

# Asked when "download" comes with nothing to download.
WHAT_TO_DOWNLOAD = "What would you like me to download?"

# Told to Claude after "Asked SoulSync for X.", as what its call did, and never said: the
# outcome is said later. Told only "Done.", Claude said on 12 November that a download asked
# for a minute before had finished - it landed five minutes later. By what was asked for, as
# an album takes longer, and is waited for longer.
ON_ITS_WAY = {
    "track": "It usually takes a few minutes; Nova will say when it's in the library, or that it couldn't get it.",
    "album": ("An album takes longer than a song; Nova will say when it's in the library, or how much of it "
              "came down."),
}

# Claude's answer to "what's downloading?" when nothing is being watched for.
NOTHING_WAITING = "Nothing is downloading."

# The questions which end an offer to download. LLMHandler._asked_to_download reads them,
# as the request which made an offer - "play Get Lucky" - never makes its answer a yes.
DOWNLOAD_IT = "Do you want me to download it?"
DOWNLOAD_EXPLICIT = "Do you want me to download the explicit one?"
OFFERS = (DOWNLOAD_IT, DOWNLOAD_EXPLICIT)

# What can be offered after "play X" finds nothing - a song, or an album. None is a song,
# as play_music takes it.
OFFERED = (None, "track", "album")

# The time limits, in minutes from the request, when the settings don't say - as
# config.yaml has them.
SONG_MINUTES = 10
ALBUM_MINUTES = 30

# What asking Navidrome can raise: out of reach, an error answered, or an answer
# that wasn't JSON.
NAVIDROME_ERRORS = (requests.RequestException, NavidromeError, ValueError)

# What's said of a song which landed and was heard, when it isn't plainly downloaded - see
# decision 0019, section 3. A clean copy is never called downloaded.
UNSURE = "{name} has been downloaded, but I couldn't tell if it's the clean version."
NOT_CHECKED = "{name} has been downloaded, but I couldn't check it for a clean version."
# Why an album's clean tracks couldn't be swapped, when every try for each came down clean.
ONLY_CLEAN = "SoulSync only brought clean copies"


def song_limit(config) -> float:
    """A song's time limit, in seconds from the request, from the settings as they are now."""
    return float(config.section("music").get("song_download_minutes", SONG_MINUTES)) * 60


def swap_said(name: str, outcome: str, reason: Optional[str] = None) -> str:
    """
    What's said of a song which landed clean, once its swap is over (clean_swap.Swapper).

    Args:
        name: The song as it's named in what's said, e.g. "endless by Gunna".
        outcome: How the swap ended - "swapped", "only_clean", "unsure", "not_judged", or
            "couldnt" with its reason.
        reason: Why it couldn't be swapped, in place of the outcome's own.

    Returns:
        The sentence, e.g. "endless by Gunna has been downloaded, but it's the clean
        version, and I couldn't swap it: Navidrome still lists it."
    """
    if outcome == "swapped":
        return f"{name} has been downloaded."
    if outcome == "only_clean":
        return f"{name} only came down clean, so I've kept it."
    if outcome == "unsure":
        return UNSURE.format(name=name)
    if outcome == "not_judged":
        return NOT_CHECKED.format(name=name)
    # The reason may be SoulSync's own sentence, with its full stop.
    reason = (reason or getattr(outcome, "reason", "") or "").strip().rstrip(".")
    return (f"{name} has been downloaded, but it's the clean version, and I couldn't swap it"
            + (f": {reason}." if reason else "."))


@dataclass(frozen=True)
class Wanted:
    """
    A song or album asked for, as it was asked for.

    Attributes:
        title: The song's title or the album's name.
        artist: Its artist, or None if only the title was given.
        kind: "track" or "album".
    """

    title: str
    artist: Optional[str]
    kind: str

    @classmethod
    def from_query(cls, query: str, search_type: Optional[str]) -> "Wanted":
        """
        What a request asks for: "Gooba by 6ix9ine" is Gooba, by 6ix9ine.

        Args:
            query: The title, with " by " and the artist if one was given.
            search_type: "album" for an album; anything else is a song.

        Returns:
            The Wanted.
        """
        title, artist = split_by(query)
        return cls(title, artist, "album" if search_type == "album" else "track")

    @property
    def name(self) -> str:
        """How it's named in what's said: "Gooba by 6ix9ine", or the title alone."""
        return f"{self.title} by {self.artist}" if self.artist else self.title

    @property
    def key(self) -> str:
        """
        The same for the same request however it was spelt or cased - "gooba by
        6IX9INE" is Gooba by 6ix9ine - to tell whether it's already waiting.
        """
        return f"{self.kind}: {normalise(self.name)}"


def _albums(tracks: List[Dict]) -> List[List[Dict]]:
    """
    The tracks of an album asked for, album by album, in the order found - so an explicit
    Rumours and a clean edition beside it, or two albums called Rumours, are two.
    """
    albums: Dict[tuple, List[Dict]] = {}
    for track in tracks:
        albums.setdefault((track.get("albumId"), track.get("album")), []).append(track)
    return list(albums.values())


def _as_had(wanted: Wanted, found: List[Dict], heard_clean: Callable[[Dict], bool]) -> Optional[str]:
    """
    What Navidrome has of a request other than clean, named as Navidrome has it -
    "Rumours by Fleetwood Mac" for "download rumours" - or None if all it has is clean.
    Clean is only what Nova heard clean: a copy on a "(clean)" album, never heard, is
    had, as SICKO MODE on "ASTROWORLD (clean)" is explicit.

    A song is had only clean when every copy of it was heard clean. An album is when
    every album found has a track heard clean: a clean edition has only the tracks it
    bleeped heard clean, as the rest have too few swears to judge - but an explicit
    edition beside it is had.

    Args:
        wanted: What was asked for.
        found: Every copy of the song, or every track of the album, Navidrome has.
        heard_clean: Whether a copy was heard clean.

    Returns:
        The song's title by its artist, or the album's name by the album's artist -
        never a track's, which may be a featured one.
    """
    if wanted.kind != "album":
        track = next((track for track in found if not heard_clean(track)), None)
        if track is None:
            return None
        title, artist = track.get("title"), track.get("artist")
    else:
        album = next((tracks for tracks in _albums(found) if not any(heard_clean(track) for track in tracks)),
                     None)
        if album is None:
            return None
        title, artist = album[0].get("album"), album[0].get("albumArtist")
    title = title or wanted.title
    return f"{title} by {artist}" if artist else title


def _asked_ago(seconds: float) -> str:
    """How long ago a request was asked for, in whole minutes: "asked for 2 minutes ago" for 2:50."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "asked for less than a minute ago"
    return f"asked for {minutes} minute{'' if minutes == 1 else 's'} ago"


def _couldnt(wanted: Wanted, reason: Optional[str]) -> str:
    """'I couldn't download X.', then the reason if there is one."""
    return f"I couldn't download {wanted.name}." + (f" {reason}" if reason else "")


def _none_queued(wanted: Wanted, had: List[Dict], heard_clean: Callable[[Dict], bool]) -> str:
    """
    What to say when SoulSync queued none of an album: "You've already got X." if
    Navidrome had any of it other than clean before it was asked for, named as
    Navidrome had it - else that SoulSync has, or is getting, every track. An album
    Navidrome had only clean isn't "got": the explicit one is what was asked for.

    Args:
        wanted: The album asked for.
        had: Every track of it Navidrome had before the request.
        heard_clean: Whether a track was heard clean.

    Returns:
        The sentence to say.
    """
    name = _as_had(wanted, had, heard_clean)
    return f"You've already got {name}." if name else _couldnt(wanted, NOTHING_QUEUED)


def _says_clean(part: str) -> bool:
    """Whether a bracketed or dashed part of a title says it's a clean version, as version_of reads one."""
    return any(_says_version(part, version) for version in CLEAN_VERSIONS)


def _without_clean_note(title: str) -> str:
    """
    A title without any part saying it's a clean version, as it's written otherwise:
    "The Chain (Clean)" and "The Chain - Radio Edit" are "The Chain", and "The Chain
    [Clean Version] (feat. X)" is "The Chain (feat. X)". Any other version stays - "The
    Chain (Live)" is a take of its own - as does "Dreams (2004 Remaster)", which is no
    version at all.

    The clean brackets go first, then a clean dashed part, read as version_of reads
    one: the other way round, "Dreams - Live at the Forum (Clean)" would have its
    whole dashed part taken for a clean version, and lose its "Live at the Forum".
    """
    title = BRACKETED.sub(lambda part: "" if _says_clean(part.group(1) or part.group(2) or "") else part.group(0),
                          title)
    dashed = DASH.split(title, 1)
    if len(dashed) == 2 and _says_clean(BRACKETED.sub("", dashed[1])):
        title = dashed[0]
    return " ".join(title.split())


def _bare_title(title: str) -> str:
    """
    A track's title without any part saying it's a clean version, normalised, so a
    clean copy and the explicit one count as one track: "Dreams (Clean)" and "Dreams
    - Radio Edit" are "dreams", as Dreams is. See _without_clean_note.
    """
    return normalise(_without_clean_note(title))


def _album_tracks(request: AlbumRequest, new: List[Dict]) -> List[List[Dict]]:
    """
    The new tracks which are the album's, as the copies of each title: a clean copy and
    the explicit one of the same track - "Dreams (Clean)" and "Dreams" - are one track,
    and both are heard to tell which is which, as their names can't be believed. With
    SoulSync's titles, only a new track with every word of one of them counts.

    Args:
        request: The AlbumRequest SoulSync gave.
        new: The tracks of the album in Navidrome which weren't there before the request.

    Returns:
        The copies of each track which has landed, a list for each title, in the order found.
    """
    if request.tracks:
        new = [track for track in new
               if any(likeness(track.get("title") or "", title) >= 2 for title in request.tracks)]
    tracks: Dict[str, List[Dict]] = {}
    for track in new:
        tracks.setdefault(_bare_title(track.get("title") or ""), []).append(track)
    return list(tracks.values())


def _of_its_tracks(count: int, start: bool) -> str:
    """'One of its tracks' (or 'one', within a sentence), or '2 of its tracks'."""
    if count == 1:
        return f"{'One' if start else 'one'} of its tracks"
    return f"{count} of its tracks"


def _album_said(lead: str, outcomes: Counter) -> str:
    """
    What's said of an album once the tracks which came down are heard: the lead, then how
    many were swapped for explicit copies and how many weren't plainly downloaded, e.g.
    "Rumours by Fleetwood Mac has been downloaded. 2 of its tracks came down clean, so I
    swapped them for the explicit ones. One of its tracks only came down clean, so I've
    kept it."

    Args:
        lead: "X has been downloaded." or "Only N of the M tracks of X came down."
        outcomes: How many tracks ended each way - "downloaded", "swapped", "only_clean",
            "couldnt", "unsure" or "not_judged".
    """
    said = [lead]
    for kind in ("swapped", "only_clean", "couldnt", "unsure", "not_judged"):
        count = outcomes.get(kind, 0)
        if not count:
            continue
        one = count == 1
        it = "it" if one else "them"
        if kind == "swapped":
            said.append(f"{_of_its_tracks(count, True)} came down clean, so I swapped {it} for the explicit "
                        f"{'one' if one else 'ones'}.")
        elif kind == "only_clean":
            said.append(f"{_of_its_tracks(count, True)} only came down clean, so I've kept {it}.")
        elif kind == "couldnt":
            said.append(f"{_of_its_tracks(count, True)} came down clean, and I couldn't swap {it}.")
        elif kind == "unsure":
            said.append(f"I couldn't tell if {_of_its_tracks(count, False)} "
                        f"{'is the clean version' if one else 'are clean versions'}.")
        else:
            said.append(f"I couldn't check {_of_its_tracks(count, False)} for "
                        f"{'a clean version' if one else 'clean versions'}.")
    return " ".join(said)


class Downloads:
    """Takes each download asked for, watches it on a thread of its own, and says how it went."""

    def __init__(self, config, say: Callable[[Optional[str], str], None],
                 soulsync: Optional[Callable[[], Optional[SoulSync]]] = None,
                 library: Optional[Callable[[], Optional[NavidromeLibrary]]] = None,
                 now: Callable[[], float] = time.monotonic, wait: Optional[Callable[[float], bool]] = None,
                 ears: Optional["Ears"] = None, swapper: Optional["Swapper"] = None,
                 store: Optional[CleanStore] = None, wall: Callable[[], float] = time.time):
        """
        Args:
            config: Configs loaded from config.yaml. The time limits are read from it
                at each check, so a change on the settings page is used straight away.
            say: Says a message about a download, given the satellite that asked -
                or None - and the message.
            soulsync: Makes the SoulSync to ask, or gives None if it isn't set up. By
                default from soulsync.url and SOULSYNC_API_KEY as they are then.
            library: Makes the NavidromeLibrary to look in, or gives None if it isn't
                set up. By default one of its own, from navidrome.url,
                navidrome.username and NAVIDROME_PASSWORD as they are then - never
                the player's, as a requests session isn't safe to share between threads.
            now: The time, in seconds, for the time limits.
            wait: Waits the seconds given between polls, and says whether Nova is
                stopping - by default the wait of the event stop sets, which it
                cuts short.
            ears: Hears each copy that lands. Without them nothing landed can be
                checked, and that's said.
            swapper: Swaps a copy heard clean for an explicit one.
            store: What Nova has heard of each song, to tell what the library has only
                clean.
            wall: The wall clock a swap is saved by, to tell how long ago one carried on
                after a restart was asked for.
        """
        self.config = config
        self._ears = ears
        self._swapper = swapper
        self._store = store
        self._wall = wall
        # Whether saved swaps are being carried on now, and the download each of those is
        # waiting for, by the swap's key.
        self._resuming = False
        self._resumed: Dict[str, Wanted] = {}
        self._say = say
        self._soulsync = soulsync or (lambda: SoulSync.from_config(config, os.getenv("SOULSYNC_API_KEY")))
        self._library = library or self._own_library
        self._now = now
        self._stopping = threading.Event()
        self._wait = wait or self._stopping.wait
        self._lock = threading.Lock()
        # What's waiting, by its key, in the order it was asked for - and when each was
        # asked for, by the same key, on the clock the time limits use, and how each song
        # is named once SoulSync has found it: "endless by Gunna" for "Endless by Gunner".
        self._waiting: Dict[str, Wanted] = {}
        self._asked_at: Dict[str, float] = {}
        self._names: Dict[str, str] = {}

    def _own_library(self) -> Optional[NavidromeLibrary]:
        """A NavidromeLibrary from the settings as they are now, or None if they're incomplete."""
        if music._missing(self.config, "navidrome"):
            return None
        settings = self.config.section("navidrome")
        return NavidromeLibrary(settings["url"].strip(), settings["username"].strip(), os.getenv("NAVIDROME_PASSWORD"))

    def problem(self) -> Optional[str]:
        """
        What stops a download being asked for, as it's said.

        Returns:
            NOT_SET_UP without SoulSync's address or key, else what Navidrome is
            missing, as the settings page says it - or None if nothing is.
        """
        address = (self.config.section("soulsync").get("url") or "").strip()
        if not address or not (os.getenv("SOULSYNC_API_KEY") or "").strip():
            return NOT_SET_UP
        return music._missing(self.config, "navidrome")

    def offer(self, query: Optional[str], search_type: Optional[str]) -> Optional[Dict]:
        """
        The offer to download what "play X" found nothing for, said in place of "I
        couldn't find X on Navidrome." - a yes to it runs request_download.

        Args:
            query: What was asked to be played, e.g. "Gooba by 6ix9ine".
            search_type: What it names, as play_music was given it.

        Returns:
            "I'm already getting X." when it's being got; else the offer, with
            'ask_first' the request on a yes - or None when there's none to make: it
            isn't a song or an album, the offer is switched off on the settings page
            (music.offer_downloads), or downloading isn't set up.
        """
        if not (query or "").strip() or search_type not in OFFERED:
            return None
        wanted = Wanted.from_query(query, search_type)
        # Before the switch, as it's on its way whatever the page says now - and "I
        # couldn't find X on Navidrome." would sound as if it never will be.
        if self._is_waiting(wanted):
            return {"success": True, "message": f"I'm already getting {wanted.name}."}
        if not self.config.section("music").get("offer_downloads", True) or self.problem():
            return None
        return self._offered(query, search_type, wanted)

    @staticmethod
    def _offered(query: str, search_type: Optional[str], wanted: Wanted) -> Dict:
        """The offer to download what Navidrome hasn't got, with the request on a yes."""
        return {"success": True, "message": f"I haven't got {wanted.name}. {DOWNLOAD_IT}",
                "ask_first": Action("request_download", {"query": query, "search_type": search_type})}

    def download(self, query: Optional[str], search_type: Optional[str], satellite: Optional[str],
                 asked: bool = True) -> Dict:
        """
        Asks SoulSync for a song or album, for "download X" - unless Navidrome already
        has the song, when that's said instead, and nothing is requested. An album
        Navidrome has is still asked for, as it may have only some of its tracks:
        SoulSync queues only the rest, and if that's none, the watch says "You've
        already got X.".

        Args:
            query: What to download, e.g. "Gooba by 6ix9ine".
            search_type: "album" for an album; "artist", everything by an artist, is
                refused; anything else is a song.
            satellite: The satellite that asked, which is told how it went - or None.
            asked: Whether the user's own words asked for a download - "download X",
                "get me X". If not, as when Claude takes an "okay" to an offer for a yes,
                it's only offered, for a plain yes, as pc_power only ever asks.

        Returns:
            Dict with 'success' and 'message' - and 'later', True, once it's requested,
            as nothing is said until SoulSync has it ("X requested.") and the message,
            "Asked SoulSync for X." and ON_ITS_WAY's words for it, is only for Claude;
            or 'ask_first', the request on a yes, when it's offered: Navidrome has only
            a clean version of it, or it wasn't asked for in words.
        """
        return self._ask(query, search_type, satellite, look=True, asked=asked)

    def request(self, query: Optional[str], search_type: Optional[str], satellite: Optional[str]) -> Dict:
        """
        Asks SoulSync for a song or album on the user's yes to an offer - as download
        does, but without looking in Navidrome again, which was done to make the offer.
        A yes to having the explicit version of what Navidrome has only clean would
        otherwise be offered it again.

        Args:
            query: What to download, as the offer was made for it.
            search_type: As the offer was made for it.
            satellite: The satellite that asked, which is told how it went - or None.

        Returns:
            As download.
        """
        return self._ask(query, search_type, satellite, look=False, asked=True)

    def _ask(self, query: Optional[str], search_type: Optional[str], satellite: Optional[str],
             look: bool, asked: bool) -> Dict:
        """
        What download and request share: what stops the request, if anything does,
        else the request, started.

        Args:
            query: What to download.
            search_type: What it names.
            satellite: The satellite that asked, or None.
            look: Whether to look in Navidrome for it first.
            asked: Whether to request it, or - not asked for in words - only offer to.

        Returns:
            As download.
        """
        problem = self.problem()
        if problem:
            return {"success": False, "message": problem}
        if search_type == "artist":
            return {"success": False, "message": WHOLE_ARTIST}
        if not (query or "").strip():
            return {"success": False, "message": WHAT_TO_DOWNLOAD}
        wanted = Wanted.from_query(query, search_type)
        # Before Navidrome is asked, which would offer the explicit one again while it's
        # still on its way - and named as it's asked for this time.
        already = {"success": True, "message": f"I'm already getting {wanted.name}."}
        if self._is_waiting(wanted):
            return already
        if look:
            had = self._in_navidrome(query, search_type, wanted, asked)
            if had:
                return had
        if not asked:
            return self._offered(query, search_type, wanted)
        if not self.start(wanted, satellite):
            # Asked for elsewhere since it was checked.
            return already
        # Kept in Claude's history as its call's answer; "X requested." is said once SoulSync has it.
        return {"success": True, "message": f"Asked SoulSync for {wanted.name}. {ON_ITS_WAY[wanted.kind]}",
                "later": True}

    def _is_waiting(self, wanted: Wanted) -> bool:
        """Whether the same request is already waiting."""
        with self._lock:
            return wanted.key in self._waiting

    def _in_navidrome(self, query: str, search_type: Optional[str], wanted: Wanted,
                      asked: bool) -> Optional[Dict]:
        """
        What to say in place of a request, when Navidrome has it or can't be asked -
        or None, to request it.

        Args:
            query: What to download, for the request on a yes.
            search_type: What it names, for the request on a yes.
            wanted: What it asks for.
            asked: Whether the user's own words asked for a download, which an album
                Navidrome has is still requested on.

        Returns:
            Navidrome's failure, which requests nothing - it may well have it; the offer
            of the explicit one when all it has was heard clean; None for an album asked
            for in words; else "You've already got X.".
        """
        try:
            found = self._find(self._library(), wanted)
        except NAVIDROME_ERRORS as e:
            return {"success": False, "message": NavidromeLibrary._failure_message(e)}
        if not found:
            return None
        had = _as_had(wanted, found, self._heard_clean)
        if had is None:
            return {"success": True,
                    "message": f"You've only got a clean version of {wanted.name}. {DOWNLOAD_EXPLICIT}",
                    "ask_first": Action("request_download", {"query": query, "search_type": search_type})}
        if wanted.kind == "album" and asked:
            # SoulSync files each song it fetches under its album, so a library filled a
            # song at a time has albums of three tracks in eleven. SoulSync knows which
            # it has, and queues only the rest - see _none_queued.
            logger.info(f"Navidrome has {had}, but perhaps not all of it, so SoulSync is asked for the rest")
            return None
        logger.info(f"Navidrome already has {had}, so {wanted.name} isn't requested")
        return {"success": True, "message": f"You've already got {had}."}

    def start(self, wanted: Wanted, satellite: Optional[str]) -> bool:
        """
        Starts watching a request, on a thread of its own.

        Args:
            wanted: What was asked for.
            satellite: The satellite that asked, which is told how it went - or None.

        Returns:
            False if the same request is already waiting, so nothing was started;
            else True.
        """
        with self._lock:
            if wanted.key in self._waiting:
                return False
            self._waiting[wanted.key] = wanted
            self._asked_at[wanted.key] = self._now()
        threading.Thread(target=self._watch, args=(wanted, satellite), daemon=True, name="download").start()
        # Anything left for later - the music folder away, say - is tried again now.
        self.resume_swaps()
        return True

    def waiting(self) -> List[str]:
        """The names of the requests waiting, in the order they were asked for."""
        with self._lock:
            return [wanted.name for wanted in self._waiting.values()]

    def describe_waiting(self) -> str:
        """
        What's waiting, for Claude to answer "what's downloading?" from: each request Nova
        is still watching for, with how long ago it was asked for - "Fashion Killa by A$AP
        Rocky, asked for 2 minutes ago." A song is named as it was asked for until SoulSync
        has found it, then as "X requested." names it. Only what Nova watches:
        SoulSync's own queue isn't asked, so anything it carries on with once Nova has
        given up isn't here.

        Returns:
            A sentence for each request waiting, in the order they were asked for - or
            NOTHING_WAITING.
        """
        with self._lock:
            now = self._now()
            waiting = [(self._names.get(key, wanted.name), now - self._asked_at[key])
                       for key, wanted in self._waiting.items()]
        if not waiting:
            return NOTHING_WAITING
        return " ".join(f"{name}, {_asked_ago(seconds)}." for name, seconds in waiting)

    def resume_swaps(self):
        """
        Carries on the saved swaps not running now - those a restart came in the middle of,
        or left for later - on a thread of its own: as the brain starts, and as each
        download starts. Never two at once.
        """
        if self._swapper is None:
            return
        with self._lock:
            if self._resuming or self._stopping.is_set():
                return
            self._resuming = True
        threading.Thread(target=self._resume_swaps, daemon=True, name="swap-resume").start()

    def _resume_swaps(self):
        try:
            self._swapper.resume(begin=self._swap_carried_on, end=self._swap_over)
        except Exception:
            logger.exception("Carrying on the saved swaps failed")
        finally:
            with self._lock:
                self._resuming = False

    def _swap_carried_on(self, swap: Dict):
        """
        Counts a download's swap, carried on after a restart forgot its watch, as waiting -
        so "download X" says it's already being got, and "what's downloading?" names it.
        The library check's swaps aren't downloads.
        """
        if swap.get("quiet", True) or not swap.get("title"):
            return
        wanted = Wanted(swap["title"], swap.get("artist"), "track")
        with self._lock:
            if wanted.key in self._waiting or self._stopping.is_set():
                return
            self._waiting[wanted.key] = wanted
            # On the time limits' clock, from when SoulSync was asked by the wall clock.
            asked_at = swap.get("asked_at")
            ago = max(0.0, self._wall() - asked_at) if isinstance(asked_at, (int, float)) else 0.0
            self._asked_at[wanted.key] = self._now() - ago
            self._names[wanted.key] = swap.get("named") or wanted.name
            self._resumed[swap.get("key")] = wanted

    def _swap_over(self, swap: Dict):
        """No longer counts a download's swap carried on as waiting, once it's over."""
        with self._lock:
            wanted = self._resumed.pop(swap.get("key"), None)
            if wanted is not None and self._waiting.get(wanted.key) is wanted:
                del self._waiting[wanted.key]
                del self._asked_at[wanted.key]
                self._names.pop(wanted.key, None)

    def stop(self):
        """
        Ends every watch, as Nova stops, and every swap at its next step - each swap is
        kept, to be carried on at the next start. Whatever is still waiting is
        forgotten, and logged, and nothing more is said of it - SoulSync carries on, so
        anything which lands later is simply in Navidrome.
        """
        with self._lock:
            forgotten = [wanted.name for wanted in self._waiting.values()]
            self._waiting.clear()
            self._asked_at.clear()
            self._names.clear()
            self._stopping.set()
        if self._swapper is not None:
            self._swapper.stop()
        if forgotten:
            logger.warning(f"Forgot {len(forgotten)} download{'' if len(forgotten) == 1 else 's'} still waiting, "
                           f"as Nova is stopping: {', '.join(forgotten)}")

    def _watch(self, wanted: Wanted, satellite: Optional[str]) -> str:
        """
        The whole of one request: asks SoulSync for it, says it's requested, then
        asks Navidrome every POLL_EVERY seconds until it has landed or run out of
        time, and says which.

        Args:
            wanted: What was asked for.
            satellite: The satellite that asked, or None.

        Returns:
            The last message said, or "" if Nova stopped first.
        """
        # How it's named in what's said - the song SoulSync found, once it has - so
        # even the answer to something unforeseen names what was requested.
        named = [wanted]
        try:
            return self._follow(wanted, satellite, named)
        except Exception:
            # Anything unforeseen still ends in an answer, so nobody waits for one
            # which never comes.
            logger.exception(f"Watching the download of {named[0].name} failed")
            return self._tell(satellite, _couldnt(named[0], None))
        finally:
            with self._lock:
                if self._waiting.get(wanted.key) is wanted:
                    del self._waiting[wanted.key]
                    del self._asked_at[wanted.key]
                    self._names.pop(wanted.key, None)

    def _follow(self, wanted: Wanted, satellite: Optional[str], named: List[Wanted]) -> str:
        """
        The watch itself, as _watch describes it - which answers even when this goes
        wrong.

        A song is named in all that's said of it from "X requested." on as SoulSync
        found it: "endless by Gunna" for a misheard "Endless by Gunner". It's looked
        for in Navidrome by the title asked for and SoulSync's artist, though: every
        word asked for is in SoulSync's title, but its import may tag the file without
        a "(feat. ...)" or a remaster SoulSync's title had.

        Args:
            wanted: What was asked for.
            satellite: The satellite that asked, or None.
            named: Holds how it's named, for _watch - set to the song SoulSync found
                once it has.
        """
        soulsync, library = self._soulsync(), self._library()
        if soulsync is None or library is None:
            # Set up when it was asked for, but not now: the settings changed in between.
            return self._tell(satellite, _couldnt(wanted, self.problem()))

        started = self._now()
        # What Navidrome is searched for, which for an album is what was asked for.
        sought = wanted
        try:
            if wanted.kind == "album":
                had = self._had(library, wanted)
                if had and self._swapper is not None and _as_had(wanted, had, self._heard_clean) is None:
                    # Every album found has tracks heard clean - a yes to the explicit one.
                    # SoulSync would skip the tracks it has rows for, so those are swapped.
                    return self._swap_album_had(wanted, had, satellite)
                request = soulsync.request_album(wanted.title, wanted.artist)
            else:
                request = soulsync.find_song(wanted.title, wanted.artist)
                sought = Wanted(wanted.title, request.artist, "track")
                found = named[0] = Wanted(request.title, request.artist, "track")
                self._name_as_found(wanted, found)
                wanted = found
                # Looked for again by SoulSync's artist: Navidrome may have Gunna's
                # "endless" though it had nothing by "Gunner". Had only clean, it's
                # still asked for, as the explicit one is what's wanted. The same look
                # as for what lands, so what was there before is never taken for it.
                had = self._had(library, sought)
                name = _as_had(wanted, had, self._heard_clean)
                if name:
                    logger.info(f"Navidrome already has {name}, so it isn't requested")
                    return self._tell(satellite, f"You've already got {name}.")
                if had and self._swapper is not None:
                    # Every copy it has was heard clean - a yes to the explicit one. SoulSync's
                    # wishlist would drop a request as "already owned", so the clean copy is swapped.
                    return self._swap_had(wanted, sought, had, satellite)
                soulsync.request_song(request)
        except SoulSyncError as e:
            return self._tell(satellite, _couldnt(wanted, e.reason))
        before = {track.get("id") for track in had}
        if isinstance(request, AlbumRequest) and request.queued <= 0:
            # Nothing of it is coming, so there's nothing to wait for.
            return self._tell(satellite, _none_queued(wanted, had, self._heard_clean))
        self._tell(satellite, f"{wanted.name} requested.")

        new: List[Dict] = []
        scanned = False
        while not self._wait(POLL_EVERY):
            limit = self._limit(wanted)
            if self._now() - started >= limit:
                new = self._look(library, sought, before, new)
                return self._tell(satellite, self._last_word(wanted, sought, request, new, satellite))
            new = self._look(library, sought, before, new)
            outcome = self._landed(wanted, sought, request, new, satellite)
            if outcome:
                return self._tell(satellite, outcome)
            why = None if scanned else self._scan_due(wanted, started, limit)
            if why:
                # Once only: a scan Navidrome refused - Nova's user not an admin - or
                # one which didn't find it won't do better asked again.
                scanned = True
                logger.info(f"{why}, so Navidrome is asked to scan")
                library.start_scan()
        return ""

    def _name_as_found(self, asked: Wanted, found: Wanted):
        """
        Names a song waiting as SoulSync found it, for describe_waiting - unless this
        request is no longer the one waiting, as once Nova has stopped.
        """
        with self._lock:
            if self._waiting.get(asked.key) is asked:
                self._names[asked.key] = found.name

    def _scan_due(self, wanted: Wanted, started: float, limit: float) -> Optional[str]:
        """
        Why Navidrome is to be asked to scan now, or None if it isn't yet: SCAN_AFTER
        seconds and a poll before the time limit, as SoulSync never says it's done.
        """
        if self._now() - started >= limit - SCAN_AFTER - POLL_EVERY:
            return f"Time is nearly up for {wanted.name}, and Navidrome hasn't got it"
        return None

    def _tell(self, satellite: Optional[str], message: str) -> str:
        """
        Says a message about a download, unless Nova is stopping - then nothing
        more is said of any of them.

        Returns:
            The message, or "" if it wasn't said.
        """
        if self._stopping.is_set():
            return ""
        logger.info(f"For {f'the {satellite} satellite' if satellite else 'no satellite'}: {message}")
        self._say(satellite, message)
        return message

    def _limit(self, wanted: Wanted) -> float:
        """The time limit for the kind asked for, in seconds, from the settings as they are now."""
        if wanted.kind == "album":
            return float(self.config.section("music").get("album_download_minutes", ALBUM_MINUTES)) * 60
        return song_limit(self.config)

    @staticmethod
    def _find(library: NavidromeLibrary, wanted: Wanted) -> List[Dict]:
        """Every copy of the song, or every track of the album, Navidrome has."""
        if wanted.kind == "album":
            return library.find_album_tracks(wanted.title, wanted.artist)
        return library.find_songs(wanted.title, wanted.artist)

    def _had(self, library: NavidromeLibrary, wanted: Wanted) -> List[Dict]:
        """
        What Navidrome had of the request before it was made - nothing if Navidrome
        couldn't be asked, when anything of it found later counts.
        """
        try:
            return self._find(library, wanted)
        except NAVIDROME_ERRORS as e:
            NavidromeLibrary._failure_message(e)
            logger.warning(f"Couldn't see what Navidrome had of {wanted.name} before asking for it, "
                           f"so anything of it found counts as the download")
            return []

    def _look(self, library: NavidromeLibrary, wanted: Wanted, before: Set[str], last: List[Dict]) -> List[Dict]:
        """
        What Navidrome has of the request now which it hadn't before - or, if it
        can't be asked this time, what it had last time, and the watch carries on.
        """
        try:
            found = self._find(library, wanted)
        except NAVIDROME_ERRORS as e:
            NavidromeLibrary._failure_message(e)
            logger.warning(f"Couldn't look for {wanted.name} in Navidrome this time, so the watch carries on")
            return last
        return [track for track in found if track.get("id") not in before]

    def _landed(self, wanted: Wanted, sought: Wanted, request: Union[SongRequest, AlbumRequest], new: List[Dict],
                satellite: Optional[str]) -> Optional[str]:
        """
        What to say if the request has landed, once what landed is heard - or None to
        wait on. A song has once a copy of it is new in Navidrome; an album once as many
        of its tracks are new as SoulSync queued - nine of Rumours' eleven, with the
        other two had.

        Args:
            wanted: What was asked for, named as SoulSync found a song.
            sought: What Navidrome is searched for - for a song, the title asked for and
                SoulSync's artist, as SoulSync is asked for it again in a swap.
            request: The SongRequest or AlbumRequest SoulSync gave.
            new: What's new in Navidrome.
            satellite: The satellite that asked, or None.
        """
        if wanted.kind != "album":
            return self._heard_song(wanted, sought, new, satellite) if new else None
        tracks = _album_tracks(request, new)
        if len(tracks) < request.queued:
            return None
        return self._heard_album(f"{wanted.name} has been downloaded.", tracks, satellite)

    def _last_word(self, wanted: Wanted, sought: Wanted, request: Union[SongRequest, AlbumRequest],
                   new: List[Dict], satellite: Optional[str]) -> str:
        """
        What to say once the watch ends without seeing all of it land, as time has run
        out: what did land, once heard, or that it couldn't be downloaded.

        Args:
            wanted: What was asked for.
            sought: What Navidrome is searched for, as _landed has it.
            request: The SongRequest or AlbumRequest SoulSync gave.
            new: What's new in Navidrome, at its last look.
            satellite: The satellite that asked, or None.
        """
        if wanted.kind != "album":
            return (self._heard_song(wanted, sought, new, satellite) if new else "") or _couldnt(wanted, None)
        tracks = _album_tracks(request, new)
        if tracks and len(tracks) < request.queued:
            return self._heard_album(f"Only {len(tracks)} of the {request.queued} tracks of {wanted.name} came down.",
                                     tracks, satellite)
        if tracks:
            # All of it, at the last look.
            return self._heard_album(f"{wanted.name} has been downloaded.", tracks, satellite)
        return _couldnt(wanted, None)

    def _heard_song(self, wanted: Wanted, sought: Wanted, copies: List[Dict], satellite: Optional[str]) -> str:
        """
        What to say of a song once the copies of it which landed are heard, and a clean
        one swapped: "downloaded" only for an explicit copy, or one with nothing to bleep.

        Returns:
            The sentence, or "" if Nova stopped part way.
        """
        verdict, copy = self._hear(copies)
        outcome = self._outcome(verdict, copy, sought, satellite)
        if outcome == "stopped":
            return ""
        if outcome == "downloaded":
            return f"{wanted.name} has been downloaded."
        if outcome == "couldnt" and self._swapper is None:
            return swap_said(wanted.name, outcome, "swapping isn't set up")
        return swap_said(wanted.name, outcome)

    def _swap_had(self, wanted: Wanted, sought: Wanted, had: List[Dict], satellite: Optional[str]) -> str:
        """
        The explicit copy of a song Navidrome has only clean, got by swapping the clean one -
        heard again first, as its file may have changed since, so only a copy still clean is
        swapped.

        Args:
            wanted: The song, named as SoulSync found it.
            sought: The song as SoulSync is asked for it again.
            had: The copies Navidrome has, every one heard clean before.
            satellite: The satellite that asked, or None.

        Returns:
            The last message said, or "" if Nova stopped first.
        """
        self._tell(satellite, f"{wanted.name} requested.")
        verdict, copy = self._hear(had)
        outcome = self._outcome(verdict, copy, sought, satellite)
        if outcome == "stopped":
            return ""
        if outcome == "downloaded":
            return self._tell(satellite, f"You've already got {wanted.name}.")
        if verdict.kind == "unsure":
            return self._tell(satellite, f"You've already got {wanted.name}, but I couldn't tell if it's the clean "
                                         f"version.")
        if verdict.kind == "not_judged":
            # Section 5's "couldn't check it for a clean version", with nothing new downloaded.
            return self._tell(satellite, f"You've already got {wanted.name}, but I couldn't check it for a clean "
                                         f"version.")
        if outcome == "couldnt":
            reason = (getattr(outcome, "reason", "") or "swapping isn't set up").strip().rstrip(".")
            return self._tell(satellite, f"I couldn't download {wanted.name}, so you've still got the clean version: "
                                         f"{reason}.")
        return self._tell(satellite, swap_said(wanted.name, outcome))

    def _swap_album_had(self, wanted: Wanted, had: List[Dict], satellite: Optional[str]) -> str:
        """
        The explicit copies of an album's tracks Navidrome has only clean, got by swapping
        each track heard clean, one at a time - each heard again first, as its file may have
        changed since.

        Args:
            wanted: The album asked for.
            had: Every track of it Navidrome has.
            satellite: The satellite that asked, or None.

        Returns:
            The last message said, or "" if Nova stopped first.
        """
        self._tell(satellite, f"{wanted.name} requested.")
        outcomes: Counter = Counter()
        reasons: List[str] = []
        for track in [track for track in had if self._heard_clean(track)]:
            outcome = self._heard_track([track], satellite)
            if outcome == "stopped":
                return ""
            outcomes[outcome] += 1
            if outcome == "couldnt":
                reasons.append((getattr(outcome, "reason", "") or "swapping isn't set up").strip().rstrip("."))
        # "Downloaded" only once something was: with nothing swapped, the library is as
        # clean as it was.
        if outcomes["swapped"]:
            lead = f"{wanted.name} has been downloaded."
        elif outcomes["couldnt"] or outcomes["only_clean"]:
            reason = reasons[0] if reasons else ONLY_CLEAN
            lead = f"I couldn't download {wanted.name}, so you've still got the clean version: {reason}."
        else:
            # None of them clean after all, or none that could be judged.
            lead = f"You've already got {wanted.name}."
        return self._tell(satellite, _album_said(lead, outcomes))

    def _heard_album(self, lead: str, tracks: List[List[Dict]], satellite: Optional[str]) -> str:
        """
        What to say of an album once each track which landed is heard, and the clean ones
        swapped one at a time - each asked for again as a song, by its own title and artist.

        Args:
            lead: What's said first, of how much of it came down.
            tracks: The copies of each track which landed, as _album_tracks gives them.
            satellite: The satellite that asked, or None.

        Returns:
            The sentence, or "" if Nova stopped part way.
        """
        outcomes: Counter = Counter()
        for copies in tracks:
            outcome = self._heard_track(copies, satellite)
            if outcome == "stopped":
                return ""
            outcomes[outcome] += 1
        return _album_said(lead, outcomes)

    def _heard_track(self, copies: List[Dict], satellite: Optional[str]) -> str:
        """
        How one track of an album ends, once heard - and swapped if it's clean, asked for
        again as a song by its own title, without a clean note SoulSync would never find
        an explicit copy under, and its own artist. "stopped" if Nova is stopping.
        """
        if self._stopping.is_set():
            return "stopped"
        verdict, copy = self._hear(copies)
        swap_as = Wanted(_without_clean_note(copy.get("title") or ""), copy.get("artist") or None, "track")
        return self._outcome(verdict, copy, swap_as, satellite)

    def _hear(self, copies: List[Dict]) -> Tuple[Verdict, Dict]:
        """
        Hears the copies of a song which landed, as a download's, which goes ahead of the
        library check. Whatever goes wrong is "not judged", so the download is still said.
        """
        if self._ears is None:
            return Verdict("not_judged", 0, 0, "listening isn't set up"), copies[0]
        try:
            return self._ears.hear_copies(copies, download=True)
        except Exception:
            logger.exception(f"Hearing {copies[0].get('title')} failed")
            return Verdict("not_judged", 0, 0, "the listening failed"), copies[0]

    def _outcome(self, verdict: Verdict, copy: Dict, swap_as: Wanted, satellite: Optional[str]) -> str:
        """
        How a song which landed ends: "downloaded" if it's explicit or has nothing to
        bleep; "unsure" or "not_judged", kept as it is; or, heard clean, how its swap
        ended - "swapped", "only_clean", "couldnt", "unsure", "not_judged" or "stopped".

        Args:
            verdict: What was heard of it.
            copy: The copy the verdict is of, to swap.
            swap_as: The song as SoulSync is asked for it again.
            satellite: The satellite that asked, or None.
        """
        if verdict.kind in ("explicit", "too_few"):
            return "downloaded"
        if verdict.kind in ("unsure", "not_judged"):
            return verdict.kind
        if self._swapper is None:
            logger.warning(f"{copy.get('title')} came down clean, and there's nothing to swap it with")
            return "couldnt"
        return self._swapper.swap(copy, swap_as, satellite, quiet=False)

    def _heard_clean(self, track: Dict) -> bool:
        """
        Whether Nova heard a copy in the library clean - judged by ear, as kept in the
        store, never by its name or Navidrome's mark. A copy whose size Navidrome now
        gives differently has changed since, so isn't known.
        """
        if self._store is None or not track.get("id"):
            return False
        record = self._store.verdict(track["id"]) or {}
        if (record.get("verdict") or {}).get("kind") != "clean":
            return False
        size, heard_size = track.get("size"), record.get("size")
        return not (isinstance(size, int) and isinstance(heard_size, int) and size != heard_size)
