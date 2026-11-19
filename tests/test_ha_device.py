"""
Home Assistant Device Test
Checks Nova appears in Home Assistant as a device of its own with its eleven
controls, that its state is sent only when it changes, and that Home Assistant's
commands reach Nova while nothing anyone says goes the other way - with stand-ins
for paho-mqtt's client, the broker and Nova itself.
"""

import dataclasses
import json
import logging
import threading
import time
from datetime import datetime

import pytest

from voice_assistant import ha_device
from voice_assistant.ha_device import DeviceControls, NovaDevice
from voice_assistant.logging_setup import TRANSCRIPT

BASE = "nova/nova_test"
DISCOVERY = "homeassistant/device/nova_test/config"
DUE = 1_800_000_000.0


class Reason:
    """Stands in for paho's ReasonCode."""

    def __init__(self, name="Success", failure=False):
        self.name, self.is_failure = name, failure

    def __str__(self):
        return self.name


class Message:
    """Stands in for paho's MQTTMessage."""

    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class FakeClient:
    """
    Stands in for paho-mqtt's client, keeping what's published and subscribed to,
    and lets the test play the broker - connecting, failing to, and delivering
    Home Assistant's messages.
    """

    def __init__(self, client_id):
        self.client_id = client_id
        self.published = []
        self.subscribed = []
        self.will = self.login = self.address = None
        self.looping = self.disconnected = False
        self.on_connect = self.on_connect_fail = self.on_disconnect = self.on_message = None

    def username_pw_set(self, username, password=None):
        self.login = (username, password)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, host, port=1883, keepalive=60):
        self.address = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.extend(topic if isinstance(topic, list) else [(topic, qos)])

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    # The broker's side

    def connects(self, refused=False):
        self.on_connect(self, None, None, Reason("Not authorized", True) if refused else Reason(), None)

    def cant_connect(self):
        self.on_connect_fail(self, None)

    def drops(self):
        self.on_disconnect(self, None, None, Reason("Unspecified error", True), None)

    def delivers(self, topic, payload, retain=False):
        self.on_message(self, None, Message(topic, payload, retain))

    def sent(self, topic):
        return [published for published in self.published if published[0] == topic]

    def latest(self):
        """What each topic was last sent."""
        return {topic: payload for topic, payload, _, _ in self.published}


class FakeNova:
    """Stands in for the rest of Nova, keeping what Home Assistant asked it to do."""

    def __init__(self):
        self.activity = "idle"
        self.levels = {"general": 60, "alarm": 80, "music": 40}
        self.overview = {"timers": [], "alarms": []}
        self.reachable = True
        self.is_muted = False
        self.calls = []
        self.broken = False
        # Cleared, setting a volume waits until it's set again.
        self.release = threading.Event()
        self.release.set()

    def controls(self):
        return DeviceControls(status=lambda: self.activity, volumes=lambda: dict(self.levels),
                              set_volume=self._set_volume, clock=lambda: self.overview, backend=lambda: self.reachable,
                              muted=lambda: self.is_muted, set_muted=lambda muted: self.calls.append(("set_muted", muted)),
                              stop=self._stop, restart=lambda: self.calls.append(("restart",)), speak=self._speak)

    def _set_volume(self, category, percent):
        self.release.wait(2)
        self.calls.append(("set_volume", category, percent))

    def _stop(self):
        if self.broken:
            raise RuntimeError("the speaker's gone")
        self.calls.append(("stop",))

    def _speak(self, message):
        self.calls.append(("speak", message))


@pytest.fixture
def nova():
    return FakeNova()


