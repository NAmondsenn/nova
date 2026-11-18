"""
Launcher Test
Checks the PC's launcher waits longer each time the satellite fails quickly, goes back
to the shortest wait after a run which lasted, and that the settings install.ps1 writes
for the PC are ones Nova accepts.
"""

import importlib.machinery
import importlib.util
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
