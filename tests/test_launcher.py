"""
Launcher Test
Checks the PC's launcher waits longer each time the satellite fails quickly, goes back
to the shortest wait after a run which lasted, forgets the last logon's running mark,
keeps what the satellite writes to stderr, to about 2 MB, lets only one launcher run, and that the
settings install.ps1 writes for the PC are ones Nova accepts. Also that it ends when Nova
is quit, starts it again at once when restarted, stays out of the way at logon when "Start with
Windows" is off, asks the running Nova to open its page when the Start menu's shortcut is used a
second time, and runs the satellite through Nova.exe when that exists.
"""

import importlib.machinery
import importlib.util
import http.server
import io
import json
import logging
import os
import re
import socket
import subprocess
import threading
import types

import pytest

import satellite
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

    The satellite's exit codes and how long each run lasts can be set before running:
    `run.codes` (default: 1 each time) and `run.lasts` (default: no time at all), and
    `run.stop_after` is the number of starts after which the launcher is stopped. Time is the
    test's own, so `run.waits` has each wait the launcher asked for.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    data = tmp_path / "nova" / "data"
    launcher = load_launcher()
    starts = []
    waits = []
    clock = [0.0]

    class Satellite:
        def __init__(self, command, cwd, env, stdin, stdout, stderr, creationflags):
            starts.append({"marked": (data / "running").exists(), "stderr": stderr, "command": command})
            self.stderr = io.BytesIO(f"start {len(starts)}\n".encode())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stderr.close()

        def wait(self):
            (data / "running").write_text("{}")
            if run.lasts:
                clock[0] += run.lasts.pop(0)
            return run.codes.pop(0) if run.codes else 1

    def sleep(seconds):
        waits.append(seconds)
        if len(starts) >= run.stop_after:
            raise Stop

    monkeypatch.setattr(launcher.subprocess, "Popen", Satellite)
    # Windows' own, which Python elsewhere doesn't have.
    monkeypatch.setattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    # The launcher's own clock, so the test's runs last as long as it says and the real one is left alone.
    monkeypatch.setattr(launcher, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))

    def run(argv=()):
        with pytest.raises(Stop):
            launcher.main(list(argv))
        return starts

    # For a test to fake Windows' kernel32 by, and to see how many starts there were.
    run.launcher = launcher
    run.starts = starts
    run.waits = waits
    run.codes = []
    run.lasts = []
    run.stop_after = 2
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
    run.launcher.main([])
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


def test_the_launcher_and_the_satellite_agree_on_the_exit_codes_and_the_mark():
    launcher = load_launcher()
    assert launcher.QUIT_CODE == satellite.QUIT_CODE == 10
    assert launcher.RESTART_CODE == satellite.RESTART_CODE == 11
    assert launcher.NO_LOGON_MARK == os.path.basename(satellite.NO_LOGON_MARK) == "no_logon_start"


def test_a_quit_ends_the_launcher(launched):
    run, data = launched
    run.codes = [10]
    # No Stop: the launcher returns by itself, without waiting or starting again.
    run.launcher.main([])
    assert len(run.starts) == 1
    assert run.waits == []
    assert "The satellite was quit, so the launcher ends" in (data / "logs" / "launcher.log").read_text()


def test_a_restart_starts_it_again_in_two_seconds_whatever_came_before(launched):
    run, data = launched
    # Three quick failures, so the next wait would be 16, then a restart after 5 seconds, then
    # another quick failure, which should start the climb again from the bottom.
    run.codes = [1, 1, 1, 11, 1]
    run.lasts = [5] * 5
    run.stop_after = 5
    run()
    assert run.waits == [2, 4, 8, 2, 2]


def test_a_restart_after_a_long_run_waits_two_seconds_too(launched):
    run, data = launched
    run.codes = [11, 1]
    run.lasts = [3600, 5]
    run()
    assert run.waits == [2, 2]