@pytest.fixture
def started(nova):
    """A NovaDevice for nova, started, with the client it made - not yet connected."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    device = NovaDevice("broker.local", 1883, "nova", "secret", nova.controls(), node_id="nova_test",
                        client_factory=factory)
    device.start()
    yield device, made[0]
    device.stop()


def _complaints(caplog):
    return [record for record in caplog.records if record.name == ha_device.__name__
            and record.levelno >= logging.WARNING]


def _eventually(condition, timeout=2):
    """Whether condition comes true within timeout seconds - for what's done on the commands' own thread."""
    give_up = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > give_up:
            return False
        time.sleep(0.01)
    return True


def test_nova_announces_itself_with_its_eleven_controls(started):
    device, client = started
    assert client.client_id == "nova_test" and device.identifier == "nova_test"
    assert client.address == ("broker.local", 1883) and client.login == ("nova", "secret") and client.looping
    # Nothing's sent until it's connected, and from then on the broker says it's
    # offline for it if the connection drops.
    assert client.published == [] and client.will == (f"{BASE}/availability", "offline", 1, True)

    client.connects()
    [(_, payload, _, retained)] = client.sent(DISCOVERY)
    config = json.loads(payload)
    assert retained
    # Home Assistant's device for Nova is found by exactly these identifiers.
    assert config["dev"]["ids"] == ["nova_test"]
    assert (config["dev"]["name"], config["dev"]["mf"], config["dev"]["mdl"]) == ("Nova", "Nova", "Raspberry Pi")
    assert config["o"]["name"] == "Nova" and config["availability_topic"] == f"{BASE}/availability"
    controls = config["cmps"].values()
    assert {control["name"] for control in controls} == {
        "Status", "Volume", "Alarm volume", "Music volume", "Next alarm", "Timers", "Stop", "Restart", "Speak",
        "Backend", "Microphone muted"}
    assert len({control["unique_id"] for control in controls}) == 11
    assert client.sent(f"{BASE}/availability") == [(f"{BASE}/availability", "online", 1, True)]
    assert {f"{BASE}/volume/alarm/set", f"{BASE}/muted/set", f"{BASE}/stop", f"{BASE}/restart", f"{BASE}/speak",
            "homeassistant/status"} <= {topic for topic, _ in client.subscribed}

    # Home Assistant says it's online again after it restarts, and is told again.
    client.delivers("homeassistant/status", "online")
    assert len(client.sent(DISCOVERY)) == 2
    client.delivers("homeassistant/status", "offline")
    assert len(client.sent(DISCOVERY)) == 2


def test_state_is_published_only_when_it_changes(started, nova):
    device, client = started
    client.connects()
    state = client.latest()
    assert {topic: state[topic] for topic in state if topic != DISCOVERY} == {
        f"{BASE}/status": "idle", f"{BASE}/volume/general": "60", f"{BASE}/volume/alarm": "80",
        f"{BASE}/volume/music": "40", f"{BASE}/next_alarm": "None", f"{BASE}/timers": "0",
        f"{BASE}/timers/attributes": json.dumps({"timers": []}), f"{BASE}/backend": "ON", f"{BASE}/muted": "OFF",
        f"{BASE}/availability": "online"}
    assert all(qos == 1 and retained for _, _, qos, retained in client.published)

    before = len(client.published)
    device.publish_state()
    assert len(client.published) == before

    nova.levels["alarm"] = 55
    nova.activity = "listening"
    device.publish_state()
    assert sorted(client.published[before:]) == [(f"{BASE}/status", "listening", 1, True),
                                                  (f"{BASE}/volume/alarm", "55", 1, True)]

    # After a dropped connection all of it is sent again, in case the broker itself
    # restarted and lost what it was keeping.
    client.drops()
    before = len(client.published)
    device.publish_state()
    assert len(client.published) == before
    client.connects()
    assert {topic for topic, _, _, _ in client.published[before:]} == set(state)


def test_the_next_alarm_and_the_timers_are_sent_as_home_assistant_reads_them(started, nova):
    device, client = started
    nova.overview = {"timers": [{"id": 1, "name": "pizza timer", "due_at": DUE},
                                {"id": 2, "name": "call mum reminder", "due_at": DUE + 600}],
                     "alarms": [{"id": 3, "name": "7am alarm", "due_at": DUE + 3600}]}
    client.connects()
    state = client.latest()

    # A time with its time zone, which is what Home Assistant's timestamp sensors take.
    next_alarm = datetime.fromisoformat(state[f"{BASE}/next_alarm"])
    assert next_alarm.tzinfo is not None and next_alarm.timestamp() == DUE + 3600
    assert state[f"{BASE}/timers"] == "2"
    # Home Assistant only takes attributes as a JSON object. Each is only when it ends:
    # a reminder's name is the user's own words.
    timers = json.loads(state[f"{BASE}/timers/attributes"])["timers"]
    assert [datetime.fromisoformat(timer["ends"]).timestamp() for timer in timers] == [DUE, DUE + 600]
    assert all(set(timer) == {"ends"} for timer in timers)

    # With no backend set up, whether it's reached isn't known.
    nova.reachable = None
    nova.overview = {"timers": [], "alarms": []}
    device.publish_state()
    state = client.latest()
    assert (state[f"{BASE}/backend"], state[f"{BASE}/next_alarm"], state[f"{BASE}/timers"]) == ("None", "None", "0")


def test_home_assistants_commands_reach_nova(started, nova):
    device, client = started
    client.connects()
    # A volume which takes a while - the music's, through Spotify - doesn't hold up
    # paho's thread, which has the connection to keep alive.
    nova.release.clear()
    client.delivers(f"{BASE}/volume/alarm/set", "40")
    client.delivers(f"{BASE}/muted/set", "ON")
    client.delivers(f"{BASE}/stop", "PRESS")
    client.delivers(f"{BASE}/restart", "PRESS")
    client.delivers(f"{BASE}/speak", "Washing's done")
    client.delivers(f"{BASE}/speak", "The dryer's done too")
    assert nova.calls == []

    # Then they're carried out one at a time, in the order they came.
    nova.release.set()
    assert _eventually(lambda: len(nova.calls) == 6)
    assert nova.calls == [("set_volume", "alarm", 40), ("set_muted", True), ("stop",), ("restart",),
                          ("speak", "Washing's done"), ("speak", "The dryer's done too")]

    # What a command changed is sent straight back, so Home Assistant's slider and
    # switch don't wait to catch up.
    nova.is_muted = True
    client.delivers(f"{BASE}/muted/set", "ON")
    assert _eventually(lambda: client.latest()[f"{BASE}/muted"] == "ON")


def test_junk_commands_are_ignored(started, nova, caplog):
    device, client = started
    client.connects()
    for junk in ("abc", "nan", "inf", ""):
        client.delivers(f"{BASE}/volume/general/set", junk)
    client.delivers(f"{BASE}/volume/general/set", "150")
    client.delivers(f"{BASE}/volume/music/set", "-5")
    client.delivers(f"{BASE}/volume/loud/set", "50")
    client.delivers(f"{BASE}/muted/set", "maybe")
    client.delivers(f"{BASE}/speak", "   ")
    # Home Assistant never has the broker keep a command, so one that's kept is left
    # over from something else - and would act every time Nova connected, restarting
    # it over and over.
    client.delivers(f"{BASE}/restart", "PRESS", retain=True)
    # Commands are carried out in the order they came, so once this one has been,
    # anything above which was going to be has been too.
    client.delivers(f"{BASE}/stop", "PRESS")
    assert _eventually(lambda: ("stop",) in nova.calls)
    assert nova.calls == [("set_volume", "general", 100), ("set_volume", "music", 0), ("stop",)]

    # A control which fails is logged, and the commands after it are still carried out.
    nova.broken = True
    client.delivers(f"{BASE}/stop", "PRESS")
    client.delivers(f"{BASE}/muted/set", "ON")
    assert _eventually(lambda: ("set_muted", True) in nova.calls)
    assert any("the speaker's gone" in record.getMessage() for record in _complaints(caplog))


def test_a_broker_which_cant_be_reached_doesnt_stop_nova(started, caplog):
    device, client = started
    # start() has already returned, leaving paho to keep trying in the background.
    assert client.looping
    for _ in range(5):
        client.cant_connect()
    complaints = _complaints(caplog)
    assert len(complaints) == 1 and "broker.local" in complaints[0].getMessage()
    device.publish_state()
    assert client.published == []

    # Once it's reached, Nova's in Home Assistant as usual.
    client.connects()
    assert client.sent(DISCOVERY)

    # Losing it again is said once more, as is a password the broker turns away.
    client.drops()
    client.cant_connect()
    client.connects(refused=True)
    complaints = _complaints(caplog)
    assert len(complaints) == 2 and "Unspecified error" in complaints[1].getMessage()
    client.connects()
    client.connects(refused=True)
    complaints = _complaints(caplog)
    assert len(complaints) == 3 and "Not authorized" in complaints[2].getMessage()


def test_without_paho_nova_starts_all_the_same(monkeypatch, nova, caplog):
    def missing(client_id):
        raise ImportError("No module named 'paho'")

    monkeypatch.setattr(ha_device, "_paho_client", missing)
    device = NovaDevice("broker.local", 1883, "nova", "secret", nova.controls(), node_id="nova_test")
    device.start()
    device.publish_state()
    device.stop()
    assert "paho-mqtt" in _complaints(caplog)[0].getMessage()


@pytest.mark.parametrize("error", [ValueError("Invalid port number."), OSError("Name or service not known")])
def test_a_broker_which_cant_be_tried_at_all_doesnt_stop_nova(nova, caplog, error):
    # paho checks the port before it tries in the background, and a port of 0 or below
    # in config.yaml stopped Nova as it started - again and again, as systemd restarted it.
    client = FakeClient("nova_test")

    def connect_async(host, port=1883, keepalive=60):
        raise error

    client.connect_async = connect_async
    device = NovaDevice("broker.local", -1, "nova", "secret", nova.controls(), node_id="nova_test",
                        client_factory=lambda client_id: client)
    device.start()
    device.publish_state()
    device.stop()

    assert not client.looping and client.published == []
    complaints = _complaints(caplog)
    assert len(complaints) == 1 and "broker.local:-1" in complaints[0].getMessage()


def test_its_off_without_a_broker(make_config, nova):
    settings = {"host": "", "port": 1883, "username": "", "name": "Nova"}
    assert NovaDevice.from_config(make_config(mqtt=settings), "pw", nova.controls()) is None


def test_its_ids_come_from_its_name_and_the_machine(monkeypatch, make_config, nova, caplog):
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "home-assistant")
    settings = {"host": " 192.168.1.20 ", "port": 1883, "username": "nova", "name": "Nova"}
    device = NovaDevice.from_config(make_config(mqtt=settings), "pw", nova.controls())
    assert device.identifier == "nova_home_assistant" and (device.host, device.port) == ("192.168.1.20", 1883)
    assert _complaints(caplog) == []

    # A second Nova, named for its room, is a second device.
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "kitchen-pi.local")
    device = NovaDevice.from_config(make_config(mqtt={**settings, "name": "Nova Kitchen"}), None, nova.controls())
    assert device.identifier == "nova_kitchen_kitchen_pi"
    assert "MQTT_PASSWORD" in _complaints(caplog)[0].getMessage()


def test_a_satellites_device_is_named_from_its_own_hostname(monkeypatch, make_config, nova):
    # The brain runs each satellite's device, and the Pi has to keep the one it always
    # had - and with it, the room Home Assistant has it in.
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "nova")
    settings = {"host": "192.168.1.20", "port": 1883, "username": "nova", "name": "Nova"}
    device = NovaDevice.from_config(make_config(mqtt=settings), "pw", nova.controls(), hostname="home-assistant.local")
    assert device.identifier == "nova_home_assistant"


def test_stopping_says_its_offline(started):
    device, client = started
    client.connects()
    device.stop()
    assert client.published[-1] == (f"{BASE}/availability", "offline", 1, True)
    assert client.disconnected and not client.looping


def test_nothing_said_is_published(started, nova):
    device, client = started
    # A reminder's name is what the user said - "remind me to take my tablets".
    nova.overview = {"timers": [{"id": 1, "name": "take my tablets reminder", "due_at": DUE}], "alarms": []}
    client.connects()
    client.delivers(f"{BASE}/speak", "Washing's done")
    assert _eventually(lambda: ("speak", "Washing's done") in nova.calls)
    nova.activity = "speaking"
    device.publish_state()
    client.delivers("homeassistant/status", "online")

    # Only how Nova is set and what it's doing - nothing anyone said, nor its answers.
    assert client.latest()[f"{BASE}/timers"] == "1"
    assert {topic for topic, _, _, _ in client.published} <= {
        DISCOVERY, f"{BASE}/availability", f"{BASE}/status", f"{BASE}/volume/general", f"{BASE}/volume/alarm",
        f"{BASE}/volume/music", f"{BASE}/next_alarm", f"{BASE}/timers", f"{BASE}/timers/attributes",
        f"{BASE}/backend", f"{BASE}/muted"}
    for said in ("tablets", "Washing"):
        assert not any(said in topic or said in payload for topic, payload, _, _ in client.published)


# Nova's music

SONG = {"track": "Fashion Killa", "artist": "A$AP Rocky", "album": "Long.Live.A$AP", "playing": True,
        "position": 12.0, "duration": 237, "cover": "al-1"}


class FakePlayer:
    """Stands in for the session's music player, keeping what it was asked and what it was read for."""

    def __init__(self):
        self.song = dict(SONG)
        self.names = ["Chill", "Gym"]
        self.picture = (b"jpeg-bytes", "image/jpeg")
        self.reads = []
        self.calls = []

    def controls(self, nova):
        controls = nova.controls()
        controls.now_playing = self._now_playing
        controls.cover = self._cover
        controls.playlists = self._playlists
        controls.playback = lambda action: self.calls.append(("playback", action))
        controls.play_playlist = lambda name: self.calls.append(("play_playlist", name))
        return controls

    def _playlists(self):
        self.reads.append("playlists")
        return list(self.names)

    def _now_playing(self):
        self.reads.append("now_playing")
        return self.song

    def _cover(self):
        self.reads.append("cover")
        return self.picture


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def with_player(nova, player, monkeypatch):
    """
    A NovaDevice for nova with a player, started, with the client it made - not yet connected.

    Its music thread reads the playlists as it starts and then waits so long between rounds that
    it never publishes: the tests call publish_music themselves, so they aren't racing it.
    """
    monkeypatch.setattr(ha_device, "MUSIC_EVERY", 3600)
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    device = NovaDevice("broker.local", 1883, "nova", "secret", player.controls(nova), node_id="nova_test",
                        client_factory=factory)
    device.start()
    # The music thread reads the playlists as it starts, so that's waited for - and any read
    # still going on is waited out - rather than the tests racing it.
    assert _eventually(lambda: "playlists" in player.reads)
    device.refresh_playlists()
    yield device, made[0]
    device.stop()


