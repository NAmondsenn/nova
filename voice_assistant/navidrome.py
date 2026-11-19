"""
Navidrome Module.

Plays music from a Navidrome server - your own music, on your own network - through
the assistant's own speaker.

Navidrome speaks the Subsonic API, which this uses to find what to play and to
fetch it. Each song is asked for as an MP3 - Navidrome converts anything else as it
goes - decoded by ffmpeg, and played through speaker.py like everything else the
assistant says. So pausing it while the assistant talks is instant and needs no
internet, where every Spotify command makes a round trip through Spotify's servers.
"""

import hashlib
import logging
import os
import queue
import random
import re
import secrets
import subprocess
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import numpy as np
import requests

from voice_assistant import network
from voice_assistant.music import means_liked_songs

# Logger setup
logger = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "nova"

# Seconds to wait for Navidrome before giving up. It's on the local network, so
# anything slower than this means it's down.
REQUEST_TIMEOUT = 5

# Seconds of a song decoded ahead of what's playing - see Speaker.play_stream.
DECODE_AHEAD = 5.0

# "Previous" this far into a song starts it again, as on most players; sooner than
# this, it goes back to the one before.
RESTART_AFTER = 3.0

# How many of an artist's albums are read when playing the artist, and how many
# songs are queued at most, so a big library isn't read all at once.
MOST_ALBUMS = 10
MOST_SONGS = 200

# Songs in a row which wouldn't play before the queue is given up on, so a broken
# library on repeat can't spin through itself forever.
MOST_FAILURES = 3

# Subsonic's error code for a wrong username or password.
WRONG_LOGIN = 40

# Its error code for a user who isn't allowed to do what was asked - a scan, say,
# which only an admin may start.
NOT_AUTHORISED = 50

# How many of the newest albums are asked for at a time to see which came in lately, and
# how many such pages at most: 4,000 albums, far more than arrive in a month. Past that,
# what's found is said to be "at least" what came.
NEWEST_ALBUMS = 200
NEWEST_PAGES = 20

# The fractional seconds of a time Navidrome writes - nine digits - which are dropped, as a
# second either way makes no difference to when an album came in.
FRACTION = re.compile(r"(\.\d+)")

# How many songs a playlist Nova makes is given in one request, so the request's address,
# which lists each song's id, stays well short of what a server or proxy in front of it takes.
PLAYLIST_SONGS_AT_ONCE = 100

# How big a cover is asked for, in pixels a side, for the settings page.
COVER_SIZE = 300

# Seconds to wait for a whole song's file, fetched at once for listening to it: a
# 50 MB FLAC takes a few seconds on the local network, far more than REQUEST_TIMEOUT.
AUDIO_TIMEOUT = 120

# Words too common in playlist names to tell one from another on their own. Asked
# for "light songs" - Whisper's "liked songs" - "Car Songs" was played, on the
# strength of "songs" alone.
GENERIC_PLAYLIST_WORDS = {"songs", "song", "music", "playlist", "playlists", "mix", "tracks", "track",
                          "list", "my", "some", "tunes"}

# The versions of a song or album which aren't the one asked for unless they're named,
# and the ones which are only the same thing with the swearing taken out. Navidrome
# has no field for them, so they're read from the names - see version_of.
VERSIONS = ("clean", "edited", "radio edit", "censored", "live", "remix", "acoustic", "instrumental", "sped up",
            "slowed", "karaoke", "demo")
CLEAN_VERSIONS = ("clean", "edited", "radio edit", "censored")

# What may follow a clean version's word, in its part of a name, and leave it a clean
# version: "(Clean Album Version)", "(Clean Lyrics)", "(Clean Advance Copy)".
CLEAN_TAILS = ("album", "version", "edit", "edition", "lyrics", "advance copy")

# The words which start a credit, where one isn't bracketed: "Money In The Grave feat. Rick
# Ross". "with" isn't one here, as "Stay With Me" is a title of its own.
CREDIT_WORDS = ("feat", "ft", "featuring")

# Where a name says which version it is: a bracketed part, round or square, or what
# follows a spaced dash - a hyphen, an en dash or an em dash, as tags have each of
# them: "Gooba - Radio Edit", "Gooba \u2013 Radio Edit".
BRACKETED = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")
DASH = re.compile(" [-\u2013\u2014] ")


class NavidromeError(Exception):
    """Navidrome answered, but with an error - a wrong password, say."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def normalise(name: str) -> str:
    """
    A name reduced to plain lowercase words, for comparing what was said with
    what's in the library: "Beyoncé - Halo (Live)!" -> "beyonce halo live".
    """
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    return name[4:] if name.startswith("the ") else name


def likeness(found: str, wanted: str) -> int:
    """
    How well a name in the library matches one asked for: 3 for the same, 2 if
    every word asked for is in it, 1 if some are, 0 if none.
    """
    found, wanted = normalise(found), normalise(wanted)
    if not wanted:
        return 0
    if found == wanted:
        return 3
    found_words, wanted_words = set(found.split()), set(wanted.split())
    if wanted_words <= found_words:
        return 2
    return 1 if found_words & wanted_words else 0


def artist_likeness(found: str, wanted: str) -> int:
    """
    How well an artist matches one asked for, as likeness has it, with the word
    "and" taken out of both names. Claude says "Jay-Z and Kanye West" where the
    library has "JAY-Z & Kanye West", and "Florence and the Machine" for "Florence +
    the Machine" - and an "and" asked for which isn't there would leave a word
    unmatched, and the artist not theirs.

    Args:
        found: The artist in the library, or SoulSync's.
        wanted: The artist as asked for.

    Returns:
        As likeness: 3 for the same, 2 if every word asked for is there, 1 if some
        are, 0 if none.
    """
    def without_and(name: str) -> str:
        # A name which is only "and" is kept, so it can still be matched.
        return " ".join(word for word in normalise(name).split() if word != "and") or normalise(name)

    return likeness(without_and(found), without_and(wanted))


def split_by(query: str) -> Tuple[str, Optional[str]]:
    """'Gooba by 6ix9ine' -> ('Gooba', '6ix9ine'); a title alone has no artist."""
    parts = re.split(r"\s+by\s+", query.strip(), flags=re.IGNORECASE)
    if len(parts) >= 2 and parts[-1]:
        return " by ".join(parts[:-1]), parts[-1]
    return query.strip(), None


def distinctive(name: str) -> str:
    """
    A playlist name without the words almost every playlist name has - "Car Songs"
    -> "car" - so sharing only "songs" doesn't make two names a match.
    """
    return " ".join(word for word in normalise(name).split() if word not in GENERIC_PLAYLIST_WORDS)


def best(items: List[Dict], name_key: str, name: str, artist: Optional[str]) -> Optional[Dict]:
    """
    The item which best matches a name, and the artist if one was given - or None
    if nothing matches the name at all.
    """
    scored = [(likeness(item.get(name_key, ""), name) * 2
               + (likeness(item.get("artist", ""), artist) if artist else 0), index, item)
              for index, item in enumerate(items) if likeness(item.get(name_key, ""), name)]
    if not scored:
        return None
    # The highest score, and the earliest of equals - Navidrome lists its best guesses first.
    return max(scored, key=lambda scored_item: (scored_item[0], -scored_item[1]))[2]


def _names_version(text: str, version: str) -> bool:
    """Whether text has the version's words, as whole words: "Alive" has no "live"."""
    return f" {version} " in f" {normalise(text)} "


