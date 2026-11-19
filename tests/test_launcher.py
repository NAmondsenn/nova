"""
Launcher Test
Checks the PC's launcher waits longer each time the satellite fails quickly, goes back
to the shortest wait after a run which lasted, forgets the last logon's running mark,
keeps what the satellite writes to stderr, to about 2 MB, lets only one launcher run, and that the
settings install.ps1 writes for the PC are ones Nova accepts.
"""

import importlib.machinery
import importlib.util
import io
import logging
import os
import re
import subprocess

import pytest

from voice_assistant.config import Config

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "windows")


def load_launcher():
    """Imports launcher.pyw, which Python doesn't know the ending of."""
    path = os.path.join(SCRIPTS, "launcher.pyw")
    loader = importlib.machinery.SourceFileLoader("launcher", path)
    spec = importlib.util.spec_from_loader("launcher", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_each_quick_failure_waits_twice_as_long_up_to_five_minutes():
    backoff = load_launcher().Backoff()
    waits = [backoff.next_wait(5) for _ in range(11)]
    assert waits == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]


@pytest.mark.parametrize("lasted", [60, 61, 3600])
def test_a_run_of_a_minute_or_more_goes_back_to_two_seconds(lasted):
    backoff = load_launcher().Backoff()
    for _ in range(5):
        backoff.next_wait(1)
    assert backoff.next_wait(lasted) == 2
    # And the next quick failure starts the climb again from the bottom.
    assert backoff.next_wait(1) == 2
    assert backoff.next_wait(1) == 4


def test_just_under_a_minute_still_counts_as_failing_quickly():
    backoff = load_launcher().Backoff()
    assert backoff.next_wait(59.9) == 2
    assert backoff.next_wait(59.9) == 4


class Stop(Exception):
    """Ends the launcher's loop, which otherwise never does."""


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """
    Runs the launcher's main() in a LOCALAPPDATA of the test's own, with a satellite which
    writes to stderr and exits at once, leaving its running mark as a crash does, until it
    has been started twice. Returns what each start found, and the data folder.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    data = tmp_path / "nova" / "data"
    launcher = load_launcher()
    starts = []

    class Satellite:
        def __init__(self, command, cwd, env, stdin, stdout, stderr, creationflags):
            starts.append({"marked": (data / "running").exists(), "stderr": stderr})
            self.stderr = io.BytesIO(f"start {len(starts)}\n".encode())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stderr.close()

        def wait(self):
            (data / "running").write_text("{}")
            return 1

    def sleep(seconds):
        if len(starts) >= 2:
            raise Stop

    monkeypatch.setattr(launcher.subprocess, "Popen", Satellite)
    # Windows' own, which Python elsewhere doesn't have.
    monkeypatch.setattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(launcher.time, "sleep", sleep)

    def run():
        with pytest.raises(Stop):
            launcher.main()
        return starts

    # For a test to fake Windows' kernel32 by, and to see how many starts there were.
    run.launcher = launcher
    run.starts = starts
    yield run, data
    for name in ("launcher", "satellite-stderr"):
        for handler in list(logging.getLogger(name).handlers):
            logging.getLogger(name).removeHandler(handler)
            handler.close()


def test_a_fresh_launcher_forgets_the_last_logons_running_mark_but_not_a_crash(launched):
    # Logging off, a shutdown and a deploy all end the satellite without its clean-up, and
    # a fresh launcher follows each - so the brain isn't told of an unclean stop for them.
    run, data = launched
    (data / "logs").mkdir(parents=True)
    (data / "running").write_text("{}")
    starts = run()
    assert [start["marked"] for start in starts] == [False, True]


def test_the_satellites_stderr_is_added_to_a_log_of_its_own(launched):
    # pythonw has no console, so start-up errors before its own log would otherwise be lost.
    run, data = launched
    log = data / "logs" / "satellite-stderr.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"earlier\n")
    starts = run()
    assert [start["stderr"] for start in starts] == [subprocess.PIPE] * 2
    assert log.read_bytes() == b"earlier\nstart 1\nstart 2\n"
    # And not to the launcher's own.
    assert "start 1" not in (data / "logs" / "launcher.log").read_text()


@pytest.fixture
def stderr_log(tmp_path):
    """A stderr log of the launcher's kind, with a small limit, and the folder holding it."""
    launcher = load_launcher()
    yield launcher, launcher.open_stderr_log(str(tmp_path / "satellite-stderr.log"), most=1000), tmp_path
    for handler in list(logging.getLogger("satellite-stderr").handlers):
        logging.getLogger("satellite-stderr").removeHandler(handler)
        handler.close()