def _components(device):
    return device.discovery()["cmps"]


MUSIC_CONTROLS = {"playing": "sensor.nova_playing", "cover": "image.nova_playing_cover",
                  "play_pause": "button.nova_play_pause", "next_track": "button.nova_next_track",
                  "previous_track": "button.nova_previous_track", "playlist": "select.nova_playlist"}


def test_music_is_announced_only_with_a_player(started, with_player):
    plain, _ = started
    assert not set(MUSIC_CONTROLS) & set(_components(plain))

    device, _ = with_player
    controls = _components(device)
    assert {control: controls[control]["default_entity_id"] for control in MUSIC_CONTROLS} == MUSIC_CONTROLS
    assert controls["cover"]["image_topic"] == f"{BASE}/playing/cover"
    assert controls["cover"]["content_type"] == "image/jpeg"
    assert controls["playing"]["state_topic"] == f"{BASE}/playing"
    assert controls["playing"]["json_attributes_topic"] == f"{BASE}/playing/attributes"
    assert controls["next_track"]["command_topic"] == f"{BASE}/playing/next"
    assert controls["playlist"]["command_topic"] == f"{BASE}/playlist/set"
    assert controls["playlist"]["state_topic"] == f"{BASE}/playlist"
    assert "optimistic" not in controls["playlist"]
    assert len({control["unique_id"] for control in controls.values()}) == len(controls)


