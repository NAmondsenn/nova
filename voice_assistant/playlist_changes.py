"""
Playlist Changes Module.

The rules for changing his playlists in Navidrome by voice: putting the song that's playing on
the playlists he names, taking it off, making a new playlist, and liking or unliking it. Each
returns what to say - {"success", "message"} - with "undo", which takes the change back, once
something has changed, and "ask_first", the Action to run on a yes, when it offers to make a
playlist he named which he hasn't got. A reply which changed nothing is never a success, so it
leaves the last change undoable (see undo.LastChange).

A song or a playlist is only ever changed in Navidrome, but SoulSync also writes Navidrome's
playlists: it remakes some on a schedule, throwing away what was added by hand, and a mirror of a
streaming service's playlist is synced to hold what SoulSync's copy does - putting back what
Nova took off, and, unless it's append only, taking off what Nova added (docs/decisions/0030). So
before a playlist is changed SoulSync is asked whether it remakes or mirrors it: a remade one is
left alone, a mirror is switched to append only for an add and refused for a removal, and with
SoulSync set up but out of reach nothing is changed, as Nova can't tell whether it would stay.
New Music is Nova's own, remade each morning, and is told by the id Nova recorded for it. These
checks are asked for only when a playlist needs them, and once for each request, however many
names it has.

Nothing is asked of Navidrome or SoulSync until one of the changes is called.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from voice_assistant.actions import Action
from voice_assistant.lists import joined
from voice_assistant.music import means_liked_songs
from voice_assistant.navidrome import NavidromeError, closest_playlist, normalise, same_song
from voice_assistant.soulsync import SoulSync, SoulSyncError

# What Navidrome failing looks like: it couldn't be reached, it answered with an error, or its
# answer couldn't be read. Each is said with NavidromeLibrary._failure_message.
NAVIDROME_FAILURES = (requests.RequestException, NavidromeError, ValueError)


def _them(names: List[str]) -> str:
    """What stands for names said just before: "it" for one, "them" for more."""
    return "it" if len(names) == 1 else "them"


def _title(song: Dict) -> str:
    """The song as it's said, by the title Navidrome gives it."""
    return song.get("title") or "that song"


def _writes(mirror: Dict, playlist: Dict) -> bool:
    """
    Whether a SoulSync mirror writes this Navidrome playlist: it's the one the mirror names by its id or, for a
    mirror which hasn't a playlist yet, the one which has the mirror's name once both are normalised. A mirror
    which has a playlist is never matched by name, and one with no name never matches anything.

    Args:
        mirror: A mirror as SoulSync.mirrors gives it.
        playlist: A playlist as Navidrome gives it, with its id and name.
    """
    if mirror.get("server_playlist_id"):
        return mirror["server_playlist_id"] == playlist["id"]
    wanted = normalise(mirror.get("sync_name") or "")
    return bool(wanted) and wanted == normalise(playlist.get("name") or "")


class _Request:
    """
    What one request has asked so far of Navidrome and SoulSync, so each is asked once and only when a playlist
    needs it - not at all if every name he gave was his liked songs or one he hasn't got.
    """

    def __init__(self, library, soulsync: Callable[[], Optional[SoulSync]], new_music_id: Callable[[], Optional[str]]):
        """
        Args:
            library: The NavidromeLibrary to change.
            soulsync: Gives SoulSync, or None if it isn't set up.
            new_music_id: Gives the id of Nova's own New Music playlist, or None if it hasn't made one.
        """
        self.library = library
        self._soulsync = soulsync
        self._new_music_id = new_music_id
        # Each thing asked, to what came of asking: its answer, or the error SoulSync gave, which is said again
        # to whichever name asks next rather than asked for again.
        self._known: Dict[str, Tuple[Any, Optional[SoulSyncError]]] = {}

    def _once(self, key: str, ask: Callable[[], Any]) -> Any:
        if key not in self._known:
            try:
                self._known[key] = (ask(), None)
            except SoulSyncError as e:
                self._known[key] = (None, e)
        answer, error = self._known[key]
        if error:
            raise error
        return answer

    def playlists(self) -> List[Dict]:
        """His playlists in Navidrome. Raises what NavidromeLibrary.all_playlists does."""
        return self._once("playlists", self.library.all_playlists)

    def new_music_id(self) -> Optional[str]:
        """The id of Nova's New Music playlist, if it's made one."""
        return self._once("new_music", self._new_music_id)

    def client(self) -> Optional[SoulSync]:
        """SoulSync, or None if it isn't set up."""
        return self._once("soulsync", self._soulsync)

    def remade(self) -> List[str]:
        """The names of the playlists SoulSync remakes - none, if it isn't set up. Raises SoulSyncError."""
        client = self.client()
        return [] if client is None else self._once("remade", client.remade_playlists)

    def mirrors(self) -> List[Dict]:
        """The mirrors SoulSync keeps - none, if it isn't set up. Raises SoulSyncError."""
        client = self.client()
        return [] if client is None else self._once("mirrors", client.mirrors)


