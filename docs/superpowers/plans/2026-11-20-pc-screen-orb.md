# Nova's Orb on the PC's Screen - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small copy of the clock's orb on the PC's screen while Nova listens, thinks and speaks there, and nothing otherwise.

**Architecture:** Pure numpy draws each frame of the orb (`orb_drawing.py`). A Windows-only window, owned by one thread of its own and made through ctypes, shows the frames see-through, topmost and click-through (`screen_orb.py`), with the fades, placement and the satellite's state rules as pure, tested pieces beside it. The satellite tells it what Nova is doing from events it already has; the brain only adds three settings and sends them with the state it already sends.

**Tech Stack:** Python 3, numpy (already on the PC), ctypes (Win32: user32, gdi32, shcore), pytest on the Pi.

**Spec:** `docs/superpowers/specs/2026-11-20-pc-screen-indicator-design.md`

## Global Constraints

- Nothing new to install on the PC or the Pi: numpy and the standard library only.
- The orb exists only on a push-to-talk satellite on Windows (`satellite.push_to_talk` and `_on_windows()`); everywhere else nothing is made, imported from Win32 or drawn.
- Nothing the satellite does - a press, the chime, sending audio, a reply - ever waits on the orb or fails because of it. Every call into it returns at once; any failure in it is logged once and the orb goes quiet.
- No CPU while hidden: no timer, no drawing.
- Shown in screenshots and screen shares (no `SetWindowDisplayAffinity`). Shown over games, anti-cheat ones included; no program list.
- Speaking is a `play_stream` of kind `reply` or `announcement` only. Never music, alarms, or any `play_sound` file (the press chime, an announcement's chime).
- Settings (config section `computer`, page section Satellites): `screen_orb` yes/no default `true`; `screen_orb_place` one of `bottom centre`, `bottom left`, `bottom right`, `top centre`, `top left`, `top right`, default `bottom centre`; `screen_orb_size` one of `small`, `medium`, `large`, default `medium`. Body sizes at 100% scaling: small 48, medium 64, large 96 px.
- The size is the bright body (the clock's 300 px blob). Every clock dimension is scaled by `k = body_px * dpi_scale / 300`; the window is the clock's aura, `ceil(520 * k)` px square.
- Code style: the repo's (memory note working-style): full-sentence comments saying why, British spelling, " - " dashes, Google-style docstrings, one commit per task.
- Tests: `NOVA_TREE=/home/nathaniel/work/Home-Assistant-indicator NOVA_CHECK=orb bash ~/work/Home-Assistant/.superpowers/tools/pi_test.sh <args>`. On CT 109: check `cat /proc/loadavg` first (wait if above 3), prefix with `nice -n 19 timeout 600`, one run at a time, no repeat runs. Never `ps` on the Pi. No `git stash`.

## Review Focus

1. A taskbar on the side, on top, or auto-hidden: the orb sits inside the work area, never under the taskbar or off the screen (Task 3, `test_place_keeps_inside_a_work_area_with_the_taskbar_on_the_left`).
2. A screen scaled to 150%: the orb is 1.5 times the pixels, so it looks the same size (Task 4, `test_the_frame_follows_the_screens_scaling`).
3. The brain lost, or no `allow` ever coming, while thinking: the orb must not stay up for good (Task 2 `test_losing_the_brain_hides_everything`, Task 3 `test_thinking_gives_up_after_thinking_most`).
4. A press over a reply (talked over), and a reply which arrives while a press is listening and is stopped: the orb goes straight to listening with no blink, and listening wins over speaking (Task 2 `test_listening_wins_over_speaking`, `test_a_press_over_a_reply_never_hides`).
5. The window can't be made (an RDP session, a locked desktop, a Win32 call failing): presses carry on, one log line, no exception out of `show` (Task 4 `test_a_backend_which_fails_is_logged_once_and_goes_quiet`).

---

### Task 1: Drawing the orb

**Files:**
- Create: `voice_assistant/orb_drawing.py`
- Test: `tests/test_orb_drawing.py`

**Interfaces:**
- Produces:
  - `SIZES: Dict[str, int] = {"small": 48, "medium": 64, "large": 96}`
  - `window_side(body_px: int) -> int` - `ceil(520 * body_px / 300)`
  - `class OrbPainter(body_px: int)` with attribute `side: int` and method `frame(state: str, dt: float, opacity: float, scale: float) -> np.ndarray` - `state` one of `"listening"`, `"thinking"`, `"speaking"`; `dt` seconds since the last frame (advances every phase); `opacity` 0-1; `scale` the whole orb's scale (grow-in, thinking's 0.82). Returns `(side, side, 4)` uint8, BGRA, premultiplied alpha.

- [ ] **Step 1: Write the failing tests** in `tests/test_orb_drawing.py` (numpy via `pytest.importorskip`):
  - `test_a_frame_is_square_bgra_bytes`: `OrbPainter(64).frame("listening", 0.03, 1, 1)` has shape `(window_side(64), window_side(64), 4)`, dtype uint8; `window_side(64) == 111`.
  - `test_colour_never_exceeds_alpha`: for each state, `(frame[..., :3] <= frame[..., 3:4]).all()`.
  - `test_the_corners_are_clear_and_the_middle_is_solid`: the four corner pixels have alpha 0; the centre pixel has alpha >= 230 and B, G, R each >= 200 (the white core).
  - `test_it_is_blue_and_violet_off_centre`: some pixel within `0.4 * side` of the centre has B > R + 40 (the blue lobe), and some has R > G + 40 and B > G + 40 (violet).
  - `test_opacity_nothing_draws_nothing`: `frame(..., opacity=0, ...)` is all zeros.
  - `test_time_moves_it`: two frames of `"listening"` 0.5 s apart differ (mean absolute difference > 1).
  - `test_each_state_moves_differently`: two painters given the same sequence of ten 0.1 s frames, one `"listening"`, one `"speaking"`, end on different frames.
  - `test_changing_state_doesnt_jump`: after ten `"listening"` frames, one `"speaking"` frame with `dt=0.03` differs from the last listening frame by a mean absolute difference under 6.
  - `test_scale_shrinks_it`: the count of pixels with alpha > 128 at `scale=0.82` is less than at `scale=1`.

- [ ] **Step 2: Run them to see them fail**

Run: `... pi_test.sh tests/test_orb_drawing.py`
Expected: FAIL, `ModuleNotFoundError: voice_assistant.orb_drawing`.

- [ ] **Step 3: Implement `orb_drawing.py`**

The clock's orb (`voice_assistant/web/display.css`, lines 402-606) drawn in numpy with every dimension times `k = body_px / 300` (the caller passes `body_px` already scaled for the screen). Layers, bottom to top: `.aura` (520 px circle), `.aura-inner` (330), the five `.lobe`s (176 x 146 ellipses, each on its `.orbit` round the centre, with its keyframe's translate/rotate/scale), the `.core` (100 px circle). Colours and stops exactly as display.css. A CSS `radial-gradient(ellipse|circle, ...)` with no size is "farthest-corner": for a w x h box its radii are `w/2 * sqrt(2)` and `h/2 * sqrt(2)`; stops are linear between the given percentages of that.

