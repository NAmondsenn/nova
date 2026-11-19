"""
Machine Test
What machine a satellite is running on, and where its Downloads folder is.
"""

import sys

import pytest

from voice_assistant import machine


@pytest.fixture
def no_model_file(monkeypatch, tmp_path):
    """Points the device-tree model file somewhere that doesn't exist."""
    monkeypatch.setattr(machine, "MODEL_FILE", str(tmp_path / "no-such-model"))


def test_windows_is_a_pc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert machine.machine_kind() == "pc"


def test_macos_is_a_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert machine.machine_kind() == "mac"


def test_a_raspberry_pi_is_a_pi(monkeypatch, tmp_path):
    model = tmp_path / "model"
    # The device tree ends the name with a NUL byte.
    model.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(machine, "MODEL_FILE", str(model))
    assert machine.machine_kind() == "pi"


def test_another_board_is_linux(monkeypatch, tmp_path):
    model = tmp_path / "model"
    model.write_bytes(b"Radxa ROCK 5B\x00")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(machine, "MODEL_FILE", str(model))
    assert machine.machine_kind() == "linux"


def test_other_linux_is_linux(monkeypatch, no_model_file):
    monkeypatch.setattr(sys, "platform", "linux")
    assert machine.machine_kind() == "linux"


def test_the_kinds_are_the_four_the_protocol_names():
    assert machine.MACHINES == ("pi", "pc", "mac", "linux")


def test_downloads_on_macos_is_home_downloads(monkeypatch, tmp_path):
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert machine.downloads_folder() == str(tmp_path / "Downloads")


def test_no_downloads_folder_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert machine.downloads_folder() is None


def test_a_file_called_downloads_is_not_a_folder(monkeypatch, tmp_path):
    (tmp_path / "Downloads").write_text("not a folder")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert machine.downloads_folder() is None


def test_linux_has_no_downloads_for_notes(monkeypatch, tmp_path, no_model_file):
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert machine.downloads_folder() is None


def test_windows_uses_the_folder_windows_names(monkeypatch, tmp_path):
    # A Downloads folder moved off the profile is still found, as Windows knows where it went.
    moved = tmp_path / "moved"
    moved.mkdir()
    profile = tmp_path / "profile"
    (profile / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(machine, "_windows_downloads_folder", lambda: str(moved))
    monkeypatch.setenv("USERPROFILE", str(profile))
    assert machine.downloads_folder() == str(moved)


@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_windows_falls_back_to_the_profile_folder(monkeypatch, tmp_path, error):
    (tmp_path / "Downloads").mkdir()

    def lookup_fails():
        raise error("no known folders here")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(machine, "_windows_downloads_folder", lookup_fails)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert machine.downloads_folder() == str(tmp_path / "Downloads")


def test_windows_with_no_downloads_folder_anywhere_is_none(monkeypatch, tmp_path):
    def lookup_fails():
        raise OSError("no known folders here")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(machine, "_windows_downloads_folder", lookup_fails)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert machine.downloads_folder() is None


@pytest.mark.skipif(sys.platform == "win32", reason="only a machine without Windows' libraries fails the lookup")
def test_the_real_lookup_has_nothing_to_ask_off_windows():
    # With no ctypes.windll to ask, it fails the way downloads_folder() expects.
    with pytest.raises(AttributeError):
        machine._windows_downloads_folder()
