# The PC Knows When You Stop - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At a push-to-talk satellite (the PC), end a recording when his voice has dropped well below his own loudness, or when no new word has come for 1.5 s - so background sound can't keep it listening - and note for a week, as numbers only, any that still run long.

**Architecture:** `record_until_silence` (recording.py) gains two optional end rules, off unless the caller gives them, and fills a report of what ended it. Nemotron's live transcript (speech_to_text.py, nemotron.py) tells it how much audio has passed since its words last changed. The session (session.py) switches both on only for push-to-talk satellites, from two new settings, notes `ended_by` on the timeline, and hands long recordings to a small notes keeper (long_recordings.py).

**Tech Stack:** Python 3, numpy, sherpa-onnx (already on the brain), pytest on the Pi.

**Spec:** `docs/superpowers/specs/2026-11-20-pc-knows-when-you-stop-design.md`

## Global Constraints

- Only push-to-talk satellites (`self.satellite.push_to_talk` in session.py) get the new rules; a wake-word satellite's (the Pi's) recordings behave exactly as today. Both rules are off by default in `record_until_silence`.
- Neither new rule may end a recording until Nemotron has heard a real word in it: non-empty words-so-far that aren't wholly one of `speech_to_text.HALLUCINATIONS` (use `_imagined`).
- Voice drop: windows of 0.15 s (5 frames of `FRAME` = 480 samples); his loudness is the highest window level (RMS of int16/32768) among windows whose newest frame Silero calls speech (`probability >= detector.threshold`) after speech started; a frame also counts towards the 0.6 s quiet when its window level is below loudness times `voice_drop`. `voice_drop` = 10 ** (-dB / 20).
- No new words: ends when `words_quiet()` returns a number >= `no_words_after` (seconds of audio since the words last changed); `words_quiet()` returns None until a real word has been heard.
- Settings: `thresholds.press_voice_drop_db` default 18, limits 6 to 40; `thresholds.press_no_words_seconds` default 1.5, limits 0.8 to 5; both read as each recording starts ("now" on the page, section Sound and voice, Listening).
- Notes: one JSON line per push-to-talk recording whose speech (speech_started to speech_ended) lasts over 5 s, at `paths.LOGS_DIR/long-recordings.jsonl`; numbers only - never audio, never words; at most 50 lines; lines over 7 days old dropped at each write; nothing written after `NOTE_UNTIL = datetime.date(2026, 11, 28)`.
- `ended_by` values: "silence", "voice drop", "no new words", "limit", "feed ended", "no speech".
- Code style (memory note working-style): full-sentence why-comments, British spelling, spaced " - " dashes (never em dashes), Google-style docstrings; one commit per task; the repo is public.
- Tests: `NOVA_TREE=/home/nathaniel/work/Home-Assistant-ends NOVA_CHECK=ends nice -n 19 timeout 600 bash ~/work/Home-Assistant/.superpowers/tools/pi_test.sh <args>`; check `cat /proc/loadavg` first; one run at a time, never in the background, no repeat runs; check no other run is going on the Pi with `ssh pi 'pgrep -fa "m [p]ytest"'` (the brackets stop it matching itself). Never `ps` on the Pi. No `git stash`.

## Review Focus

1. Background called speech from the press, and he starts talking 2 s later: nothing ends before his first word (Task 2 `test_neither_rule_ends_it_before_a_real_word`).
2. A quiet word or a dip between syllables mid-sentence: not an end (Task 2 `test_a_short_quiet_dip_mid_sentence_doesnt_end_it`).
3. Nemotron running behind real time: its lag mustn't count as quiet words (Task 1 `test_quiet_words_counts_audio_not_clock_time`).
4. The press chime heard by the PC's mic louder than his voice: it mustn't set his loudness (Task 2 `test_loud_sound_silero_doesnt_call_speech_never_sets_the_loudness`).
5. The Pi unchanged, and a speech-to-text with no `quiet_words` (the test fakes, any other) handled (Task 3 `test_a_wake_word_satellite_gets_neither_rule`, `test_a_listener_without_quiet_words_gets_only_the_voice_drop`).

---

### Task 1: Nemotron says how long its words have been still

**Files:**
- Modify: `voice_assistant/nemotron.py`, `voice_assistant/speech_to_text.py`
- Test: `tests/test_speech_to_text.py`

