"""
Playlist Changes Test
Checks the rules of adding the playing song to playlists, taking it off, making playlists and liking
songs, with a fake Navidrome keeping playlists, their songs and the starred ones, and a fake SoulSync
saying which playlists it remakes and mirrors. Every song, playlist and id is made up.
"""

import pytest
import requests

from voice_assistant.actions import Action
from voice_assistant.navidrome import NavidromeError, NavidromeLibrary
from voice_assistant.playlist_changes import PlaylistChanges
from voice_assistant.soulsync import SoulSyncError

SONG = {"id": "s1", "title": "Song 1", "artist": "Made Up Band"}
OTHER_SONG = {"id": "s2", "title": "Song 2", "artist": "Made Up Band"}
# The same song as the album has it, as the single does: another id, the same title and artist.
SINGLE = {"id": "s9", "title": "Song 1", "artist": "Made Up Band"}

DOWN = requests.ConnectionError("http://navidrome.local/rest/ping?t=secret")


class FakeLibrary:
    """Stands in for NavidromeLibrary, keeping playlists and starred songs, and noting each call."""

    _failure_message = staticmethod(NavidromeLibrary._failure_message)

    def __init__(self):
        # Each playlist's id to its name and its songs, in order.
        self.playlists = {}
        self.starred = set()
        # Each call as (method, arguments).
        self.calls = []
        # A method's name to (which call of it fails, from 1, and what it raises).
        self.failures = {}
        self._made = 0
        self._counts = {}

    def playlist(self, name, songs=()):
        playlist_id = f"pl{len(self.playlists) + 1}"
        self.playlists[playlist_id] = {"name": name, "songs": [dict(song) for song in songs]}
        return playlist_id

    def called(self, method):
        return [arguments for name, arguments in self.calls if name == method]

    def _ask(self, method, *arguments):
        self.calls.append((method, arguments))
        self._counts[method] = self._counts.get(method, 0) + 1
        if method in self.failures and self.failures[method][0] == self._counts[method]:
            raise self.failures[method][1]

    def all_playlists(self):
        self._ask("all_playlists")
        return [{"id": playlist_id, "name": playlist["name"]} for playlist_id, playlist in self.playlists.items()]

    def playlist_entries(self, playlist_id):
        self._ask("playlist_entries", playlist_id)
        return [dict(song) for song in self.playlists[playlist_id]["songs"]]

    def add_songs(self, playlist_id, song_ids):
        self._ask("add_songs", playlist_id, list(song_ids))
        songs = {song["id"]: song for song in (SONG, OTHER_SONG, SINGLE)}
        self.playlists[playlist_id]["songs"] += [dict(songs[song_id]) for song_id in song_ids]

    def remove_entries(self, playlist_id, indexes):
        self._ask("remove_entries", playlist_id, sorted(indexes))
        songs = self.playlists[playlist_id]["songs"]
        self.playlists[playlist_id]["songs"] = [song for at, song in enumerate(songs) if at not in indexes]

    def delete_playlist(self, playlist_id):
        self._ask("delete_playlist", playlist_id)
        del self.playlists[playlist_id]

    def make_playlist(self, name):
        self._ask("make_playlist", name)
        return self.playlist(name)

    def set_starred(self, song_id, starred):
        self._ask("set_starred", song_id, starred)
        (self.starred.add if starred else self.starred.discard)(song_id)

    def is_starred(self, song_id):
        self._ask("is_starred", song_id)
        return song_id in self.starred


class FakeSoulSync:
    """Stands in for SoulSync, with the playlists it remakes and the mirrors it keeps, and noting each call."""

    def __init__(self, remade=(), mirrors=(), down=False, refuses_append=False):
        self.remade = list(remade)
        self.kept = [dict(mirror) for mirror in mirrors]
        self.down = down
        self.refuses_append = refuses_append
        self.calls = []

    def remade_playlists(self):
        self.calls.append("remade_playlists")
        if self.down:
            raise SoulSyncError("SoulSync isn't answering.")
        return list(self.remade)

    def mirrors(self):
        self.calls.append("mirrors")
        if self.down:
            raise SoulSyncError("SoulSync isn't answering.")
        return [dict(mirror) for mirror in self.kept]

    def set_append(self, mirror_id):
        self.calls.append(("set_append", mirror_id))
        if self.refuses_append:
            raise SoulSyncError("SoulSync didn't let me.")
        for mirror in self.kept:
            if mirror["id"] == mirror_id:
                mirror["sync_mode"] = "append"