def test_no_playlist_select_without_playlists(nova, player):
    player.names = []
    device = NovaDevice("broker.local", 1883, "nova", "secret", player.controls(nova), node_id="nova_test",
                        client_factory=FakeClient)
    device.refresh_playlists()
    assert "playlist" not in _components(device)
    player.names = ["Chill", "Gym"]
    device.refresh_playlists()
    assert _components(device)["playlist"]["options"] == ["Chill", "Gym"]


def test_announcing_uses_the_playlists_last_read(with_player, player):
    device, _ = with_player
    # Announcing never reads them: only what the music thread last read is used, so a
    # changed list isn't in it until it's read again.
    player.names = ["Gym"]
    assert _components(device)["playlist"]["options"] == ["Chill", "Gym"]
    device.refresh_playlists()
    assert _components(device)["playlist"]["options"] == ["Gym"]


def test_announcing_never_reads_the_playlists(nova):
    called = []
    controls = nova.controls()
    controls.now_playing = lambda: None
    controls.playlists = lambda: called.append("playlists") or ["Chill"]
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=FakeClient)
    device.discovery()
    assert called == []


def test_a_changed_list_of_playlists_is_announced_again(with_player, player):
    device, client = with_player
    client.connects()
    device.refresh_playlists()
    assert len(client.sent(DISCOVERY)) == 1
    player.names = ["Chill"]
    device.refresh_playlists()
    assert len(client.sent(DISCOVERY)) == 2
    assert json.loads(client.sent(DISCOVERY)[-1][1])["cmps"]["playlist"]["options"] == ["Chill"]


