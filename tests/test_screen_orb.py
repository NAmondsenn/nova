"""
Screen Orb Test
Checks what the PC's orb shows from what the satellite is doing - listening while a
recording is sent, thinking until Nova's words start or its turn ends, speaking while
they play, and hidden otherwise - where it sits on the screen, and how it fades and
grows in and out over time, without any Windows: it is all plain Python.
"""

import threading
import time

import pytest

from voice_assistant.screen_orb import (
    FADE,
    GROW,
    LINGER,
    PLACES,
    THINKING_MOST,
    THINKING_SCALE,
    Fade,
    OrbStates,
    place,
)


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


# The work area of a 1920 x 1080 screen with a 40 px taskbar along the bottom, and the
# window's side for the medium orb at 100% scaling.
WORK_AREA = (0, 0, 1920, 1040)
SIDE = 111


def test_place_bottom_centre_sits_just_above_the_taskbar():
    assert place(WORK_AREA, SIDE, "bottom centre", 12) == (904, 917)


def test_place_each_corner():
    assert place(WORK_AREA, SIDE, "top left", 12) == (12, 12)
    assert place(WORK_AREA, SIDE, "top right", 12) == (1797, 12)
    assert place(WORK_AREA, SIDE, "bottom right", 12) == (1797, 917)
    assert place(WORK_AREA, SIDE, "bottom left", 12) == (12, 917)
    assert place(WORK_AREA, SIDE, "top centre", 12) == (904, 12)


def test_place_keeps_inside_a_work_area_with_the_taskbar_on_the_left():
    assert place((62, 0, 1920, 1080), SIDE, "bottom left", 12) == (74, 957)


def test_place_on_a_second_screen_left_of_the_main_one():
    assert place((-1920, 0, 0, 1040), SIDE, "bottom centre", 12) == (-1016, 917)


def test_an_unknown_place_is_bottom_centre():
    expected = place(WORK_AREA, SIDE, "bottom centre", 12)
    assert place(WORK_AREA, SIDE, "middle of nowhere", 12) == expected
    assert place(WORK_AREA, SIDE, "", 12) == expected


def test_every_place_on_the_settings_page_has_a_window_inside_the_work_area():
    left, top, right, bottom = WORK_AREA
    for where in PLACES:
        x, y = place(WORK_AREA, SIDE, where, 12)
        assert left <= x and x + SIDE <= right
        assert top <= y and y + SIDE <= bottom


def test_it_fades_in():
    fade = Fade()
    fade.want("listening", 0)
    look, opacity, scale = fade.step(0)
    assert look == "listening"
    assert opacity == pytest.approx(0, abs=0.01)
    assert scale == pytest.approx(0.6, abs=0.01)
    look, opacity, scale = fade.step(FADE)
    assert look == "listening"
    assert opacity == 1


def test_it_grows_in_with_a_little_overshoot_and_settles_at_one():
    fade = Fade()
    fade.want("speaking", 0)
    scales = [fade.step(i * 0.01)[2] for i in range(int(GROW * 100) + 1)]
    assert max(scales) > 1
    assert scales[-1] == 1
    # It is well on its way part way through, not just at the end.
    assert fade.step(GROW / 2)[2] > 0.9


def test_nothing_is_drawn_before_anything_is_wanted():
    fade = Fade()
    assert fade.step(0) is None
    fade.want("hidden", 1)
    assert fade.step(2) is None


def test_hidden_lingers_then_fades_then_is_gone():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("hidden", 1)
    assert fade.step(1 + LINGER - 0.01)[1] == 1
    assert 0 < fade.step(1 + LINGER + FADE / 2)[1] < 1
    assert fade.step(1 + LINGER + FADE + 0.01) is None


def test_hidden_keeps_the_last_look_and_size_while_it_goes():
    fade = Fade()
    fade.want("speaking", 0)
    fade.want("hidden", 1)
    look, _, scale = fade.step(1 + LINGER + FADE / 2)
    assert look == "speaking"
    assert scale == 1


def test_it_can_appear_again_once_gone():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("hidden", 1)
    assert fade.step(5) is None
    fade.want("speaking", 6)
    look, opacity, scale = fade.step(6)
    assert (look, opacity) == ("speaking", 0)
    assert scale == pytest.approx(0.6, abs=0.01)
    assert fade.step(6 + GROW)[1:] == (1, 1)


