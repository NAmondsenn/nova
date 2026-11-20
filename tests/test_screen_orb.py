"""
Screen Orb Test
Checks what the PC's orb shows from what the satellite is doing - listening while a
recording is sent, thinking until Nova's words start or its turn ends, speaking while
they play, and hidden otherwise - where it sits on the screen, how it fades and grows
in and out over time, and how its window is drawn on a thread of its own, which
never keeps the satellite waiting. None of it needs Windows: the window is a fake
here.
"""

import logging
import threading
import time

import pytest

from voice_assistant.orb_drawing import window_side
from voice_assistant.screen_orb import (
    FADE,
    GROW,
    LINGER,
    PLACES,
    THINKING_MOST,
    THINKING_SCALE,
    Fade,
    OrbStates,
    ScreenOrb,
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


class FakeBackend:
    """
    Stands in for the orb's window: a 1920 x 1080 main screen with its taskbar along
    the bottom, keeping each frame put up and each hide and destroy in the order they
    came.
    """

    def __init__(self, scale=1.0):
        self.scale = scale
        self.puts = []
        self.events = []
        self._lock = threading.Lock()

    def work_area(self):
        return WORK_AREA

    def dpi_scale(self):
        return self.scale

    def put(self, frame, x, y):
        with self._lock:
            self.puts.append((frame.shape, x, y))
            self.events.append("put")

    def hide(self):
        with self._lock:
            self.events.append("hide")

    def pump(self):
        pass

    def destroy(self):
        with self._lock:
            self.events.append("destroy")

    def after_last_hide(self):
        """What has happened since the latest hide, or None if there has been none."""
        with self._lock:
            if "hide" not in self.events:
                return None
            last = len(self.events) - 1 - self.events[::-1].index("hide")
            return self.events[last + 1:]


def _until(happened, seconds):
    """Waits up to that long for something to happen, and says whether it did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if happened():
            return True
        time.sleep(0.01)
    return happened()


@pytest.fixture
def orbs():
    """Makes ScreenOrbs on fake windows, and closes each once the test is done."""
    made = []

    def make(backend):
        orb = ScreenOrb(lambda: backend)
        made.append(orb)
        return orb

    yield make
    for orb in made:
        orb.close()


def _orb_threads():
    """The orbs' own threads which are running."""
    return [thread for thread in threading.enumerate() if thread.name == "screen-orb"]


def test_off_windows_it_does_nothing():
    before = _orb_threads()
    orb = ScreenOrb()
    assert orb.available is False
    assert _orb_threads() == before
    orb.show("listening")
    orb.configure(False, "top left", "small")
    orb.close()


def test_listening_draws_frames_bottom_centre(orbs):
    window = FakeBackend()
    orb = orbs(window)
    assert orb.available
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 5, 1)
    assert set(window.puts) == {((111, 111, 4), 904, 917)}


def test_hidden_stops_drawing(orbs):
    window = FakeBackend()
    orb = orbs(window)
    orb.show("listening")
    assert _until(lambda: window.puts, 1)
    orb.show("hidden")
    assert _until(lambda: window.after_last_hide() is not None, LINGER + FADE + 0.5)
    time.sleep(0.5)
    assert window.after_last_hide() == []


def test_the_frame_follows_the_screens_scaling(orbs):
    window = FakeBackend(scale=1.5)
    orb = orbs(window)
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 3, 1)
    # The medium orb's 64 px body is 96 px at 150%, and the margin 18 px.
    side = window_side(96)
    assert set(window.puts) == {((side, side, 4), (1920 - side) // 2, 1040 - 18 - side)}


def test_configure_moves_resizes_and_switches_off(orbs):
    window = FakeBackend()
    orb = orbs(window)
    orb.configure(True, "top right", "large")
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 3, 1)
    side = window_side(96)
    assert set(window.puts) == {((side, side, 4), 1920 - 12 - side, 12)}

    # Off hides at once, well inside the linger a hide would wait, and nothing more is
    # drawn, whatever is shown.
    orb.configure(False, "top right", "large")
    assert _until(lambda: window.after_last_hide() is not None, LINGER / 2)
    orb.show("speaking")
    time.sleep(0.3)
    assert window.after_last_hide() == []

    # On again, it shows what is shown next, where and how big it is now asked to be.
    orb.configure(True, "bottom left", "small")
    orb.show("listening")
    assert _until(lambda: window.after_last_hide(), 1)
    small = window_side(48)
    assert window.puts[-1] == ((small, small, 4), 12, 1040 - 12 - small)


def test_unknown_place_and_size_keep_the_defaults(orbs):
    window = FakeBackend()
    orb = orbs(window)
    orb.configure(True, "middle of nowhere", "huge")
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 3, 1)
    assert set(window.puts) == {((111, 111, 4), 904, 917)}