def test_what_is_playing_is_sent_on_change_only(with_player):
    device, client = with_player
    client.connects()
    device.publish_music()
    device.publish_music()
    assert client.sent(f"{BASE}/playing") == [(f"{BASE}/playing", "playing", 1, True)]
    [(_, attributes, _, retained)] = client.sent(f"{BASE}/playing/attributes")
    assert retained and json.loads(attributes) == {
        "track": "Fashion Killa", "artist": "A$AP Rocky", "album": "Long.Live.A$AP", "duration": 237}

    device.controls.now_playing = lambda: {**SONG, "playing": False, "position": 99.0}
    device.publish_music()
    assert [payload for _, payload, _, _ in client.sent(f"{BASE}/playing")] == ["playing", "paused"]
    assert len(client.sent(f"{BASE}/playing/attributes")) == 1


def test_nothing_playing_is_idle(with_player):
    device, client = with_player
    device.controls.now_playing = lambda: None
    client.connects()
    device.publish_music()
    assert client.latest()[f"{BASE}/playing"] == "idle"
    assert json.loads(client.latest()[f"{BASE}/playing/attributes"]) == {}
    assert client.latest()[f"{BASE}/playing/cover"] == b""


def test_music_elsewhere_is_idle(with_player):
    # Spotify's now_playing is the account's playback wherever it is; Navidrome's has no "here".
    device, client = with_player
    client.connects()
    device.controls.now_playing = lambda: {**SONG, "here": False}
    device.publish_music()
    assert client.latest()[f"{BASE}/playing"] == "idle"
    assert json.loads(client.latest()[f"{BASE}/playing/attributes"]) == {}
    assert client.latest()[f"{BASE}/playing/cover"] == b""

    device.controls.now_playing = lambda: {**SONG, "here": True}
    device.publish_music()
    assert client.latest()[f"{BASE}/playing"] == "playing"
    device.controls.now_playing = lambda: {**SONG, "here": False}
    device.publish_music()
    assert client.latest()[f"{BASE}/playing"] == "idle"


def test_the_playlist_select_is_always_told_it_has_no_option(with_player):
    # So the same playlist can be chosen twice in a row: the select never keeps the last one.
    device, client = with_player
    client.connects()
    assert client.sent(f"{BASE}/playlist") == [(f"{BASE}/playlist", "None", 1, True)]


def test_the_cover_is_fetched_once_per_song(with_player, player):
    device, client = with_player
    client.connects()
    for _ in range(3):
        device.publish_music()
    assert player.reads.count("cover") == 1
    assert client.sent(f"{BASE}/playing/cover") == [(f"{BASE}/playing/cover", b"jpeg-bytes", 1, True)]

    player.song = {**SONG, "cover": "al-2"}
    player.picture = (b"png-bytes", "image/png")
    announced = len(client.sent(DISCOVERY))
    device.publish_music()
    device.publish_music()
    assert player.reads.count("cover") == 2
    assert client.latest()[f"{BASE}/playing/cover"] == b"png-bytes"
    # A new content type is announced with it.
    assert len(client.sent(DISCOVERY)) == announced + 1
    assert json.loads(client.sent(DISCOVERY)[-1][1])["cmps"]["cover"]["content_type"] == "image/png"

    # A song with no picture leaves none shown.
    player.song = {**SONG, "cover": None}
    device.publish_music()
    assert client.latest()[f"{BASE}/playing/cover"] == b""
    assert player.reads.count("cover") == 2


def test_the_state_thread_never_reads_the_music(nova, monkeypatch):
    # The music thread's own rounds are put off, so only the state thread could read it. Its
    # first read of the playlists is its job, so that one is allowed - and must not fail,
    # or it would log in whichever test is running by then.
    monkeypatch.setattr(ha_device, "MUSIC_EVERY", 3600)
    read = []
    controls = nova.controls()
    controls.now_playing = lambda: read.append("now_playing")
    controls.cover = lambda: read.append("cover")
    controls.playlists = lambda: read.append("playlists") or ["Chill"]
    made = []
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=lambda client_id: made.append(FakeClient(client_id)) or made[-1])
    device.start()
    try:
        assert _eventually(lambda: read == ["playlists"])
        made[0].connects()
        device.publish_state()
        assert made[0].sent(f"{BASE}/status")
        assert not made[0].sent(f"{BASE}/playing")
        assert read == ["playlists"]
    finally:
        device.stop()


def test_a_failing_player_is_logged_once(with_player, player, caplog):
    device, client = with_player
    client.connects()

    def broken():
        raise RuntimeError("Spotify's gone")

    device.controls.now_playing = broken
    with caplog.at_level(logging.ERROR, logger=ha_device.__name__):
        device.publish_music()
        device.publish_music()
    assert len(_complaints(caplog)) == 1


