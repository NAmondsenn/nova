"""
Screen Orb Test
Checks what the PC's orb shows from what the satellite is doing - listening while a
recording is sent, thinking until Nova's words start or its turn ends, speaking while
they play, and hidden otherwise - without any Windows: the states are plain Python.
"""

import threading
import time

from voice_assistant.screen_orb import OrbStates


def _driven():
    """An OrbStates, and the list of every state it has told its on_change about."""
    changes = []
    return OrbStates(changes.append), changes


def _a_press_and_its_reply(states):
    """A recording sent and answered, Nova's words played, and the brain's turn done."""
    states.listening_started()
    states.listening_ended(True)
    states.speaking_started()
    states.speaking_ended()
    states.turn_ended()


def test_it_starts_hidden_and_says_nothing():
    states, changes = _driven()
    assert states.state == "hidden"
    assert changes == []


def test_a_press_goes_listening_thinking_speaking_hidden():
    states, changes = _driven()
    _a_press_and_its_reply(states)
    assert changes == ["listening", "thinking", "speaking", "hidden"]


def test_nothing_said_goes_from_thinking_to_hidden_at_allow():
    states, changes = _driven()
    states.listening_started()
    states.listening_ended(True)
    states.turn_ended()
    assert changes == ["listening", "thinking", "hidden"]


def test_a_dropped_recording_hides():
    states, changes = _driven()
    states.listening_started()
    states.listening_ended(False)
    assert changes == ["listening", "hidden"]


def test_after_speaking_there_is_no_thinking():
    states, changes = _driven()
    states.listening_started()
    states.listening_ended(True)
    states.speaking_started()
    states.speaking_ended()
    assert states.state == "hidden"
    assert changes[-1] == "hidden"


def test_a_follow_up_listens_again():
    states, changes = _driven()
    states.listening_started()
    states.listening_ended(True)
    states.speaking_started()
    states.speaking_ended()
    states.listening_started()
    assert states.state == "listening"
    assert changes[-1] == "listening"


def test_listening_wins_over_speaking():
    states, changes = _driven()
    states.listening_started()
    states.speaking_started()
    assert states.state == "listening"
    states.speaking_ended()
    states.listening_ended(True)
    assert states.state == "thinking"
    assert changes == ["listening", "thinking"]


def test_a_press_over_a_reply_never_hides():
    states, changes = _driven()
    states.speaking_started()
    states.listening_started()
    states.speaking_ended()
    assert changes == ["speaking", "listening"]


def test_two_streams_need_two_ends():
    states, changes = _driven()
    states.speaking_started()
    states.speaking_started()
    states.speaking_ended()
    assert states.state == "speaking"
    states.speaking_ended()
    assert changes == ["speaking", "hidden"]


def test_an_announcement_alone_speaks_then_hides():
    states, changes = _driven()
    states.speaking_started()
    states.speaking_ended()
    assert changes == ["speaking", "hidden"]


def test_losing_the_brain_hides_everything():
    for setting_up in (
        lambda s: s.listening_started(),
        lambda s: (s.listening_started(), s.listening_ended(True)),
        lambda s: (s.speaking_started(), s.speaking_started()),
        lambda s: (s.speaking_started(), s.listening_started()),
    ):
        states, changes = _driven()
        setting_up(states)
        states.lost()
        assert states.state == "hidden"
        assert changes[-1] == "hidden"


def test_a_stream_ending_after_the_brain_is_lost_does_not_go_negative():
    states, changes = _driven()
    states.speaking_started()
    states.lost()
    states.speaking_ended()
    del changes[:]
    states.speaking_started()
    assert states.state == "speaking"
    states.speaking_ended()
    assert states.state == "hidden"
    assert changes == ["speaking", "hidden"]


def test_a_recording_ending_after_the_brain_is_lost_does_not_start_thinking():
    # The recording's own thread can finish after the connection has gone, and no allow
    # would ever come to take the orb back down.
    states, changes = _driven()
    states.listening_started()
    states.lost()
    states.listening_ended(True)
    assert states.state == "hidden"
    assert changes == ["listening", "hidden"]


def test_on_change_only_hears_of_changes():
    states, changes = _driven()
    states.turn_ended()
    states.speaking_ended()
    states.lost()
    states.listening_started()
    states.listening_started()
    states.speaking_started()
    assert changes == ["listening"]


def test_on_change_is_called_outside_the_lock():
    # An on_change which asks for the state, or changes it, would wait for ever on a lock
    # held through the call.
    told = []
    states = None

    def on_change(state):
        told.append((state, states.state))
        if state == "speaking":
            states.listening_started()

    states = OrbStates(on_change)
    worker = threading.Thread(target=states.speaking_started, daemon=True)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert told == [("speaking", "speaking"), ("listening", "listening")]


def test_two_threads_changing_it_are_told_one_at_a_time_and_end_on_the_final_state():
    told = []
    inside = threading.Event()
    release = threading.Event()
    at_once = []
    now = [0]

    def on_change(state):
        now[0] += 1
        at_once.append(now[0])
        told.append(state)
        if state == "listening":
            inside.set()
            release.wait(5)
        now[0] -= 1

    states = OrbStates(on_change)
    first = threading.Thread(target=states.listening_started, daemon=True)
    first.start()
    assert inside.wait(2)
    # The second change is made while the first is still being told.
    second = threading.Thread(target=lambda: states.listening_ended(True), daemon=True)
    second.start()
    deadline = time.monotonic() + 2
    while states.state != "thinking" and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)
    assert at_once == [1]
    release.set()
    first.join(2)
    second.join(2)
    assert max(at_once) == 1
    assert told == ["listening", "thinking"]
    assert told[-1] == states.state