Compositing, in straight colour then premultiplied at the end. For each layer with straight colour `Cs` and alpha `as`, over what's built so far `Cb`, `ab`:

```
mixed = (1 - ab) * Cs + ab * B(Cb, Cs)      # B = Cs for the auras (normal),
                                            # Cb + Cs - Cb * Cs for lobes and core (screen)
co    = as * mixed + (1 - as) * ab * Cb     # premultiplied result
ao    = as + ab * (1 - as)
Cb    = co / ao  (where ao > 0),  ab = ao
```

Motion: every animation (each orbit's spin, each lobe's wobble, each aura's breath, the core's hum) keeps a phase in 0-1 that `frame` advances by `dt / period`, so a state change changes speed with no jump. Periods per state, from display.css (seconds):

| | o1 | o2 | o3 | o4 | o5 | lobe wobbles | aura | aura-inner | core hum |
|---|---|---|---|---|---|---|---|---|---|
| listening | 8 | 10 (reverse) | 12 | 9 (reverse) | 14 | 2.8, 3.4, 4, 3.1, 3.7 | 4.5 | 3 (reverse) | 3 |
| thinking | 1.4 | 1.8 (reverse) | 1.4 | 6 (reverse) | 11 | as listening | 1.6 | 3 (reverse) | 3 |
| speaking | 2.4 | 3 (reverse) | 3.6 | 2.7 (reverse) | 4 | all 0.6 | 0.9 | 0.6 (reverse) | 0.4 |