def test_a_failing_round_does_not_end_the_music_thread(nova, player, monkeypatch, caplog):
    monkeypatch.setattr(ha_device, "MUSIC_EVERY", 0.02)
    made = []
    device = NovaDevice("broker.local", 1883, "nova", "secret", player.controls(nova), node_id="nova_test",
                        client_factory=lambda client_id: made.append(FakeClient(client_id)) or made[-1])
    rounds = []

    def broken():
        rounds.append(1)
        raise RuntimeError("the broker's gone")

    device._announce_if_stale = broken
    with caplog.at_level(logging.ERROR, logger=ha_device.__name__):
        device.start()
        try:
            made[0].connects()
            assert _eventually(lambda: len(rounds) >= 4)
            assert any(thread.name == "ha-device-music" and thread.is_alive() for thread in threading.enumerate())
        finally:
            device.stop()
    assert len(_complaints(caplog)) == 1


def test_music_buttons_reach_the_player(with_player, player):
    device, client = with_player
    client.connects()
    assert {f"{BASE}/playing/play_pause", f"{BASE}/playing/next", f"{BASE}/playing/previous",
            f"{BASE}/playlist/set"} <= {topic for topic, _ in client.subscribed}
    client.delivers(f"{BASE}/playing/play_pause", "PRESS")
    client.delivers(f"{BASE}/playing/next", "PRESS")
    client.delivers(f"{BASE}/playing/previous", "PRESS")
    assert _eventually(lambda: len(player.calls) == 3)
    assert player.calls == [("playback", "play_pause"), ("playback", "next"), ("playback", "previous")]


def test_only_an_offered_playlist_is_played(with_player, player, nova):
    device, client = with_player
    client.connects()
    device.refresh_playlists()
    client.delivers(f"{BASE}/playlist/set", "Gone")
    client.delivers(f"{BASE}/playlist/set", "Gym")
    # A Stop queued after is carried out after them, so both have been looked at by then.
    client.delivers(f"{BASE}/stop", "PRESS")
    assert _eventually(lambda: ("stop",) in nova.calls)
    assert player.calls == [("play_playlist", "Gym")]


def test_music_commands_kept_by_the_broker_are_ignored(with_player, player):
    device, client = with_player
    client.connects()
    client.delivers(f"{BASE}/playing/next", "PRESS", retain=True)
    time.sleep(0.1)
    assert player.calls == []


def test_playlists_read_after_the_announcement_are_announced_again(nova, player, monkeypatch):
    # The broker answers before the music thread's first read: the announcement has no
    # playlists, and the read finishes before the device counts as connected, so nothing
    # announces it then. The music loop does, on its next round.
    monkeypatch.setattr(ha_device, "MUSIC_EVERY", 0.02)
    made = []
    controls = player.controls(nova)
    release = threading.Event()
    read = controls.playlists
    controls.playlists = lambda: release.wait(2) and read()
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=lambda client_id: made.append(FakeClient(client_id)) or made[-1])
    device.start()
    try:
        client = made[0]
        device._announce()
        assert "playlist" not in json.loads(client.sent(DISCOVERY)[-1][1])["cmps"]
        release.set()
        assert _eventually(lambda: device._playlists == ["Chill", "Gym"])
        assert len(client.sent(DISCOVERY)) == 1
        device._connected = True
        assert _eventually(lambda: len(client.sent(DISCOVERY)) == 2)
        assert json.loads(client.sent(DISCOVERY)[-1][1])["cmps"]["playlist"]["options"] == ["Chill", "Gym"]
        time.sleep(0.1)
        assert len(client.sent(DISCOVERY)) == 2
    finally:
        device.stop()


def test_a_cover_which_could_not_be_had_is_asked_for_again(with_player, player):
    device, client = with_player
    client.connects()
    player.picture = None
    device.publish_music()
    assert client.latest()[f"{BASE}/playing/cover"] == b""
    player.picture = (b"jpeg-bytes", "image/jpeg")
    device.publish_music()
    device.publish_music()
    assert player.reads.count("cover") == 2
    assert client.latest()[f"{BASE}/playing/cover"] == b"jpeg-bytes"


def test_playlists_which_could_not_be_read_keep_the_old_ones(with_player, player):
    device, client = with_player
    player.names = []
    device.refresh_playlists()
    assert _components(device)["playlist"]["options"] == ["Chill", "Gym"]
    player.names = ["Gym"]
    device.refresh_playlists()
    assert _components(device)["playlist"]["options"] == ["Gym"]


# Ask Nova

TYPED = "play Fashion Killa"


@pytest.fixture
def asked():
    """What was typed into Ask Nova, in the order it came."""
    return []