def _mirror(mirror_id, playlist_id, name="", mode=None):
    return {"id": mirror_id, "server_playlist_id": playlist_id, "sync_name": name, "sync_mode": mode}


class Built:
    """A PlaylistChanges with the fake SoulSync it's given, counting how often it asks for it."""

    def __init__(self, soulsync=None, new_music=None):
        self.soulsync = soulsync
        self.asked = []
        self.changes = PlaylistChanges(lambda: self._ask_soulsync(), lambda: new_music)

    def _ask_soulsync(self):
        self.asked.append("soulsync")
        return self.soulsync


@pytest.fixture
def library():
    return FakeLibrary()


def test_the_song_goes_on_each_playlist_named_and_the_reply_names_them(library):
    gym = library.playlist("Gym Bangers")
    car = library.playlist("Car")
    library.playlist("Chill")
    reply = Built().changes.add(library, SONG, ["gym", "car"])
    assert reply["success"] is True
    assert reply["message"] == "Added Song 1 to Gym Bangers and Car."
    assert library.called("add_songs") == [(gym, ["s1"]), (car, ["s1"])]
    assert callable(reply["undo"])
    assert "ask_first" not in reply


def test_three_playlists_are_named_as_a_list_is_said(library):
    library.playlist("Gym Bangers")
    library.playlist("Car")
    library.playlist("Chill")
    reply = Built().changes.add(library, SONG, ["gym", "car", "chill"])
    assert reply["message"] == "Added Song 1 to Gym Bangers, Car and Chill."


def test_the_same_playlist_named_twice_is_changed_once(library):
    library.playlist("Gym Bangers")
    reply = Built().changes.add(library, SONG, ["gym", "gym bangers"])
    assert reply["message"] == "Added Song 1 to Gym Bangers."
    assert len(library.called("add_songs")) == 1


def test_a_song_already_on_a_playlist_isnt_added_again(library):
    # The same title and artist with another id, as the single is to the album's song.
    library.playlist("Gym Bangers", [SINGLE])
    reply = Built().changes.add(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "Song 1 is already on Gym Bangers."}
    assert library.called("add_songs") == []


def test_a_song_on_one_of_two_playlists_is_added_to_the_other(library):
    library.playlist("Gym Bangers", [SONG])
    car = library.playlist("Car")
    reply = Built().changes.add(library, SONG, ["gym", "car"])
    assert reply["success"] is True
    assert reply["message"] == "Added Song 1 to Car. Song 1 is already on Gym Bangers."
    assert library.called("add_songs") == [(car, ["s1"])]


def test_a_name_with_no_playlist_offers_to_make_it_with_the_song(library):
    library.playlist("Gym Bangers")
    reply = Built().changes.add(library, SONG, ["Jim"])
    assert reply["success"] is True
    assert reply["message"] == "You haven't got a playlist called Jim. Want me to make it?"
    assert reply["ask_first"] == Action("make_offered_playlist", {"name": "Jim", "song": SONG})
    assert "undo" not in reply
    assert library.called("add_songs") == []


def test_the_offer_carries_the_song_as_asked_about_whatever_else_the_dict_has(library):
    playing = {**SONG, "album": "Made Up Album", "duration": 200}
    reply = Built().changes.add(library, playing, ["Jim"])
    assert reply["ask_first"].params["song"] == {"id": "s1", "title": "Song 1", "artist": "Made Up Band"}


