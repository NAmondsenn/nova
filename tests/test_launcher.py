"""
Launcher Test
Checks the PC's launcher waits longer each time the satellite fails quickly, goes back
to the shortest wait after a run which lasted, forgets the last logon's running mark,
keeps what the satellite writes to stderr, and that the settings install.ps1 writes for
the PC are ones Nova accepts.
"""

import importlib.machinery
import importlib.util
import logging
import os
import re

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
            starts.append({"marked": (data / "running").exists(), "stderr": getattr(stderr, "name", stderr),
                           "mode": getattr(stderr, "mode", None)})
            if hasattr(stderr, "write"):
                stderr.write(f"start {len(starts)}\n".encode())

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

    yield run, data
    for handler in list(logging.getLogger("launcher").handlers):
        logging.getLogger("launcher").removeHandler(handler)
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
    assert [(start["stderr"], start["mode"]) for start in starts] == [(str(log), "ab")] * 2
    assert log.read_bytes() == b"earlier\nstart 1\nstart 2\n"


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