@pytest.fixture
def asking(nova, asked):
    """A started NovaDevice which can be asked things, with the client it made - no music."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    controls = dataclasses.replace(nova.controls(), ask=asked.append)
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=factory)
    device.start()
    try:
        yield device, made[0]
    finally:
        device.stop()


def test_ask_is_announced_only_when_nova_can_be_asked(started, asking):
    plain, _ = started
    assert "ask" not in _components(plain)
    assert f"{BASE}/ask/set" not in {topic for topic, _ in plain._subscriptions()}

    device, _ = asking
    ask = _components(device)["ask"]
    assert ask["p"] == "text"
    assert ask["name"] == "Ask Nova"
    assert ask["command_topic"] == f"{BASE}/ask/set"
    assert ask["state_topic"] == f"{BASE}/ask"
    # Whatever the state topic says, the box's state is empty.
    assert ask["value_template"] == ha_device.BOX_TEMPLATE
    assert ask["max"] == 255
    assert ask["icon"] == "mdi:keyboard-outline"
    assert ask["default_entity_id"] == "text.nova_ask"
    controls = _components(device)
    assert len({control["unique_id"] for control in controls.values()}) == len(controls)


def test_what_is_typed_reaches_nova_and_is_never_sent_back(asking, asked):
    device, client = asking
    client.connects()
    assert (f"{BASE}/ask/set", 1) in client.subscribed
    client.delivers(f"{BASE}/ask/set", TYPED)
    assert _eventually(lambda: asked == [TYPED])

    # Home Assistant's box is only ever sent the placeholder, which its template turns
    # into empty, so it never shows, nor keeps, what was typed. Not "": the broker takes
    # an empty retained message as forgetting it, and Home Assistant would never get it.
    device.publish_state()
    # The placeholder, then - once what was typed was taken - the flipped one, so Home
    # Assistant redraws the box empty.
    assert _eventually(lambda: client.sent(f"{BASE}/ask") == [(f"{BASE}/ask", "-", 1, True),
                                                                (f"{BASE}/ask", "--", 1, True)])
    assert not any("Fashion Killa" in topic or "Fashion Killa" in str(payload)
                   for topic, payload, _, _ in client.published)


def test_what_is_typed_is_logged_only_as_a_transcript(asking, asked, caplog):
    device, client = asking
    caplog.set_level(logging.INFO)
    client.connects()
    client.delivers(f"{BASE}/ask/set", TYPED)
    assert _eventually(lambda: asked == [TYPED])
    device.publish_state()

    heard = [record for record in caplog.records if "Fashion Killa" in record.getMessage()]
    assert heard
    assert all(getattr(record, "transcript", False) is TRANSCRIPT["transcript"] for record in heard)


def test_a_kept_or_empty_ask_is_ignored(asking, asked, nova):
    device, client = asking
    client.connects()
    client.delivers(f"{BASE}/ask/set", "hello", retain=True)
    client.delivers(f"{BASE}/ask/set", "   ")
    # Commands are carried out in the order they came, so once this one has been, an
    # ask above which was going to be has been too.
    client.delivers(f"{BASE}/stop", "PRESS")
    assert _eventually(lambda: ("stop",) in nova.calls)
    assert asked == []



@pytest.fixture
def said():
    """What was typed into Say on Nova, in the order it came."""
    return []


@pytest.fixture
def saying(nova, said):
    """A started NovaDevice which can be given words to say, with the client it made - no music."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    controls = dataclasses.replace(nova.controls(), say=said.append)
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=factory)
    device.start()
    try:
        yield device, made[0]
    finally:
        device.stop()


def test_say_is_announced_only_when_nova_can_be_given_words(started, saying):
    plain, _ = started
    assert "say" not in _components(plain)
    assert f"{BASE}/say/set" not in {topic for topic, _ in plain._subscriptions()}

    device, _ = saying
    say = _components(device)["say"]
    assert say["p"] == "text"
    assert say["name"] == "Say on Nova"
    assert say["command_topic"] == f"{BASE}/say/set"
    assert say["state_topic"] == f"{BASE}/say"
    assert say["value_template"] == ha_device.BOX_TEMPLATE
    assert say["max"] == 255
    assert say["icon"] == "mdi:bullhorn-outline"
    assert say["default_entity_id"] == "text.nova_say"
    controls = _components(device)
    assert len({control["unique_id"] for control in controls.values()}) == len(controls)


def test_words_to_say_reach_nova_and_are_never_sent_back(saying, said, caplog):
    device, client = saying
    caplog.set_level(logging.INFO)
    client.connects()
    assert (f"{BASE}/say/set", 1) in client.subscribed
    client.delivers(f"{BASE}/say/set", TYPED)
    assert _eventually(lambda: said == [TYPED])

    device.publish_state()
    assert _eventually(lambda: client.sent(f"{BASE}/say") == [(f"{BASE}/say", "-", 1, True),
                                                                (f"{BASE}/say", "--", 1, True)])
    assert not any("Fashion Killa" in topic or "Fashion Killa" in str(payload)
                   for topic, payload, _, _ in client.published)
    heard = [record for record in caplog.records if "Fashion Killa" in record.getMessage()]
    assert heard
    assert all(getattr(record, "transcript", False) is TRANSCRIPT["transcript"] for record in heard)


def test_kept_or_empty_words_to_say_are_ignored(saying, said, nova):
    device, client = saying
    client.connects()
    client.delivers(f"{BASE}/say/set", "hello", retain=True)
    client.delivers(f"{BASE}/say/set", "   ")
    client.delivers(f"{BASE}/stop", "PRESS")
    assert _eventually(lambda: ("stop",) in nova.calls)
    assert said == []


# Timers and alarms added from the Nova view, and its playlist tiles