def test_one_name_done_and_one_missing_does_the_one_and_offers_the_other(library):
    gym = library.playlist("Gym Bangers")
    reply = Built().changes.add(library, SONG, ["gym", "Jim"])
    assert reply["success"] is True
    assert reply["message"] == ("Added Song 1 to Gym Bangers. You haven't got a playlist called Jim. "
                                "Want me to make it?")
    assert reply["ask_first"].params["name"] == "Jim"
    assert library.called("add_songs") == [(gym, ["s1"])]
    assert callable(reply["undo"])


def test_two_names_missing_offer_nothing(library):
    library.playlist("Gym Bangers")
    reply = Built().changes.add(library, SONG, ["Jim", "Kim"])
    assert reply == {"success": False, "message": "You haven't got playlists called Jim and Kim."}


def test_taking_off_a_playlist_which_isnt_there_says_so_and_offers_nothing(library):
    library.playlist("Gym Bangers")
    reply = Built().changes.remove(library, SONG, ["Jim"])
    assert reply == {"success": False, "message": "You haven't got a playlist called Jim."}


def test_the_closest_playlist_being_remade_is_refused_not_the_next(library):
    # A weaker match listed first, which a fall through would take.
    library.playlist("Daily Drive")
    library.playlist("Daily Mix 1")
    soulsync = FakeSoulSync(remade=["Daily Mix 1", "Your Listening Mix"])
    reply = Built(soulsync).changes.add(library, SONG, ["daily mix 1"])
    assert reply == {"success": False, "message": "SoulSync remakes Daily Mix 1, so that wouldn't stay."}
    assert library.called("add_songs") == []
    assert library.called("playlist_entries") == []


def test_a_remade_playlist_is_refused_for_taking_a_song_off_too(library):
    library.playlist("Daily Mix 1", [SONG])
    reply = Built(FakeSoulSync(remade=["Daily Mix 1"])).changes.remove(library, SONG, ["daily mix"])
    assert reply == {"success": False, "message": "SoulSync remakes Daily Mix 1, so that wouldn't stay."}
    assert library.called("remove_entries") == []


def test_new_music_is_refused_by_its_id(library):
    # Renamed on the settings page, so its name says nothing: only the id Nova recorded does.
    library.playlist("Gym Bangers")
    fresh = library.playlist("Fresh Finds")
    soulsync = FakeSoulSync()
    reply = Built(soulsync, new_music=fresh).changes.add(library, SONG, ["fresh finds"])
    assert reply == {"success": False, "message": "I remake Fresh Finds every morning, so that wouldn't stay."}
    assert library.called("add_songs") == []
    assert soulsync.calls == []


def test_a_playlist_which_isnt_new_music_is_changed_whatever_new_musics_id_is(library):
    gym = library.playlist("Gym Bangers")
    reply = Built(new_music="some-other-id").changes.add(library, SONG, ["gym"])
    assert reply["success"] is True
    assert library.called("add_songs") == [(gym, ["s1"])]


def test_a_mirror_is_switched_to_append_only_once_and_says_so(library):
    gym = library.playlist("Gym")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym", mode="reconcile")])
    built = Built(soulsync)
    reply = built.changes.add(library, SONG, ["gym"])
    assert reply["success"] is True
    assert reply["message"] == "Added Song 1 to Gym, and set Gym to append only in SoulSync so it stays."
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == [("set_append", 7)]
    # The second time it's already append only, so nothing is switched or said.
    reply = built.changes.add(library, OTHER_SONG, ["gym"])
    assert reply["message"] == "Added Song 2 to Gym."
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == [("set_append", 7)]


def test_a_mirror_never_set_up_is_switched_as_one_in_replace_mode_is(library):
    gym = library.playlist("Gym")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym", mode=None)])
    reply = Built(soulsync).changes.add(library, SONG, ["gym"])
    assert reply["message"] == "Added Song 1 to Gym, and set Gym to append only in SoulSync so it stays."


def test_a_mirror_already_append_only_is_left_as_it_is(library):
    gym = library.playlist("Gym")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym", mode="append")])
    reply = Built(soulsync).changes.add(library, SONG, ["gym"])
    assert reply["message"] == "Added Song 1 to Gym."
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == []


def test_a_mirror_with_no_playlist_yet_is_found_by_its_name_and_never_by_an_empty_one(library):
    gym = library.playlist("Gym Bangers")
    car = library.playlist("Car")
    # The first has no playlist, and its name is the playlist's once normalised; the second has none and no name.
    soulsync = FakeSoulSync(mirrors=[_mirror(7, None, "the gym bangers", mode="reconcile"),
                                     _mirror(8, None, "", mode="reconcile")])
    built = Built(soulsync)
    reply = built.changes.add(library, SONG, ["gym", "car"])
    assert reply["message"] == ("Added Song 1 to Gym Bangers and Car, and set Gym Bangers to append only in "
                                "SoulSync so it stays.")
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == [("set_append", 7)]
    assert library.called("add_songs") == [(gym, ["s1"]), (car, ["s1"])]


def test_a_mirror_which_has_a_playlist_is_never_matched_by_name(library):
    # Its playlist is another one, which happens to be called Car too.
    library.playlist("Car")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, "elsewhere", "Car", mode="reconcile")])
    reply = Built(soulsync).changes.remove(library, SONG, ["car"])
    assert reply["message"] == "Song 1 isn't on Car."


def test_two_mirrors_of_one_playlist_are_both_switched(library):
    gym = library.playlist("Gym")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym"), _mirror(8, gym, "Gym again")])
    reply = Built(soulsync).changes.add(library, SONG, ["gym"])
    assert reply["success"] is True
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == [("set_append", 7), ("set_append", 8)]


def test_several_mirrors_switched_are_said_together(library):
    gym = library.playlist("Gym")
    car = library.playlist("Car")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym"), _mirror(8, car, "Car")])
    reply = Built(soulsync).changes.add(library, SONG, ["gym", "car"])
    assert reply["message"] == ("Added Song 1 to Gym and Car, and set Gym and Car to append only in SoulSync "
                                "so they stay.")


def test_a_mirror_soulsync_wont_switch_isnt_added_to(library):
    gym = library.playlist("Gym")
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym")], refuses_append=True)
    reply = Built(soulsync).changes.add(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "I couldn't set Gym to append only in SoulSync, so I haven't added "
                                                  "Song 1 to it. Set it to append only there and ask me again."}
    assert library.called("add_songs") == []


def test_a_song_already_on_a_mirror_leaves_it_as_it_is(library):
    # Nothing was changed, so nothing is switched either: it's all or nothing for the reply's success.
    gym = library.playlist("Gym", [SONG])
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym")])
    reply = Built(soulsync).changes.add(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "Song 1 is already on Gym."}
    assert [call for call in soulsync.calls if isinstance(call, tuple)] == []


def test_taking_a_song_off_a_mirror_is_refused(library):
    gym = library.playlist("Gym", [SONG])
    soulsync = FakeSoulSync(mirrors=[_mirror(7, gym, "Gym")])
    reply = Built(soulsync).changes.remove(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "SoulSync copies Gym from Spotify, so it would put Song 1 back. "
                                                  "Stop mirroring it in SoulSync and I can."}
    assert library.called("remove_entries") == []


def test_soulsync_out_of_reach_changes_nothing_that_needed_it(library):
    library.playlist("Gym")
    library.playlist("Car")
    soulsync = FakeSoulSync(down=True)
    built = Built(soulsync)
    reply = built.changes.add(library, SONG, ["gym", "car"])
    assert reply == {"success": False, "message": "I can't reach SoulSync to check Gym and Car, so I haven't "
                                                  "changed them."}
    assert library.called("add_songs") == []
    # Asked once for the whole request, whatever the number of names.
    assert built.asked == ["soulsync"]
    assert soulsync.calls == ["remade_playlists"]


def test_soulsync_out_of_reach_still_lets_through_what_didnt_need_it(library):
    library.playlist("Gym")
    reply = Built(FakeSoulSync(down=True)).changes.add(library, SONG, ["gym", "my liked songs"])
    assert reply["success"] is True
    assert reply["message"] == "Liked Song 1. I can't reach SoulSync to check Gym, so I haven't changed it."
    assert library.starred == {"s1"}
    assert library.called("add_songs") == []