def test_coming_back_during_the_linger_doesnt_blink():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("hidden", 1)
    fade.want("listening", 1.3)
    for i in range(21):
        look, opacity, scale = fade.step(1 + i * 0.05)
        assert look == "listening"
        assert opacity == 1
        assert scale == 1


def test_coming_back_during_the_fade_takes_over_from_where_it_is():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("hidden", 1)
    midway = 1 + LINGER + FADE / 2
    before = fade.step(midway)[1]
    assert 0 < before < 1
    fade.want("speaking", midway)
    after = fade.step(midway)
    # The look changes at once, the opacity carries on from where it was, and the size,
    # which has long since grown, does not start growing again.
    assert after[0] == "speaking"
    assert after[1] == pytest.approx(before)
    assert after[2] == 1
    later = [fade.step(midway + i * 0.02)[1] for i in range(1, 30)]
    assert later == sorted(later)
    assert later[-1] == 1


def test_coming_back_during_the_grow_in_doesnt_restart_it():
    fade = Fade()
    fade.want("listening", 0)
    grown = fade.step(0.3)[2]
    fade.want("speaking", 0.3)
    assert fade.step(0.3)[2] == pytest.approx(grown)


def test_a_different_state_while_shown_switches_the_look_at_once():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("thinking", 2)
    assert fade.step(2)[0] == "thinking"
    fade.want("speaking", 3)
    assert fade.step(3)[0] == "speaking"
    assert fade.step(3)[1] == 1


def test_thinking_shrinks():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("thinking", 2)
    assert fade.step(2)[2] == 1
    assert fade.step(2 + GROW)[2] == pytest.approx(THINKING_SCALE, abs=0.01)


def test_thinking_from_nothing_grows_in_to_the_smaller_size():
    fade = Fade()
    fade.want("thinking", 0)
    assert fade.step(GROW)[2] == pytest.approx(THINKING_SCALE, abs=0.01)


def test_leaving_thinking_grows_back_to_full_size_from_wherever_it_is():
    fade = Fade()
    fade.want("thinking", 0)
    halfway = fade.step(GROW / 2)[2]
    fade.want("speaking", GROW / 2)
    assert fade.step(GROW / 2)[2] == pytest.approx(halfway)
    assert fade.step(GROW / 2 + GROW)[2] == 1


def test_thinking_gives_up_after_thinking_most():
    fade = Fade()
    fade.want("thinking", 0)
    assert fade.step(THINKING_MOST - 1)[1] == 1
    assert fade.step(THINKING_MOST + LINGER + FADE + 0.1) is None


def test_thinking_gives_up_when_stepped_only_once_it_is_long_over():
    fade = Fade()
    fade.want("thinking", 100)
    assert fade.step(100 + THINKING_MOST + LINGER + FADE + 0.1) is None


def test_thinking_most_counts_from_when_thinking_began():
    fade = Fade()
    fade.want("listening", 0)
    fade.want("thinking", 20)
    assert fade.step(20 + THINKING_MOST - 1)[1] == 1
    assert fade.step(20 + THINKING_MOST + LINGER + FADE + 0.1) is None


def test_the_thinking_limit_does_not_hide_what_follows_thinking():
    fade = Fade()
    fade.want("thinking", 0)
    fade.want("speaking", 10)
    assert fade.step(THINKING_MOST + LINGER + FADE + 5) == ("speaking", 1, 1)


def test_hidden_while_it_is_fading_from_the_thinking_limit_changes_nothing():
    fade = Fade()
    fade.want("thinking", 0)
    fade.step(THINKING_MOST + LINGER + FADE / 2)
    fade.want("hidden", THINKING_MOST + LINGER + FADE / 2)
    assert fade.step(THINKING_MOST + LINGER + FADE + 0.01) is None


def test_wanting_the_same_state_again_changes_nothing():
    fade = Fade()
    fade.want("thinking", 0)
    fade.want("thinking", THINKING_MOST - 1)
    assert fade.step(THINKING_MOST + LINGER + FADE + 0.1) is None