def _says_version(part: str, version: str) -> bool:
    """
    Whether a bracketed or dashed part of a name says it's this version.

    A clean version's word must end the part, bar CLEAN_TAILS: "(Clean)", "(Clean
    Album Version)", "(Clean Lyrics)" and "(Radio Edit)" say so, but
    "(feat. Clean Bandit)" and "(Clean Bandit Remix)" only credit an artist called
    Clean. Taking those for clean versions would have an explicit song reported as
    a clean one.
    """
    if version in CLEAN_VERSIONS:
        tails = "|".join(CLEAN_TAILS)
        return re.search(rf"(?:^| ){version}(?: (?:{tails}))*$", normalise(part)) is not None
    return _names_version(part, version)


def version_of(name: str) -> Optional[str]:
    """
    The first of VERSIONS which a title or album name says it is, in a bracketed
    part or after a spaced dash (a hyphen, an en dash or an em dash): "Gooba
    (Clean)" -> "clean", "Gooba - Live at Wembley" -> "live". Anywhere else the
    word is only part of the title, so Taylor Swift's "Clean" and "Edited
    Highlights" are no version of anything.

    A clean version's word counts only when nothing but CLEAN_TAILS comes after it
    in its part: "(Clean Version)", "(Clean Lyrics)", "(Edited Album Version)" and
    "(Live - Radio Edit)" are clean versions, but "(feat. Clean Bandit)" is no
    version and "(Clean Bandit Remix)" is a remix.

    Args:
        name: A song's title or an album's name.

    Returns:
        The version, as spelt in VERSIONS, or None if the name says none.
    """
    name = name or ""
    # Each part on its own, so "(Radio) (Edit)" isn't taken for a radio edit.
    parts = [text for brackets in BRACKETED.findall(name) for text in brackets]
    dashed = DASH.split(name, 1)
    if len(dashed) == 2:
        # Without its brackets, which are parts of their own: "Gooba - Radio Edit
        # (feat. X)" is a radio edit, which a credit after it would otherwise hide.
        parts.append(BRACKETED.sub("", dashed[1]))
    return next((version for version in VERSIONS if any(_says_version(part, version) for part in parts)), None)


def marked_clean(name: str) -> bool:
    """Whether a title or album name says it's a clean version."""
    return version_of(name) in CLEAN_VERSIONS


def is_clean(track: Dict) -> bool:
    """
    Whether a track is a clean version. Navidrome's own mark wins - its explicitStatus,
    "explicit" or "clean", from the file's tags. Most songs with nothing to bleep
    carry no mark at all, so an unmarked track is clean only if its title or its
    album's name says so.

    Args:
        track: A song as Navidrome describes it, with its title, album and explicitStatus.
    """
    status = (track.get("explicitStatus") or "").strip().lower()
    if status in ("clean", "explicit"):
        return status == "clean"
    return marked_clean(track.get("title") or "") or marked_clean(track.get("album") or "")


def _own_words(name: str) -> List[str]:
    """
    The words of a song's title which are its own: not those in brackets, nor after a
    spaced dash, nor after an unbracketed credit - "Gooba (Live - Radio Edit)" and "Money
    In The Grave feat. Rick Ross" are "gooba" and "money in the grave". The brackets go
    first, so a dash inside them doesn't cut the title short.
    """
    words = normalise(DASH.split(BRACKETED.sub("", name), 1)[0]).split()
    cut = next((index for index, word in enumerate(words) if index and word in CREDIT_WORDS), len(words))
    return words[:cut]


def _wider_title(name: str, titles: List[str]) -> bool:
    """
    Whether a song's title has words of its own (see _own_words) which none of the titles
    wanted has: "Endless Love" is wider than "Endless", and another song. "Endless (feat.
    X)" and "Endless - 2011 Remaster" aren't wider, and nor is "Sweet Dreams (Are Made of
    This)" for "Sweet Dreams".

    Args:
        name: The song's title in the library.
        titles: The titles it may be: the one asked for, and the one SoulSync found for it,
            which may have more words - "Endless Love" for "Endless".
    """
    own = set(_own_words(name))
    return not any(own <= set(normalise(title).split()) for title in titles)


def _asked_for(item: Dict, name_key: str, title: str, artist: Optional[str], also: Optional[str] = None) -> bool:
    """
    Whether a song or album found is what was asked for: its name has every word of
    the title, it's by the artist if one was given, and it isn't a live version,
    remix or the like unless the title says so. A clean version always is, as the
    same thing without the swearing. A song isn't one with more words to its title,
    which is another song: "Endless Love" isn't "Endless". An album's name may have
    more, as "Rumours (Deluxe Edition)" has. The song SoulSync found for it - also - may
    have more words than was asked for, and then it is the one asked for.
    """
    name = item.get(name_key) or ""
    if likeness(name, title) < 2 or (artist and artist_likeness(item.get("artist") or "", artist) < 2):
        return False
    if name_key == "title" and _wider_title(name, [title] + ([also] if also else [])):
        return False
    version = version_of(name)
    return version is None or version in CLEAN_VERSIONS or _names_version(title, version)


def _arrived(item: Dict) -> Optional[float]:
    """
    When an album or a song came into Navidrome, in seconds since the epoch, from its created
    time - e.g. "2026-11-15T20:01:02.123456789Z" - or None if it hasn't one, or can't be read.
    A time without an offset is taken for UTC, which is how Navidrome writes them.
    """
    try:
        text = FRACTION.sub("", str(item.get("created") or "")).replace("Z", "+00:00")
        arrived = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (arrived if arrived.tzinfo else arrived.replace(tzinfo=timezone.utc)).timestamp()