def test_soulsync_out_of_reach_refuses_taking_a_song_off_too(library):
    library.playlist("Gym", [SONG])
    reply = Built(FakeSoulSync(down=True)).changes.remove(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "I can't reach SoulSync to check Gym, so I haven't changed it."}
    assert library.called("remove_entries") == []


def test_soulsync_is_asked_once_for_each_thing_whatever_the_number_of_names(library):
    library.playlist("Gym")
    library.playlist("Car")
    library.playlist("Chill")
    soulsync = FakeSoulSync()
    built = Built(soulsync)
    built.changes.add(library, SONG, ["gym", "car", "chill"])
    assert built.asked == ["soulsync"]
    assert soulsync.calls == ["remade_playlists", "mirrors"]
    assert library.called("all_playlists") == [()]


def test_without_soulsync_set_up_navidrome_alone_is_changed(library):
    gym = library.playlist("Gym", [SONG])
    car = library.playlist("Car")
    built = Built(None)
    assert built.changes.add(library, SONG, ["car"])["message"] == "Added Song 1 to Car."
    assert built.changes.remove(library, SONG, ["gym"])["message"] == "Took Song 1 off Gym."
    assert library.called("add_songs") == [(car, ["s1"])]
    assert library.called("remove_entries") == [(gym, [0])]


def test_nothing_is_asked_of_navidrome_or_soulsync_by_making_one(library):
    built = Built(FakeSoulSync())
    assert library.calls == []
    assert built.asked == []


def test_every_copy_of_the_song_is_taken_off(library):
    gym = library.playlist("Gym Bangers", [SONG, OTHER_SONG, SINGLE, SONG])
    reply = Built().changes.remove(library, SONG, ["gym"])
    assert reply["success"] is True
    assert reply["message"] == "Took Song 1 off Gym Bangers."
    assert library.called("remove_entries") == [(gym, [0, 2, 3])]
    assert library.playlists[gym]["songs"] == [OTHER_SONG]
    assert callable(reply["undo"])


def test_a_song_which_isnt_on_the_playlist_says_so(library):
    library.playlist("Gym Bangers", [OTHER_SONG])
    reply = Built().changes.remove(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "Song 1 isn't on Gym Bangers."}
    assert library.called("remove_entries") == []


def test_a_song_taken_off_one_playlist_and_not_on_another_says_both(library):
    library.playlist("Gym Bangers", [SONG])
    library.playlist("Car", [OTHER_SONG])
    reply = Built().changes.remove(library, SONG, ["gym", "car"])
    assert reply["success"] is True
    assert reply["message"] == "Took Song 1 off Gym Bangers. Song 1 isn't on Car."


def test_a_live_version_isnt_the_song_so_it_stays(library):
    live = {"id": "s5", "title": "Song 1 (Live)", "artist": "Made Up Band"}
    gym = library.playlist("Gym Bangers", [live, SONG])
    Built().changes.remove(library, SONG, ["gym"])
    assert library.called("remove_entries") == [(gym, [1])]


def test_liked_songs_named_as_a_playlist_like_the_song(library):
    reply = Built().changes.add(library, SONG, ["my liked songs"])
    assert reply["success"] is True
    assert reply["message"] == "Liked Song 1."
    assert library.starred == {"s1"}
    assert callable(reply["undo"])
    # No playlist was looked for or offered.
    assert library.called("all_playlists") == []
    assert "ask_first" not in reply


def test_liked_songs_named_for_taking_off_unlike_the_song(library):
    library.starred.add("s1")
    reply = Built().changes.remove(library, SONG, ["liked songs"])
    assert reply["success"] is True
    assert reply["message"] == "Unliked Song 1."
    assert library.starred == set()