**Interfaces:**
- Produces:
  - `Nemotron.words(stream) -> str`: the words so far (`self.recognizer.get_result(stream)`, stripped).
  - `NemotronTranscription.quiet_words() -> Optional[float]`: None until a real word has been heard in this recording; then the seconds of audio given to Nemotron since its words last changed. Thread-safe (the `_hear` thread updates it; the recording's thread reads it).
- In `_hear`, after each `nemotron.add(stream, piece)`: count the samples given so far; read `nemotron.words(stream)`; when it differs from the last words, note the count; once the words are non-empty and not `_imagined`, a real word has been heard.

- [ ] **Step 1: Write the failing tests** (extend `FakeNemotron` with a `words(stream)` that returns, for the stream's piece count, a script the test sets - e.g. `["", "", "Set", "Set a", "Set a", "Set a"]`):
  - `test_quiet_words_is_none_until_a_word`: pieces whose words stay "" give None; "You" (imagined) still None.
  - `test_quiet_words_counts_audio_since_the_words_last_changed`: 0.1 s pieces, words change at piece 3 then stay: after piece 8, `quiet_words()` is 0.5 (to 0.01).
  - `test_quiet_words_counts_audio_not_clock_time`: with `FakeNemotron.busy` holding pieces, waiting wall-clock time changes nothing: `quiet_words()` only grows as pieces are taken.
  - `test_a_nemotron_without_words_leaves_quiet_words_none`: a Nemotron whose `words` raises gives None, logs nothing more than once, and the transcript at finish is unchanged.
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** `Nemotron.words` and the tracking in `NemotronTranscription`
- [ ] **Step 4: Run them to see them pass** (plus the rest of tests/test_speech_to_text.py)
- [ ] **Step 5: Commit**: "Have Nemotron say how long its words have been still"

---

### Task 2: Two more ways a recording ends

**Files:**
- Modify: `voice_assistant/recording.py`
- Test: `tests/test_recording.py`

**Interfaces:**
- Produces: `record_until_silence(..., voice_drop: Optional[float] = None, words_quiet: Optional[Callable[[], Optional[float]]] = None, no_words_after: float = 1.5, report: Optional[Dict] = None)`. With `report` given it is filled with: `ended_by` (see Global Constraints), `loudness` (float or None), `levels` (a list: each 0.15 s window's level from speech start to the end, rounded to 4 places), `speech` (each window's mean Silero probability, rounded to 2), `words_quiet` (the last value seen, or None). `ended_by` is "voice drop" when any frame of the final quiet run counted only through the voice drop, else "silence".
- `VOICE_WINDOW = 5` (frames, 0.15 s).

- [ ] **Step 1: Write the failing tests** (the existing `FakeDetector` calls a frame speech above an absolute mean of 1000; add a fake `words_quiet` the test controls, e.g. returning None until a given feed position and then a value):
  - `test_a_quieter_background_ends_it_with_the_voice_drop`: 1 s at 8000, then 3 s at 1500 (speech to the fake detector, ~15 dB down), `voice_drop=10**(-12/20)`, words real from 0.3 s: ends about 0.6 s after the loud part (audio 1.6 s, to 0.1); `report["ended_by"] == "voice drop"`; with `voice_drop=None` it runs to the timeout and `ended_by == "limit"`.
  - `test_a_short_quiet_dip_mid_sentence_doesnt_end_it`: 8000 for 0.5 s, 1500 for 0.3 s, 8000 for 0.5 s, then 0: ends after the second loud part, `ended_by == "silence"`.
  - `test_neither_rule_ends_it_before_a_real_word`: 2 s at 1500 (background called speech from the press), then 8000 for 0.5 s, then 1500: with `words_quiet` returning None until 2.2 s then real, the recording lasts past 2.5 s (it isn't ended in the first 2 s by either rule).
  - `test_loud_sound_silero_doesnt_call_speech_never_sets_the_loudness`: a detector that calls the first 0.3 s quiet though it's 30000 (the chime), then 8000 speech, then 1500: loudness comes from the 8000 (report's loudness about 0.244, not 0.9), and it ends by the voice drop.
  - `test_words_still_for_long_enough_end_it`: steady 8000 (speech throughout), `words_quiet` real and growing with the audio from 0.5 s: ends when it reaches 1.5 (audio about 2.0 s), `ended_by == "no new words"`; with words that keep changing (always 0.1) it runs to the limit.
  - `test_the_report_is_numbers_only`: every value in the report is a number, None, a string from the `ended_by` list, or a list of numbers.
  - `test_without_the_new_arguments_nothing_changes`: the existing recording tests' cases give the same lengths with `report={}` passed (and `ended_by` "silence", "feed ended", "no speech" as each ends).
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement** in `record_until_silence` (keep the per-frame cost tiny: a running sum over the last 5 frames' squares, no per-frame array copies)
- [ ] **Step 4: Run them to see them pass** (all of tests/test_recording.py)
- [ ] **Step 5: Commit**: "End a recording when the voice drops away or the words stop, when asked"

---

### Task 3: The PC's recordings use them, from two settings

**Files:**
- Modify: `config.yaml` (thresholds, after `silence_duration`, each with a comment), `voice_assistant/config.py` (LIMITS), `voice_assistant/settings_page.py` (EDITABLE, after `silence_duration`, "now"), `voice_assistant/session.py` (`_handle_interaction`)
- Test: `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: Task 1's `quiet_words`, Task 2's arguments and report.
- In `_handle_interaction`, when `self.satellite.push_to_talk`: `voice_drop=10 ** (-db / 20)` from `thresholds.press_voice_drop_db`, `words_quiet=getattr(listening, "quiet_words", None)`, `no_words_after` from `thresholds.press_no_words_seconds`; always `report={}`; then `timeline.note(ended_by=report.get("ended_by"))`. Read with the other thresholds (once per recording).
- Page labels: "Quieter than your voice that counts as stopped, at a computer (dB)" and "Seconds without a new word that end it, at a computer".
- Produces for Task 4: the `report` dict and the recording's speech seconds available after `record_until_silence` returns (from the timeline's speech_started/speech_ended marks or the report).

- [ ] **Step 1: Write the failing tests**:
  - config: defaults 18 and 1.5; 5 and 41 refused for the dB; 0.5 and 6 refused for the seconds.
  - settings page: both rows under Sound and voice with those labels, used straight away.
  - session: `test_a_push_to_talk_satellite_gets_both_rules` (a fake `record_until_silence` spy, or the real one with a spy detector: the call carries `voice_drop` about 0.126, the listener's `quiet_words`, 1.5); `test_a_saved_setting_is_used_by_the_next_recording`; `test_a_wake_word_satellite_gets_neither_rule` (voice_drop None, words_quiet None); `test_a_listener_without_quiet_words_gets_only_the_voice_drop`; `test_the_timeline_notes_what_ended_it`.
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to see them pass**, then the page's JS tests (`~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs`, niced, after the load check)
- [ ] **Step 5: Commit**: "Use the new ends for recordings at a computer, from two settings"

---

### Task 4: Note the long ones for a week, as numbers

**Files:**
- Create: `voice_assistant/long_recordings.py`
- Modify: `voice_assistant/session.py` (after the recording, push-to-talk only), wherever the brain makes per-session helpers if needed
- Test: `tests/test_long_recordings.py`, `tests/test_session.py`

**Interfaces:**
- Produces: `LONGEST_SPEECH = 5.0`, `NOTE_UNTIL = datetime.date(2026, 11, 28)`, `MOST = 50`, `KEEP_DAYS = 7`; `note_long_recording(path: str, at: datetime.datetime, speech_seconds: float, report: Dict, until: datetime.date = NOTE_UNTIL) -> bool` - appends one line `{"at": ISO, "speech_seconds": ..., **report}` when `speech_seconds > LONGEST_SPEECH` and `at.date() <= until`; rewrites the file keeping only lines within `KEEP_DAYS` of `at` and the newest `MOST`; returns whether it wrote. Never raises (logs a warning once). Path: `os.path.join(paths.LOGS_DIR, "long-recordings.jsonl")`.
- The session calls it only for push-to-talk satellites, with the Task 3 report, off its own thread's critical path (it's a small file; writing inline after the recording is fine, before speech-to-text finishes is not required).

- [ ] **Step 1: Write the failing tests**: writes only over 5 s; never after NOTE_UNTIL; keeps at most 50; drops lines over 7 days; the line has no audio and no text (only the report's keys plus at and speech_seconds); a file it can't write logs once and returns False; session: a push-to-talk recording over 5 s is noted, a Pi one isn't.
- [ ] **Step 2: Run them to see them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to see them pass**
- [ ] **Step 5: Commit**: "Note a week of long recordings at the computer, as numbers only"

---

### Task 5: Decision record, the whole suite, ruff

**Files:**
- Create: `docs/decisions/<next free>-the-pc-knows-when-you-stop.md` (check `ls docs/decisions` and `git ls-tree --name-only origin/main docs/decisions/` after a fetch; 0036 if free)
- Modify: `docs/release-checklist.md` (a short section after the orb's: his check of normal use at the PC, with a game or video playing; how to read long-recordings.jsonl; the two settings)

- [ ] **Step 1: Write the decision record** (why, the evidence, the two rules and why both, the first-word wait, numbers-only notes and why - the weekly backup keeps up to 3 months - the defaults and their settings, what isn't done: the Pi, echo cancelling, telling voices apart)
- [ ] **Step 2: The whole suite, twice, one after the other**: plain, then `TZ_OVERRIDE=UTC`. Expected: all pass.
- [ ] **Step 3: Ruff**: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ends && /tmp/nova-check-pytest/bin/ruff check .'`
- [ ] **Step 4: Commit**: "Decision NNNN: the PC knows when you stop speaking"

Deploy is his named job (brain only: push to GitHub's main, pull as nova, restart nova-backend).