def test_the_box_template_shows_a_box_empty_either_way():
    # "-" shows "" and "--" a zero-width space: both blank, and a change Home Assistant redraws
    # on - which a plain space wouldn't be, as Home Assistant strips what a template gives.
    assert ha_device.BOX_TEMPLATE == "{{ '\u200b' if value == '--' else '' }}"
    assert ha_device.ZERO_WIDTH.strip() == ha_device.ZERO_WIDTH != ""


def test_a_zero_width_space_left_in_a_box_is_dropped_from_what_was_typed(clocked):
    device, client, added = clocked
    client.connects()
    client.delivers(f"{BASE}/add_timer/set", "\u200b10")
    assert _eventually(lambda: added == [("timer", "10")])


@pytest.fixture
def clocked(nova):
    """A started NovaDevice which can add timers and alarms, with what each was given and its client."""
    added, made = [], []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    controls = dataclasses.replace(nova.controls(), add_timer=lambda text: added.append(("timer", text)),
                                   add_alarm=lambda text: added.append(("alarm", text)))
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=factory)
    device.start()
    try:
        yield device, made[0], added
    finally:
        device.stop()


def test_add_timer_and_alarm_are_announced_only_when_they_can_be(started, clocked):
    plain, _ = started
    assert not {"add_timer", "add_alarm"} & set(_components(plain))
    device, _, _ = clocked
    controls = _components(device)
    assert controls["add_timer"]["default_entity_id"] == "text.nova_add_timer"
    assert controls["add_alarm"]["default_entity_id"] == "text.nova_add_alarm"
    assert controls["add_timer"]["command_topic"] == f"{BASE}/add_timer/set"
    assert controls["add_alarm"]["state_topic"] == f"{BASE}/add_alarm"
    assert controls["add_timer"]["value_template"] == ha_device.BOX_TEMPLATE


def test_timers_and_alarms_typed_reach_nova_and_clear_the_box(clocked, nova):
    device, client, added = clocked
    client.connects()
    client.delivers(f"{BASE}/add_timer/set", "10")
    client.delivers(f"{BASE}/add_alarm/set", "07:00")
    client.delivers(f"{BASE}/add_alarm/set", "08:00", retain=True)
    client.delivers(f"{BASE}/add_timer/set", "  ")
    client.delivers(f"{BASE}/stop", "PRESS")
    assert _eventually(lambda: ("stop",) in nova.calls)
    assert added == [("timer", "10"), ("alarm", "07:00")]
    assert _eventually(lambda: client.sent(f"{BASE}/add_timer")[-1][1] == "--")
    assert client.sent(f"{BASE}/add_alarm")[-1][1] == "--"


def test_each_playlist_has_a_tile_which_plays_it(nova, player, monkeypatch):
    monkeypatch.setattr(ha_device, "MUSIC_EVERY", 3600)
    covers = {"Chill": (b"chill-cover", "image/png")}
    controls = player.controls(nova)
    controls.playlist_covers = lambda names: {name: covers.get(name) for name in names}
    made = []
    device = NovaDevice("broker.local", 1883, "nova", "secret", controls, node_id="nova_test",
                        client_factory=lambda client_id: made.append(FakeClient(client_id)) or made[-1])
    device.start()
    try:
        assert _eventually(lambda: "playlists" in player.reads)
        device.refresh_playlists()
        client = made[0]
        client.connects()
        tiles = _components(device)
        assert tiles["playlist_1"]["name"] == "Chill" and tiles["playlist_2"]["name"] == "Gym"
        assert tiles["playlist_1"]["default_entity_id"] == "button.nova_playlist_1"
        assert tiles["playlist_2_cover"]["default_entity_id"] == "image.nova_playlist_2"
        assert tiles["playlist_1_cover"]["content_type"] == "image/png"
        assert tiles["playlist_2"]["payload_press"] == "Gym"
        assert "playlist_3" not in tiles

        device.publish_music()
        device.publish_music()
        # Sent once, unchanged ones not again.
        assert client.sent(f"{BASE}/playlist/1/cover") == [(f"{BASE}/playlist/1/cover", b"chill-cover", 1, True)]
        assert client.sent(f"{BASE}/playlist/2/cover") == [(f"{BASE}/playlist/2/cover", b"", 1, True)]

        # A press plays the playlist its button names, wherever it is in the list now -
        # and nothing for a name no longer offered.
        player.names = ["Alpha", "Chill", "Gym"]
        device.refresh_playlists()
        client.delivers(f"{BASE}/playlist/2/play", "Gym")
        client.delivers(f"{BASE}/playlist/9/play", "Gone")
        assert _eventually(lambda: ("play_playlist", "Gym") in player.calls)
        assert [call for call in player.calls if call[0] == "play_playlist"] == [("play_playlist", "Gym")]

        # A tile which goes is sent as its platform alone once, so Home Assistant removes it,
        # and its cover is emptied; after that it's left out.
        player.names = ["Chill"]
        device.refresh_playlists()
        tiles = json.loads(client.sent(DISCOVERY)[-1][1])["cmps"]
        assert tiles["playlist_2"] == {"p": "button"} and tiles["playlist_2_cover"] == {"p": "image"}
        assert tiles["playlist_3"] == {"p": "button"} and tiles["playlist_1"]["name"] == "Chill"
        assert "playlist_2" not in _components(device)
        device.publish_music()
        assert client.sent(f"{BASE}/playlist/2/cover")[-1] == (f"{BASE}/playlist/2/cover", b"", 1, True)
    finally:
        device.stop()