def test_liked_songs_and_a_playlist_are_both_done(library):
    gym = library.playlist("Gym Bangers")
    reply = Built().changes.add(library, SONG, ["gym", "liked songs"])
    assert reply["message"] == "Added Song 1 to Gym Bangers. Liked Song 1."
    assert library.starred == {"s1"}
    assert library.called("add_songs") == [(gym, ["s1"])]
    # One undo takes back both.
    undone = reply["undo"]()
    assert undone == {"success": True, "message": "Took Song 1 back off Gym Bangers. Unliked Song 1."}
    assert library.starred == set()
    assert library.playlists[gym]["songs"] == []


def test_a_song_already_liked_says_so(library):
    library.starred.add("s1")
    reply = Built().changes.like(library, SONG, True)
    assert reply == {"success": False, "message": "You've already liked Song 1."}
    assert library.called("set_starred") == []
    # Named as a playlist it's the same.
    assert Built().changes.add(library, SONG, ["liked songs"]) == reply


def test_a_song_not_liked_cant_be_unliked(library):
    reply = Built().changes.like(library, SONG, False)
    assert reply == {"success": False, "message": "You haven't liked Song 1."}
    assert library.called("set_starred") == []


def test_liking_goes_by_what_navidrome_says_not_what_the_song_was_queued_with(library):
    library.starred.add("s1")
    playing = {**SONG, "starred": ""}
    assert Built().changes.like(library, playing, True)["success"] is False


def test_making_a_playlist_that_exists_adds_to_it(library):
    chill = library.playlist("Chill")
    reply = Built().changes.make(library, "chill", SONG)
    assert reply["success"] is True
    assert reply["message"] == "You've already got Chill. Added Song 1 to Chill."
    assert library.called("make_playlist") == []
    assert library.called("add_songs") == [(chill, ["s1"])]
    assert callable(reply["undo"])


def test_making_a_playlist_that_exists_without_a_song_says_so(library):
    library.playlist("Chill")
    reply = Built().changes.make(library, "Chill", None)
    assert reply == {"success": False, "message": "You've already got Chill."}
    assert library.called("make_playlist") == []


def test_making_a_playlist_that_exists_with_the_song_on_it_says_both(library):
    library.playlist("Chill", [SONG])
    reply = Built().changes.make(library, "Chill", SONG)
    assert reply == {"success": False, "message": "You've already got Chill. Song 1 is already on Chill."}


def test_making_a_playlist_that_exists_is_still_refused_if_soulsync_remakes_it(library):
    library.playlist("Daily Mix 1")
    reply = Built(FakeSoulSync(remade=["Daily Mix 1"])).changes.make(library, "daily mix 1", SONG)
    assert reply["success"] is False
    assert reply["message"] == "You've already got Daily Mix 1. SoulSync remakes Daily Mix 1, so that wouldn't stay."
    assert library.called("add_songs") == []


def test_a_name_loosely_like_one_of_his_is_still_made(library):
    # Only the same name once normalised is taken for the playlist he has: "Gym" isn't "Gym Bangers".
    library.playlist("Gym Bangers")
    reply = Built().changes.make(library, "Gym", None)
    assert reply == {"success": True, "message": "Made Gym.", "undo": reply["undo"]}
    assert library.called("make_playlist") == [("Gym",)]


def test_a_new_playlist_is_made_empty_or_with_the_song(library):
    empty = Built().changes.make(library, "Chill", None)
    assert empty["success"] is True
    assert empty["message"] == "Made Chill."
    assert library.called("add_songs") == []
    with_song = Built().changes.make(library, "Study", SONG)
    assert with_song["message"] == "Made Study with Song 1 on it."
    study = next(playlist_id for playlist_id, playlist in library.playlists.items() if playlist["name"] == "Study")
    assert library.called("add_songs") == [(study, ["s1"])]


def test_making_a_playlist_needs_no_soulsync_check(library):
    built = Built(FakeSoulSync(down=True))
    assert built.changes.make(library, "Chill", SONG)["success"] is True
    assert built.asked == []


def test_a_playlist_without_a_name_isnt_made(library):
    assert Built().changes.make(library, "  ", SONG) == {"success": False,
                                                          "message": "I didn't catch the playlist's name."}
    assert Built().changes.make(library, "", None)["message"] == "I didn't catch the playlist's name."
    assert library.calls == []


