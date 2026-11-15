"""
Music Device Test
Checks what Nova knows of what the user listens to shows in Home Assistant as a device for the
brain - a sensor for each window and the library, and an image for each top artist's picture,
under entity ids which are fixed from the start - that it follows the listening snapshot without
ever asking SoulSync, and that a picture is sent once, with stand-ins for paho-mqtt's client
and the listening stats.
"""

import json
import threading
from datetime import datetime

import pytest

from test_ha_device import FakeClient
from voice_assistant import ha_device, music_device
from voice_assistant.listening import Snapshot
from voice_assistant.music_device import MusicDevice

BASE = "nova/nova_music_nova"
DISCOVERY = "homeassistant/device/nova_music_nova/config"

WINDOWS = ("7d", "30d", "12m", "all")
SLUGS = {"7d": "7_days", "30d": "30_days", "12m": "12_months", "all": "all_time"}
NAMES = {"7d": "7 days", "30d": "30 days", "12m": "12 months", "all": "all time"}


def _window(plays, artists=7, previous=True, count=25):
    """A made-up window as SoulSync gives it: its lists use play_count."""
    artist_list = [{"name": f"Artist {n}", "play_count": 100 - n, "image_url": f"/cover/artist-{n}", "id": f"a{n}"}
                   for n in range(1, count + 1)]
    return {
        "overview": {"total_plays": plays, "total_time_ms": plays * 220000, "unique_artists": artists,
                     "unique_albums": 18, "unique_tracks": 60},
        "previous": ({"total_plays": plays // 2, "total_time_ms": plays * 110000, "unique_artists": artists,
                      "unique_albums": 9, "unique_tracks": 0} if previous else None),
        "top_artists": artist_list,
        "top_albums": [{"name": f"Album {n}", "artist": f"Artist {n}", "play_count": 50 - n, "image_url": "/c",
                        "id": f"b{n}"} for n in range(1, count + 1)],
        "top_tracks": [{"name": f"Song {n}", "artist": f"Artist {n}", "album": "Album", "play_count": 20 - n,
                        "id": f"t{n}"} for n in range(1, count + 1)],
        "timeline": [{"date": f"2026-11-{n:02d}", "plays": n} for n in range(1, 8)],
        "genres": [{"genre": f"Genre {n}", "play_count": 70 - n, "percentage": 10.5} for n in range(1, count + 1)],
        "clock": {"grid": [[(day + hour) % 4 for hour in range(24)] for day in range(7)],
                  "peak": {"weekday": 4, "hour": 18, "plays": 3}, "total": plays},
        "rhythm": {"current_streak": 5, "longest_streak": 7, "busiest_day": {"date": "2026-11-13", "plays": 30},
                   "active_days": 6},
    }


def _snapshot():
    windows = {"7d": _window(120), "30d": _window(400), "12m": _window(3000), "all": _window(9000, previous=False)}
    pictures = {f"/cover/artist-{n}": (b"\xff\xd8jpeg" + bytes([n]), "image/jpeg") for n in range(1, 6)}
    return Snapshot(
        windows=windows, year={}, pictures=pictures, at=1_800_000_000.0,
        health={"total_tracks": 800, "unplayed_count": 200, "unplayed_percentage": 25.0,
                "format_breakdown": {"MP3": 300, "FLAC": 500}, "total_duration_ms": 180_000_000},
        disk={"success": True, "has_data": True, "total_bytes": 9900000000, "tracks_with_size": 790,
              "tracks_without_size": 10, "by_format": {"flac": 7000000000, "mp3": 2000000000}})


class FakeListening:
    """Stands in for Listening, with the snapshot it's given. It has no refresh: the device must not ask."""

    def __init__(self, snapshot):
        self.current = snapshot

    def snapshot(self):
        return self.current


@pytest.fixture
def listening():
    return FakeListening(_snapshot())


