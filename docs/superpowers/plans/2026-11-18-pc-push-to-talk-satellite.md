# The PC as a Push-to-Talk Satellite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Windows PC join Nova's brain as a satellite that listens only when Numpad 3 is pressed, plays replies and music itself, hands its timers to the Pi, and shares stopping, pausing and volume with the rest of its room.

**Architecture:** The Pi's `satellite.py` gains a `push_to_talk` trigger: no wake word, a local-only press listener, and sound streams opened only when needed. The hello says the satellite is push-to-talk; the brain keeps each satellite's kind and last room, hands timers from a push-to-talk satellite to an always-on one in its room, and acts room-wide on stopping, pausing and volume. A Windows launcher, installer and a CT 109 deploy script put it on the PC.

**Tech Stack:** Python 3 (the satellite's existing packages: numpy, sounddevice, soundfile, soxr, pyyaml, python-dotenv), `http.server` for the press, PowerShell and Task Scheduler on Windows, pytest on the Pi.

**Spec:** `docs/superpowers/specs/2026-11-18-pc-push-to-talk-satellite-design.md`

## Global Constraints

- The Pi's behaviour is unchanged while it is the only satellite in its room: `satellite.trigger` defaults to `"wake_word"`, every existing test passes unchanged except where a task says so, and the `wake` message carries no new field unless the recording was pressed.
- No new third-party package anywhere. The PC installs only: numpy, soundfile, sounddevice, soxr, python-dotenv, pyyaml (versions from `requirements.lock`).
- The microphone stream on the PC is open only while recording; the speaker stream closes after `IDLE_CLOSE_SECONDS = 5` with nothing sounding. `LIMIT_SECONDS` and `MOST_SECONDS` still bound every recording.
- The press listener binds `127.0.0.1` only, accepts only `POST /press` with header `X-Nova-Press: 1` and a `Host` of `127.0.0.1:<port>`, and answers nothing else.
- Satellite name for the PC: `pc`. Brain address: `nova:10400`. Press port: `satellite.press_port`, default `10401`.
- Building and testing on CT 109: check `cat /proc/loadavg` first and wait while the first figure is above 3; every run prefixed `nice -n 19 timeout 600`; one suite at a time; no repeat runs.
- Tests: `NOVA_TREE=/home/nathaniel/work/Home-Assistant-pc NOVA_CHECK=pc nice -n 19 timeout 600 bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh <tests>`, then again with `TZ_OVERRIDE=UTC`. Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/pc && /tmp/nova-check-pytest/bin/ruff check .'`.
- Nothing is deployed, pushed or merged by any task. Never `git stash`. Work in `/home/nathaniel/work/Home-Assistant-pc` on branch `pc-satellite`.
- The project's style: docstrings and comments in plain British English, as the surrounding code writes them; no em dashes.

## Review Focus

1. **Numpad 3 held down** (key repeat sends many presses): presses within `PRESS_DEBOUNCE = 0.4` s of the last are ignored. Test in Task 3.
2. **A web page posting to `127.0.0.1:10401/press`** (a browser can't add `X-Nova-Press` cross-origin; DNS rebinding changes `Host`): refused with 403, no press. Tests in Task 3.
3. **The PC wakes from sleep, or the default microphone or speakers change** (headset plugged in): each press opens a fresh stream on the current default device. Test in Task 4.
4. **The brain goes away mid-recording, or is down when he presses:** the press ends in the recorded "I can't reach the server", never a hang. Test in Task 3.
5. **Music paused by him on the PC** (a paused playback sits in the speaker): the speaker stream still closes after 5 s, so the PC can sleep, and reopens when the music resumes. Test in Task 4.

---

### Task 1: Push-to-talk in the handshake and the wake message

**Files:**
- Modify: `voice_assistant/protocol.py` (docstring 1-47, `greet` 163, `admit` 196-240)
- Modify: `voice_assistant/satellite_link.py` (`SatelliteLink.__init__` 180, `_connect` 334)
- Modify: `voice_assistant/remote.py` (`RemoteSatellite.__init__` 451, `_woken` 715)
- Test: `tests/test_protocol.py`, `tests/test_remote.py`, `tests/test_satellite_link.py`

**Interfaces:**
- Produces: `greet(..., speaker=None, push_to_talk: bool = False)`, sending `"push_to_talk": true` in hello only when True. `admit(...)` returns the existing dict plus `"push_to_talk": bool` (False when absent; anything not a bool refused like a bad `speaker`). `SatelliteLink(..., speaker=None, push_to_talk: bool = False)`. `RemoteSatellite.push_to_talk: bool`. `RemoteSatellite._woken` puts `"pressed": True` in the wake dict only when the message has `"pressed": true`; a pressed wake keeps `pre_roll` as sent (0). `audio_end` reason gains `"pressed"`.

- [ ] **Step 1: Write the failing tests**
  - `test_protocol.py::test_hello_says_push_to_talk_only_when_it_is`: greet with `push_to_talk=True` → the hello dict has `push_to_talk is True`; with the default → no `push_to_talk` key.
  - `test_protocol.py::test_admit_reads_push_to_talk`: `_admitted(push_to_talk=True)["push_to_talk"] is True`; `_admitted()["push_to_talk"] is False`; `push_to_talk="yes"` → refused (same error path as a bad speaker).
  - `test_remote.py::test_a_pressed_wake_is_marked`: a `wake` with `pressed: true, pre_roll: 0` → the queued wake has `pressed is True` and `pre_roll == 0`; an ordinary wake has no `pressed` key.
  - `test_remote.py::test_the_satellite_says_it_is_push_to_talk`: `RemoteSatellite(stream, hello_with_push_to_talk).push_to_talk is True`, and False for an older hello.
  - `test_satellite_link.py::test_the_link_sends_push_to_talk_in_hello`.
- [ ] **Step 2: Run them, expect failures** (`tests/test_protocol.py tests/test_remote.py tests/test_satellite_link.py`).
- [ ] **Step 3: Implement** as in Interfaces; update the protocol docstring's `hello`, `wake` and `audio_end` lines.
- [ ] **Step 4: Run the three files in London and UTC, expect PASS; ruff clean.**
- [ ] **Step 5: Commit** `Say in the hello and the wake when a satellite is push-to-talk`.

### Task 2: The satellite's push-to-talk setting, without the wake word

**Files:**
- Modify: `config.yaml` (new `satellite:` section after `backend:`), `voice_assistant/config.py` (`CHOICES`, `LIMITS`)
- Modify: `voice_assistant/wake_word.py:14` (import)
- Modify: `satellite.py` (`__init__` 170-284, `_listen_forever` 339, `_keep_clip`, `_muted_since`, `_take_state` 658-689, `_check_voice_at` 691-709, `run` 299-301)
- Test: `tests/test_satellite.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 1's `SatelliteLink(push_to_talk=...)`.
- Produces: config `satellite.trigger` (`"wake_word"` | `"push_to_talk"`, default `"wake_word"`), `satellite.press_port` (number, whole, 1024-65535, default `10401`). `Satellite.push_to_talk: bool`. In push-to-talk mode `self.wake is None` and `self.clips is None`, and the link is made with `push_to_talk=True`. The idle loop in push-to-talk mode is `_wait_for_presses()` (Task 3 fills in the press; here it only serves follow-ups and `_interrupted`).

- [ ] **Step 1: Write the failing tests**
  - `test_config.py::test_satellite_trigger_is_one_of_two`: `"push_to_talk"` accepted, `"button"` refused.
  - `test_satellite.py::test_push_to_talk_makes_no_wake_word_or_clips`: `make(satellite={"trigger": "push_to_talk"})` → `WakeWordDetector` and `WakeClips` fakes never constructed, even with `wake_clips={"keep": True}`; the brain's hello has `push_to_talk is True`.
  - `test_satellite.py::test_push_to_talk_takes_state_without_a_wake_word`: the brain sends `state` with `wake_confidence` and `voice_check`; no exception, volumes and mute are stored.
  - `test_satellite.py::test_wake_word_satellite_hello_is_unchanged`: default `make()` → hello has no `push_to_talk`.
- [ ] **Step 2: Run them, expect failures.**
- [ ] **Step 3: Implement.** `wake_word.py`: `try: from openwakeword.model import Model` / `except ImportError: Model = None`, and `WakeWordDetector.__init__` raises `RuntimeError("openwakeword isn't installed")` if `Model is None`, so `satellite.py` imports cleanly without it and `satellite.WakeWordDetector` / `satellite.WakeClips` stay module names for the tests. Every use of `self.wake` and `self.clips` in `satellite.py` is guarded for None (mute is then kept in `self.state` only).
- [ ] **Step 4: Run `tests/test_satellite.py tests/test_config.py tests/test_wake_word.py tests/test_end_to_end.py` in London and UTC; ruff.**
- [ ] **Step 5: Commit** `Give the satellite a push-to-talk setting which loads no wake word`.

### Task 3: The press

**Files:**
- Create: `voice_assistant/press.py`
- Modify: `satellite.py` (`_woken` 413-465 refactored, `_send_recording` 478-523, `_clean_up` 856)
- Test: `tests/test_press.py` (new), `tests/test_satellite.py`

**Interfaces:**
- Consumes: Task 2's `push_to_talk`, `satellite.press_port`.
- Produces: `class PressListener` in `press.py`: `__init__(self, port: int, on_press: Callable[[], None])`, `start(self) -> None` (binds `127.0.0.1:port`, `ThreadingHTTPServer` on a daemon thread), `close(self) -> None`, `port` property (the bound port, so tests can pass 0). Module constant `PRESS_DEBOUNCE = 0.4`. `Satellite._pressed(self) -> None` (the `on_press`). `_woken` becomes `_begin(self, heard_at: int, start: int, pressed: bool = False) -> None`, used by the wake word (`pressed=False`, unchanged behaviour) and the press.

What a press does (`_pressed`), by state:
- idle (nothing recording, no reply playing): play `listening.wav` at `self.state.volume("general")` and `_begin(position, position, pressed=True)`, sending `wake` with `pressed=True, pre_roll=0`.
- a reply playing: the same, with `talked_over` computed as `_begin` already does.
- recording: end it now: set the recording's stop event and send `audio_end` with `reason="pressed"`.
- the fallback ringer ringing: dismiss, as the wake word does.
- mute: ignored (a press is deliberate).

- [ ] **Step 1: Write the failing tests**
  - `test_press.py::test_a_post_with_the_header_presses`: POST `/press` with `X-Nova-Press: 1` → 204, `on_press` called once.
  - `test_press.py::test_without_the_header_nothing_happens` → 403, not called.
  - `test_press.py::test_a_foreign_host_is_refused`: `Host: evil.example:10401` → 403, not called.
  - `test_press.py::test_other_paths_and_methods_are_refused`: GET `/press`, POST `/other`, OPTIONS → 403/404/405, not called.
  - `test_press.py::test_presses_close_together_count_once`: two POSTs 0.1 s apart → one call; a third after `PRESS_DEBOUNCE` → two.
  - `test_press.py::test_it_listens_on_loopback_only`: the server's address is `127.0.0.1`.
  - `test_satellite.py::test_a_press_starts_a_recording`: push-to-talk satellite, `_pressed()` → brain gets `{"type":"wake","talked_over":False,"pre_roll":0,"recording":"wake-1","pressed":True}` then `audio`; `listening.wav` played locally.
  - `test_satellite.py::test_a_second_press_ends_the_recording`: press, some audio, press → `audio_end` with `reason == "pressed"`.
  - `test_satellite.py::test_a_press_over_a_reply_stops_it_and_listens`: reply playing → it is stopped, `wake` has `talked_over is True`.
  - `test_satellite.py::test_a_press_while_muted_still_listens`.
  - `test_satellite.py::test_a_press_with_no_brain_gets_the_phrase` (Review Focus 4): no brain → the phrase plays, no hang; and brain dropped mid-recording → the same.
  - `test_satellite.py::test_a_press_while_the_fallback_rings_dismisses_it`.
  - Existing wake tests (exact dicts at 358, 927, 955) still pass unchanged.
- [ ] **Step 2: Run them, expect failures.**
- [ ] **Step 3: Implement** `press.py` (refuse anything not matching before calling `on_press`; debounce inside the listener) and the satellite side; start the listener in `run()` only in push-to-talk mode, close it in `_clean_up`.
- [ ] **Step 4: Run `tests/test_press.py tests/test_satellite.py tests/test_end_to_end.py` in London and UTC; ruff.**
- [ ] **Step 5: Commit** `Start and end a push-to-talk recording on a local press`.

### Task 4: Sound streams opened only when needed

**Files:**
- Modify: `voice_assistant/microphone.py` (`Microphone.__init__` 51-97, `_open`, `_read`, `close`)
- Modify: `voice_assistant/speaker.py` (`Speaker.__init__` 181-215, `_fill`, `play` 355-360, `close`)
- Modify: `satellite.py` (construction 202-203; recordings hold the microphone)
- Test: `tests/test_microphone.py`, `tests/test_speaker.py`, `tests/test_satellite.py`

**Interfaces:**
- Produces: `Microphone(..., always_open: bool = True)`; with False the constructor opens nothing, and `hold(self) -> None` / `release(self) -> None` (counted) open a fresh `sd.InputStream` on the first hold and close it on the last release. `position` keeps counting across opens (the gap is not filled with silence; a reader started after `hold()` reads only new audio). A stall-recovery while not held never reopens. `Speaker(..., always_open: bool = True)`; with False the stream opens on `play`/`play_stream`/`play_file` or an unpause, and closes once nothing unpaused and unfinished has sounded for `IDLE_CLOSE_SECONDS = 5`. The satellite builds both with `always_open=not self.push_to_talk`, and holds the microphone for each recording and follow-up (`_send_recording`, `_send_follow_up`), releasing in `finally`.

- [ ] **Step 1: Write the failing tests**
  - `test_microphone.py::test_an_on_demand_microphone_opens_only_when_held`: `always_open=False` → no `FakeInputStream` until `hold()`; closed after `release()`; two holds then one release → still open.
  - `test_microphone.py::test_each_hold_opens_a_fresh_stream` (Review Focus 3): hold, release, hold → two streams created, the second with the current default device.
  - `test_microphone.py::test_reading_after_a_reopen_gets_new_audio`: position after the second hold is ≥ before; a reader started then reads the new stream's samples.
  - `test_speaker.py::test_an_on_demand_speaker_closes_when_quiet`: `always_open=False` → no stream until `play`; after the playback ends and 5 s pass (fake clock) → closed; `play` again → reopened.
  - `test_speaker.py::test_paused_music_lets_the_speaker_close` (Review Focus 5): a stream playback paused → closed after 5 s; unpausing → reopened and it carries on from where it was.
  - `test_satellite.py::test_push_to_talk_opens_the_microphone_only_while_recording`.
  - Existing microphone, speaker and satellite tests pass unchanged (always-open stays the default).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.** The idle check runs from the speaker's existing timing (no new thread if one already ticks; else one daemon timer).
- [ ] **Step 4: Run `tests/test_microphone.py tests/test_speaker.py tests/test_satellite.py` in London and UTC; ruff.**
- [ ] **Step 5: Commit** `Open the push-to-talk satellite's microphone and speaker only when needed`.

### Task 5: The brain knows each satellite's kind and room

**Files:**
- Create: `voice_assistant/satellite_rooms.py`
- Modify: `voice_assistant/brain.py` (`__init__` near 270, `_start_session` 1090-1143), `voice_assistant/session.py` (`_look_up_device` 1262-1284, `_answer`/`_handle_interaction` 583-681, `_not_heard` 918), `voice_assistant/settings_page.py` (`_satellites` 596-618) and its page script
- Test: `tests/test_satellite_rooms.py` (new), `tests/test_brain.py`, `tests/test_session.py`, `tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 1's `RemoteSatellite.push_to_talk`, the wake dict's `pressed`.
- Produces: `class SatelliteRooms` in `satellite_rooms.py`, kept in `DATA_DIR/satellite_rooms.json` with `storage.save_json`: `__init__(self, path: str)`, `seen(self, name: str, push_to_talk: bool) -> None`, `set_area(self, name: str, area: Optional[str]) -> None` (None doesn't overwrite a known area), `push_to_talk(self, name: str) -> bool`, `area(self, name: str) -> Optional[str]`, `room_of(self, name: str, among: Iterable[str]) -> List[str]` (every name in `among` with the same known area, `name` first; just `[name]` when its area isn't known), `always_on_in_room(self, name: str, among: Iterable[str]) -> Optional[str]` (the first in `room_of` that isn't push-to-talk and isn't `name`). `Brain.rooms: SatelliteRooms`, `Brain.room_of(self, name: str) -> List[str]` (over `self.satellites()`), `Brain.room_sessions(self, name: str) -> List[Session]` (connected sessions of `room_of`, own first). Session: a `pressed` wake plays no `listening.wav` from the brain, and a pressed recording with no speech plays no `not_heard.wav`. `SettingsPage._satellites` gives each satellite `"push_to_talk": bool`, and the page shows "push-to-talk" after its name in the satellite list.

- [ ] **Step 1: Write the failing tests**
  - `test_satellite_rooms.py`: `test_kind_and_area_survive_a_restart` (new instance, same file); `test_an_unknown_area_is_a_room_of_its_own`; `test_room_of_lists_the_same_area_first_self`; `test_always_on_in_room_skips_push_to_talk_and_self`; `test_none_keeps_the_known_area`.
  - `test_brain.py::test_a_connecting_satellite_is_remembered_as_push_to_talk`.
  - `test_session.py::test_the_room_found_in_home_assistant_is_kept` (`find_device` sets `area` → `brain.rooms.set_area` called); add `rooms` to the `brain` fixture's SimpleNamespace.
  - `test_settings_page.py::test_the_satellite_list_marks_push_to_talk`.
  - `test_session.py::test_a_pressed_wake_plays_no_listening_sound` and `test_a_pressed_wake_with_nothing_said_says_nothing`; the existing listening-sound tests (2174) still pass for ordinary wakes.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the five files in London and UTC; ruff.**
- [ ] **Step 5: Commit** `Keep each satellite's kind and room on the brain`.

### Task 6: Timers and alarms set at a push-to-talk satellite ring on its room's always-on one

**Files:**
- Modify: `voice_assistant/brain.py` (new `ring_on`), `voice_assistant/actions.py` (`__init__` 285-287, `set_timer` near 762, `set_alarm` near 861), `voice_assistant/session.py:226` (ActionExecutor construction)
- Test: `tests/test_brain.py`, `tests/test_actions.py`

**Interfaces:**
- Consumes: Task 5's `Brain.rooms`, `Brain.room_of`.
- Produces: `Brain.ring_on(self, name: str) -> str`: `name` if it isn't push-to-talk; else `rooms.always_on_in_room(name, self.satellites())`; else `self.default_satellite()` if that isn't `name`; else `name`. `ActionExecutor(..., ring_on: Callable[[str], str] = lambda name: name)`; `set_timer` and `set_alarm` pass `satellite=self.ring_on(self.satellite)`. When the chosen satellite's area differs from the asker's (or either is unknown and they differ), the reply gains " It'll ring on the {chosen}." (satellite name).

- [ ] **Step 1: Write the failing tests**
  - `test_brain.py::test_ring_on`: four cases: wake-word satellite → itself; PC with the Pi in the same area (Pi disconnected) → the Pi; PC alone in its area, default is the Pi → the Pi; PC the only satellite → the PC.
  - `test_actions.py::test_a_timer_set_at_the_pc_rings_on_the_pi`: `clock.set_timer` receives `satellite="bedroom"`; the reply has no "It'll ring" (same room).
  - `test_actions.py::test_a_timer_handed_to_another_room_says_so`: reply ends "It'll ring on the kitchen."
  - `test_actions.py::test_an_alarm_set_at_the_pc_rings_on_the_pi`.
  - Existing set_timer/set_alarm tests unchanged (default `ring_on` is the identity).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/test_brain.py tests/test_actions.py tests/test_clock.py` in London and UTC; ruff.**
- [ ] **Step 5: Commit** `Hand timers and alarms set at a push-to-talk satellite to its room`.

### Task 7: Stopping, pausing and volume act on the whole room

**Files:**
- Modify: `voice_assistant/clock_ringing.py` (`_rings_on` 382, `is_ringing` 368, `ringing_state` 399, `dismiss` 466)
- Modify: `voice_assistant/conversation.py` (110-112, 201, 210-219, 310), `voice_assistant/actions.py` (`dismiss_timer` 792, `control_playback` 675, `_unnamed_volume_category` 982, `_change_music_volume` 995), `voice_assistant/session.py` (`_converse` 544-581, `_answer` 599, ActionExecutor and Conversation construction)
- Test: `tests/test_clock.py`, `tests/test_conversation.py`, `tests/test_actions.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: Task 5's `Brain.room_of`, `Brain.room_sessions`.
- Produces: in `clock_ringing`, `satellite` may be `None`, a `str`, or a list of names (rings on any of them). `Conversation(..., room: Callable[[], List[str]] = None)`; where it now passes `satellite=self.satellite` to the clock it passes `satellite=self._room()` (`[self.satellite]` when `room` is None). `ActionExecutor(..., room_players: Callable[[], List] = None)` returning the music players of the room's connected sessions, own first (`[self.music]` when None). Session builds both from `self.brain.room_of(name)` / `room_sessions(name)`.

Rules (the spec's "The room acts as one"):
- Ringing: `is_ringing`, `ringing_state`, `dismiss` and `dismiss_timer` act on every satellite in the room.
- A wake or press: `_converse` holds `music` on every session in `room_sessions` (`hold()` on each at the start, `release()` on each in `finally`, the same ones).
- Pause, stop and volume with no category: act on every room player that is playing now; if none is playing, the own player (today's behaviour).
- Resume ("play"/"resume" with nothing named): the own player if it has paused music; else the first room player that has; else today's behaviour.

- [ ] **Step 1: Write the failing tests**
  - `test_clock.py::test_dismissing_a_room_stops_ringing_on_any_of_it`: rings on "bedroom", `dismiss(satellite=["pc","bedroom"])` stops it; `is_ringing(satellite=["pc"])` is False while it rings on "bedroom".
  - `test_conversation.py::test_stop_at_the_pc_stops_the_pi_ringing`: `room=lambda: ["pc","bedroom"]`, FakeClock ringing on bedroom → dismissed. (FakeClock gains list support.)
  - `test_actions.py::test_pause_at_the_pc_pauses_the_pis_music`: own player idle, room player playing → that one paused, own untouched.
  - `test_actions.py::test_turn_it_up_turns_up_both_when_both_play`.
  - `test_actions.py::test_resume_prefers_its_own_paused_music`.
  - `test_session.py::test_a_wake_holds_the_music_on_the_whole_room`: two sessions in one room → both held and both released.
  - `test_session.py::test_a_satellite_alone_holds_only_its_own` (unchanged Pi behaviour).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/test_clock.py tests/test_conversation.py tests/test_actions.py tests/test_session.py` in London and UTC, then the whole suite once in London; ruff.**
- [ ] **Step 5: Commit** `Act on the whole room for stopping, pausing and volume`.

### Task 8: Running it on Windows, and the deploy from CT 109

**Files:**
- Create: `requirements-pc.txt` (the six packages, pinned to `requirements.lock`'s versions)
- Create: `scripts/windows/launcher.pyw`, `scripts/windows/install.ps1`, `scripts/deploy_pc.sh`
- Create: `docs/decisions/0027-the-pc-as-a-push-to-talk-satellite.md`
- Modify: `docs/roadmap.md` (Phase 6 lines 11 and 104), `README.md` (a short "The PC" section)
- Test: `tests/test_launcher.py` (new)

**Interfaces:**
- Produces: `launcher.pyw` with `next_wait(self, ran_for: float) -> float` on a small `Backoff` class (2 s after a run shorter than 60 s, doubling to at most 300 s; back to 2 s after a run of 60 s or more) and `main()`, which sets `NOVA_CONFIG=%LOCALAPPDATA%\nova\config`, `NOVA_DATA=%LOCALAPPDATA%\nova\data`, runs `satellite.py` with the venv's `pythonw.exe` in a loop, and logs to `%LOCALAPPDATA%\nova\data\logs\launcher.log`. `install.ps1 -Source <folder>`: makes `%LOCALAPPDATA%\nova\{app,config,data}`, copies the code to `app`, makes `app\venv` with `py -3`, `pip install --no-deps -r requirements-pc.txt` (only when the file changed), writes `config\config.yaml` (`backend.address: "nova:10400"`, `backend.satellite: "pc"`, `satellite.trigger: "push_to_talk"`) only if missing, and registers the Task Scheduler task `Nova satellite` at his logon running the launcher, replacing an older one; it never writes `.env` (the token is added by hand, from `add-satellite pc`). `deploy_pc.sh`: reads the brain's commit with `ssh nova 'sudo -n -u nova git -C /home/nova/Home-Assistant rev-parse HEAD'`, refuses unless that commit is in the local repo, `git archive`s it, copies it to the PC with `scp`, runs `install.ps1` over `ssh pc` with `-EncodedCommand`, and ends the running satellite so the launcher starts the new one.

- [ ] **Step 1: Write the failing test** `tests/test_launcher.py`: `Backoff().next_wait(5) == 2`, then `4`, `8`, … capped at `300`; `next_wait(120) == 2` after any run of 60 s or more. (Import `launcher.pyw` with `importlib.machinery.SourceFileLoader`.)
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** the three scripts, the decision record (what, why, the four pieces, the calls in the spec), the roadmap and README lines.
- [ ] **Step 4: Run `tests/test_launcher.py`; ruff; `bash -n scripts/deploy_pc.sh`.**
- [ ] **Step 5: Commit** `Run the satellite on the PC, and deploy it there from CT 109`.

---

## After the build (his named jobs, not tasks)

1. Merge `pc-satellite` into `main` and deploy the brain (as for every brain change).
2. `add-satellite pc` on the brain; the password goes into the PC's `%LOCALAPPDATA%\nova\config\.env` as `NOVA_BACKEND_TOKEN`.
3. `scripts/deploy_pc.sh` to install on the PC.
4. Give the `nova_nathanspc` device the Bedroom area in Home Assistant.
5. `flow100.ahk` (homelab repo `desktop/pc/`): Numpad 3 posts to `http://127.0.0.1:10401/press` with `X-Nova-Press: 1` through `WinHttp.WinHttpRequest.5.1`, as `HAWebhook` does.
6. His checks, from the spec's "Adding it" step 5.