Keyframes `0%, 100%` and `50%` with `ease-in-out` (a smoothstep is close enough). `scale` and `opacity` multiply the whole orb (scale about the centre). Turning the keyframes into functions of time is the implementer's choice.

- [ ] **Step 4: Run them to see them pass**

Run: `... pi_test.sh tests/test_orb_drawing.py`
Expected: all pass.

- [ ] **Step 5: Commit** `voice_assistant/orb_drawing.py`, `tests/test_orb_drawing.py`: "Draw the clock's orb in numpy, for the PC's screen".

---

### Task 2: What the orb shows, from what the satellite does

**Files:**
- Create: `voice_assistant/screen_orb.py` (this task adds `OrbStates` only)
- Test: `tests/test_screen_orb.py`

**Interfaces:**
- Produces: `class OrbStates(on_change: Callable[[str], None])`, thread-safe (one lock), with `state: str` (`"listening"`, `"thinking"`, `"speaking"` or `"hidden"`) and:
  - `listening_started()` - a recording began being sent (press or follow-up)
  - `listening_ended(answered: bool)` - it ended; `answered` True for `stopped`, `pressed`, `limit`, False for `dropped` or an exception
  - `speaking_started()` / `speaking_ended()` - one of Nova's spoken streams began / ended (counted, so two at once need two ends)
  - `turn_ended()` - the brain's `allow`
  - `lost()` - the brain is gone: everything off
  - `on_change(state)` is called, outside the lock, only when `state` changes.
- The rule: listening if a recording is being sent; else speaking if any spoken stream plays; else thinking if a recording ended answered and neither speech nor `allow` has come since; else hidden.

- [ ] **Step 1: Write the failing tests**, each driving an `OrbStates` with a list as `on_change`:
  - `test_a_press_goes_listening_thinking_speaking_hidden`: `listening_started`, `listening_ended(True)`, `speaking_started`, `speaking_ended`, `turn_ended` gives changes `["listening", "thinking", "speaking", "hidden"]`.
  - `test_nothing_said_goes_from_thinking_to_hidden_at_allow`: `listening_started`, `listening_ended(True)`, `turn_ended` gives `["listening", "thinking", "hidden"]`.
  - `test_a_dropped_recording_hides`: `listening_ended(False)` after `listening_started` gives `["listening", "hidden"]`.
  - `test_after_speaking_there_is_no_thinking`: after `speaking_ended` with no `allow` yet, state is `"hidden"`, not `"thinking"`.
  - `test_a_follow_up_listens_again`: the press sequence to `speaking_ended`, then `listening_started` gives a final `"listening"`.
  - `test_listening_wins_over_speaking`: `listening_started`, `speaking_started` (a reply arriving while listening), state stays `"listening"`; `speaking_ended`, `listening_ended(True)` gives `"thinking"`.
  - `test_a_press_over_a_reply_never_hides`: `speaking_started`, `listening_started`, `speaking_ended` - changes are `["speaking", "listening"]`, no `"hidden"` between.
  - `test_two_streams_need_two_ends`: two `speaking_started`, one `speaking_ended` - still `"speaking"`.
  - `test_an_announcement_alone_speaks_then_hides`: `speaking_started`, `speaking_ended` gives `["speaking", "hidden"]`.
  - `test_losing_the_brain_hides_everything`: in any state, `lost()` gives `"hidden"`, and a later `speaking_ended` doesn't go negative (a fresh `speaking_started`/`speaking_ended` pair still ends hidden).

- [ ] **Step 2: Run them to see them fail**

Run: `... pi_test.sh tests/test_screen_orb.py`
Expected: FAIL, module or class missing.

- [ ] **Step 3: Implement `OrbStates`** in `voice_assistant/screen_orb.py` (module docstring: what the orb is, pointing at the spec), with a lock and the counters/flags the rule needs.