@pytest.mark.parametrize("code, lasted, wait", [(0, 5, 2), (1, 5, 2), (0, 100, 2), (1, 100, 2), (None, 5, 2)])
def test_any_other_exit_is_as_before(launched, code, lasted, wait):
    run, data = launched
    run.codes = [code, code]
    run.lasts = [lasted, lasted]
    run()
    # The second wait is where today's climb shows: doubled after a quick failure, not after a long run.
    assert run.waits == [wait, 4 if lasted < 60 else 2]
    assert f"The satellite exited (code {code}) after" in (data / "logs" / "launcher.log").read_text()


def test_at_logon_with_the_mark_it_exits_without_starting(launched):
    run, data = launched
    kernel32 = FakeKernel32()
    run.launcher._kernel32 = lambda: kernel32
    (data / "logs").mkdir(parents=True)
    (data / "no_logon_start").write_text("")
    (data / "running").write_text("{}")
    run.launcher.main(["--logon"])
    assert run.starts == []
    # Before anything else: no mutex taken, the running mark not touched.
    assert kernel32.made == []
    assert (data / "running").exists()
    log = (data / "logs" / "launcher.log").read_text()
    assert "Start with Windows" in log
    assert len(log.splitlines()) == 1


def test_without_logon_the_mark_is_ignored(launched):
    # The deploy's start and the Start menu's shortcut are the user's own wish to run Nova.
    run, data = launched
    (data / "logs").mkdir(parents=True)
    (data / "no_logon_start").write_text("")
    assert len(run([])) == 2
    run.starts.clear()
    assert len(run(["--open"])) == 2


def test_at_logon_without_the_mark_it_starts(launched):
    run, data = launched
    assert len(run(["--logon"])) == 2


class PressServer:
    """A local server answering the satellite's POST /open, noting what it was sent."""

    def __init__(self, status=204):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"line": self.requestline, "headers": dict(self.headers.items()),
                                       "body": self.rfile.read(length)})
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def press_server():
    servers = []

    def make(status=204):
        servers.append(PressServer(status))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def free_port():
    """A port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_second_launcher_with_open_asks_the_first(launched, press_server):
    run, data = launched
    server = press_server()
    (data / "logs").mkdir(parents=True)
    (data / "running").write_text(json.dumps({"started_at": 1.0, "press_port": server.port}))
    run.launcher._kernel32 = lambda: FakeKernel32(already=True)
    run.launcher.main(["--open"])
    assert run.starts == []
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["line"] == "POST /open HTTP/1.1"
    assert request["headers"]["X-Nova-Press"] == "1"
    assert request["headers"]["Host"] == f"127.0.0.1:{server.port}"
    assert request["body"] == b""
    # The running satellite's mark isn't this launcher's to take away.
    assert (data / "running").exists()


@pytest.mark.parametrize("argv", [["--logon"], []])
def test_a_second_launcher_without_open_only_exits(launched, press_server, argv):
    run, data = launched
    server = press_server()
    (data / "logs").mkdir(parents=True)
    (data / "running").write_text(json.dumps({"press_port": server.port}))
    run.launcher._kernel32 = lambda: FakeKernel32(already=True)
    run.launcher.main(argv)
    assert run.starts == []
    assert server.requests == []
    assert "already running" in (data / "logs" / "launcher.log").read_text()


def test_a_first_launcher_with_open_just_starts(launched, press_server):
    run, data = launched
    server = press_server()
    (data / "logs").mkdir(parents=True)
    (data / "running").write_text(json.dumps({"press_port": server.port}))
    assert len(run(["--open"])) == 2
    assert server.requests == []


@pytest.fixture
def ask(tmp_path, caplog):
    """ask_to_open of a fresh launcher for a data folder of the test's own, and that folder."""
    launcher = load_launcher()
    data = tmp_path / "data"
    data.mkdir()
    caplog.set_level(logging.INFO, logger="launcher")
    return launcher.ask_to_open, data