def test_a_satellite_which_keeps_writing_leaves_about_twice_the_limit_at_most(stderr_log):
    launcher, log, folder = stderr_log
    lines = b"".join(f"line {number:05d} {'x' * 40}\n".encode() for number in range(2000))
    launcher.copy_lines(io.BytesIO(lines), log)
    sizes = {path.name: path.stat().st_size for path in folder.iterdir()}
    assert sorted(sizes) == ["satellite-stderr.log", "satellite-stderr.log.1"]
    assert all(size <= 1000 for size in sizes.values())
    # The newest lines are the ones kept, whole.
    kept = (folder / "satellite-stderr.log").read_text().splitlines()
    assert kept[-1] == f"line 01999 {'x' * 40}"
    assert all(line.startswith("line ") for line in kept)


def test_a_satellite_which_never_writes_leaves_the_log_empty(stderr_log):
    launcher, log, folder = stderr_log
    launcher.copy_lines(io.BytesIO(b""), log)
    assert (folder / "satellite-stderr.log").read_bytes() == b""
    assert not (folder / "satellite-stderr.log.1").exists()


def test_bytes_which_arent_text_and_windows_line_ends_are_copied_without_failing(stderr_log):
    launcher, log, folder = stderr_log
    launcher.copy_lines(io.BytesIO(b"bad \xff byte\r\nlast, no end"), log)
    assert (folder / "satellite-stderr.log").read_text(encoding="utf-8") == "bad \ufffd byte\nlast, no end\n"


def test_the_settings_install_writes_are_ones_nova_accepts(tmp_path):
    with open(os.path.join(SCRIPTS, "install.ps1"), encoding="ascii") as file:
        script = file.read()
    written = re.search(r"@'\n(.*?)\n'@", script, re.DOTALL).group(1)
    (tmp_path / "config.yaml").write_text(written)
    section = Config(path=str(tmp_path / "config.yaml")).section
    assert section("backend")["address"] == "nova:10400"
    assert section("backend")["satellite"] == "pc"
    assert section("satellite")["trigger"] == "push_to_talk"
    assert section("audio")["echo_cancellation"] is False


class FakeKernel32:
    """Stands in for Windows' kernel32: makes a named mutex, saying whether one of that name was already there."""

    def __init__(self, already=False, handle=4242):
        self.already = already
        self.handle = handle
        self.made = []

    def CreateMutexW(self, security, owned, name):
        self.made.append((security, owned, name))
        return self.handle

    def GetLastError(self):
        return 183 if self.already else 0


def test_a_second_launcher_exits_without_starting_a_satellite(launched, caplog):
    # As opening the Nova shortcut from the Start menu would make, while the logon's runs.
    run, data = launched
    kernel32 = FakeKernel32(already=True)
    run.launcher._kernel32 = lambda: kernel32
    (data / "logs").mkdir(parents=True)
    # The running satellite's mark, which isn't this launcher's to take away.
    (data / "running").write_text("{}")
    run.launcher.main()
    assert run.starts == []
    assert (data / "running").exists()
    assert "already running" in (data / "logs" / "launcher.log").read_text()
    assert kernel32.made == [(None, False, "Local\\NovaSatelliteLauncher")]


def test_the_first_launcher_keeps_its_mutex_and_carries_on(launched):
    run, data = launched
    kernel32 = FakeKernel32()
    run.launcher._kernel32 = lambda: kernel32
    starts = run()
    assert len(starts) == 2
    assert kernel32.made == [(None, False, "Local\\NovaSatelliteLauncher")]
    # Kept for as long as the launcher runs, so the next one finds it.
    assert run.launcher._mutex == 4242


def test_a_mutex_which_cant_be_made_still_starts_the_satellite(launched):
    # Better two launchers than none.
    run, data = launched
    run.launcher._kernel32 = lambda: FakeKernel32(handle=None)
    assert len(run()) == 2
    assert "Couldn't" in (data / "logs" / "launcher.log").read_text()


def test_off_windows_there_is_no_mutex_and_the_launcher_carries_on(launched):
    # As on the Pi, where the tests run: there's no kernel32.
    run, data = launched
    assert run.launcher._kernel32() is None
    assert len(run()) == 2