- [ ] **Step 4: Run them to see them pass**

- [ ] **Step 5: Commit**: "Work out what the PC's orb shows from what the satellite is doing".

---

### Task 3: Fades and placement

**Files:**
- Modify: `voice_assistant/screen_orb.py`
- Test: `tests/test_screen_orb.py`

**Interfaces:**
- Produces:
  - Constants: `LINGER = 0.6` (seconds a hide waits, in case something follows), `FADE = 0.4`, `GROW = 0.7`, `THINKING_SCALE = 0.82`, `THINKING_MOST = 30` (seconds thinking may last before it's hidden anyway), `MARGIN = 12` (px at 100% scaling between the window and the work area's edge), `PLACES = ("bottom centre", "bottom left", "bottom right", "top centre", "top left", "top right")`.
  - `place(work_area: Tuple[int, int, int, int], side: int, where: str, margin: int) -> Tuple[int, int]` - the window's top-left; `work_area` is (left, top, right, bottom) in pixels; an unknown `where` is `"bottom centre"`. Centre is `(left + right - side) // 2`; left `left + margin`; right `right - margin - side`; top `top + margin`; bottom `bottom - margin - side`.
  - `class Fade` with `want(state: str, now: float)` and `step(now: float) -> Optional[Tuple[str, float, float]]` - `(look, opacity, scale)` to draw, where `look` is the state whose motion to use, or None once fully hidden (nothing to draw, the window can go).
