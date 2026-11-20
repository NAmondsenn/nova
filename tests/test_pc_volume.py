"""
PC Volume Test
Checks how the PC satellite changes Windows' own volume and mute, against a fake of Core
Audio's endpoint - comtypes is never imported: it's only on the PC.
"""

import threading

import pytest

from voice_assistant import pc_volume, remote
from voice_assistant.pc_volume import SystemVolume


class FakeEndpoint:
    """The default output's volume, as 0 to 1, and its mute - noting the thread each call is made on."""

    def __init__(self, level=0.4, muted=False):
        self._level = level
        self._muted = muted
        self.threads = set()

    def level(self):
        self.threads.add(threading.current_thread().name)
        return self._level

    def set_level(self, level):
        self.threads.add(threading.current_thread().name)
        self._level = level

    def muted(self):
        return self._muted

    def set_mute(self, mute):
        self.threads.add(threading.current_thread().name)
        self._muted = mute


class FakeCoreAudio:
    def __init__(self, endpoint, microphone=None):
        self.made_on = threading.current_thread().name
        self._endpoint = endpoint
        self._microphone = microphone

    def endpoint(self):
        return self._endpoint

    def microphone(self):
        if self._microphone is None:
            raise OSError("no microphone")
        return self._microphone


@pytest.fixture
def endpoint():
    return FakeEndpoint()


@pytest.fixture
def microphone():
    return FakeEndpoint(level=0.8)


@pytest.fixture
def volume(endpoint, microphone):
    made = SystemVolume(lambda: FakeCoreAudio(endpoint, microphone))
    yield made
    made.close()


def test_a_level_is_set_and_kept_to_0_to_100(volume, endpoint):
    assert volume.change(level=30) == {"level": 30, "muted": False}
    assert volume.change(level=140) == {"level": 100, "muted": False}
    assert volume.change(level=-5) == {"level": 0, "muted": False}


def test_up_and_down_move_by_the_step_given_or_ten(volume, endpoint):
    assert volume.change(direction="up", step=5) == {"level": 45, "muted": False}
    assert volume.change(direction="down") == {"level": 35, "muted": False}
    endpoint._level = 0.97
    assert volume.change(direction="up", step=10) == {"level": 100, "muted": False}


def test_a_level_or_direction_change_also_unmutes(volume, endpoint):
    endpoint._muted = True
    assert volume.change(direction="up") == {"level": 50, "muted": False}
    endpoint._muted = True
    assert volume.change(level=20) == {"level": 20, "muted": False}


def test_mute_and_unmute_leave_the_level(volume, endpoint):
    assert volume.change(mute=True) == {"level": 40, "muted": True}
    assert volume.change(mute=False) == {"level": 40, "muted": False}


def test_nothing_asked_only_says_what_it_is(volume, endpoint):
    endpoint._muted = True
    assert volume.change() == {"level": 40, "muted": True}


def test_core_audio_is_only_used_on_its_own_thread(volume, endpoint):
    volume.change(level=50)
    volume.change(direction="down")
    assert volume._backend.made_on == "windows-volume"
    assert endpoint.threads == {"windows-volume"}


def test_a_failure_is_raised_to_the_caller(endpoint):
    def broken():
        raise OSError("no output device")

    endpoint.level = broken
    made = SystemVolume(lambda: FakeCoreAudio(endpoint))
    try:
        with pytest.raises(OSError):
            made.change(direction="up")
    finally:
        made.close()


def test_a_backend_which_cant_be_made_leaves_no_volume():
    def missing():
        raise ImportError("No module named 'comtypes'")

    made = SystemVolume(missing)
    try:
        assert made.available is False
        with pytest.raises(RuntimeError):
            made.change(level=10)
    finally:
        made.close()


def test_off_windows_there_is_no_volume(monkeypatch):
    monkeypatch.setattr(pc_volume.sys, "platform", "linux")
    made = SystemVolume()
    assert made.available is False
    assert made._owner is None


def test_a_change_gives_up_before_the_brain_stops_waiting():
    # So the satellite's own "couldn't" reaches the brain, rather than the brain's.
    assert pc_volume.JOB_WAIT < remote.COMPUTER_WAIT


def test_unmute_if_muted_unmutes_and_keeps_the_level(volume, endpoint):
    endpoint._muted = True
    assert volume.unmute_if_muted() is True
    assert endpoint._muted is False and endpoint._level == 0.4


def test_unmute_if_muted_changes_nothing_when_not_muted(volume, endpoint):
    assert volume.unmute_if_muted() is False
    assert endpoint._muted is False and endpoint._level == 0.4


def test_unmute_if_muted_without_a_volume_raises():
    made = SystemVolume(lambda: (_ for _ in ()).throw(OSError("none")))
    with pytest.raises(RuntimeError):
        made.unmute_if_muted()
    made.close()


# The microphone (20 Nov): "mute the mic" at the PC mutes Windows' own, for everything.

def test_the_microphone_is_muted_and_unmuted_on_its_own_thread(volume, endpoint, microphone):
    assert volume.mute_microphone(True) is True
    assert microphone._muted is True and microphone._level == 0.8
    assert volume.mute_microphone(False) is False
    assert microphone._muted is False
    assert microphone.threads == {"windows-volume"}
    # The output is left as it was.
    assert endpoint._muted is False and endpoint.threads == set()


def test_unmute_microphone_if_muted_only_unmutes_a_muted_one(volume, endpoint, microphone):
    assert volume.unmute_microphone_if_muted() is False
    microphone._muted = True
    assert volume.unmute_microphone_if_muted() is True
    assert microphone._muted is False and microphone._level == 0.8
    assert endpoint._muted is False


def test_no_microphone_is_raised_to_the_caller(endpoint):
    made = SystemVolume(lambda: FakeCoreAudio(endpoint))
    try:
        with pytest.raises(OSError):
            made.mute_microphone(True)
        with pytest.raises(OSError):
            made.unmute_microphone_if_muted()
        # The output still works.
        assert made.change(level=20) == {"level": 20, "muted": False}
    finally:
        made.close()


def test_the_microphone_without_a_volume_raises():
    made = SystemVolume(lambda: (_ for _ in ()).throw(OSError("none")))
    with pytest.raises(RuntimeError):
        made.mute_microphone(True)
    with pytest.raises(RuntimeError):
        made.unmute_microphone_if_muted()
    made.close()


def test_core_audio_asks_for_the_default_capture_endpoint_for_the_microphone():
    # eCapture and eConsole, through the same IAudioEndpointVolume as the output.
    asked = []

    class Enumerator:
        def GetDefaultAudioEndpoint(self, flow, role):
            asked.append((flow, role))
            return Device()

    class Device:
        def Activate(self, iid, context, params):
            return Activated()

    class Activated:
        def QueryInterface(self, interface):
            return "volume"

    core = pc_volume._CoreAudio.__new__(pc_volume._CoreAudio)
    core._enumerator, core._interface, core._clsctx = Enumerator(), type("I", (), {"_iid_": "iid"}), 0
    core.microphone()
    core.endpoint()
    assert asked == [(1, 0), (0, 0)]