def test_the_liked_songs_arent_a_playlist_to_make(library):
    reply = Built().changes.make(library, "my liked songs", SONG)
    assert reply == {"success": False, "message": "Your liked songs aren't a playlist. Say 'like this' to add a "
                                                  "song to them."}
    assert library.calls == []


def test_each_change_is_undone_as_the_spec_says(library):
    changes = Built().changes
    gym = library.playlist("Gym Bangers")
    car = library.playlist("Car")

    # An add: the last entry with the id added, on each playlist, and not what came after it or another entry.
    added = changes.add(library, SONG, ["gym", "car"])
    library.playlists[gym]["songs"].append(dict(OTHER_SONG))
    library.playlists[car]["songs"].insert(0, dict(OTHER_SONG))
    undone = added["undo"]()
    assert undone == {"success": True, "message": "Took Song 1 back off Gym Bangers and Car."}
    assert library.playlists[gym]["songs"] == [OTHER_SONG]
    assert library.playlists[car]["songs"] == [OTHER_SONG]
    # An undo can't itself be undone.
    assert "undo" not in undone

    # A removal: the first removed entry's id, back at the end.
    library.playlists[gym]["songs"] = [dict(SINGLE), dict(OTHER_SONG), dict(SONG)]
    removed = changes.remove(library, SONG, ["gym"])
    assert library.playlists[gym]["songs"] == [OTHER_SONG]
    undone = removed["undo"]()
    assert undone == {"success": True, "message": "Put Song 1 back on Gym Bangers."}
    assert library.playlists[gym]["songs"] == [OTHER_SONG, SINGLE]

    # A new playlist: deleted.
    made = changes.make(library, "Chill", SONG)
    chill = next(playlist_id for playlist_id, playlist in library.playlists.items() if playlist["name"] == "Chill")
    assert made["undo"]() == {"success": True, "message": "Deleted Chill."}
    assert chill not in library.playlists
    assert library.called("delete_playlist") == [(chill,)]

    # A like and an unlike.
    liked = changes.like(library, SONG, True)
    assert liked["undo"]() == {"success": True, "message": "Unliked Song 1."}
    assert library.starred == set()
    library.starred.add("s1")
    unliked = changes.like(library, SONG, False)
    assert unliked["message"] == "Unliked Song 1."
    assert unliked["undo"]() == {"success": True, "message": "Liked Song 1 again."}
    assert library.starred == {"s1"}


def test_an_undo_of_a_removal_from_two_playlists_names_both(library):
    gym = library.playlist("Gym Bangers", [SONG])
    car = library.playlist("Car", [SINGLE])
    removed = Built().changes.remove(library, SONG, ["gym", "car"])
    undone = removed["undo"]()
    assert undone == {"success": True, "message": "Put Song 1 back on Gym Bangers and Car."}
    # Each gets the id of its own first entry taken off.
    assert library.called("add_songs") == [(gym, ["s1"]), (car, ["s9"])]


def test_an_undo_of_an_add_which_has_already_gone_says_so_and_changes_nothing(library):
    gym = library.playlist("Gym Bangers")
    added = Built().changes.add(library, SONG, ["gym"])
    library.playlists[gym]["songs"] = []
    undone = added["undo"]()
    assert undone == {"success": False, "message": "Song 1 isn't on Gym Bangers any more."}
    assert library.called("remove_entries") == []