@dataclass
class _Outcome:
    """What a request did to each playlist he named, to be said in one reply and taken back in one undo."""

    song: Dict
    adding: bool
    # Each playlist changed, as (its id, its name, the id of the song's entry: the one added, or the first of
    # those taken off - which is the one an undo puts back).
    changed: List[Tuple[str, str, str]] = field(default_factory=list)
    # The names of the playlists above which were mirrors, and were switched to append only to do it.
    switched: List[str] = field(default_factory=list)
    # The playlists the song was already on, or for a removal wasn't on.
    already: List[str] = field(default_factory=list)
    # The reasons a playlist was left alone, each a sentence.
    refusals: List[str] = field(default_factory=list)
    # The playlists SoulSync couldn't be asked about, and those it wouldn't switch to append only.
    unreachable: List[str] = field(default_factory=list)
    unswitched: List[str] = field(default_factory=list)
    # The names he gave which are none of his playlists, as he said them.
    missing: List[str] = field(default_factory=list)
    # His liked songs, if he named them: what's said of it, and how to take it back if it was changed.
    liked: Optional[str] = None
    liked_undo: Optional[Callable[[], Dict]] = None
    # What to say of Navidrome failing, which ended the request there.
    failure: Optional[str] = None


class PlaylistChanges:
    """Changes his playlists and likes by the rules above, one request at a time."""

    def __init__(self, soulsync: Callable[[], Optional[SoulSync]], new_music_id: Callable[[], Optional[str]]):
        """
        Args:
            soulsync: Gives SoulSync, or None if it isn't set up. Called once for each request which needs it, so
                a key or a setting changed on the page is the one used.
            new_music_id: Gives the id of Nova's own New Music playlist, or None if it hasn't made one yet.
        """
        self._soulsync = soulsync
        self._new_music_id = new_music_id

    def add(self, library, song: Dict, names: List[str]) -> Dict:
        """
        Puts a song on each playlist named - "add this to Gym and Car" - or likes it, for his liked songs.

        Args:
            library: The NavidromeLibrary to change.
            song: The song, as Navidrome describes it, with its id, title and artist.
            names: The playlists as he said them.

        Returns:
            {"success", "message"}, with "undo" if anything changed, and "ask_first" - making the playlist with
            this song - if exactly one name was none of his playlists.
        """
        return self._change(library, song, names, adding=True)

    def remove(self, library, song: Dict, names: List[str]) -> Dict:
        """
        Takes every copy of a song off each playlist named - "take this off Gym" - or unlikes it, for his liked songs.

        Args:
            library: The NavidromeLibrary to change.
            song: The song, as Navidrome describes it, with its id, title and artist.
            names: The playlists as he said them.

        Returns:
            {"success", "message"}, with "undo" if anything changed.
        """
        return self._change(library, song, names, adding=False)

    def make(self, library, name: str, song: Optional[Dict]) -> Dict:
        """
        Makes a new playlist, with the song on it if one is given. One he has already, by the same name once
        normalised, isn't made twice - but the song is added to it.

        Args:
            library: The NavidromeLibrary to change.
            name: The playlist as he said it.
            song: The song to put on it, or None to make it empty.

        Returns:
            {"success", "message"}, with "undo" - which deletes a playlist made, or takes the song back off one he
            had - if anything changed.
        """
        name = (name or "").strip()
        if not normalise(name):
            return {"success": False, "message": "I didn't catch the playlist's name."}
        if means_liked_songs(name):
            return {"success": False,
                    "message": "Your liked songs aren't a playlist. Say 'like this' to add a song to them."}
        request = _Request(library, self._soulsync, self._new_music_id)
        try:
            had = next((playlist for playlist in request.playlists()
                        if normalise(playlist.get("name") or "") == normalise(name)), None)
            if had is None:
                return self._make_new(library, name, song)
            if song is None:
                return {"success": False, "message": f"You've already got {had['name']}."}
            # Adding to a playlist he has is adding like any other, so a remade or mirrored one is as it is there.
            outcome = _Outcome(song, adding=True)
            try:
                self._change_playlist(request, outcome, had)
            except NAVIDROME_FAILURES as e:
                outcome.failure = library._failure_message(e)
            reply = self._reply(library, outcome)
            reply["message"] = f"You've already got {had['name']}. {reply['message']}"
            return reply
        except NAVIDROME_FAILURES as e:
            return {"success": False, "message": library._failure_message(e)}

    def like(self, library, song: Dict, liked: bool) -> Dict:
        """
        Likes a song, or unlikes it.

        Args:
            library: The NavidromeLibrary to change.
            song: The song, as Navidrome describes it, with its id and title.
            liked: True to like it, False to unlike it.

        Returns:
            {"success", "message"}, with "undo" if it changed.
        """
        try:
            message, undo = self._like(library, song, liked)
        except NAVIDROME_FAILURES as e:
            return {"success": False, "message": library._failure_message(e)}
        return {"success": True, "message": message, "undo": undo} if undo else {"success": False, "message": message}

    def _like(self, library, song: Dict, liked: bool) -> Tuple[str, Optional[Callable[[], Dict]]]:
        """
        Likes or unlikes a song - by what Navidrome says of it now, not what it was queued with.

        Returns:
            What to say, and how to take it back, or None if it was already as asked.

        Raises:
            requests.RequestException, NavidromeError, ValueError: As NavidromeLibrary does.
        """
        title = _title(song)
        if library.is_starred(song["id"]) == liked:
            return (f"You've already liked {title}." if liked else f"You haven't liked {title}."), None
        library.set_starred(song["id"], liked)

        def undo() -> Dict:
            try:
                library.set_starred(song["id"], not liked)
            except NAVIDROME_FAILURES as e:
                return {"success": False, "message": library._failure_message(e)}
            return {"success": True, "message": f"Unliked {title}." if liked else f"Liked {title} again."}

        return (f"Liked {title}." if liked else f"Unliked {title}."), undo

    def _make_new(self, library, name: str, song: Optional[Dict]) -> Dict:
        """
        Makes a playlist which isn't there yet. Making one is never a question for SoulSync, which only mirrors
        and remakes the playlists it has.

        Raises:
            requests.RequestException, NavidromeError, ValueError: If the playlist couldn't be made.
        """
        playlist_id = library.make_playlist(name)

        def undo() -> Dict:
            try:
                library.delete_playlist(playlist_id)
            except NAVIDROME_FAILURES as e:
                return {"success": False, "message": library._failure_message(e)}
            return {"success": True, "message": f"Deleted {name}."}

        if song is None:
            return {"success": True, "message": f"Made {name}.", "undo": undo}
        try:
            library.add_songs(playlist_id, [song["id"]])
        except NAVIDROME_FAILURES as e:
            # It's made, so it can still be undone.
            return {"success": True, "undo": undo, "message": f"Made {name}, but I couldn't put {_title(song)} on it. "
                                                              f"{library._failure_message(e)}"}
        return {"success": True, "message": f"Made {name} with {_title(song)} on it.", "undo": undo}

    def _change(self, library, song: Dict, names: List[str], adding: bool) -> Dict:
        """Adds a song to each playlist named, or takes it off - see add and remove."""
        names = [name.strip() for name in names or [] if normalise(name or "")]
        if not names:
            return {"success": False, "message": "I didn't catch which playlist."}
        request = _Request(library, self._soulsync, self._new_music_id)
        outcome = _Outcome(song, adding)
        # Two names may be the one playlist: "gym" and "gym bangers".
        done = set()
        try:
            for name in names:
                if means_liked_songs(name):
                    if outcome.liked is None:
                        outcome.liked, outcome.liked_undo = self._like(library, song, adding)
                    continue
                playlist = closest_playlist(request.playlists(), name)
                if playlist is None:
                    outcome.missing.append(name)
                elif playlist["id"] not in done:
                    done.add(playlist["id"])
                    self._change_playlist(request, outcome, playlist)
        except NAVIDROME_FAILURES as e:
            # Whatever was done before it stays done, and is still said and can still be undone.
            outcome.failure = library._failure_message(e)
        return self._reply(library, outcome)

    def _screen(self, request: _Request, outcome: _Outcome, playlist: Dict) -> Tuple[Optional[str], List[Dict]]:
        """
        Whether a playlist can be changed so that the change stays, in the order of the rules: one which is
        remade is refused, whatever else is near its name; a mirror is refused a removal and kept for adding.

        Returns:
            The sentence which says why it's refused - or None, and the mirrors which write it.

        Raises:
            SoulSyncError: If SoulSync is set up, and couldn't say.
        """
        name = playlist["name"]
        recorded = request.new_music_id()
        if recorded and playlist["id"] == recorded:
            return f"I remake {name} every morning, so that wouldn't stay.", []
        remade = {normalise(remade_name) for remade_name in request.remade()}
        if normalise(name) and normalise(name) in remade:
            return f"SoulSync remakes {name}, so that wouldn't stay.", []
        mirrors = [mirror for mirror in request.mirrors() if _writes(mirror, playlist)]
        if mirrors and not outcome.adding:
            return (f"SoulSync copies {name} from Spotify, so it would put {_title(outcome.song)} back. "
                    "Stop mirroring it in SoulSync and I can."), []
        return None, mirrors

    def _change_playlist(self, request: _Request, outcome: _Outcome, playlist: Dict):
        """
        Adds the song to one playlist, or takes it off, and notes what came of it in the outcome.

        Raises:
            requests.RequestException, NavidromeError, ValueError: If Navidrome failed.
        """
        name, song = playlist["name"], outcome.song
        try:
            refusal, mirrors = self._screen(request, outcome, playlist)
        except SoulSyncError:
            outcome.unreachable.append(name)
            return
        if refusal:
            outcome.refusals.append(refusal)
            return
        entries = request.library.playlist_entries(playlist["id"])
        if outcome.adding:
            if any(same_song(entry, song) for entry in entries):
                outcome.already.append(name)
                return
            # Switched only now that there's something to add, so a song already there changes nothing.
            to_switch = [mirror for mirror in mirrors if mirror["sync_mode"] != "append"]
            try:
                for mirror in to_switch:
                    request.client().set_append(mirror["id"])
            except SoulSyncError:
                outcome.unswitched.append(name)
                return
            request.library.add_songs(playlist["id"], [song["id"]])
            outcome.changed.append((playlist["id"], name, song["id"]))
            if to_switch:
                outcome.switched.append(name)
            return
        gone = [at for at, entry in enumerate(entries) if same_song(entry, song)]
        if not gone:
            outcome.already.append(name)
            return
        request.library.remove_entries(playlist["id"], gone)
        outcome.changed.append((playlist["id"], name, entries[gone[0]].get("id") or song["id"]))

    def _reply(self, library, outcome: _Outcome) -> Dict:
        """What a request is told it did: one sentence for each kind of thing which came of it, and how to undo it."""
        title, adding = _title(outcome.song), outcome.adding
        sentences = []
        if outcome.changed:
            names = [name for _, name, _ in outcome.changed]
            sentence = f"Added {title} to {joined(names)}" if adding else f"Took {title} off {joined(names)}"
            if outcome.switched:
                sentence += (f", and set {joined(outcome.switched)} to append only in SoulSync so "
                             f"{'it stays' if len(outcome.switched) == 1 else 'they stay'}")
            sentences.append(sentence + ".")
        if outcome.liked:
            sentences.append(outcome.liked)
        if outcome.already:
            sentences.append(f"{title} is already on {joined(outcome.already)}." if adding
                             else f"{title} isn't on {joined(outcome.already)}.")
        sentences += outcome.refusals
        if outcome.unreachable:
            sentences.append(f"I can't reach SoulSync to check {joined(outcome.unreachable)}, so I haven't changed "
                             f"{_them(outcome.unreachable)}.")
        if outcome.unswitched:
            sentences.append(f"I couldn't set {joined(outcome.unswitched)} to append only in SoulSync, so I haven't "
                             f"added {title} to {_them(outcome.unswitched)}. Set {_them(outcome.unswitched)} to "
                             "append only there and ask me again.")
        ask_first = None
        # Only a single name he hasn't a playlist for is offered, and not after Navidrome failed, which would
        # leave the question in the middle of what's said.
        if len(outcome.missing) == 1 and adding and not outcome.failure:
            sentences.append(f"You haven't got a playlist called {outcome.missing[0]}. Want me to make it?")
            song = outcome.song
            ask_first = Action("make_offered_playlist", {
                "name": outcome.missing[0],
                "song": {"id": song.get("id"), "title": song.get("title"), "artist": song.get("artist")}})
        elif len(outcome.missing) == 1:
            sentences.append(f"You haven't got a playlist called {outcome.missing[0]}.")
        elif outcome.missing:
            sentences.append(f"You haven't got playlists called {joined(outcome.missing)}.")
        if outcome.failure:
            sentences.append(outcome.failure)

        undos = []
        if outcome.changed:
            undos.append(_take_back(library, outcome.song, outcome.changed) if adding
                         else _put_back(library, outcome.song, outcome.changed))
        if outcome.liked_undo:
            undos.append(outcome.liked_undo)
        # Something changed, or the offer is a question to say yes to - anything else changed nothing.
        reply: Dict[str, Any] = {"success": bool(undos or ask_first), "message": " ".join(sentences)}
        if undos:
            reply["undo"] = undos[0] if len(undos) == 1 else _both(undos)
        if ask_first:
            reply["ask_first"] = ask_first
        return reply