class SlowBackend(FakeBackend):
    """A window which takes a second to put up each frame."""

    def __init__(self):
        super().__init__()
        self.putting = threading.Event()

    def put(self, frame, x, y):
        self.putting.set()
        time.sleep(1)
        super().put(frame, x, y)


def test_show_never_waits(orbs):
    window = SlowBackend()
    orb = orbs(window)
    orb.show("listening")
    assert window.putting.wait(1)
    for told in (lambda: orb.show("thinking"), lambda: orb.show("speaking"),
                 lambda: orb.configure(True, "top left", "small"), lambda: orb.show("hidden")):
        start = time.monotonic()
        told()
        assert time.monotonic() - start < 0.05


class BrokenBackend(FakeBackend):
    """A window which Windows refuses every frame of, as it may while the PC is locked, until it is back."""

    def __init__(self):
        super().__init__()
        self.tries = 0
        self.refusing = True

    def put(self, frame, x, y):
        self.tries += 1
        if self.refusing:
            raise OSError("refused")
        super().put(frame, x, y)


def _warnings(caplog):
    """The warnings the orb has logged."""
    return [record for record in caplog.records
            if record.name == "voice_assistant.screen_orb" and record.levelno == logging.WARNING]


def test_a_backend_which_keeps_failing_is_logged_once_and_never_given_up_on(orbs, caplog):
    caplog.set_level(logging.INFO)
    window = BrokenBackend()
    orb = orbs(window)
    assert orb.available
    # Each showing fails and is dropped, so each show is a showing of its own - a day of
    # spoken reminders while the PC is locked, say. Every one is tried, for one frame
    # each, and only the first failure is logged.
    for shown in range(1, 16):
        orb.show("listening")
        assert _until(lambda shown=shown: window.tries >= shown, 1), shown
    time.sleep(0.1)
    assert window.tries == 15
    assert "destroy" not in window.events
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage()
    assert not [record for record in caplog.records
                if record.name == "voice_assistant.screen_orb" and record.levelno == logging.INFO]

    # Once the desktop is back, as when the PC is unlocked, the next showing is drawn.
    window.refusing = False
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 3, 1)
    assert set(window.puts) == {((111, 111, 4), 904, 917)}

    # A window which can't be made at all leaves the orb unavailable, which is said once.
    caplog.clear()

    def refused():
        raise OSError("no desktop")

    orb = ScreenOrb(refused)
    assert orb.available is False
    orb.show("listening")
    orb.configure(True, "top left", "small")
    orb.close()
    assert len(_warnings(caplog)) == 1


class FailsOnceBackend(FakeBackend):
    """
    A window whose first frame Windows refuses, as it may while the desktop is away (a
    remote session left disconnected, a graphics driver resetting), and which works
    once it is back.
    """

    def __init__(self):
        super().__init__()
        self.refused = threading.Event()

    def put(self, frame, x, y):
        if not self.refused.is_set():
            self.refused.set()
            raise OSError("the desktop is away")
        super().put(frame, x, y)


def test_a_showing_which_fails_is_dropped_and_the_next_one_draws(orbs, caplog):
    caplog.set_level(logging.WARNING)
    window = FailsOnceBackend()
    orb = orbs(window)
    orb.show("listening")
    assert window.refused.wait(1)
    # The failed showing is taken down and dropped, not tried frame after frame.
    assert _until(lambda: "hide" in window.events, 1)
    time.sleep(0.2)
    assert window.puts == []
    orb.show("listening")
    assert _until(lambda: len(window.puts) >= 3, 1)
    assert set(window.puts) == {((111, 111, 4), 904, 917)}
    assert "destroy" not in window.events
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage()


def test_close_destroys_the_window_and_ends_the_thread():
    before = _orb_threads()
    window = FakeBackend()
    orb = ScreenOrb(lambda: window)
    orb.show("listening")
    assert _until(lambda: window.puts, 1)
    start = time.monotonic()
    orb.close()
    assert time.monotonic() - start < 2
    assert window.events[-1] == "destroy"
    assert _orb_threads() == before
    # Anything told it after it is closed is ignored.
    orb.show("speaking")
    orb.configure(False, "top left", "small")
    orb.close()
    assert window.events.count("destroy") == 1
