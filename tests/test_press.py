"""
Press Test
Checks the local listener a push-to-talk satellite is pressed through: that it takes
one request and refuses everything else, before anything is called. It's a real server
on a free port of this machine's loopback address.
"""

import http.client
import threading
import time

import pytest

from voice_assistant.press import PRESS_DEBOUNCE, PressListener


@pytest.fixture
def listener():
    presses = []
    made = PressListener(0, lambda: presses.append(time.monotonic()))
    made.presses = presses
    made.start()
    yield made
    made.close()


def ask(listener, method="POST", path="/press", headers="default", host=None):
    """Sends one request, and returns the status it got."""
    if headers == "default":
        headers = {"X-Nova-Press": "1"}
    headers = dict(headers)
    connection = http.client.HTTPConnection("127.0.0.1", listener.port, timeout=5)
    try:
        # putrequest, so the Host is this test's to say.
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{listener.port}")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_post_with_the_header_presses(listener):
    assert ask(listener) == 204
    assert len(listener.presses) == 1


def test_without_the_header_nothing_happens(listener):
    assert ask(listener, headers={}) == 403
    assert ask(listener, headers={"X-Nova-Press": "0"}) == 403
    assert listener.presses == []


def test_a_foreign_host_is_refused(listener):
    assert ask(listener, host=f"evil.example:{listener.port}") == 403
    assert ask(listener, host="evil.example") == 403
    assert ask(listener, host="127.0.0.1:1") == 403
    assert listener.presses == []


def test_other_paths_and_methods_are_refused(listener):
    assert ask(listener, "GET", "/press") in (403, 404, 405)
    assert ask(listener, "POST", "/other") in (403, 404, 405)
    assert ask(listener, "POST", "/press/more") in (403, 404, 405)
    assert ask(listener, "POST", "/press?x=1") in (403, 404, 405)
    assert ask(listener, "OPTIONS", "/press") in (403, 404, 405)
    assert ask(listener, "PUT", "/press") in (403, 404, 405)
    assert ask(listener, "FOO", "/press") in (403, 404, 405, 501)
    assert listener.presses == []


def test_presses_close_together_count_once(listener):
    assert ask(listener) == 204
    time.sleep(0.1)
    # Answered like any other, so a key's script isn't told something went wrong.
    assert ask(listener) == 204
    assert len(listener.presses) == 1
    time.sleep(PRESS_DEBOUNCE)
    assert ask(listener) == 204
    assert len(listener.presses) == 2


def test_it_listens_on_loopback_only(listener):
    assert listener._server.server_address[0] == "127.0.0.1"


def test_a_press_which_fails_is_answered_500_and_the_next_still_counts(caplog):
    calls = []

    def on_press():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Broken")

    made = PressListener(0, on_press)
    made.start()
    try:
        with caplog.at_level("ERROR"):
            assert ask(made) == 500
        assert "A press failed" in caplog.text
        time.sleep(PRESS_DEBOUNCE)
        assert ask(made) == 204
        assert len(calls) == 2
    finally:
        made.close()


def test_the_port_is_the_one_bound_and_close_frees_it():
    made = PressListener(0, lambda: None)
    made.start()
    port = made.port
    assert port != 0
    made.close()
    # Closed twice, or never started, is fine - and the port can be had again.
    made.close()
    PressListener(0, lambda: None).close()
    again = PressListener(port, lambda: None)
    again.start()
    try:
        assert again.port == port
    finally:
        again.close()


def test_a_port_which_is_taken_raises(listener):
    with pytest.raises(OSError):
        PressListener(listener.port, lambda: None).start()


def test_it_stops_answering_once_closed(listener):
    port = listener.port
    listener.close()
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("POST", "/press")


def test_the_press_runs_on_the_listeners_thread(listener):
    seen = []
    listener._on_press = lambda: seen.append(threading.current_thread() is not threading.main_thread())
    ask(listener)
    assert seen == [True]