def _take_back(library, song: Dict, added: List[Tuple[str, str, str]]) -> Callable[[], Dict]:
    """
    How to take a song back off the playlists it was added to: on each, the last entry with the id which was
    added. A playlist it has gone from since is passed over, and not said.

    Returns:
        An undo, which answers {"success", "message"} and never raises.
    """
    title = _title(song)

    def undo() -> Dict:
        taken = []
        try:
            for playlist_id, name, song_id in added:
                at = [index for index, entry in enumerate(library.playlist_entries(playlist_id))
                      if entry.get("id") == song_id]
                if at:
                    library.remove_entries(playlist_id, [at[-1]])
                    taken.append(name)
        except NAVIDROME_FAILURES as e:
            # What was taken back before it stays done, so it's said first, as for the change itself - and the
            # slot is spent, since asking again would only find it gone from the first.
            failure = library._failure_message(e)
            if taken:
                return {"success": True, "message": f"Took {title} back off {joined(taken)}. {failure}"}
            return {"success": False, "message": failure}
        if not taken:
            names = joined([name for _, name, _ in added])
            return {"success": False, "message": f"{title} isn't on {names} any more."}
        return {"success": True, "message": f"Took {title} back off {joined(taken)}."}

    return undo


def _put_back(library, song: Dict, removed: List[Tuple[str, str, str]]) -> Callable[[], Dict]:
    """
    How to put a song back on the playlists it was taken off: on each, the first entry taken off, added at the
    end - Nova shuffles playlists anyway - and once, whatever number of copies there were.

    Returns:
        An undo, which answers {"success", "message"} and never raises.
    """
    title = _title(song)

    def undo() -> Dict:
        put = []
        try:
            for playlist_id, name, entry_id in removed:
                library.add_songs(playlist_id, [entry_id])
                put.append(name)
        except NAVIDROME_FAILURES as e:
            # What was put back before it stays done, so it's said first, as for the change itself.
            failure = library._failure_message(e)
            if put:
                return {"success": True, "message": f"Put {title} back on {joined(put)}. {failure}"}
            return {"success": False, "message": failure}
        return {"success": True, "message": f"Put {title} back on {joined(put)}."}

    return undo


def _both(undos: List[Callable[[], Dict]]) -> Callable[[], Dict]:
    """How to take back a change of playlists and a like together: each is taken back, and each said."""
    def undo() -> Dict:
        replies = [one() for one in undos]
        return {"success": all(reply["success"] for reply in replies),
                "message": " ".join(reply["message"] for reply in replies)}

    return undo
