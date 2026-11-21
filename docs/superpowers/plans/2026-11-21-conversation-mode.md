# Conversation Mode - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An open conversation with Nova at the Pi and the PC: no wake word between turns, his voice cuts in on a reply (dip, then stop on real words), the whole conversation is remembered, and planning moves up to a bigger Claude model.

**Architecture:** One brain-wide `OpenConversation` (open_conversation.py) holds whether a conversation is on, where, and what ends it. The session (session.py) follows every reply with an open listen on the existing listen/feed path, and during a reply runs a `CutIn` (cut_in.py) over the same feed to decide dip, undip and stop. The satellite gains an uncapped "open" recording, a volume fade, and, at the PC, devices held open with the Pi's echo canceller (echo.py, speexdsp shipped for Windows) for the length of a conversation.

**Tech Stack:** Python 3, numpy, sherpa-onnx (Nemotron), Silero, speexdsp through ctypes, MQTT discovery for Home Assistant, pytest on the Pi, PowerShell for the PC install.

**Spec:** `docs/superpowers/specs/2026-11-21-conversation-mode-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Outside a conversation nothing changes: an ordinary request at either satellite behaves exactly as today. Every new behaviour is behind `OpenConversation.on_at(satellite)`.
- A conversation never starts by itself and never closes its microphone silently: every end that he didn't ask for says "I'll leave it there." and plays the falling sound.
- One conversation at a time, brain-wide.
- Settings, all in config section `conversation`, all read as they are used:
  `enabled` true; `pause_seconds` 1.5 (0.6 to 5); `longest_turn` 90 (15 to 300); `idle_minutes` 5 (1 to 30); `talk_over` "voice" (or "wake_word_or_press"); `dip_level` 0.3 (0.1 to 0.8); `bigger_model` "claude-sonnet-5-5" (or "claude-opus-5-5"); `switch_itself` true; `history_exchanges` 100 (20 to 300).
- Start phrases (whole utterance, any capitals, trailing punctuation ignored): "let's talk", "let's have a conversation", "conversation mode", "start conversation mode", "start a conversation". End phrases, whole utterance only: "end conversation", "end the conversation", "end conversation mode", "stop the conversation", "that's all", "that's everything", "goodbye". Never "bye".
- What Nova says: on starting "Go ahead."; on ending when asked "Okay."; on any other end "I'll leave it there."; asked for at a second satellite "We're already talking at the {name}."; before thinking harder "Let me think about that."; on going back "Okay, quick answers."
- Constants until the measurements say otherwise (Gate A): `DIP_AFTER = 0.15` s, `CUT_IN_WAIT = 1.0` s, `DIP_FADE = 0.05` s, `CUT_IN_PRE_ROLL = 0.5` s, `OPEN_PRE_ROLL = 0.5` s, `MUSIC_SHARE = 0.3`, `NOT_FOR_ME_MOST = 3`.
- Nothing from a conversation is written to disk. What the console shows follows `logging.log_transcripts`. Measurement recordings hold room sound: they stay in a temporary folder on the device, are deleted once scored, and never enter the repository.
- Hardware: Nemotron is fed during a reply only once Silero has heard a voice.
- Code style (memory notes working-style, design-principles): full-sentence why-comments, British spelling, spaced " - " dashes (never em dashes), Google-style docstrings; one commit per task; a test that fails on the old code for every rule; the repository is public, so nothing personal or secret in it.
- Tests: `NOVA_TREE=/home/nathaniel/work/Home-Assistant-talk NOVA_CHECK=talk nice -n 19 timeout 600 bash ~/work/Home-Assistant/.superpowers/tools/pi_test.sh <args>`; check `cat /proc/loadavg` first (wait above 3); one run at a time, never in the background, no repeat runs; check no other run is going with `ssh pi 'pgrep -fa "m [p]ytest"'`. Never `ps` on the Pi. No `git stash`. Reviewers read diffs and run nothing.
- Anything that makes sound in his room, touches his PC, deploys, pushes or restarts `nova-backend` waits for his go; say what will sound first.

## Review Focus

1. Nova's own voice getting past the echo canceller: it must not stop or keep dipping its own reply (Task 12 `test_the_replys_own_words_dont_stop_it`; Task 13 `test_no_voice_cut_in_until_the_echo_canceller_is_ready`).
2. Nobody there, a film playing: the conversation must still end (Task 4 `test_only_answered_turns_put_off_the_idle_end`, `test_three_turns_not_for_nova_in_a_row_end_it`).
3. The link dropping or the brain restarting mid-conversation: the PC's devices close and its microphone light goes off; the brain's conversation is over (Task 7 `test_losing_the_brain_ends_the_open_devices`; Task 8 `test_the_satellite_going_ends_the_conversation`).
4. An end phrase inside a longer sentence ("that's all I can think of for the kitchen") must not end it (Task 5 `test_an_end_phrase_inside_a_sentence_doesnt_end_it`).
5. An ordinary request outside a conversation: no listen during its reply, three follow-ups at most, 0.6 s of quiet (Task 8 `test_outside_a_conversation_a_reply_isnt_followed_by_listening`; Task 13 `test_outside_a_conversation_nothing_listens_during_a_reply`).

---

### Task 0: Bring the branch up to date

**Files:** none of its own.

- [ ] **Step 1:** In the worktree, `git fetch origin` and merge `origin/main` into `conversation-mode`.
- [ ] **Step 2:** `press-ends` (worktree `~/work/Home-Assistant-ends`) gives `Nemotron.words` and `NemotronTranscription.quiet_words`, which Tasks 6 and 12 build on; `pc-app` (worktree `~/work/Home-Assistant-app`) changes how the PC satellite is installed, which Task 1 must fit. For each: if it is already on `origin/main`, nothing to do. If not, read its ledger; merge the local branch only if every task there is done and reviewed, and never edit, commit to or run anything in those worktrees. If `press-ends` isn't finished, stop and tell the controller.
- [ ] **Step 3:** Run the whole suite once (London). Expected: all pass. Note the count in the ledger.

---

### Task 1: The PC can load the echo canceller

**Files:**
- Modify: `voice_assistant/echo.py`, `scripts/windows/install.ps1`, `scripts/deploy_pc.sh` (only if the library needs copying), `config.yaml` (the comment on `audio.echo_cancellation`)
- Test: `tests/test_echo.py`

**Interfaces:**
- Produces:
  - `echo.library_path() -> Optional[str]`: on Windows, the full path of the shipped library (`speexdsp.dll` or `libspeexdsp-1.dll`, whichever is there) under `paths.DATA_DIR/lib`, else None; elsewhere `ctypes.util.find_library("speexdsp")` as today.
  - `EchoCanceller.ready -> bool`: True once the canceller has lined the speaker up with the microphone and is cancelling (the gap measured and agreed, as the module's docstring describes); False before, and again after `reset()`.
  - `EchoCanceller.reset() -> None`: forgets what was lined up, for devices opened afresh.
- `make_echo_canceller` is unchanged for callers: None, with today's warning, when the library isn't there.

- [ ] **Step 1: Write the failing tests:**
  - `test_on_windows_the_shipped_library_is_loaded`: with `_on_windows` true and a file at the shipped path, `library_path()` returns it.
  - `test_on_windows_without_the_library_there_is_no_canceller`: `make_echo_canceller` returns None and logs the warning.
  - `test_ready_only_once_the_gap_is_agreed`: a canceller fed played and heard audio with a fixed gap is not `ready` at first and is after enough agreeing measurements; `reset()` makes it not ready.
- [ ] **Step 2:** Run `tests/test_echo.py`. Expected: the three fail.
- [ ] **Step 3:** Implement the three in echo.py.
- [ ] **Step 4:** install.ps1 fetches the library: a pinned archive with its SHA-256 written in the script, checked before anything is extracted, into `%LOCALAPPDATA%\nova\lib`; skipped when the file is already there with the right hash; a failure warns and carries on (no canceller is today's behaviour). Source, in order of preference: conda-forge's `speexdsp` win-64 package (an MSVC build, no MinGW runtime to ship); MSYS2's `mingw-w64-ucrt-x86_64-speexdsp`. Record the URL, version and hash chosen in the script's comment and in the ledger. If `pc-app` is merged, fit its install layout.
- [ ] **Step 5:** Run `tests/test_echo.py`. Expected: pass. Commit: "Load the echo canceller at a computer, from a library shipped with it".

---

### Task 2: The measuring scripts

**Files:**
- Create: `scripts/manual/measure_cut_in.py` (runs on a satellite), `scripts/manual/score_cut_in.py` (runs on the brain), `scripts/manual/measure_voice_load.py` (runs on the brain)
- Modify: `docs/release-checklist.md` ("The manual scripts")
- Test: `tests/test_measure_cut_in.py`

**Interfaces:**
- `measure_cut_in.py --play <wav> --seconds N --out <folder> [--music <wav> --music-share 0.3]`: opens the satellite's own `Speaker` and `Microphone` with the echo canceller, plays the WAV (and the music under it, if given), and writes the echo-cancelled microphone as 16 kHz mono `heard.wav` plus `ready.json` (`{"ready_after": seconds or null}` from `EchoCanceller.ready`). Refuses an `--out` inside the repository.
- `score_cut_in.py <heard.wav> [--said <text>]`: prints one JSON object: `speech_share` (frames Silero scores at or above 0.5), `longest_speech` (seconds), `first_speech_at`, `words` (Nemotron's), `own_words` (whether `cut_in.own_words(words, said)` would call them the reply's own; until Task 12 exists, a plain containment check).
- `measure_voice_load.py --seconds N`: synthesises a long reply with the brain's voice without playing it, with and without Nemotron working through a spoken clip at the same time, and prints each run's real-time factor and longest gap between pieces.

- [ ] **Step 1: Write the failing tests** for the pure parts: `score` over a made-up array with a fake detector gives the right `speech_share` and `longest_speech`; `measure_cut_in` refuses an output folder inside the repository.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Scripts to measure what the microphone hears of Nova's own voice".

### Gate A: measure, on his go (controller, not a subagent)

Each run makes sound in his room or uses his PC. Tell him what will sound and wait for his go each time. Recordings stay in a temporary folder on the device and are deleted after scoring.

1. **Pi:** play about 30 s of Nova's voice at the general volume, then the same with music at `MUSIC_SHARE` under it. Score both. Then once with him talking over it on cue (how soon Silero fires on his voice, how soon words come).
2. **PC** (awake, he's there): copy the worktree's satellite and the library to a temporary folder (not the live install), run the same three. Also `ready.json`: how long the canceller takes from freshly opened devices.
3. **Brain:** `measure_voice_load.py` through the read-only check command. It makes no sound.

Write the numbers (no audio, none of his words) to the ledger, tell him what they mean, and settle with him:

| Finding | Change to the plan |
|---|---|
| Clean at both | Build as written. |
| Nova's voice reads as speech but not as words | Raise the detector's threshold while a reply plays and/or `DIP_AFTER`; record the values in Global Constraints. |
| Music's leftovers read as speech | Task 14's music rule becomes the fallback: music asked for starts as the conversation ends, and Nova says "I'll play it when we're done." |
| The voice stutters while Nemotron listens | Task 13 feeds Nemotron only between the voice's sentences, or the stop uses the detector alone with a longer wait. Decide with him. |
| The PC's echo can't be made reliable | Task 13's default for a push-to-talk satellite is `wake_word_or_press`; say so to him before going on. |
| The PC's canceller is slow to be ready | Task 7 keeps what the canceller learnt between conversations if the devices are the same; otherwise the press-only start stands. |

---

### Task 3: The settings

**Files:**
- Modify: `config.yaml`, `voice_assistant/config.py` (`LIMITS`, `CHOICES`), `voice_assistant/settings_page.py` (`EDITABLE`), `voice_assistant/web/settings_list.js` (a "Conversation" section). The page preview's stub (`~/work/Home-Assistant/.superpowers/tools/page-preview/stub.js`) is in the main checkout and shared: don't edit it, note in the ledger that it needs refreshing for Task 17's shots
- Test: `tests/test_config.py`, `tests/test_settings_page.py`, `tests/web/*.test.mjs` where the list of sections is tested

**Interfaces:**
- Produces: the nine `conversation.*` settings in Global Constraints, each with a full comment in config.yaml saying what raising or lowering it does; `talk_over` and `bigger_model` in `CHOICES`; every row "now" on the page under a section named "Conversation" in the group that holds Claude's settings. Page labels: "Conversations can be started", "Seconds of quiet which end your turn", "Longest turn, in seconds", "Minutes of quiet which end a conversation", "What cuts in on a reply", "How far a reply turns down when you talk", "Model for planning", "Nova may move up by itself", "Most exchanges remembered".

- [ ] **Step 1: Write the failing tests:** each default loads; each limit refuses one value below and one above; a wrong `talk_over` or `bigger_model` is refused; each setting has a row on the page.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run those tests and the page's JavaScript tests (`~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs`). Expected: pass.
- [ ] **Step 5:** Commit: "Settings for conversations".

---

### Task 4: Whether a conversation is on

**Files:**
- Create: `voice_assistant/open_conversation.py`
- Modify: `voice_assistant/brain.py` (the Brain makes one: `self.open_conversation`)
- Test: `tests/test_open_conversation.py`

**Interfaces:**
- Produces:

```python
class OpenConversation:
    def __init__(self, settings: Callable[[], Dict], now: Callable[[], float] = time.monotonic,
                 ended: Optional[Callable[[List[Dict]], None]] = None): ...
    @property
    def satellite(self) -> Optional[str]: ...          # where it's on, or None
    def on_at(self, satellite: Optional[str]) -> bool: ...
    def start(self, satellite: str) -> str: ...        # "started", "already here", "elsewhere" or "off" (enabled is false)
    def end(self, reason: str) -> bool: ...            # False if none was on; calls ended(exchanges) once
    def answered(self, user: str, assistant: str, model: str) -> None: ...
    def not_for_nova(self) -> bool: ...                # True on the NOT_FOR_ME_MOST-th in a row
    def idle_in(self) -> float: ...                    # seconds until the quiet spell ends it; 0 or less means due
    bigger: bool                                       # whether it has moved up
    def model(self, usual: str) -> str: ...            # settings' bigger_model while bigger, else usual
```

- `settings` returns the `conversation` section. Reasons for `end`: "said", "switch", "idle", "not for nova", "muted", "gone". Exchanges given to `ended`: `{"user", "assistant", "at" (ISO wall time), "model"}`, capped at `history_exchanges`. Thread-safe: sessions, the Brain's threads and Home Assistant's all call it.

- [ ] **Step 1: Write the failing tests:**
  - `test_start_and_end`: start gives "started", `on_at` true there and false elsewhere; a second start there "already here"; at another satellite "elsewhere"; with `enabled` false "off"; `end` true once, false after.
  - `test_only_answered_turns_put_off_the_idle_end`: `idle_in()` falls with the fake clock; `answered` resets it to `idle_minutes * 60`; `not_for_nova()` doesn't.
  - `test_three_turns_not_for_nova_in_a_row_end_it`: False, False, True; an `answered` in between starts the count again.
  - `test_the_bigger_model_stays_until_moved_down_or_the_end`: `model("h")` is "h"; after `bigger = True` it is the setting's; a new conversation starts on "h".
  - `test_ended_gets_the_whole_conversation_once`: three `answered` then `end("said")` calls `ended` with three exchanges and their models; never again.
  - `test_a_changed_setting_is_used_at_once`: lowering `idle_minutes` mid-conversation shortens `idle_in()`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Keep whether a conversation is on, where, and what ends it".

---

### Task 5: Starting and ending by voice

**Files:**
- Modify: `voice_assistant/intents.py`, `voice_assistant/actions.py`, `voice_assistant/llm.py` (tool definitions), `voice_assistant/conversation.py` (hands the executor the conversation and its satellite)
- Test: `tests/test_intents.py`, `tests/test_actions.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: `OpenConversation` (Task 4).
- Produces:
  - Tools `start_conversation` and `end_conversation` (no parameters) in `ActionExecutor.tools()`, offered to Claude only while `conversation.enabled`; `end_conversation` only while one is on at this satellite.
  - `ActionExecutor.start_conversation() -> ActionResult` and `.end_conversation() -> ActionResult`, saying the lines in Global Constraints; the executor learns its satellite's name the way `save_note` does.
  - The fixed phrases matched in intents.py to those tools, whole utterance only; the end phrases only while a conversation is on at this satellite.
  - `ActionResult` for a start carries `confirm` such that no chime plays (the session plays the listening sound, Task 8).

- [ ] **Step 1: Write the failing tests:**
  - `test_each_start_phrase_starts_one` (parametrised over the five), `test_each_end_phrase_ends_it` (over the seven).
  - `test_an_end_phrase_inside_a_sentence_doesnt_end_it`: "that's all I can think of for the kitchen" matches nothing.
  - `test_bye_alone_isnt_an_end_phrase`.
  - `test_end_phrases_mean_nothing_outside_a_conversation`: "that's all" with none on matches nothing here.
  - `test_a_second_satellite_is_told_where_it_is`: start at "pc" while on at "pi" says "We're already talking at the Pi." (the name as `satellite_names` gives it) and starts nothing.
  - `test_switched_off_there_are_no_phrases_or_tools`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Start and end a conversation by voice".

---

### Task 6: Listening that waits

**Files:**
- Modify: `voice_assistant/recording.py`, `voice_assistant/speech_to_text.py`
- Test: `tests/test_recording.py`, `tests/test_speech_to_text.py`

**Interfaces:**
- Produces:
  - `record_until_silence(..., open_wait: Optional[float] = None)`. When given: the recording may wait that many seconds for speech to start (in place of `speech_timeout`); audio from more than `OPEN_PRE_ROLL` before the speech start is neither kept nor passed to `heard`; `timeout` counts from the speech start; `report["ended_by"]` is "no speech" when the wait runs out. The press-ends rules (`voice_drop`, `words_quiet`) keep working with it.
  - `NemotronTranscription.words() -> str`: the real words heard so far in this recording ("" until one that isn't `_imagined`, and "" once Nemotron can't say), safe from another thread.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_open_wait_outlasts_the_speech_timeout`: 20 s of quiet then speech is recorded with `open_wait=60`, `speech_timeout=3`.
  - `test_a_long_wait_then_a_long_turn_isnt_cut_off`: 100 s of quiet then 30 s of speech with `timeout=90`, `open_wait=300` returns all 30 s.
  - `test_the_wait_isnt_kept_or_heard`: the returned audio and what `heard` was given start no more than `OPEN_PRE_ROLL` before the speech.
  - `test_an_open_wait_that_runs_out_says_no_speech`.
  - `test_words_gives_real_words_only`: "" for "", for "You"; "Set a" once heard.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run both files. Expected: pass.
- [ ] **Step 5:** Commit: "Let a recording wait for someone to start, without keeping the wait".

---

### Task 7: The satellite's side

**Files:**
- Modify: `satellite.py`, `voice_assistant/remote.py`, `voice_assistant/protocol.py` if message kinds are listed there
- Test: `tests/test_satellite.py`, `tests/test_remote.py`

**Interfaces:**
- Consumes: `EchoCanceller.ready`, `.reset()` (Task 1).
- Produces, brain side:
  - `RemoteSatellite.listen(open: bool = False) -> AudioFeed`: with `open`, sends `listen` with `open=True` and the feed has no length limit.
  - `RemoteSatellite.set_conversation(on: bool) -> bool`: sends `{"type": "conversation", "on": on}`.
  - `RemoteSatellite.echo_ready -> bool`: the last `echo_ready` the satellite sent; False until it has.
  - `AudioFeed.cut_in`: a `threading.Event` set when the satellite sends `cut_in` for that feed's recording.
- Produces, satellite side:
  - A recording asked for with `open` has no cap (`longest_request`, `MOST_SECONDS` don't apply); starting it stops nothing that's playing and plays nothing.
  - `conversation` on: a push-to-talk satellite holds its microphone and speaker open, resets and engages the echo canceller, and unmutes Windows' microphone as a press does. Off, or the link dropping: it lets them go, and the microphone is as an ordinary press leaves it. A wake-word satellite only notes it.
  - `echo_ready` (`{"ready": bool}`) sent whenever the canceller's `ready` changes, and once on connecting; False where there's no canceller.
  - A press while an open recording is going and a reply is playing: the reply stops at once, `cut_in` (`{"recording": id}`) goes to the brain, and the recording carries on. With no reply playing, a press ends the recording as today.
  - An older brain sending none of this leaves the satellite exactly as it is.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_open_recording_runs_past_the_cap`: 45 s of audio is sent for `open`; an ordinary one still ends at the cap.
  - `test_an_open_listen_stops_nothing_and_plays_nothing`: a reply playing when it starts is still playing.
  - `test_a_computer_holds_its_devices_for_a_conversation`: after `conversation` on, the microphone and speaker stay open between recordings; after off, they close.
  - `test_losing_the_brain_ends_the_open_devices`: the link dropping mid-conversation closes them.
  - `test_starting_a_conversation_unmutes_windows`.
  - `test_a_press_over_a_reply_in_an_open_recording_cuts_in`: reply stopped, `cut_in` sent, recording not ended; `test_a_press_with_nothing_playing_ends_the_turn`.
  - `test_the_satellite_says_when_its_echo_canceller_is_ready`.
  - remote: `test_an_open_feed_has_no_limit`, `test_cut_in_sets_the_feeds_event`, `test_echo_ready_is_false_until_said`.
- [ ] **Step 2:** Run both files. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Open recordings, and a computer's devices held open for a conversation".

---

### Task 8: Turn after turn

**Files:**
- Modify: `voice_assistant/session.py`, `voice_assistant/conversation.py`, `voice_assistant/brain.py` (ending on a satellite going)
- Test: `tests/test_session.py`, `tests/test_conversation.py`, `tests/test_brain.py`

**Interfaces:**
- Consumes: Tasks 3 to 7.
- Produces:
  - In `_converse`, once the first request is answered: while `open_conversation.on_at(self.name)`, another turn. Each turn calls `self.conversation.start(...)` as `_answer` does, then `_handle_interaction(feed, follow_up=<the last reply asked something>, timeline=..., open_turn=True)` on `self.satellite.listen(open=True)` (or the feed Task 13 leaves open).
  - `_handle_interaction(..., open_turn: bool = False, listen_from: Optional[int] = None)`. With `open_turn`: `silence_duration` is `conversation.pause_seconds` (and the press-ends rules wait at least that long); `timeout` is `conversation.longest_turn`; `open_wait` is `open_conversation.idle_in()`; no listening sound; nothing said for silence, noise or words imagined in it: the turn simply ends and the next begins.
  - `Conversation.handle` is called with `follow_up=False` for a turn that doesn't answer a question, so local commands, routines and dismissals run. `max_follow_ups` doesn't apply in a conversation.
  - After each turn Claude or a local command answered: `open_conversation.answered(text, reply, model)`. After a `not_for_me` turn: `open_conversation.not_for_nova()`, ending with reason "not for nova" when it returns True.
  - Starting: when a turn's actions started a conversation, the session sends `set_conversation(True)`, says "Go ahead." and plays the listening sound once. Ending for any reason: `set_conversation(False)`, the line from Global Constraints, the falling sound, and the room's music let go as at the end of any request.
  - `Session.end_conversation(reason: str) -> None`: ends one running here from another thread (the switch, mute, idle), stopping the open listen so the turn loop falls out.
  - `Brain`: a satellite going away ends a conversation there with "gone", saying nothing.

- [ ] **Step 1: Write the failing tests** (with the file's fake satellite, detector and speech-to-text):
  - `test_a_reply_in_a_conversation_is_followed_by_listening`; `test_outside_a_conversation_a_reply_isnt_followed_by_listening`.
  - `test_more_than_three_turns`: five turns each answered.
  - `test_a_turn_uses_the_conversations_pause_and_longest_turn`: `record_until_silence` gets 1.5 and 90 and an `open_wait`; an ordinary request gets 0.6 and 15 and none.
  - `test_no_listening_sound_between_turns`; `test_starting_plays_it_once_and_says_go_ahead`.
  - `test_silence_and_noise_between_turns_say_nothing`: no "didn't catch", no falling sound, and the next turn listens.
  - `test_a_command_mid_conversation_runs_locally`: "pause the music" doesn't reach Claude; `test_a_turn_after_a_question_is_its_answer`.
  - `test_the_quiet_spell_ends_it_and_says_so`: an open wait which runs out ends it with "I'll leave it there." and `set_conversation(False)`.
  - `test_three_turns_not_for_nova_end_it`; `test_an_end_phrase_ends_it_with_okay`.
  - `test_the_satellite_going_ends_the_conversation`; `test_ending_from_another_thread_stops_the_listen`.
  - `test_ended_is_given_every_exchange`.
  - `test_something_typed_mid_conversation_is_a_turn`: a request typed in Home Assistant while it waits is answered as the next turn, and the conversation carries on.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run the three files. Expected: pass.
- [ ] **Step 5:** Commit: "Listen again after every reply while a conversation is on".

---

### Task 9: Remembering the conversation

**Files:**
- Modify: `voice_assistant/llm.py`, `voice_assistant/conversation.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `OpenConversation` (Task 4), handed to the LLM as `LLM.open_conversation` with its satellite's name.
- Produces: while one is on here, `history` keeps up to `conversation.history_exchanges` and `forget_stale_history` forgets nothing; when it ends, the history is cut to `history_length`. The system prompt gains `CONVERSATION_PROMPT` as a block of its own after the cached one (so the cached part is unchanged); the newest history message carries `cache_control`. `CONVERSATION_PROMPT` says: this is a spoken back-and-forth; a few sentences unless asked for more; one question at a time; don't end every reply with a question; speech from something playing is still `not_for_me`.

- [ ] **Step 1: Write the failing tests:** `test_a_conversation_keeps_more_than_five_exchanges`; `test_a_conversation_forgets_nothing_after_five_minutes`; `test_the_history_is_capped_at_the_setting`; `test_ending_leaves_the_last_five`; `test_the_prompt_has_the_conversation_part_only_in_one`; `test_the_cached_system_block_is_unchanged`; `test_the_history_is_marked_for_caching`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Remember a whole conversation, and tell Claude it's in one".

---

### Task 10: The bigger model

**Files:**
- Modify: `voice_assistant/llm.py`, `voice_assistant/conversation.py`, `voice_assistant/intents.py`, `voice_assistant/actions.py`, `voice_assistant/usage.py`, `config.yaml`, `voice_assistant/config.py`, `voice_assistant/settings_page.py`
- Test: `tests/test_llm.py`, `tests/test_conversation.py`, `tests/test_intents.py`, `tests/test_usage.py`

**Interfaces:**
- Consumes: `OpenConversation.bigger`, `.model()` (Task 4).
- Produces:
  - Every request in a conversation uses `open_conversation.model(self.model_name)`.
  - Tool `think_harder` (no parameters), offered only in a conversation, while `switch_itself` is on and it isn't already bigger. When Claude calls it: `bigger = True`, "Let me think about that." is said, and the same turn is asked again of the bigger model; only that second answer goes in the history.
  - Phrases: "think harder", "use the bigger model", "use the big model" move up. Said alone, the last turn's request (`llm.history[-1]["user"]`) is asked again and its exchange replaced; followed by more words, the rest is asked. "quick answers", "back to normal", "use the quick model" set `bigger = False` and say "Okay, quick answers." Outside a conversation the phrases match nothing.
  - `usage.bigger_input_price` and `usage.bigger_output_price` (settings, "now", beside the existing prices), used by `UsageLog` for any model other than `llm.model`. Take the ids, the list prices and whether the bigger models accept the request settings Nova sends (effort, temperature, thinking) from the claude-api skill; if a setting is refused, leave it out for the bigger model and say why in a comment, as config.yaml does for Haiku's temperature.

- [ ] **Step 1: Write the failing tests:** `test_a_conversation_starts_on_the_usual_model`; `test_think_harder_asks_again_on_the_bigger_model_and_keeps_one_exchange`; `test_it_stays_bigger_for_the_next_turn`; `test_the_tool_is_hidden_when_it_may_not_move_itself`, `..._when_already_bigger`, `..._outside_a_conversation`; `test_think_harder_alone_answers_the_last_turn_again`; `test_think_harder_with_more_asks_the_rest`; `test_quick_answers_goes_back`; `test_the_bigger_models_spending_uses_its_own_prices`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run the four files. Expected: pass.
- [ ] **Step 5:** Commit: "Move a conversation up to a bigger model for planning".

---

### Task 11: A reply's volume fades

**Files:**
- Modify: `voice_assistant/speaker.py`, `satellite.py` (`_set_playback`), `voice_assistant/remote.py` (`RemotePlayback`)
- Test: `tests/test_speaker.py`, `tests/test_remote.py`, `tests/test_satellite.py`

**Interfaces:**
- Produces: `Playback.fade_to(volume: float, seconds: float) -> None` (a straight ramp from the volume it has now, sample by sample, in the audio callback's path); `RemotePlayback.fade_to(volume, seconds)` sending `set_playback` with `volume` and `fade`; the satellite's `_set_playback` using the fade when the message has one and jumping as today when it hasn't.

- [ ] **Step 1: Write the failing tests:** `test_a_fade_ramps_without_a_jump` (no step between neighbouring samples bigger than the ramp allows, the end level right); `test_a_fade_back_up_returns_to_the_first_volume`; `test_set_playback_without_a_fade_jumps_as_before`; `test_the_brain_sends_the_fade`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Fade a playback's volume when asked".

---

### Task 12: Deciding dip, undip and stop

**Files:**
- Create: `voice_assistant/cut_in.py`
- Test: `tests/test_cut_in.py`

**Interfaces:**
- Produces:

```python
LISTENING_NOISES: frozenset   # "yeah", "yes", "yep", "mm", "mhm", "mm-hm", "uh-huh", "okay", "ok", "right", "sure", "i see", "got it"

def own_words(heard: str, saying: str) -> bool: ...
def only_listening_noises(heard: str) -> bool: ...

class CutIn:
    def __init__(self, detector, saying: str, threshold: Optional[float] = None,
                 dip_after: float = DIP_AFTER, wait: float = CUT_IN_WAIT): ...
    def hear(self, frame: np.ndarray, words: str) -> Optional[str]: ...   # "dip", "undip", "stop" or None
    voice_from: Optional[int]    # frames since the start at which the voice that dipped it began
```

- `own_words`: True when the words heard, lower-cased and stripped of punctuation, are a run of consecutive words of `saying` (at least all of them when there are one or two; allowing one word wrong in every four when there are more).
- `hear` is called once per `recording.FRAME` of the feed while the reply plays, with the words Nemotron has so far for the audio since `voice_from` ("" before a dip). It returns "dip" once speech (probability at or above `threshold`, the detector's own when None) has lasted `dip_after`; after a dip, "stop" as soon as `words` holds something that isn't `own_words` and isn't `only_listening_noises`, or holds "stop"; "undip" once `wait` has passed since the voice ended with no such words. After an undip it can dip again.
- The detector is its own instance, not the one recordings use.

- [ ] **Step 1: Write the failing tests:**
  - `test_voice_dips_after_dip_after`: speech frames give "dip" at 0.15 s and not before; a single loud frame doesn't.
  - `test_words_after_a_dip_stop_it`; `test_no_words_undips_after_the_wait`; `test_it_can_dip_again_after_an_undip`.
  - `test_the_replys_own_words_dont_stop_it`: saying "The weather today is dry and bright", hearing "weather today is dry" gives no stop, then undip.
  - `test_own_words_allows_one_wrong_in_four`; `test_two_words_must_match_exactly`.
  - `test_listening_noises_dont_stop_it`: "yeah", "mm-hm okay"; `test_a_noise_then_more_stops_it`: "okay but what about"; `test_stop_always_stops_it`, even when "stop" is in the reply.
  - `test_a_raised_threshold_is_used`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Decide when a voice dips a reply, and when words stop it".

---

### Task 13: Cutting in on a reply

**Files:**
- Modify: `voice_assistant/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `CutIn` (Task 12), `RemotePlayback.fade_to` (Task 11), `listen(open=True)`, `AudioFeed.cut_in`, `echo_ready` (Task 7), `NemotronTranscription.words()` (Task 6), `_handle_interaction(open_turn, listen_from)` (Task 8).
- Produces:
  - `Session._voice_may_cut_in() -> bool`: a conversation is on here, `conversation.talk_over` is "voice", `self.satellite.echo_ready`, and nothing else is playing in the room (the test `media_wake_word_confidence` uses).
  - In `_say(..., listen=True)` when that is true: the open listen starts as the reply does; frames go to a `CutIn` made with the reply's text; on "dip" `playback.fade_to(volume * dip_level, DIP_FADE)` and a `stt.listen()` begins from `CUT_IN_PRE_ROLL` before the voice; on "undip" `playback.fade_to(volume, DIP_FADE)` and that listen is cancelled; on "stop", or the feed's `cut_in` event (a press), `playback.stop()` and `_say` returns `{"talked_over": True, "feed": feed, "from": <position CUT_IN_PRE_ROLL before the voice, or now for a press>, "cut_in": True}`.
  - A reply that ends without a stop leaves its feed open: the next turn uses it, from where the reply ended, or from before a voice that had already dipped it.
  - The turn loop passes `feed` and `listen_from` on to `_handle_interaction`; a cut-in turn's exchange is marked cut short in the history as a talked-over one is today.
  - When `_voice_may_cut_in()` is false, `_say` behaves exactly as today (`_heard_over`), and the open listen starts after the reply.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_voice_dips_the_reply_and_words_stop_it`: the playback is faded to 0.3 of its volume, then stopped, and the next turn's recording starts 0.5 s before the voice, so its first word is in it.
  - `test_a_cough_dips_it_and_it_carries_on`: faded down, faded back, the reply finishes, and the next turn listens on the same feed.
  - `test_nova_hearing_its_own_reply_doesnt_stop_it`.
  - `test_a_press_cuts_in`: the feed's `cut_in` event stops the reply and the turn begins.
  - `test_hey_nova_over_a_reply_stops_it_and_is_taken_off_the_turn`: "Hey Nova, what about Tuesday" stops the reply and the turn's request is "what about Tuesday".
  - `test_no_voice_cut_in_until_the_echo_canceller_is_ready`; `test_no_voice_cut_in_while_something_else_plays_in_the_room`; `test_talk_over_set_to_the_wake_word_or_a_press`: in each, no listen is asked for during the reply and a wake word over it still stops it.
  - `test_outside_a_conversation_nothing_listens_during_a_reply`.
  - `test_nemotron_only_hears_once_a_voice_has`: no `stt.listen()` before a dip.
  - `test_a_cut_in_reply_is_marked_cut_short`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Let his voice cut in on a reply in a conversation".

---

### Task 14: Things that happen during a conversation

**Files:**
- Modify: `voice_assistant/session.py`, `voice_assistant/brain.py`, `voice_assistant/actions.py` or `voice_assistant/music.py` (where music's volume and holds are set)
- Test: `tests/test_session.py`, `tests/test_brain.py`, `tests/test_music.py`

**Interfaces:**
- Produces:
  - **Gaps.** `announce` (and whatever rings timers and alarms through it) waits, in a conversation, for a gap and no longer for the session to be idle: a gap is an open wait in which he hasn't started speaking. An announcement begun in a gap finishes before a reply is said. Dismissing what rings needs no wake word (turns are fresh requests, Task 8).
  - **Music.** Music playing as a conversation starts is held as for any request and let go when it ends. Music an action starts during one is let play at `MUSIC_SHARE` of the music volume, and returns to the full music volume when the conversation ends. (If Gate A chose the fallback: such music starts as the conversation ends and the reply is "I'll play it when we're done.")
  - **Mute.** Wherever the brain learns a satellite's microphone has been muted (Nova's own mute for a wake-word satellite, Windows' microphone for a computer), a conversation there ends with reason "muted".

- [ ] **Step 1: Write the failing tests:** `test_a_timer_due_mid_conversation_rings_at_the_next_gap` (not while Nova speaks, not while he does, and not held until the conversation ends); `test_stop_dismisses_it_without_the_wake_word`; `test_music_asked_for_plays_quietly_until_the_end` then at the full volume; `test_music_from_before_comes_back_when_it_ends`; `test_muting_ends_the_conversation` for each kind of satellite.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Timers, music and mute during a conversation".

---

### Task 15: The other satellite in the room

**Files:**
- Modify: `voice_assistant/quiet_others.py`, `voice_assistant/brain.py` (`_quiet_others_forever`)
- Test: `tests/test_quiet_others.py`, `tests/test_brain.py`

**Interfaces:**
- Produces: `QuietOthers.tell(speaking, now, also_quiet: AbstractSet[Hashable] = frozenset())`: keys in `also_quiet` are told to be quiet whoever is speaking, renewed every `QUIET_RENEW` like the rest. The Brain passes the connections of every other satellite in the same room as the conversation's satellite (rooms as `satellite_rooms` has them), and the empty set when none is on.

- [ ] **Step 1: Write the failing tests:** `test_a_same_room_satellite_stays_quiet_through_a_conversation` (told quiet with nobody speaking, renewed, and released once it ends); `test_another_rooms_satellite_isnt`; `test_without_also_quiet_it_is_as_before`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "Keep the room's other satellite quiet for a whole conversation".

---

### Task 16: The switch in Home Assistant

**Files:**
- Modify: `voice_assistant/ha_device.py`, `voice_assistant/brain.py`, `voice_assistant/session.py`
- Test: `tests/test_ha_device.py`, `tests/test_brain.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: `Session.end_conversation` (Task 8).
- Produces: a `conversation` switch on each satellite's device, named "Conversation", beside "Microphone muted", absent while `conversation.enabled` is off; controls `in_conversation() -> bool` and `set_conversation(on: bool) -> None`. On: `Session.start_conversation() -> str` starts one here as the phrase does (the same refusals, "Go ahead.", the listening sound, the open listen) by putting a wake on the satellite's queue that goes straight to an open turn. Off: `end_conversation("switch")`. The state is published whenever a conversation starts or ends anywhere.

- [ ] **Step 1: Write the failing tests:** `test_the_switch_is_discovered_and_shows_the_state`; `test_turning_it_on_starts_one_and_listens`; `test_turning_it_off_ends_it_with_okay`; `test_on_while_one_runs_elsewhere_stays_off`; `test_no_switch_when_conversations_are_off`.
- [ ] **Step 2:** Run. Expected: fail. **Step 3:** Implement. **Step 4:** Run. Expected: pass.
- [ ] **Step 5:** Commit: "A Conversation switch in Home Assistant".

---

### Task 17: Records, checks and the whole suite

**Files:**
- Create: `docs/decisions/00NN-conversation-mode.md` (the next free number at the time; other branches hold some)
- Modify: `docs/release-checklist.md`, `README.md` where features are listed

- [ ] **Step 1:** The decision record: what and why, his calls and mine from the spec, Gate A's numbers and what they changed.
- [ ] **Step 2:** Hand checks added to the release checklist, numbered on from the last, each marked "Makes sound": start by phrase at the Pi and by a press at the PC; five turns with no wake word; cut in mid-reply by voice; a cough mid-reply; "yeah" mid-reply; a ten-second thinking pause before a turn; a command mid-conversation; "think harder" then "quick answers"; a timer going off mid-conversation; "that's all"; five minutes of quiet; the switch on and off from the phone; the other satellite ignoring "Hey Nova" throughout; an ordinary request after it, behaving as before.
- [ ] **Step 3:** The whole suite once in London and once with `TZ_OVERRIDE=UTC`, ruff, the page's JavaScript tests, and the page preview's shots of the new section (say in the ledger that the stub was refreshed, or that it needs to be).
- [ ] **Step 4:** Commit: "Record conversation mode, and how to check it by hand".
- [ ] **Step 5:** Whole-branch review (Opus), one fix wave, then give him the deploy sentence. Deploying is his named job: push to GitHub's main, pull as nova and restart nova-backend, the Pi's update.sh, then deploy_pc.sh.
