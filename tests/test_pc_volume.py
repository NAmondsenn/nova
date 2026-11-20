"""
PC Volume Test
Checks how the PC satellite changes Windows' own volume and mute, against a fake of Core
Audio's endpoint - comtypes is never imported: it's only on the PC.
"""

import threading

import pytest

from voice_assistant import pc_volume
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
    def __init__(self, endpoint):
        self.made_on = threading.current_thread().name
        self._endpoint = endpoint

    def endpoint(self):
        return self._endpoint


@pytest.fixture
def endpoint():
    return FakeEndpoint()


@pytest.fixture
def volume(endpoint):
    made = SystemVolume(lambda: FakeCoreAudio(endpoint))
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