class NavidromeLibrary:
    """
    Asks Navidrome what it holds, and to scan for more - without playing any of it - and
    keeps the one playlist Nova makes there (see new_music.py).
    """

    def __init__(self, url: str, username: str, password: str):
        """
        Args:
            url: Navidrome's address, e.g. "http://navidrome.local:4533".
            username: The Navidrome user to sign in as.
            password: Its password.
        """
        self.url = url.rstrip("/")
        self.username = username
        self._password = password
        self._session = requests.Session()
        # The native API's token, from signing in to it once - see song_file - and the lock
        # which lets only one thread sign in for a new one: listening and swapping share a
        # library, and Navidrome limits how often it can be signed in to.
        self._native_token: Optional[str] = None
        self._native_lock = threading.Lock()

    # Talking to Navidrome

    def _sign_in(self) -> Dict[str, str]:
        """
        The parameters which sign each request in. The password itself is never
        sent - only a hash of it with a random salt, new each time, as the Subsonic
        API has it.
        """
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()
        return {"u": self.username, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}

    def _call(self, endpoint: str, **params) -> Dict:
        """
        One Subsonic API request.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
            ValueError: If its answer wasn't JSON, or wasn't a Subsonic response.
        """
        # On the local network, so it's still asked with only the internet down - but
        # not with no network at all, which only waited out looking its name up. See
        # HomeAssistant._request.
        if not network.online():
            raise requests.ConnectionError("No network connection")
        response = self._session.get(f"{self.url}/rest/{endpoint}", params={**self._sign_in(), **params},
                                     timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        answer = response.json()
        body = answer.get("subsonic-response") if isinstance(answer, dict) else None
        # Any other shape, a list say, is an answer that couldn't be read, which callers handle
        # as ValueError, as they do JSON that isn't; one without it at all is an unknown error.
        if not isinstance(answer, dict) or not isinstance(body, (dict, type(None))):
            raise ValueError("Navidrome's answer wasn't a Subsonic response")
        body = body or {}
        if body.get("status") != "ok":
            error = body.get("error") or {}
            raise NavidromeError(error.get("message") or "unknown error", error.get("code"))
        return body

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """
        What to say when Navidrome couldn't be asked. Only the kind of error is
        logged, never the error itself, which can hold the request's address -
        and with it the signed-in token.
        """
        logger.error(f"Navidrome request failed: {type(error).__name__}"
                     + (f" ({error})" if isinstance(error, NavidromeError) else ""))
        if isinstance(error, NavidromeError) and error.code == WRONG_LOGIN:
            return "Navidrome didn't accept my password. NAVIDROME_PASSWORD in the .env file needs checking."
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return "I can't reach Navidrome right now."
        return "Navidrome had a problem with that."

    @staticmethod
    def _cant_ask(what: str, error: Exception) -> NavidromeError:
        """
        The error to raise when Navidrome couldn't be asked for something: the kind of
        error and any HTTP status, never the error itself, which can hold the address
        and with it the token - "Couldn't ask Navidrome for the song's file: HTTPError 500".
        It's raised "from None", so a traceback doesn't show the original either.
        """
        if isinstance(error, NavidromeError):
            return NavidromeError(f"Couldn't ask Navidrome for {what}: {error}", error.code)
        if isinstance(error, ValueError):
            return NavidromeError(f"Couldn't read Navidrome's answer for {what}")
        status = getattr(getattr(error, "response", None), "status_code", None)
        return NavidromeError(f"Couldn't ask Navidrome for {what}: {type(error).__name__}"
                              + (f" {status}" if status else ""))

    def _search(self, query: str, songs: int = 0, albums: int = 0, artists: int = 0) -> Dict:
        return self._call("search3", query=query, songCount=songs, albumCount=albums,
                          artistCount=artists).get("searchResult3") or {}

    def _album_songs(self, album_id: str) -> List[Dict]:
        return self._call("getAlbum", id=album_id).get("album", {}).get("song") or []

    # Finding what's there, for a download

    def _matches(self, kind: str, most: int, name_key: str, title: str, artist: Optional[str],
                 also: Optional[str] = None) -> List[Dict]:
        """
        The songs or albums a search finds which are what was asked for - see
        _asked_for. The title and artist are searched together, and the title alone
        if nothing they find is what was asked for - _find_song's way, a little
        wider: the library may spell the artist differently from how it was said.

        Args:
            kind: "song" or "album".
            most: How many Navidrome is asked for.
            name_key: "title" for a song, "name" for an album.
            title: The title or album as asked for.
            artist: The artist as asked for, or None.
            also: The title SoulSync found for a song, if it has more words than was asked for.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        for query in ([f"{title} {artist}", title] if artist else [title]):
            found = self._search(query, **{f"{kind}s": most}).get(kind) or []
            matches = [item for item in found if _asked_for(item, name_key, title, artist, also)]
            if matches:
                return matches
        return []

    def find_songs(self, title: str, artist: Optional[str], also: Optional[str] = None) -> List[Dict]:
        """
        The songs in Navidrome which are the one asked for - every copy, so a clean
        one and an explicit one are both there to tell apart with is_clean, but no
        song with more words to its title: see _asked_for.

        Args:
            title: The song's title as asked for.
            artist: Its artist, or None if only the title was given.
            also: The title SoulSync found for it, which may have more words than was
                asked for: "Endless Love" for "Endless" is the song asked for, when
                SoulSync found it, and no other song when it found "endless".

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        return self._matches("song", 20, "title", title, artist, also)

    def find_album_tracks(self, title: str, artist: Optional[str]) -> List[Dict]:
        """
        Every track of each album in Navidrome which is the one asked for, each with
        its album's name, for is_clean to read - and its id and artist, as albumId and
        albumArtist, so an explicit edition and a clean one beside it are told apart,
        and named by the album's artist rather than a track's.

        Args:
            title: The album's name as asked for.
            artist: Its artist, or None if only the name was given.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        albums = self._matches("album", 10, "name", title, artist)
        return [{**track, "album": album.get("name") or "", "albumId": album["id"],
                 "albumArtist": album.get("artist") or ""}
                for album in albums for track in self._album_songs(album["id"])]

    def newest_albums(self, since: float) -> Tuple[List[Dict], bool]:
        """
        The albums which came into Navidrome at or after a time, newest first, read a
        page of NEWEST_ALBUMS at a time until a page reaches back past the time, or
        runs out, or NEWEST_PAGES have been read.

        Args:
            since: The time, in seconds since the epoch.

        Returns:
            Each album's id, name, artist and songCount - and whether that's all of
            them: False if NEWEST_PAGES of them came in, when there may be more. An
            album with no readable created time isn't counted, as there's no saying
            when it came.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        found = []
        for page in range(NEWEST_PAGES):
            albums = self._call("getAlbumList2", type="newest", size=NEWEST_ALBUMS,
                                offset=page * NEWEST_ALBUMS).get("albumList2", {}).get("album") or []
            older = False
            # The whole page is read, not only up to the first older album, in case one is a little out of order.
            for album in albums:
                arrived = _arrived(album)
                if arrived is None:
                    continue
                if arrived < since:
                    older = True
                    continue
                found.append({"id": album.get("id"), "name": album.get("name") or "",
                              "artist": album.get("artist") or "", "songCount": album.get("songCount") or 0})
            if older or len(albums) < NEWEST_ALBUMS:
                return found, True
        return found, False

    def arrived_songs(self, since: float, most: int) -> List[str]:
        """
        The songs which came into Navidrome at or after a time: those of the albums
        newest_albums finds, newest album first, each album's in its own order.

        Args:
            since: The time, in seconds since the epoch.
            most: How many songs at most - the newest.

        Returns:
            The songs' ids. A song with a created time of its own before the time is
            left out, being an older song of an album which has had more added since.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        songs = []
        for album in self.newest_albums(since)[0]:
            for song in self._album_songs(album["id"]):
                arrived = _arrived(song)
                if song.get("id") and (arrived is None or arrived >= since):
                    songs.append(song["id"])
                if len(songs) >= most:
                    return songs
        return songs

    def playlist_with_id(self, playlist_id: str) -> Optional[Dict]:
        """
        The playlist of this id, if Navidrome still has it and it belongs to the user Nova
        signs in as - never another user's, even one Navidrome would let an admin change -
        or None. Nova only ever writes a playlist it recorded the id of when it made it
        (see new_music.py), so this is asked by id and never by name.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        if not playlist_id:
            return None
        playlists = self._call("getPlaylists").get("playlists", {}).get("playlist") or []
        return next((playlist for playlist in playlists if playlist.get("id") == playlist_id
                     and str(playlist.get("owner") or "").casefold() == self.username.casefold()), None)

    def make_playlist(self, name: str) -> str:
        """
        Makes a new, empty playlist of this name, always a new one whatever else is called
        that, and gives its id for the caller to record before it's filled.

        Args:
            name: The playlist's name.

        Returns:
            The new playlist's id.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error, or didn't say which playlist it
                made and it couldn't be told from those which were there before.
        """
        before = {playlist.get("id") for playlist in self._call("getPlaylists").get("playlists", {}).get(
            "playlist") or []}
        made = self._call("createPlaylist", name=name).get("playlist") or {}
        if made.get("id"):
            return made["id"]
        # A server too old to say what it made: the one of this exact name which wasn't there before,
        # the newest if more, and never a playlist which was.
        wanted = normalise(name)
        playlists = self._call("getPlaylists").get("playlists", {}).get("playlist") or []
        new = [playlist for playlist in playlists if playlist.get("id") not in before and wanted
               and normalise(playlist.get("name") or "") == wanted
               and str(playlist.get("owner") or "").casefold() == self.username.casefold()]
        if not new:
            raise NavidromeError("the playlist made wasn't found")
        # Newest by the time Navidrome gives it, else the last it lists.
        return max(enumerate(new), key=lambda at: (str(at[1].get("created") or at[1].get("changed") or ""), at[0]))[1]["id"]

    def replace_playlist(self, playlist_id: str, song_ids: List[str]):
        """
        Makes the playlist of this id, one Nova made, hold just these songs, in this order,
        and public so whichever Navidrome user listens sees it. No other playlist is touched.

        Args:
            playlist_id: The playlist's id, as make_playlist gave it.
            song_ids: The songs, at least one.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error - part way, the playlist may
                hold only some of the songs until it's made again.
        """
        first, rest = song_ids[:PLAYLIST_SONGS_AT_ONCE], song_ids[PLAYLIST_SONGS_AT_ONCE:]
        # Given a playlist's id, Navidrome's createPlaylist replaces its songs with those given.
        self._call("createPlaylist", playlistId=playlist_id, songId=first)
        for start in range(0, len(rest), PLAYLIST_SONGS_AT_ONCE):
            self._call("updatePlaylist", playlistId=playlist_id, songIdToAdd=rest[start:start + PLAYLIST_SONGS_AT_ONCE])
        self._call("updatePlaylist", playlistId=playlist_id, public="true")

    def start_scan(self) -> bool:
        """
        Asks Navidrome to scan its folders for new music, which it does in its own time.

        Returns:
            True if Navidrome took the request, False if it couldn't be asked or
            refused - which only an admin is allowed to ask of it.
        """
        try:
            self._call("startScan")
        except (requests.RequestException, NavidromeError, ValueError) as e:
            if isinstance(e, NavidromeError) and e.code == NOT_AUTHORISED:
                logger.warning("Navidrome refused to scan: the user Nova signs in as needs to be an admin for that")
            else:
                self._failure_message(e)
            return False
        return True

    # What a song is, for listening to it (decision 0022)

    def _native_sign_in(self) -> str:
        """
        Signs in to Navidrome's own API, which the Subsonic signing doesn't open, and
        keeps the token it hands out.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it refused the username or password, or gave no token.
        """
        response = self._session.post(f"{self.url}/auth/login",
                                      json={"username": self.username, "password": self._password},
                                      timeout=REQUEST_TIMEOUT)
        if response.status_code == 401:
            raise NavidromeError("Navidrome didn't accept the username or password", WRONG_LOGIN)
        response.raise_for_status()
        body = response.json()
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise NavidromeError("Navidrome's sign-in gave no token")
        self._native_token = token
        return token

    def _native_token_for(self, refused: Optional[str]) -> str:
        """
        The native API's token to ask with: the one kept, unless it's the one just refused,
        when it's signed in for a new one - unless another thread already has.

        Args:
            refused: The token Navidrome just refused, or None.

        Raises:
            As _native_sign_in.
        """
        with self._native_lock:
            if self._native_token and self._native_token != refused:
                return self._native_token
            self._native_token = None
            return self._native_sign_in()

    def song_file(self, song_id: str) -> Dict:
        """
        Where a song's file really is, from Navidrome's own API. The Subsonic path is
        built from the tags - "Gunna/The Last Wun (explicit)/01 - Endless.mp3" - and
        isn't where the file is.

        It signs in once and keeps the token, signing in again only when Navidrome no
        longer takes it - after a restart of Navidrome, say.

        Args:
            song_id: Navidrome's id for the song.

        Returns:
            Dict with the file's 'path' as Navidrome gives it, relative to its library
            ("Gunna/The Last Wun/endless.mp3"), its 'size' in bytes, and 'updated',
            Navidrome's updatedAt as given ("2026-11-15T10:00:00Z").

        Raises:
            NavidromeError: If Navidrome couldn't be asked, refused, or answered without
                them. Its message never holds the address, token or password.
        """
        try:
            if not network.online():
                raise requests.ConnectionError("No network connection")
            # Quoted whole, so an id can only ever name a song: "../auth/login" is asked
            # for as "..%2Fauth%2Flogin", not as another address.
            address = f"{self.url}/api/song/{quote(song_id, safe='')}"
            # A 401 is a token Navidrome no longer takes, so it's signed in again - once,
            # so a token it won't take even fresh is an error rather than a loop.
            refused = None
            for _ in range(2):
                token = self._native_token_for(refused)
                response = self._session.get(address, headers={"x-nd-authorization": f"Bearer {token}"},
                                             timeout=REQUEST_TIMEOUT)
                if response.status_code != 401:
                    break
                refused = token
            response.raise_for_status()
            song = response.json()
        except (requests.RequestException, NavidromeError, ValueError) as e:
            raise self._cant_ask("the song's file", e) from None
        path, size, updated = (song.get("path"), song.get("size"), song.get("updatedAt")) \
            if isinstance(song, dict) else (None, None, None)
        if not isinstance(path, str) or not path or isinstance(size, bool) or not isinstance(size, int) \
                or not isinstance(updated, str):
            raise NavidromeError("Navidrome's answer for the song's file had no path, size or time")
        return {"path": path, "size": size, "updated": updated}

    def audio(self, song_id: str) -> bytes:
        """
        A song's file as it's stored - FLAC as FLAC - rather than converted, so what's
        listened to is what's in the library.

        Args:
            song_id: Navidrome's id for the song.

        Returns:
            The file's bytes.

        Raises:
            NavidromeError: If Navidrome couldn't be asked, or sent something other than
                audio - an error, say, which the stream sends as JSON. Its message never
                holds the address or token.
        """
        try:
            if not network.online():
                raise requests.ConnectionError("No network connection")
            response = self._session.get(f"{self.url}/rest/stream",
                                         params={**self._sign_in(), "id": song_id, "format": "raw"},
                                         timeout=AUDIO_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise self._cant_ask("the song's audio", e) from None
        if not response.headers.get("Content-Type", "").startswith("audio"):
            raise NavidromeError("Navidrome sent something other than audio for the song")
        return response.content

    def lyrics(self, song_id: str) -> Optional[str]:
        """
        A song's lyrics as Navidrome has them, from the .lrc or .txt beside it - or its
        tags.

        Args:
            song_id: Navidrome's id for the song.

        Returns:
            The lines of the first copy with any words, joined with newlines - one copy
            only, as a .lrc and a .txt beside the same song would count every swear
            twice. None for a song with none, or one Navidrome refuses to say - "Song
            not found", say.

        Raises:
            NavidromeError: If Navidrome couldn't be asked, or refused the password. Its
                message never holds the address or token.
        """
        try:
            body = self._call("getLyricsBySongId", id=song_id)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            # A wrong password isn't a song without lyrics: LRCLIB's would be taken
            # instead, and the error said nowhere.
            if isinstance(e, NavidromeError) and e.code != WRONG_LOGIN:
                return None
            raise self._cant_ask("the lyrics", e) from None
        found = body.get("lyricsList")
        entries = found.get("structuredLyrics") if isinstance(found, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            lines = entry.get("line") if isinstance(entry, dict) else None
            lines = [line["value"] for line in (lines if isinstance(lines, list) else [])
                     if isinstance(line, dict) and isinstance(line.get("value"), str)]
            if any(line.strip() for line in lines):
                return "\n".join(lines)
        return None


class NavidromePlayer(NavidromeLibrary):
    """Finds music on Navidrome and plays it through the assistant's speaker."""

    name = "Navidrome"
    # It's on the local network, so it carries on with the internet down.
    needs_internet = False

    def __init__(self, url: str, username: str, password: str, speaker, volume):
        """
        Args:
            url: Navidrome's address, e.g. "http://navidrome.local:4533".
            username: The Navidrome user to sign in as.
            password: Its password.
            speaker: The Speaker to play through.
            volume: The VolumeControl, which keeps the music's own volume.
        """
        super().__init__(url, username, password)
        self.speaker = speaker
        self.volume = volume

        self._lock = threading.Condition()
        # What's playing and what's next, in the order it'll be played, and the
        # order it was in before any shuffling.
        self._queue: List[Dict] = []
        self._unshuffled: List[Dict] = []
        self._index = 0
        # How far into its song the playing one began, in seconds - not 0 for music
        # another player handed over part way through. See hand_over.
        self._offset = 0.0
        # Changed by anything which changes what plays next - skipping, a new
        # request - so the song which was stopped for it doesn't move the queue on
        # as well, as if it had finished by itself.
        self._turn = 0
        self._playback = None
        self._paused = False
        # While the assistant is talking, and whether the music carries on after it -
        # see pause_for_speech.
        self._talking = False
        self._resume_after = False
        self._interrupted = False
        self._repeat = "off"
        self._shuffle = False
        # The last cover fetched, by its id, so the settings page only has it fetched
        # once a song - see cover.
        self._cover: Optional[Tuple[str, Tuple[bytes, str]]] = None
        # Set by close, which ends _play_queue's thread. Without it, the thread waited
        # for something to play for ever, and held the whole of a satellite's old
        # session after it connected again.
        self._closed = False

        threading.Thread(target=self._play_queue, daemon=True, name="navidrome").start()

    @classmethod
    def from_config(cls, config, password: Optional[str], speaker, volume) -> Optional["NavidromePlayer"]:
        """
        The Navidrome config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - navidrome.url and username.
            password: NAVIDROME_PASSWORD from .env.
            speaker: The Speaker to play through.
            volume: The VolumeControl.

        Returns:
            A NavidromePlayer, or None without an address, a username and a password.
        """
        settings = config.section("navidrome")
        url, username = (settings.get("url") or "").strip(), (settings.get("username") or "").strip()
        if not url or not username:
            logger.warning("music.player is navidrome, but navidrome.url or navidrome.username isn't set")
            return None
        if not password:
            logger.warning("music.player is navidrome, but NAVIDROME_PASSWORD isn't in .env")
            return None
        logger.info(f"Navidrome at {url}, as {username}")
        return cls(url, username, password, speaker, volume)

    # Finding music

    def _find_song(self, query: str) -> Tuple[List[Dict], str]:
        """
        A song, followed by more by the same artist, shuffled - as Spotify carries on
        with something similar once the song asked for has finished.
        """
        title, artist = split_by(query)
        found = self._search(f"{title} {artist}" if artist else title, songs=20).get("song") or []
        if not found and artist:
            found = self._search(title, songs=20).get("song") or []
        song = best(found, "title", title, artist)
        if not song:
            return [], ""
        more = [other for other in self._search(song.get("artist", ""), songs=50).get("song") or []
                if other.get("id") != song.get("id")]
        random.shuffle(more)
        return [song] + more, f"{song.get('title')} by {song.get('artist')}"

    def _find_artist(self, query: str) -> Tuple[List[Dict], str]:
        """Everything by an artist, shuffled."""
        artist = best(self._search(query, artists=5).get("artist") or [], "name", query, None)
        if not artist:
            return [], ""
        albums = self._call("getArtist", id=artist["id"]).get("artist", {}).get("album") or []
        songs = [song for album in albums[:MOST_ALBUMS] for song in self._album_songs(album["id"])]
        random.shuffle(songs)
        return songs, artist.get("name", query)

    def _find_album(self, query: str) -> Tuple[List[Dict], str]:
        """An album, in order."""
        title, artist = split_by(query)
        albums = self._search(f"{title} {artist}" if artist else title, albums=10).get("album") or []
        album = best(albums, "name", title, artist)
        if not album:
            return [], ""
        return self._album_songs(album["id"]), f"{album.get('name')} by {album.get('artist')}"

    def _find_playlist(self, name: str, exact: bool = False, playlist_id: Optional[str] = None
                       ) -> Tuple[List[Dict], str]:
        """
        One of the user's playlists, or their starred songs, shuffled.

        Args:
            name: The playlist's name.
            exact: Whether only a playlist of just this name will do, however it's cased or
                punctuated, and never the starred songs - for a name from the settings, which
                is the playlist itself, not a spoken guess at one. Nova's own of the name comes
                before another user's.
            playlist_id: The id of the playlist Nova made, which is then the only one played,
                whatever it's called now (see new_music.py); none if it's gone.
        """
        if playlist_id is None and not exact and means_liked_songs(name):
            return self._liked_songs()
        playlists = self._call("getPlaylists").get("playlists", {}).get("playlist") or []
        if playlist_id is not None:
            playlist = next((found for found in playlists if found.get("id") == playlist_id), None)
        elif exact:
            wanted = normalise(name)
            named = [found for found in playlists if wanted and normalise(found.get("name", "")) == wanted]
            playlist = next((found for found in named if str(found.get("owner") or "").casefold()
                             == self.username.casefold()), named[0] if named else None)
        else:
            wanted = distinctive(name)
            playlist = wanted and best([{**playlist, "distinctive": distinctive(playlist.get("name", ""))}
                                        for playlist in playlists], "distinctive", wanted, None)
        if not playlist:
            return [], ""
        songs = self._call("getPlaylist", id=playlist["id"]).get("playlist", {}).get("entry") or []
        random.shuffle(songs)
        return songs, playlist.get("name", name)

    def _find_genre(self, genre: str) -> Tuple[List[Dict], str]:
        """A genre or mood: a playlist of that name if there is one, or songs tagged with it."""
        songs, said = self._find_playlist(genre)
        if songs:
            return songs, said
        songs = self._call("getSongsByGenre", genre=genre, count=MOST_SONGS).get("songsByGenre", {}).get("song") or []
        random.shuffle(songs)
        return songs, f"some {genre}"

    def _liked_songs(self) -> Tuple[List[Dict], str]:
        songs = self._call("getStarred2").get("starred2", {}).get("song") or []
        random.shuffle(songs)
        return songs, "your liked songs"

    # What the assistant asks for

    def play(self, query: Optional[str] = None, search_type: Optional[str] = None) -> Dict:
        """
        Plays something, or carries on with what was playing.

        Args:
            query: What to play, e.g. "Gooba by 6ix9ine", or None to carry on - or,
                with nothing to carry on with, to play the liked songs.
            search_type: "track", "artist", "album" or "playlist" (a genre or mood).

        Returns:
            Dict with 'success' and a 'message' - silent when carrying on, since the
            music coming back says so.
        """
        try:
            if not query:
                with self._lock:
                    if self._index < len(self._queue):
                        self._carry_on()
                        return {"success": True, "message": "Resumed playback", "silent": True}
                songs, said = self._liked_songs()
                if not songs:
                    songs = self._call("getRandomSongs", size=MOST_SONGS).get("randomSongs", {}).get("song") or []
                    said = "something from your library"
            elif search_type == "artist":
                songs, said = self._find_artist(query)
            elif search_type == "album":
                songs, said = self._find_album(query)
            elif search_type == "playlist":
                songs, said = self._find_genre(query)
            else:
                songs, said = self._find_song(query)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            return {"success": False, "message": self._failure_message(e)}

        if not songs:
            result = {"success": False, "message": f"I couldn't find {query or 'anything to play'} on Navidrome."}
            if query:
                # Navidrome hasn't got it, which isn't the same as not being able to ask,
                # so whoever asked can offer to download it.
                result["missing"] = True
            return result
        self._start(songs[:MOST_SONGS])
        logger.info(f"Playing {said} ({len(songs)} song{'s' if len(songs) != 1 else ''} queued)")
        return {"success": True, "message": f"Playing {said}"}

    def play_playlist(self, name: str, exact: bool = False, playlist_id: Optional[str] = None) -> Dict:
        """
        Plays one of the user's playlists, shuffled.

        Args:
            name: The playlist name as the user said it.
            exact: Whether only a playlist of just this name is played - see _find_playlist -
                rather than the closest to what was said.
            playlist_id: Play only the playlist of this id - Nova's own New Music, as it recorded
                the id when it made it - and say it's missing if it's gone, not look by name.
        """
        try:
            songs, said = self._find_playlist(name, exact, playlist_id)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            return {"success": False, "message": self._failure_message(e)}
        if not songs:
            # Navidrome hasn't got it, which isn't the same as not being able to ask.
            return {"success": False, "message": f"I couldn't find a playlist called {name}.", "missing": True}
        self._start(songs[:MOST_SONGS])
        return {"success": True, "message": f"Playing {said}"}

    def pause(self) -> Dict:
        with self._lock:
            self._set_paused(True)
        return {"success": True, "message": "Paused", "chime": True}

    def skip(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "There's nothing playing to skip."}
            following = self._index + 1
            if following >= len(self._queue) and self._repeat == "all":
                following = 0
            self._jump(following)
        return {"success": True, "message": "Skipped", "silent": True}

    def previous(self) -> Dict:
        with self._lock:
            if not self._queue:
                return {"success": False, "message": "There's nothing to go back to."}
            # Once the queue has run out, the last song is the one to go back to.
            index = min(self._index, len(self._queue) - 1)
            played = self._playback.position / self.speaker.sample_rate if self._playback else 0
            if played > RESTART_AFTER or index == 0:
                self._jump(index)
                return {"success": True, "message": "Starting it again", "silent": True}
            self._jump(index - 1)
        return {"success": True, "message": "Previous track", "silent": True}

    def restart(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "There's nothing playing to start again."}
            self._jump(self._index)
        return {"success": True, "message": "Starting it again", "silent": True}

    def repeat(self, mode: str) -> Dict:
        mode = "all" if mode in ("all", "context", "playlist", "album") else mode
        messages = {"off": "Repeat off", "track": "Repeating this track", "all": "Repeating everything"}
        if mode not in messages:
            return {"success": False, "message": f"I don't know how to repeat '{mode}'."}
        with self._lock:
            self._repeat = mode
        return {"success": True, "message": messages[mode], "chime": True}

    def shuffle(self, enabled: bool) -> Dict:
        """Shuffles what's still to come, or puts it back in its own order."""
        with self._lock:
            self._shuffle = enabled
            if self._index < len(self._queue):
                current = self._queue[self._index]
                if enabled:
                    rest = self._queue[self._index + 1:]
                    random.shuffle(rest)
                    self._queue = self._queue[:self._index + 1] + rest
                else:
                    self._queue = list(self._unshuffled)
                    self._index = next((index for index, song in enumerate(self._queue) if song is current), 0)
        return {"success": True, "message": "Shuffle on" if enabled else "Shuffle off", "chime": True}

    def current_track(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "Nothing playing"}
            song = self._queue[self._index]
        return {"success": True, "track": song.get("title", "something"), "artist": song.get("artist", "someone")}

    # For the settings page

    def now_playing(self) -> Optional[Dict]:
        """
        The song playing, for the settings page - None with nothing queued.

        Returns:
            Dict of its "track", "artist" and "album"; whether it's "playing" - which
            it is while paused for the assistant to talk, as it carries on after;
            its "position" and "duration" in seconds; and its "cover" id, if any.
        """
        with self._lock:
            if self._index >= len(self._queue):
                return None
            song = self._queue[self._index]
            playing = not self._paused or (self._talking and self._resume_after)
            played = self._playback.position / self.speaker.sample_rate if self._playback is not None else 0.0
        return {"track": song.get("title") or "Something", "artist": song.get("artist") or "",
                "album": song.get("album") or "", "playing": playing, "position": round(played, 1),
                "duration": song.get("duration"), "cover": song.get("coverArt")}

    def cover(self) -> Optional[Tuple[bytes, str]]:
        """
        The cover of the song playing, as an image and its type - or None, without
        one, or if Navidrome couldn't be asked.
        """
        with self._lock:
            song = self._queue[self._index] if self._index < len(self._queue) else {}
        art = song.get("coverArt")
        if not art:
            return None
        if self._cover is not None and self._cover[0] == art:
            return self._cover[1]
        try:
            if not network.online():
                raise requests.ConnectionError("No network connection")
            response = self._session.get(f"{self.url}/rest/getCoverArt",
                                         params={**self._sign_in(), "id": art, "size": COVER_SIZE},
                                         timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            self._failure_message(e)
            return None
        kind = response.headers.get("Content-Type", "")
        if not kind.startswith("image/"):
            return None
        self._cover = (art, (response.content, kind))
        return self._cover[1]

    def playlists(self) -> List[str]:
        """The names of the user's playlists, for the settings page to offer - none if Navidrome couldn't be asked."""
        try:
            found = self._call("getPlaylists").get("playlists", {}).get("playlist") or []
        except (requests.RequestException, NavidromeError) as e:
            self._failure_message(e)
            return []
        return sorted({playlist["name"] for playlist in found if playlist.get("name")}, key=str.lower)

    def get_volume(self) -> int:
        return self.volume.percent("music")

    def set_volume(self, percent) -> Dict:
        result = self.volume.set("music", percent)
        with self._lock:
            if self._playback is not None:
                self._playback.volume = self.volume.get("music")
        return result

    # Making way for the assistant - see MusicHold

    def pause_for_speech(self):
        """
        Pauses the music while the assistant talks. Anything asked for meanwhile -
        a new song, the next one - waits too, so it starts once the reply is over
        rather than under it.
        """
        with self._lock:
            self._interrupted = self._index < len(self._queue) and not self._paused
            # Only music which was actually playing comes back afterwards. Music the
            # user had already stopped used to start again after the next thing they
            # said, because every conversation ended by un-pausing it.
            self._resume_after = self._interrupted
            self._talking = True
            self._set_paused(True)

    def resume_after_speech(self):
        """Lets the music carry on, if it was playing or asked for while the assistant talked."""
        with self._lock:
            self._talking = False
            if self._resume_after:
                self._resume_after = False
                self._set_paused(False)

    def cancel_resume(self):
        """Keeps the music paused once the assistant has finished - it was asked to pause."""
        with self._lock:
            self._resume_after = False

    def _carry_on(self):
        """
        Plays on - straight away, or once the assistant has finished talking. Called
        with the lock held.
        """
        if self._talking:
            self._resume_after = True
        else:
            self._set_paused(False)

    def was_playing(self) -> bool:
        """Whether music was playing when the user started speaking."""
        return self._interrupted

    # Moving the music to another player, and taking it from one

    def has_music(self) -> bool:
        """Whether there's a song to play, now or when it's carried on - playing or paused."""
        with self._lock:
            return self._index < len(self._queue)

    def is_playing(self) -> bool:
        """
        Whether music is playing - which it is while held for the assistant to talk, if
        it was playing before, as it carries on after.
        """
        with self._lock:
            if not self.has_music():
                return False
            return self._interrupted if self._talking else not self._paused

    def hand_over(self) -> Optional[Dict]:
        """
        Gives up the music, to be played by another player from the same place, and stops.

        Returns:
            Dict of the "queue" and how it was "unshuffled", the "index" of the song
            playing and its "position" in seconds, and the "repeat" and "shuffle" modes - the
            lists copies, so the two players never share one. None, with no music.
        """
        with self._lock:
            if not self.has_music():
                return None
            # Where the song began as well as how far it's got, since a song which was
            # itself handed over part way began there: moved at 60 seconds and again 10
            # seconds later, it's at 70, not 10.
            playback = self._playback
            rate = getattr(playback, "sample_rate", 0)
            played = getattr(playback, "position", 0) / rate if rate else 0.0
            state = {"queue": list(self._queue), "unshuffled": list(self._unshuffled), "index": self._index,
                     "position": self._offset + played, "repeat": self._repeat, "shuffle": self._shuffle}
            self._queue, self._unshuffled = [], []
            # Nothing's left here to carry on after the reply, or to have been playing.
            self._resume_after = self._interrupted = False
            self._jump(0)
        return state

    def take_over(self, state: Dict) -> Dict:
        """
        Plays what another player handed over, from the same place - as _start does, so
        once the assistant has finished talking, if it's talking now.

        Args:
            state: What the other player's hand_over gave.

        Returns:
            Dict with 'success' and a 'message'.
        """
        with self._lock:
            self._repeat, self._shuffle = state["repeat"], state["shuffle"]
            self._start(state["queue"], state["index"], state["position"], state["unshuffled"])
        return {"success": True, "message": "Playing"}

    def close(self):
        """Stops the music, for good - the player's own thread ends."""
        with self._lock:
            self._closed = True
            self._queue, self._index = [], 0
            self._jump(0)

    # Playing the queue - called with the lock held

    def _set_paused(self, paused: bool):
        self._paused = paused
        if self._playback is not None:
            self._playback.paused = paused

    def _start(self, songs: List[Dict], index: int = 0, start: float = 0.0, unshuffled: Optional[List[Dict]] = None):
        """
        Plays these instead of whatever was queued - paused while the assistant is talking.

        Args:
            songs: The queue, in the order it's to be played.
            index: The song in it to start with.
            start: How many seconds into that song to start.
            unshuffled: The queue in its own order, if that isn't how it's to be played.
        """
        with self._lock:
            self._unshuffled = list(songs if unshuffled is None else unshuffled)
            self._queue = list(songs)
            self._paused = self._talking
            if self._talking:
                self._resume_after = True
            self._jump(index, start)

    def _jump(self, index: int, start: float = 0.0):
        """
        Plays the song at this place in the queue, stopping the one playing.

        Args:
            index: The song to play.
            start: How many seconds into it to start - only for the song jumped to, so any
                other jump, skipping or a new request, starts a song at its beginning.
        """
        self._index = index
        self._offset = max(0.0, start)
        self._turn += 1
        if self._playback is not None:
            self._playback.stop()
            # None of the new song has played, whatever the stopped one had - see hand_over.
            self._playback = None
        self._lock.notify_all()

    def _play_queue(self):
        """Plays the queue, a song at a time, until the player's closed."""
        failures = 0
        while True:
            with self._lock:
                while self._index >= len(self._queue) and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                song, turn = self._queue[self._index], self._turn
                playback = self.speaker.play_stream(self._decode(song, self._offset), volume=self.volume.get("music"),
                                                    kind="music", paused=self._paused, most_ahead=DECODE_AHEAD)
                self._playback = playback

            # Not playback.wait(), which gives up on a sound which stops moving -
            # as a paused song rightly does, for as long as it likes.
            while not playback.wait(timeout=1.0):
                pass

            with self._lock:
                if self._turn != turn:
                    continue
                # It finished by itself: on to the next, unless none of it played. Even
                # the same song, repeated, is played from its beginning, with none of it
                # played yet - see hand_over.
                self._offset = 0.0
                self._playback = None
                failures = failures + 1 if playback.duration == 0 else 0
                if failures >= MOST_FAILURES:
                    logger.error(f"{failures} songs in a row wouldn't play, so the queue is stopped")
                    self._queue, failures = [], 0
                elif self._repeat != "track":
                    following = self._index + 1
                    self._index = 0 if following >= len(self._queue) and self._repeat == "all" else following

    def _open_song(self, song_id: str) -> requests.Response:
        """Starts downloading a song from Navidrome, as an MP3 whatever it's stored as."""
        if not network.online():
            raise requests.ConnectionError("No network connection")
        response = self._session.get(f"{self.url}/rest/stream",
                                     params={**self._sign_in(), "id": song_id, "format": "mp3", "maxBitRate": 320},
                                     timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").startswith("audio"):
            response.close()
            raise NavidromeError("Navidrome sent something other than audio")
        return response

    def _decode(self, song: Dict, start: float = 0.0) -> Iterator[np.ndarray]:
        """
        A song as stereo audio at the speaker's rate, a piece at a time.

        The download and the decoding are kept apart: the song downloads as fast as
        Navidrome sends it, into memory - a few megabytes - while ffmpeg only
        decodes as fast as it's played. Feeding the download straight into ffmpeg
        would hold the connection still for as long as the music was paused, and
        Navidrome would give up on it.

        Args:
            song: The song to play.
            start: How many seconds into it to start, for music handed over part way. It's
                asked of ffmpeg after the input, which skips what's before as it decodes it,
                since a download isn't something it could jump about in.
        """
        response = ffmpeg = None
        try:
            response = self._open_song(song["id"])
            seek = ["-ss", f"{start:.3f}"] if start > 0 else []
            ffmpeg = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", *seek, "-f", "f32le", "-ac", "2",
                 "-ar", str(self.speaker.sample_rate), "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            downloaded: "queue.Queue[Optional[bytes]]" = queue.Queue()
            threading.Thread(target=self._download, args=(response, downloaded), daemon=True,
                             name="navidrome-download").start()
            threading.Thread(target=self._pass_on, args=(downloaded, ffmpeg.stdin), daemon=True,
                             name="navidrome-decode").start()

            frame = 8  # two channels of four-byte samples
            leftover = b""
            decoded = False
            while True:
                data = os.read(ffmpeg.stdout.fileno(), 65536)
                if not data:
                    break
                data = leftover + data
                whole = len(data) - len(data) % frame
                leftover = data[whole:]
                if whole:
                    decoded = True
                    yield np.frombuffer(data[:whole], dtype="<f4").reshape(-1, 2)
            if not decoded:
                ffmpeg.wait()
                logger.error(f"Couldn't play '{song.get('title')}': "
                             f"{ffmpeg.stderr.read(2000).decode(errors='replace').strip()}")
        except (requests.RequestException, NavidromeError, OSError) as e:
            self._failure_message(e)
        finally:
            if ffmpeg is not None:
                if ffmpeg.poll() is None:
                    ffmpeg.kill()
                ffmpeg.wait()
                ffmpeg.stdout.close()
                ffmpeg.stderr.close()
            if response is not None:
                response.close()

    @staticmethod
    def _download(response: requests.Response, into: "queue.Queue[Optional[bytes]]"):
        """Downloads a song into memory as fast as Navidrome sends it."""
        try:
            for chunk in response.iter_content(65536):
                into.put(chunk)
        except (requests.RequestException, OSError, ValueError) as e:
            logger.warning(f"A song stopped downloading part way: {type(e).__name__}")
        finally:
            into.put(None)

    @staticmethod
    def _pass_on(downloaded: "queue.Queue[Optional[bytes]]", into):
        """Hands the download to ffmpeg as fast as it decodes it."""
        try:
            while (chunk := downloaded.get()) is not None:
                into.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            # ffmpeg was stopped - the song was skipped.
            pass
        finally:
            try:
                into.close()
            except OSError:
                pass