- Behaviour: a shown state appears at once with opacity rising 0 to 1 over `FADE` and scale from 0.6 to 1 over `GROW` (an ease-out with a little overshoot, as the clock's `cubic-bezier(0.2, 0.9, 0.3, 1.2)`); `"thinking"` eases its scale to `THINKING_SCALE`; `"hidden"` keeps the last look for `LINGER`, then fades out over `FADE`; any shown state during the linger or the fade takes over from where it is, with no restart of the grow; `"thinking"` wanted for longer than `THINKING_MOST` fades out as if hidden.

- [ ] **Step 1: Write the failing tests**:
  - `test_place_bottom_centre_sits_just_above_the_taskbar`: `place((0, 0, 1920, 1040), 111, "bottom centre", 12) == (904, 917)`.
  - `test_place_each_corner`: on `(0, 0, 1920, 1040)`, side 111, margin 12: top left `(12, 12)`, top right `(1797, 12)`, bottom right `(1797, 917)`, top centre `(904, 12)`.
  - `test_place_keeps_inside_a_work_area_with_the_taskbar_on_the_left`: `place((62, 0, 1920, 1080), 111, "bottom left", 12) == (74, 957)`.
  - `test_place_on_a_second_screen_left_of_the_main_one`: `place((-1920, 0, 0, 1040), 111, "bottom centre", 12) == (-1016, 917)`.
  - `test_an_unknown_place_is_bottom_centre`.
  - `test_it_fades_in`: `want("listening", 0)`; `step(0)` opacity 0 (or near), `step(FADE)` opacity 1, look `"listening"`.
  - `test_hidden_lingers_then_fades_then_is_gone`: shown at 0, `want("hidden", 1)`; at `1 + LINGER - 0.01` opacity still 1; at `1 + LINGER + FADE / 2` between 0 and 1; at `1 + LINGER + FADE + 0.01` `step` returns None.
  - `test_coming_back_during_the_linger_doesnt_blink`: hidden at 1, `want("listening", 1.3)`: every `step` from 1 to 2 in 0.05 s steps has opacity 1.
  - `test_thinking_shrinks`: after `want("thinking")` and `GROW` seconds, scale is `THINKING_SCALE` (to 0.01).
  - `test_thinking_gives_up_after_thinking_most`: `want("thinking", 0)`; `step(THINKING_MOST + LINGER + FADE + 0.1)` is None.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement `place` and `Fade`**
- [ ] **Step 4: Run them to see them pass**
- [ ] **Step 5: Commit**: "Fade the PC's orb in and out, and place it on the screen".

---

### Task 4: The window, on a thread of its own

**Files:**
- Modify: `voice_assistant/screen_orb.py`
- Test: `tests/test_screen_orb.py`

**Interfaces:**
- Consumes: `OrbPainter`, `SIZES`, `window_side` (Task 1); `Fade`, `place`, `PLACES`, `MARGIN` (Task 3).
- Produces: `class ScreenOrb(backend: Optional[Callable[[], object]] = None)`:
  - `available: bool` - the window could be made
  - `show(state: str)` - one of the four states; never waits
  - `configure(on: bool, place: str, size: str)` - never waits; `on=False` hides at once (no linger) and draws nothing until on again; an unknown `place` or `size` keeps the default
  - `close()` - ends the thread and destroys the window; waits at most 2 s
  - Backend interface (what `_Win32Window` provides and the tests fake), all called only on the orb's thread: `work_area() -> Tuple[int, int, int, int]` (the main screen's, now), `dpi_scale() -> float` (that screen's scaling, 1.0 = 100%), `put(frame: np.ndarray, x: int, y: int)` (show the frame there, topmost, without activating; re-assert topmost when it was hidden before), `hide()`, `pump()` (handle any waiting window messages), `destroy()`.
- Like `pc_volume.SystemVolume`: `backend=None` means `_Win32Window` on `sys.platform == "win32"` and no backend elsewhere (`available` False, every call a no-op). The thread makes the backend, then loops: while something is drawn, about every 1/30 s it pumps, takes any new state or settings from a queue, steps the `Fade`, draws a frame with `OrbPainter` (one per size and scaling, made again when either changes), works out the place afresh when the orb appears, and `put`s it; while nothing is drawn it calls `hide()` once and blocks on the queue (pumping every 0.5 s at most), drawing nothing.
- `_Win32Window`, via ctypes with every `argtypes`/`restype` set (handles as `c_void_p`, so nothing is cut to 32 bits): on its thread, `SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)`; a registered class and `CreateWindowExW` with `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` and `WS_POPUP`; frames through a 32-bit top-down DIB section and `UpdateLayeredWindow` with `BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)` and `ULW_ALPHA`; shown with `ShowWindow(SW_SHOWNOACTIVATE)` and `SetWindowPos(HWND_TOPMOST, ..., SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)`; `work_area` from `MonitorFromPoint((0, 0), MONITOR_DEFAULTTOPRIMARY)` and `GetMonitorInfoW`'s `rcWork`; `dpi_scale` from `GetDpiForMonitor(MDT_EFFECTIVE_DPI) / 96`; `pump` a `PeekMessageW(PM_REMOVE)` loop. No title, no taskbar button.

- [ ] **Step 1: Write the failing tests** with a `FakeBackend` (records `put` calls as `(frame.shape, x, y)`, `hide` and `destroy` calls; `work_area` `(0, 0, 1920, 1040)`; `dpi_scale` settable):
  - `test_off_windows_it_does_nothing`: `ScreenOrb()` on the Pi: `available` False; `show`, `configure`, `close` return without error.
  - `test_listening_draws_frames_bottom_centre`: `show("listening")`; within 1 s there are several `put`s, each `((111, 111, 4), 904, 917)`.
  - `test_hidden_stops_drawing`: `show("listening")`, then `show("hidden")`; within `LINGER + FADE + 0.5` s `hide` is called and no `put` comes after it for 0.5 s.
  - `test_the_frame_follows_the_screens_scaling`: `dpi_scale` 1.5: frames are `window_side(96)` square (64 x 1.5), placed with margin 18.
  - `test_configure_moves_resizes_and_switches_off`: `configure(True, "top right", "large")` then `show("listening")`: frames `window_side(96)` square at the top right; `configure(False, ...)`: `hide` at once, no more `put`s, and `show("speaking")` draws nothing.
  - `test_unknown_place_and_size_keep_the_defaults`.
  - `test_show_never_waits`: with a backend whose `put` sleeps 1 s, `show` returns within 0.05 s.
  - `test_a_backend_which_fails_is_logged_once_and_goes_quiet`: a backend whose `put` raises: one warning logged across several `show`s, no exception out of `show`; a backend whose making raises: `available` False.
  - `test_close_destroys_the_window_and_ends_the_thread`.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement `ScreenOrb` and `_Win32Window`**
- [ ] **Step 4: Run them to see them pass**
- [ ] **Step 5: Commit**: "Show the orb in a see-through, click-through window on the PC".

---

### Task 5: The brain's three settings, sent with the state

**Files:**
- Modify: `config.yaml` (section `computer`, after `search_site`, each with a comment), `voice_assistant/config.py` (`CHOICES` for place and size), `voice_assistant/settings_page.py` (`EDITABLE`, after `search_site`, all `"now"`), `voice_assistant/brain.py` (`SENT_IN_STATE` gains the three), `voice_assistant/remote.py` (`set_state`), `voice_assistant/session.py` (`send_state`), `voice_assistant/protocol.py` (the `state` line in the docstring)
- Test: `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_remote.py`, `tests/test_session.py`

**Interfaces:**
- Produces: `RemoteSatellite.set_state(..., screen_orb: Optional[Dict] = None)` adds `"screen_orb": {"on": bool, "place": str, "size": str}` to the `state` message when given. `Session.send_state` gives it only when `self.satellite.push_to_talk`, read from `config.section("computer")` as it sends, so a save reaches the PC at once.
- Page labels: `screen_orb` "Show Nova on the computer's screen while it listens and speaks"; `screen_orb_place` "Where on the screen"; `screen_orb_size` "Size on the screen".

- [ ] **Step 1: Write the failing tests**:
  - `tests/test_config.py::test_the_screen_orbs_place_and_size_are_choices`: `computer.screen_orb_place: middle` refused with "should be one of bottom centre, bottom left, bottom right, top centre, top left, top right, not 'middle'"; `screen_orb_size: huge` refused likewise; the defaults load as `True`, `"bottom centre"`, `"medium"`.
  - `tests/test_settings_page.py::test_the_screen_orb_settings_are_on_the_page_used_straight_away`: the three keys are shown with those labels, `screen_orb` as yes/no, the other two with their choices in that order; saving each says "Saved. It's used straight away."
  - `tests/test_remote.py::test_the_screen_orb_goes_with_the_state_only_when_given`: `set_state(volumes, False)` sends no `screen_orb`; with `screen_orb={"on": True, "place": "top left", "size": "small"}` it's in the message as given.
  - `tests/test_session.py::test_a_push_to_talk_satellite_is_sent_its_screen_orb`: a push-to-talk satellite gets `{"on": True, "place": "bottom centre", "size": "medium"}`; after `brain.config.apply({("computer", "screen_orb_size"): "large"})` and `send_state`, `"large"`; a wake-word satellite's state has no `screen_orb`.
  - `tests/test_brain.py` (or wherever `apply_settings` is tested): saving `computer.screen_orb_place` sends every connected satellite its state again.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** the settings, validation, `set_state` field, `send_state` and the protocol line
- [ ] **Step 4: Run them to see them pass**, then the page's JavaScript tests: `~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs` (from the worktree, niced, after the load check)
- [ ] **Step 5: Commit**: "Put the PC's orb settings on the settings page and send them with the state".

---

### Task 6: The satellite drives the orb

**Files:**
- Modify: `satellite.py`
- Test: `tests/test_satellite.py`

**Interfaces:**
- Consumes: `ScreenOrb`, `OrbStates`, `PLACES` (Tasks 2-4); `SIZES` (Task 1); the `screen_orb` state field (Task 5).
- Changes in `satellite.py`:
  - In `__init__`, beside `SystemVolume`: on a push-to-talk satellite on Windows, `orb = ScreenOrb()`, kept as `self.orb` if `orb.available`, else None; and `self._orb_states = OrbStates(self._show_orb)`, where `_show_orb(state)` calls `self.orb.show(state)` when there's an orb.
  - `_send_recording`: `self._orb_states.listening_started()` as it starts (after `microphone.hold()`); in its `finally`, `listening_ended(answered)` with `answered` True for a return of `"stopped"`, `"pressed"` or `"limit"`, False for `"dropped"` or an exception.
  - `_follow`: for `playing.kind in ("reply", "announcement")` and `playing.pieces is not None` (a stream, not a file), `speaking_started()`; `_report`: `speaking_ended()` for the same as it finishes.
  - `_allow`: `turn_ended()`.
  - `_on_connected(False)`: `lost()`.
  - `_take_state`: before the `if self.wake is None: return`, a `screen_orb` that is a dict whose `on` is a bool, `place` in `PLACES` and `size` in `SIZES` goes to `self.orb.configure(on, place, size)` (when there's an orb); anything else is ignored, logged once per distinct value as `longest_request` is.
  - `_clean_up`: `self.orb.close()` beside `system_volume.close()`.

- [ ] **Step 1: Write the failing tests** with a `FakeScreenOrb` (records `show` and `configure` calls, `available` True) monkeypatched over `satellite.ScreenOrb`, and `satellite._on_windows` patched to True, on a `pressable` satellite:
  - `test_a_press_shows_listening_thinking_speaking_then_hides`: press; brain `stop_listening`; brain `play_stream` kind `reply`, then `stream_end`; brain `allow`. The orb's `show` calls, in order: `listening`, `thinking`, `speaking`, `hidden`.
  - `test_the_press_chime_and_music_never_show_speaking`: a press's own chime, a `play_sound` kind `announcement`, and a `play_stream` kind `music` never produce `show("speaking")`.
  - `test_a_spoken_announcement_shows_speaking`: `play_stream` kind `announcement` with no press: `speaking`, then `hidden` when it ends.
  - `test_a_follow_up_shows_listening`: after a reply, brain `listen` gives `show("listening")`.
  - `test_the_screen_orb_setting_reaches_the_orb`: brain `state` with `screen_orb={"on": False, "place": "top left", "size": "small"}` gives `configure(False, "top left", "small")`; one with `place: "middle"` gives no call and one warning.
  - `test_losing_the_brain_hides_the_orb`.
  - `test_no_orb_on_a_wake_word_satellite_or_off_windows`: a wake-word satellite on Windows, and a push-to-talk one with `_on_windows` False, make no `ScreenOrb`.
  - `test_an_unavailable_orb_changes_nothing`: `available` False: a full press, reply and allow work as before.

- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement the wiring**
- [ ] **Step 4: Run them to see them pass**
- [ ] **Step 5: Commit**: "Show the orb on the PC while Nova listens, thinks and speaks there".

---

### Task 7: Decision record, the whole suite, and a check on the PC

**Files:**
- Create: `docs/decisions/<next free number>-the-orb-on-the-computers-screen.md` (check `ls docs/decisions` and GitHub's main for the next free number first; another session may have taken one)
- Modify: `docs/release-checklist.md` or wherever the release checks list lives (his hand checks below), if the repo has one

- [ ] **Step 1: Write the decision record**: why, what was chosen (approach 1) and why not the others, his calls (bottom centre, ~twice a taskbar icon, shown in screenshots and streams, shown over games), what isn't done (Mac, heard words, exclusive full screen).
- [ ] **Step 2: The whole suite, twice**: `... pi_test.sh` then `TZ_OVERRIDE=UTC ... pi_test.sh` (one after the other, never together). Expected: everything passes in both.
- [ ] **Step 3: Ruff**: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/orb && /tmp/nova-check-pytest/bin/ruff check .'`. Expected: clean.
- [ ] **Step 4: A check on the PC, if it's awake** (no window shows on his desktop from ssh, and nothing plays): copy the branch's `voice_assistant/orb_drawing.py` and `voice_assistant/screen_orb.py` to `%TEMP%\nova-orb-check\voice_assistant\` on the PC with `scp`, and run from there with the satellite's venv Python a short script that makes `ScreenOrb()`, prints `available`, shows `listening` for 2 s, `speaking` for 1 s, `hidden`, waits 1.5 s, closes it, and prints how long the frames took on average. Expected: `available True`, no traceback, frames well under 10 ms. Then delete the folder. Skip if the PC is asleep, and say so.
- [ ] **Step 5: Commit**: "Decision NNNN: Nova's orb on the computer's screen".

Deploy is not part of this plan: pushing to GitHub's main, restarting nova-backend and `scripts/deploy_pc.sh` are his named jobs. His hand checks after deploy: a press, a question with a follow-up, a reply, a spoken reminder; over a borderless game; each place and size from the settings page; a taskbar moved to the side.
