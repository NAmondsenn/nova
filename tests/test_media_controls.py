"""
Media Controls Test
Checks what the brain tells a satellite with Windows' media controls of its own music:
only what changed, the cover only when it changes, and music elsewhere as none - see
docs/decisions/0030. Needs nothing beyond the standard library.
"""

import logging

from voice_assistant import protocol
from voice_assistant.media_controls import NowPlayingFeed


class Satellite:
    """Stands in for RemoteSatellite.send_now_playing, keeping each message as it was given."""

    def __init__(self):
        self.sent = []
        self.connected = True

    def send_now_playing(self, state, track, artist, album, **cover):
        self.sent.append(dict(state=state, track=track, artist=artist, album=album, **cover))
        return self.connected


class Covers:
    """Stands in for a player's cover(), giving the picture for whichever id is playing, and counting the asks."""

    def __init__(self, picture=(b"jpeg", "image/jpeg")):
        self.picture = picture
        self.asked = 0

    def __call__(self):
        self.asked += 1
        return self.picture


def song(playing=True, track="Song", artist="Band", album="Album", cover="al-1", **more):
    return {"track": track, "artist": artist, "album": album, "playing": playing, "position": 12.5,
            "duration": 200, "cover": cover, **more}


def made():
    satellite = Satellite()
    return NowPlayingFeed(satellite.send_now_playing), satellite


PLAYING = {"state": "playing", "track": "Song", "artist": "Band", "album": "Album"}
STOPPED = {"state": "stopped", "track": "", "artist": "", "album": ""}


def test_the_first_look_is_always_sent_even_with_nothing_playing():
    feed, satellite = made()
    feed.update(None, Covers())
    assert satellite.sent == [STOPPED]


def test_whats_playing_is_sent_with_its_cover_then_only_when_it_changes():
    feed, satellite = made()
    covers = Covers()
    feed.update(song(), covers)
    # Only the position has moved, which isn't shown.
    feed.update(dict(song(), position=17.5), covers)
    feed.update(song(), covers)
    assert satellite.sent == [dict(PLAYING, cover=b"jpeg")]
    assert covers.asked == 1

    feed.update(song(playing=False), covers)
    assert satellite.sent[-1] == dict(PLAYING, state="paused")


def test_the_cover_goes_again_only_when_it_changes():
    feed, satellite = made()
    covers = Covers()
    feed.update(song(track="One"), covers)
    # The next song off the same album keeps its cover.
    feed.update(song(track="Two"), covers)
    covers.picture = (b"png", "image/png")
    feed.update(song(track="Three", album="Other", cover="al-2"), covers)
    assert [each.get("cover", "kept") for each in satellite.sent] == [b"jpeg", "kept", b"png"]
    assert [each["track"] for each in satellite.sent] == ["One", "Two", "Three"]
    assert covers.asked == 2


def test_a_song_without_a_cover_takes_the_last_one_away():
    feed, satellite = made()
    feed.update(song(), Covers())
    feed.update(song(track="Bare", cover=None), Covers())
    assert satellite.sent[-1] == dict(PLAYING, track="Bare", cover=None)
    # And a song with none, after one with none, says nothing of it.
    feed.update(song(track="Bare too", cover=None), Covers())
    assert "cover" not in satellite.sent[-1]


def test_music_playing_elsewhere_counts_as_stopped():
    # Spotify says what the account plays anywhere; only its own speaker's is this satellite's.
    feed, satellite = made()
    feed.update(song(here=True), Covers())
    feed.update(song(here=False), Covers())
    assert satellite.sent == [dict(PLAYING, cover=b"jpeg"), STOPPED]


def test_after_stopping_the_cover_is_sent_again():
    # The satellite clears everything when it's told the music stopped, the cover with it.
    feed, satellite = made()
    covers = Covers()
    feed.update(song(), covers)
    feed.update(None, covers)
    feed.update(song(), covers)
    assert satellite.sent == [dict(PLAYING, cover=b"jpeg"), STOPPED, dict(PLAYING, cover=b"jpeg")]


def test_a_cover_which_cant_be_had_is_asked_for_again_and_sent_once_it_can():
    feed, satellite = made()
    covers = Covers(picture=None)
    feed.update(song(), covers)
    assert satellite.sent == [PLAYING]
    feed.update(song(), covers)
    # Nothing has changed that the satellite shows, so nothing more is sent.
    assert satellite.sent == [PLAYING]
    covers.picture = (b"jpeg", "image/jpeg")
    feed.update(song(), covers)
    assert satellite.sent == [PLAYING, dict(PLAYING, cover=b"jpeg")]
    assert covers.asked == 3


def test_a_new_cover_which_cant_be_had_takes_the_old_one_away():
    feed, satellite = made()
    feed.update(song(), Covers())
    feed.update(song(track="Next", cover="al-2"), Covers(picture=None))
    assert satellite.sent[-1] == dict(PLAYING, track="Next", cover=None)


def test_a_cover_too_big_for_a_message_is_left_out_and_logged_once(caplog):
    feed, satellite = made()
    covers = Covers(picture=(b"x" * (protocol.MAX_AUDIO + 1), "image/jpeg"))
    with caplog.at_level(logging.WARNING):
        feed.update(song(), covers)
        feed.update(song(), covers)
        feed.update(song(), covers)
    assert satellite.sent == [PLAYING]
    assert len([record for record in caplog.records if "cover" in record.getMessage()]) == 1
    # One just small enough goes.
    feed.update(song(cover="al-2"), Covers(picture=(b"x" * protocol.MAX_AUDIO, "image/jpeg")))
    assert satellite.sent[-1] == dict(PLAYING, cover=b"x" * protocol.MAX_AUDIO)


def test_forced_it_is_sent_unchanged_without_the_cover():
    # Straight after a button, so the satellite shows what the brain made of it.
    feed, satellite = made()
    feed.update(song(), Covers())
    feed.update(song(), Covers(), force=True)
    assert satellite.sent == [dict(PLAYING, cover=b"jpeg"), PLAYING]


def test_what_couldnt_be_sent_is_sent_next_time():
    feed, satellite = made()
    satellite.connected = False
    feed.update(song(), Covers())
    satellite.connected = True
    feed.update(song(), Covers())
    assert satellite.sent == [dict(PLAYING, cover=b"jpeg"), dict(PLAYING, cover=b"jpeg")]


def test_missing_or_odd_names_are_sent_as_text():
    feed, satellite = made()
    feed.update({"playing": True, "track": None, "artist": 7, "cover": None}, Covers())
    assert satellite.sent == [{"state": "playing", "track": "", "artist": "7", "album": ""}]