def test_open_asks_with_a_two_second_timeout_and_ignores_proxies(ask, monkeypatch, press_server):
    ask_to_open, data = ask
    server = press_server()
    # A proxy set for everything, which 127.0.0.1 mustn't be sent through.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    (data / "running").write_text(json.dumps({"press_port": server.port}))
    assert ask_to_open(str(data)) is True
    assert len(server.requests) == 1
    assert load_launcher().OPEN_TIMEOUT == 2


def test_open_with_no_running_mark_does_nothing(ask, caplog):
    ask_to_open, data = ask
    assert ask_to_open(str(data)) is False
    assert len(caplog.records) == 1


@pytest.mark.parametrize("mark", [
    "not json at all", "", "[1, 2]", "{}", '{"press_port": null}', '{"press_port": "10401"}',
    '{"press_port": 10401.5}', '{"press_port": true}', '{"press_port": 0}', '{"press_port": 65536}',
    '{"press_port": -1}',
])
def test_open_with_no_usable_port_does_nothing(ask, caplog, mark):
    ask_to_open, data = ask
    (data / "running").write_text(mark)
    assert ask_to_open(str(data)) is False
    assert len(caplog.records) == 1


def test_open_with_a_refused_connection_does_nothing(ask, caplog):
    ask_to_open, data = ask
    (data / "running").write_text(json.dumps({"press_port": free_port()}))
    assert ask_to_open(str(data)) is False
    assert len(caplog.records) == 1


def test_open_with_an_http_error_does_nothing(ask, caplog, press_server):
    ask_to_open, data = ask
    server = press_server(status=403)
    (data / "running").write_text(json.dumps({"press_port": server.port}))
    assert ask_to_open(str(data)) is False
    assert len(caplog.records) == 1


def test_open_with_a_listener_which_never_answers_gives_up(tmp_path, caplog, monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "OPEN_TIMEOUT", 0.2)
    caplog.set_level(logging.INFO, logger="launcher")
    # Accepts the connection (the backlog does) and never replies.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        (tmp_path / "running").write_text(json.dumps({"press_port": sock.getsockname()[1]}))
        assert launcher.ask_to_open(str(tmp_path)) is False
    assert len(caplog.records) == 1


def test_open_with_a_stranger_on_the_port_does_nothing(ask, caplog):
    # A stale running mark whose port another program has since taken, which answers with
    # something which isn't HTTP.
    ask_to_open, data = ask

    def answer():
        connection, _ = sock.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(b"garbage\r\n\r\n")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        (data / "running").write_text(json.dumps({"press_port": sock.getsockname()[1]}))
        assert ask_to_open(str(data)) is False
        thread.join(5)
    finally:
        sock.close()
    assert len(caplog.records) == 1


def test_unknown_arguments_are_ignored_with_one_log_line(launched):
    run, data = launched
    assert len(run(["--logon", "--sing", "extra"])) == 2
    log = (data / "logs" / "launcher.log").read_text()
    assert len([line for line in log.splitlines() if "--sing" in line]) == 1


def test_nova_exe_is_used_when_its_there(launched, tmp_path):
    run, data = launched
    scripts = tmp_path / "nova" / "app" / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "Nova.exe").write_text("")
    (scripts / "pythonw.exe").write_text("")
    starts = run()
    assert [start["command"][0] for start in starts] == [os.path.join(str(scripts), "Nova.exe")] * 2
    assert starts[0]["command"][1] == os.path.join(str(tmp_path), "nova", "app", "satellite.py")


def test_pythonw_is_used_when_it_isnt(launched, tmp_path):
    run, data = launched
    scripts = tmp_path / "nova" / "app" / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_text("")
    starts = run()
    assert [start["command"][0] for start in starts] == [os.path.join(str(scripts), "pythonw.exe")] * 2


def test_program_is_decided_by_the_file_alone(tmp_path):
    launcher = load_launcher()
    # Neither there (a fresh install's first start): pythonw, which is what install.ps1 puts in.
    assert launcher.program(str(tmp_path)) == os.path.join(str(tmp_path), "venv", "Scripts", "pythonw.exe")