@pytest.mark.parametrize("error", [DOWN, NavidromeError("no such playlist", 70), ValueError("not Subsonic")])
def test_an_undo_which_navidrome_fails_says_so_and_never_raises(library, error):
    library.playlist("Gym Bangers")
    changes = Built().changes
    added = changes.add(library, SONG, ["gym"])
    library.failures["playlist_entries"] = (2, error)
    assert added["undo"]() == {"success": False, "message": NavidromeLibrary._failure_message(error)}

    # The same for each of the others.
    made = changes.make(library, "Chill", None)
    library.failures["delete_playlist"] = (1, error)
    assert made["undo"]() == {"success": False, "message": NavidromeLibrary._failure_message(error)}
    liked = changes.like(library, SONG, True)
    library.failures["set_starred"] = (2, error)
    assert liked["undo"]() == {"success": False, "message": NavidromeLibrary._failure_message(error)}
    library.playlists[next(iter(library.playlists))]["songs"] = [dict(SONG)]
    removed = changes.remove(library, SONG, ["gym"])
    library.failures["add_songs"] = (2, error)
    assert removed["undo"]() == {"success": False, "message": NavidromeLibrary._failure_message(error)}


def test_navidrome_failing_half_way_keeps_the_undo_of_what_was_done(library):
    gym = library.playlist("Gym Bangers")
    car = library.playlist("Car")
    library.failures["add_songs"] = (2, DOWN)
    reply = Built().changes.add(library, SONG, ["gym", "car"])
    # Gym was done, so that's said first, and the failure; and Gym can be undone.
    assert reply["success"] is True
    assert reply["message"] == "Added Song 1 to Gym Bangers. I can't reach Navidrome right now."
    assert library.playlists[gym]["songs"] == [SONG]
    assert library.playlists[car]["songs"] == []
    assert reply["undo"]() == {"success": True, "message": "Took Song 1 back off Gym Bangers."}
    assert library.playlists[gym]["songs"] == []


def test_navidrome_failing_before_anything_was_done_changes_nothing(library):
    library.playlist("Gym Bangers")
    library.failures["all_playlists"] = (1, DOWN)
    reply = Built().changes.add(library, SONG, ["gym"])
    assert reply == {"success": False, "message": "I can't reach Navidrome right now."}


def test_navidrome_failing_on_the_first_playlist_says_so_without_an_undo(library):
    library.playlist("Gym Bangers")
    library.playlist("Car")
    library.failures["add_songs"] = (1, NavidromeError("broken"))
    reply = Built().changes.add(library, SONG, ["gym", "car"])
    assert reply == {"success": False, "message": "Navidrome had a problem with that."}
    # It stops there, rather than going on to ask again.
    assert len(library.called("add_songs")) == 1


def test_navidrome_failing_on_a_removal_keeps_the_undo_of_the_one_done(library):
    gym = library.playlist("Gym Bangers", [SONG])
    library.playlist("Car", [SONG])
    library.failures["remove_entries"] = (2, ValueError("not Subsonic"))
    reply = Built().changes.remove(library, SONG, ["gym", "car"])
    assert reply["success"] is True
    assert reply["message"] == "Took Song 1 off Gym Bangers. Navidrome had a problem with that."
    assert reply["undo"]() == {"success": True, "message": "Put Song 1 back on Gym Bangers."}
    assert library.playlists[gym]["songs"] == [SONG]


def test_a_new_playlist_made_but_not_filled_keeps_the_undo_of_making_it(library):
    library.failures["add_songs"] = (1, DOWN)
    reply = Built().changes.make(library, "Chill", SONG)
    assert reply["success"] is True
    assert reply["message"] == "Made Chill, but I couldn't put Song 1 on it. I can't reach Navidrome right now."
    assert reply["undo"]() == {"success": True, "message": "Deleted Chill."}


def test_navidrome_failing_to_make_a_playlist_changes_nothing(library):
    library.failures["make_playlist"] = (1, DOWN)
    assert Built().changes.make(library, "Chill", None) == {"success": False,
                                                            "message": "I can't reach Navidrome right now."}


def test_navidrome_failing_to_like_changes_nothing(library):
    library.failures["set_starred"] = (1, NavidromeError("broken"))
    assert Built().changes.like(library, SONG, True) == {"success": False,
                                                         "message": "Navidrome had a problem with that."}


def test_no_names_at_all_changes_nothing(library):
    assert Built().changes.add(library, SONG, []) == {"success": False, "message": "I didn't catch which playlist."}
    assert Built().changes.remove(library, SONG, ["", "  "])["success"] is False
    assert library.calls == []