@pytest.fixture
def started(listening):
    """A MusicDevice for listening, started, with the client it made - not yet connected."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    device = MusicDevice("broker.local", 1883, "nova", "secret", listening, client_factory=factory, hostname="nova")
    device.start()
    yield device, made[0]
    device.stop()


def _config(client):
    [(_, payload, _, retained)] = client.sent(DISCOVERY)
    assert retained
    return json.loads(payload)


def _attributes(client, control):
    return json.loads(client.latest()[f"{BASE}/{control}/attributes"])


def test_discovery_lists_sensors_and_images(started):
    device, client = started
    assert client.client_id == "nova_music_nova" and device.identifier == "nova_music_nova"
    client.connects()
    config = _config(client)
    assert config["dev"]["ids"] == ["nova_music_nova"]
    assert (config["dev"]["name"], config["dev"]["mdl"]) == ("Nova music", "Nova brain")
    assert config["availability_topic"] == f"{BASE}/availability"

    controls = config["cmps"]
    sensors = {control: s for control, s in controls.items() if s["p"] == "sensor"}
    images = {control: s for control, s in controls.items() if s["p"] == "image"}
    assert set(sensors) == {"plays_7d", "plays_30d", "plays_12m", "plays_all", "library"}
    assert set(images) == {f"artist_{w}_{n}" for w in WINDOWS for n in range(1, 6)} and len(images) == 20
    # One unique id for each, in the device's own id.
    assert len({s["unique_id"] for s in controls.values()}) == 25
    for control, settings in controls.items():
        assert settings["unique_id"] == f"nova_music_nova_{control}"
        # Read-only: nothing here for Home Assistant to press or set.
        assert "command_topic" not in settings
    for control, settings in sensors.items():
        assert settings["state_topic"] == f"{BASE}/{control}"
        assert settings["json_attributes_topic"] == f"{BASE}/{control}/attributes"
    for window in WINDOWS:
        assert sensors[f"plays_{window}"]["name"] == f"Plays {NAMES[window]}"
        for n in range(1, 6):
            assert images[f"artist_{window}_{n}"]["image_topic"] == f"{BASE}/artist_{window}_{n}"
            assert images[f"artist_{window}_{n}"]["content_type"] == "image/jpeg"


def test_entity_ids_are_pinned(started):
    device, client = started
    client.connects()
    pinned = {control: settings["default_entity_id"] for control, settings in _config(client)["cmps"].items()}
    expected = {"library": "sensor.nova_music_library"}
    for window in WINDOWS:
        expected[f"plays_{window}"] = f"sensor.nova_music_plays_{SLUGS[window]}"
        for n in range(1, 6):
            expected[f"artist_{window}_{n}"] = f"image.nova_music_artist_{SLUGS[window]}_{n}"
    # Renaming one later loses its history, and the view is built on these, so they're set.
    assert pinned == expected


def test_state_from_snapshot(started):
    device, client = started
    client.connects()
    sent = client.latest()
    assert sent[f"{BASE}/plays_7d"] == "120" and sent[f"{BASE}/plays_all"] == "9000"
    attributes = _attributes(client, "plays_7d")
    # total_time_ms is 120 * 220000, so 440 minutes.
    assert (attributes["minutes"], attributes["artists"], attributes["albums"], attributes["tracks"]) == (440, 7, 18, 60)
    assert attributes["top_artists"][0] == {"name": "Artist 1", "plays": 99, "picture": True}
    assert attributes["top_albums"][0] == {"name": "Album 1", "artist": "Artist 1", "plays": 49}
    assert attributes["top_tracks"][0] == {"name": "Song 1", "artist": "Artist 1", "plays": 19}
    assert len(attributes["top_artists"]) == len(attributes["top_albums"]) == len(attributes["top_tracks"]) == 10
    assert attributes["timeline"][0] == {"date": "2026-11-01", "plays": 1} and len(attributes["timeline"]) == 7
    assert attributes["clock"]["peak"] == {"weekday": 4, "hour": 18, "plays": 3}
    assert len(attributes["clock"]["grid"]) == 7 and len(attributes["clock"]["grid"][0]) == 24
    assert attributes["rhythm"]["current_streak"] == 5
    assert len(attributes["genres"]) == 5 and attributes["genres"][0]["genre"] == "Genre 1"
    # Plays doubled since the previous 7 days, and so did the minutes; its tracks had none to compare.
    assert attributes["change"] == {"plays": 100.0, "minutes": 100.0, "artists": 0.0, "albums": 100.0, "tracks": None}

    library = sent[f"{BASE}/library"]
    assert library == "800"
    attributes = _attributes(client, "library")
    assert (attributes["unplayed_count"], attributes["unplayed_percentage"]) == (200, 25.0)
    # 180,000,000 ms is 50 hours.
    assert attributes["hours"] == 50.0
    assert attributes["formats"] == {"MP3": 300, "FLAC": 500}
    assert attributes["disk_bytes"] == 9900000000
    assert attributes["disk_formats"] == {"flac": 7000000000, "mp3": 2000000000}
    assert datetime.fromisoformat(attributes["refreshed"]).timestamp() == 1_800_000_000.0
    assert attributes["problem"] is None
    assert sent[f"{BASE}/availability"] == "online"


def test_change_none_when_previous_zero(started, listening):
    device, client = started
    window = listening.current.windows["7d"]
    window["previous"]["total_plays"] = 0
    client.connects()
    change = _attributes(client, "plays_7d")["change"]
    assert change["plays"] is None and change["artists"] == 0.0
    # All time has nothing to compare with, so every figure is None.
    assert _attributes(client, "plays_all")["change"] == {
        "plays": None, "minutes": None, "artists": None, "albums": None, "tracks": None}


def test_pictures_are_matched_to_artists_and_sent_as_bytes(started):
    device, client = started
    client.connects()
    sent = client.latest()
    for window in WINDOWS:
        for n in range(1, 6):
            assert sent[f"{BASE}/artist_{window}_{n}"] == b"\xff\xd8jpeg" + bytes([n])
    [(_, _, qos, retained)] = client.sent(f"{BASE}/artist_7d_1")
    assert retained and qos == 1


def test_picture_bytes_published_once(started, listening):
    device, client = started
    client.connects()
    count = len(client.published)
    device.publish_state()
    device.publish_state()
    assert len(client.published) == count
    assert len(client.sent(f"{BASE}/artist_7d_1")) == 1

    # A changed picture is sent again, and only that one.
    url = "/cover/artist-1"
    listening.current = _replaced(listening.current, pictures={**listening.current.pictures,
                                                               url: (b"new picture", "image/jpeg")})
    device.publish_state()
    assert [p[1] for p in client.sent(f"{BASE}/artist_7d_1")] == [b"\xff\xd8jpeg\x01", b"new picture"]
    assert len(client.sent(f"{BASE}/artist_7d_2")) == 1


def test_an_artist_with_no_picture_sends_an_empty_one_so_home_assistant_shows_none(started, listening):
    device, client = started
    client.connects()
    pictures = dict(listening.current.pictures)
    del pictures["/cover/artist-2"]
    listening.current = _replaced(listening.current, pictures=pictures)
    device.publish_state()
    assert client.latest()[f"{BASE}/artist_30d_2"] == b""
    assert client.latest()[f"{BASE}/artist_30d_3"] != b""


def test_the_top_five_artists_say_whether_they_have_a_picture(started, listening):
    # Home Assistant gives an image entity an address whether or not it has a picture, so the Music view
    # needs telling which have one to show a plain disc for the rest.
    device, client = started
    pictures = dict(listening.current.pictures)
    del pictures["/cover/artist-2"]
    pictures["/cover/artist-3"] = (b"", "image/jpeg")
    listening.current = _replaced(listening.current, pictures=pictures)
    client.connects()
    artists = _attributes(client, "plays_30d")["top_artists"]
    assert [artist.get("picture") for artist in artists[:5]] == [True, False, False, True, True]
    # Only the first five have an image to show, so only they say.
    assert all("picture" not in artist for artist in artists[5:])
    assert client.latest()[f"{BASE}/artist_30d_3"] == b""


def test_a_picture_of_another_type_is_announced_again(started, listening):
    device, client = started
    client.connects()
    assert len(client.sent(DISCOVERY)) == 1
    pictures = {**listening.current.pictures, "/cover/artist-1": (b"\x89PNG", "image/png")}
    listening.current = _replaced(listening.current, pictures=pictures)
    device.publish_state()
    assert len(client.sent(DISCOVERY)) == 2
    assert _config(client_with_last(client))["cmps"]["artist_7d_1"]["content_type"] == "image/png"
    device.publish_state()
    assert len(client.sent(DISCOVERY)) == 2


def client_with_last(client):
    """A stand-in with only the newest announcement, for _config."""
    last = FakeClient(client.client_id)
    last.published = [client.sent(DISCOVERY)[-1]]
    return last


def _replaced(snapshot, **changes):
    return Snapshot(**{**snapshot.__dict__, **changes})


def test_no_snapshot_yet(started, listening):
    device, client = started
    listening.current = Snapshot(problem="I haven't got your listening stats from SoulSync yet.")
    client.connects()
    sent = client.latest()
    for window in WINDOWS:
        assert sent[f"{BASE}/plays_{window}"] == "None"
        assert sent[f"{BASE}/artist_{window}_1"] == b""
    assert sent[f"{BASE}/library"] == "None"
    attributes = _attributes(client, "library")
    assert attributes["refreshed"] is None and attributes["problem"].startswith("I haven't got")
    assert attributes["disk_bytes"] is None and attributes["formats"] is None
    assert sent[f"{BASE}/availability"] == "online"


def test_the_problem_is_shown_beside_the_last_good_stats(started, listening):
    device, client = started
    client.connects()
    listening.current = _replaced(listening.current, problem="SoulSync couldn't give your listening stats.")
    device.publish_state()
    assert _attributes(client, "library")["problem"] == "SoulSync couldn't give your listening stats."
    assert client.latest()[f"{BASE}/plays_7d"] == "120"


def test_attributes_under_16kb(started, listening):
    device, client = started
    # 25-item lists as SoulSync gives, and a long timeline which would be too much on its own.
    for window in listening.current.windows.values():
        window["timeline"] = [{"date": f"2026-{n // 28 % 12 + 1:02d}-{n % 28 + 1:02d}", "plays": n}
                              for n in range(1200)]
    client.connects()
    for control in [f"plays_{w}" for w in WINDOWS] + ["library"]:
        payload = client.latest()[f"{BASE}/{control}/attributes"]
        assert len(payload.encode()) < 16 * 1024
    # The newest days are the ones which stay.
    timeline = _attributes(client, "plays_7d")["timeline"]
    assert timeline and timeline[-1]["plays"] == 1199


def test_attributes_too_big_without_the_timeline_still_publish(started, listening, caplog):
    device, client = started
    # A rhythm which alone is over the limit, as a changed SoulSync could give, with a timeline of three.
    window = listening.current.windows["7d"]
    window["rhythm"] = {"days": [{"date": f"2026-11-{n % 28 + 1:02d}", "plays": n, "note": "x" * 20}
                                 for n in range(500)]}
    window["timeline"] = window["timeline"][:3]

    # In a thread with a timeout, so the old code, which never ended, fails rather than hangs the run.
    result = []
    thread = threading.Thread(target=lambda: result.append(device._state()), daemon=True)
    with caplog.at_level("WARNING", logger=music_device.__name__):
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        device._state()
    state = result[0]
    attributes = json.loads(state[f"{BASE}/plays_7d/attributes"])
    assert attributes["timeline"] == [] and len(attributes["rhythm"]["days"]) == 500
    assert state[f"{BASE}/plays_7d"] == "120"
    # Said once for the window, whatever it was asked twice, and with nothing of what's listened to.
    warnings = [r.getMessage() for r in caplog.records if r.name == music_device.__name__]
    assert len(warnings) == 1 and "7d" in warnings[0] and "Artist" not in warnings[0]


def test_the_snapshot_is_left_as_it_was(started, listening):
    device, client = started
    before = json.dumps(listening.current.windows, sort_keys=True)
    client.connects()
    assert json.dumps(listening.current.windows, sort_keys=True) == before


def test_subscribes_to_ha_status_only(started):
    device, client = started
    client.connects()
    assert client.subscribed == [("homeassistant/status", 1)]


def test_it_announces_again_when_home_assistant_restarts_and_takes_no_commands(started):
    device, client = started
    client.connects()
    assert len(client.sent(DISCOVERY)) == 1
    client.delivers("homeassistant/status", "online")
    assert len(client.sent(DISCOVERY)) == 2

    count = len(client.published)
    for topic in (f"{BASE}/stop", f"{BASE}/restart", f"{BASE}/speak", f"{BASE}/plays_7d/set", "nova/nova_nova/stop"):
        client.delivers(topic, "PRESS")
    assert len(client.published) == count


def test_a_broken_snapshot_is_logged_once_and_doesnt_stop_it(started, listening, caplog):
    device, client = started
    client.connects()

    def broken():
        raise RuntimeError("no stats")

    listening.snapshot = broken
    with caplog.at_level("ERROR", logger=ha_device.__name__):
        device.publish_state()
        device.publish_state()
    assert len([r for r in caplog.records if r.name == ha_device.__name__ and r.levelname == "ERROR"]) == 1


def test_a_window_the_snapshot_lacks_is_unknown(started, listening):
    device, client = started
    del listening.current.windows["12m"]
    client.connects()
    assert client.latest()[f"{BASE}/plays_12m"] == "None"
    assert client.latest()[f"{BASE}/plays_7d"] == "120"


def test_its_off_without_a_broker(make_config, listening):
    assert MusicDevice.from_config(make_config(mqtt={"host": ""}), "pw", listening) is None


def test_its_id_comes_from_the_brains_hostname(monkeypatch, make_config, listening):
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "nova.lan")
    settings = {"host": " 192.168.1.20 ", "port": 1883, "username": "nova", "name": "Nova"}
    device = MusicDevice.from_config(make_config(mqtt=settings), "pw", listening)
    assert device.identifier == "nova_music_nova" and (device.host, device.port) == ("192.168.1.20", 1883)
    assert device.username == "nova" and device.password == "pw" and device.name == "Nova music"
    assert device.listening is listening
