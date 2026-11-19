# Moving Music and Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Nova's music from one satellite to another by voice, start music on a named satellite, and save spoken notes as text files in a PC's or Mac's Downloads folder.

**Architecture:** Each satellite says in its hello what machine it is and whether it saves notes; the brain keeps that in `satellite_rooms.json` with names set on the settings page, and matches spoken words to a satellite. Moving music hands the Navidrome player's queue and place in the song from one session's player to another's. A note is a new `save_note` message the brain sends to the satellite spoken to, which writes the file itself and answers `note_saved`.

**Tech Stack:** Python 3.13 (brain and satellites), ffmpeg (decoding), vanilla JS settings page with Node's test runner.

**Spec:** `docs/superpowers/specs/2026-11-19-music-moves-and-notes-design.md`

## Global Constraints

- Machine values: `pi`, `pc`, `mac`, `linux`. Machine words: `pi`: "pi", "pie", "raspberry pi", "raspberry pie"; `pc`: "pc", "computer", "desktop"; `mac`: "mac", "macbook", "laptop", "computer"; `linux`: none. Spoken in replies: "the Pi", "the PC", "the Mac"; else the first page name; else the satellite's name. A page name, when set, is used in replies before the machine word.
- Hello fields `machine` and `saves_notes` are optional both ways: a missing or invalid value is taken as unknown (`None`, `False`), never a refusal. An old brain must keep admitting a new satellite and a new brain an old one.
- Page names ("Also called"): split on commas, trimmed, empties dropped, at most 5 names of at most 30 characters each; a name another satellite has (its own name or a page name) is refused.
- Notes: only into the Downloads folder itself; never overwrite (exclusive create); `.txt`; title made safe and at most 80 characters, "Note" if empty; " (2)" to " (99)" when taken; UTF-8; `\r\n` line endings on Windows, `\n` elsewhere; text over 50,000 characters refused; the brain waits 5 seconds for `note_saved`; the note's text is never logged.
- Copy fixed by the spec: "Moved to the PC." / "It's already on the PC." / "The PC isn't connected, so the music's staying on the Pi." / "There's no music to move." / "Which one: the PC or the Mac?" / "Saved to your Downloads as Bins." / "I couldn't save it on the PC: <why>." / "The PC didn't answer, so the note wasn't saved." / "Moving music only works with Navidrome." (with "the PC" and "the Pi" standing for the spoken names, capitalised at the start of a sentence).
- The Pi's behaviour is unchanged apart from saying `machine: pi` in its hello: no notes tool there.
- House style, tests on the Pi, CT 109 load limits and "never push or deploy": `.superpowers/sdd/2026-11-19-music-moves-and-notes/common.md`.

## Review Focus

1. Moving music while Nova is replying in the target's room: the target must start only when the reply ends, and the source must not resume afterwards. (Task 6, `test_move_during_a_reply_waits_for_it_and_the_source_stays_quiet`)
2. Moving a song that was itself moved part way: the place handed on is the earlier offset plus what has played since. (Task 5, `test_hand_over_twice_counts_the_first_offset`)
3. A note title that tries to leave Downloads or is otherwise hostile (`../../x`, `C:\Windows\x`, `con`, `.bashrc`, empty, 500 characters) still lands as a safe new file directly in Downloads. (Task 1, `test_hostile_titles_stay_in_downloads`)
4. A satellite that never answers `save_note`, or drops while the brain waits: the brain gives up after 5 seconds or at once on a drop, and a late `note_saved` is ignored. (Task 2, `test_save_note_gives_up_after_the_wait` and `test_save_note_ends_when_the_satellite_goes`)
5. Existing phrases keep their meaning: "send it to my mac" and "put it on my phone" still send the reply, "add milk to my notes" at the PC is still the list, "play" alone still resumes. (Task 6, `test_send_and_put_phrases_still_send_the_reply`; Task 7, `test_add_to_my_notes_is_still_the_list_at_the_pc`)

---

### Task 1: What machine this is, its Downloads folder, and saving a note

**Files:**
- Create: `voice_assistant/machine.py`, `voice_assistant/notes.py`
- Test: `tests/test_machine.py`, `tests/test_notes.py`

**Interfaces:**
- Produces: `machine.MACHINES = ("pi", "pc", "mac", "linux")`; `machine.machine_kind() -> str`; `machine.downloads_folder() -> Optional[str]`; `notes.MOST_TEXT = 50_000`; `notes.NoteRefused(Exception)` (its message is a sentence to say); `notes.safe_name(title: str) -> str` (no extension); `notes.save_note(folder: str, title: str, text: str, windows: bool) -> str` (the file name written, e.g. "Bins.txt").

- [ ] **Step 1: Write the failing tests**

`tests/test_machine.py`: `test_windows_is_a_pc` (monkeypatch `sys.platform` to "win32" → "pc"), `test_macos_is_a_mac` ("darwin" → "mac"), `test_a_raspberry_pi_is_a_pi` (Linux, the model file read gives "Raspberry Pi 5 Model B Rev 1.0\x00" → "pi"; read the path through a module constant `MODEL_FILE = "/proc/device-tree/model"` so the test points it at a tmp file), `test_other_linux_is_linux` (no model file → "linux"), `test_downloads_on_macos_is_home_downloads` (darwin, HOME a tmp dir with `Downloads` → that path), `test_no_downloads_folder_is_none` (darwin, no folder → None), `test_linux_has_no_downloads_for_notes` (linux → None even if ~/Downloads exists), `test_windows_falls_back_to_the_profile_folder` (win32, the known-folder lookup made to raise `OSError`, `USERPROFILE` a tmp dir with `Downloads` → that path).

`tests/test_notes.py`: `test_saves_a_new_text_file` (returns "Bins.txt", content exactly the text, `\n`), `test_windows_line_endings` (windows=True → `"a\r\nb"` on disk for `"a\nb"`), `test_never_overwrites` (existing "Bins.txt" → "Bins (2).txt", original unchanged; then "Bins (3).txt"), `test_gives_up_after_99` (Bins.txt and (2)..(99) present → `NoteRefused`), `test_hostile_titles_stay_in_downloads` (each of `"../../x"`, `"C:\\Windows\\x"`, `"con"`, `".bashrc"`, `""`, `"a" * 500`, `"bins\x00\x07"`, `"what?.  "` saves a file whose parent is the folder itself, no leading dot, at most 84 characters with `.txt`, `con` → "con note.txt", empty → "Note.txt"), `test_too_long_text_is_refused` (50,001 characters → `NoteRefused`, nothing written), `test_missing_folder_is_refused` (`NoteRefused`).

- [ ] **Step 2: Run them and see them fail**

Run: `bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh tests/test_machine.py tests/test_notes.py` (with `NOVA_TREE`/`NOVA_CHECK` from the dispatch). Expected: import errors.

- [ ] **Step 3: Implement `machine.py` and `notes.py`**

`downloads_folder()`: Windows asks Windows for the Downloads known folder (`SHGetKnownFolderPath` through `ctypes`, FOLDERID_Downloads `{374DE290-123F-4565-9164-39C4925E467B}`, freeing the string with `CoTaskMemFree`), falling back to `%USERPROFILE%\Downloads` on any `OSError`/`AttributeError`; macOS `~/Downloads`; anything else None; None too when the folder isn't a directory. `safe_name`: drop `<>:"/\|?*` and characters below 32, collapse whitespace, strip leading dots and spaces and trailing dots and spaces, cut to 80, "Note" if empty, append " note" to Windows' reserved names (CON, PRN, AUX, NUL, COM1-9, LPT1-9, any case). `save_note` opens with mode `"x"`, `encoding="utf-8"`, `newline="\r\n" if windows else "\n"`; `FileExistsError` moves to the next number. Sentences for `NoteRefused`: "That note's too long to save.", "There's no Downloads folder to save it in.", "There are already 99 notes called that.", and for another `OSError` "The file couldn't be written."

- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.

- [ ] **Step 5: Commit** — "Say what machine a satellite is, and save a note safely in its Downloads folder"

### Task 2: The hello's new fields, and save_note over the link

**Files:**
- Modify: `voice_assistant/protocol.py` (`greet`, `admit`, the message list in the module docstring), `voice_assistant/satellite_link.py` (passes the two fields to `greet`), `satellite.py` (hello values; `save_note` handler), `voice_assistant/remote.py` (`RemoteSatellite`), `voice_assistant/satellite_rooms.py` (`seen` keeps them), `voice_assistant/brain.py:948` (passes them to `seen`)
- Test: `tests/test_protocol.py`, `tests/test_satellite_link.py`, `tests/test_satellite.py`, `tests/test_remote.py`, `tests/test_satellite_rooms.py`, `tests/test_brain.py`

**Interfaces:**
- Consumes: Task 1's `machine_kind`, `downloads_folder`, `save_note`, `NoteRefused`.
- Produces: `greet(..., machine: Optional[str] = None, saves_notes: bool = False)`; `admit(...)` result gains `"machine": Optional[str]` (one of `MACHINES` or None) and `"saves_notes": bool`; `RemoteSatellite.machine: Optional[str]`, `RemoteSatellite.saves_notes: bool`, `RemoteSatellite.save_note(title: str, text: str, wait: float = NOTE_WAIT) -> Optional[Dict]` with `NOTE_WAIT = 5.0`, returning `{"file": "Bins.txt"}` or `{"error": "<sentence>"}`, or None if it didn't answer or went; `SatelliteRooms.seen(name, push_to_talk, machine: Optional[str] = None, saves_notes: bool = False)`, `SatelliteRooms.machine(name) -> Optional[str]`, `SatelliteRooms.saves_notes(name) -> bool`. Messages: brain → satellite `save_note {id, title, text}`; satellite → brain `note_saved {id, file}` or `{id, error}`.

- [ ] **Step 1: Write the failing tests**

- `test_protocol.py`: `test_hello_says_the_machine_and_notes` (greet with machine="pc", saves_notes=True → admit returns both); `test_hello_leaves_them_out_when_unknown` (greet with defaults sends neither key, so an old brain's hello is unchanged); `test_odd_machine_or_notes_values_are_unknown_not_refused` (raw hello with `machine: 5` and `saves_notes: "yes"` → admitted, `None` and `False`); `test_old_hello_is_still_admitted` (no keys → `None`, `False`).
- `test_satellite.py`: `test_hello_carries_machine_and_notes` (the satellite's link is made with `machine_kind()` and `downloads_folder() is not None`); `test_save_note_writes_and_answers` (a `save_note` message → file in a tmp Downloads, `note_saved` sent with the same id and `file`); `test_save_note_refusal_is_answered` (`NoteRefused("That note's too long to save.")` → `note_saved` with `error` of that sentence); `test_save_note_without_downloads_answers_an_error`; `test_save_note_never_logs_the_text` (caplog has no part of the text).
- `test_remote.py`: `test_save_note_sends_and_returns_the_answer`; `test_save_note_gives_up_after_the_wait` (no answer → None after `wait`, a late `note_saved` for that id is ignored without error); `test_save_note_ends_when_the_satellite_goes` (connection lost while waiting → None well before the wait ends); `test_hello_fields_kept` (`machine`, `saves_notes` from the hello, defaults when absent).
- `test_satellite_rooms.py`: `test_seen_keeps_machine_and_notes_across_a_restart`; `test_file_from_before_is_read` (entries with only `push_to_talk` and `area` → machine None, saves_notes False); `test_an_old_brain_can_still_read_the_new_file` (the written entries still have `push_to_talk` bool and `area`).
- `test_brain.py`: `test_connecting_records_machine_and_notes`.

- [ ] **Step 2: Run them and see them fail.**

- [ ] **Step 3: Implement.** The satellite answers `save_note` on a short thread of its own so the link's reader never waits on the disk; it logs "Saved a note as Bins.txt" or the refusal, never the text. The brain matches answers to waiters by id (a dict of `threading.Event`s under a lock); `_lost` wakes every waiter. `seen` saves only when something changed.

- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.

- [ ] **Step 5: Commit** — "Satellites say what machine they are and whether they save notes, and the brain can ask one to save a note"

### Task 3: Naming a satellite

**Files:**
- Create: `voice_assistant/satellite_names.py`
- Modify: `voice_assistant/satellite_rooms.py` (page names), `voice_assistant/brain.py` (lookups)
- Test: `tests/test_satellite_names.py`, `tests/test_satellite_rooms.py`, `tests/test_brain.py`

**Interfaces:**
- Consumes: Task 2's `SatelliteRooms.machine`.
- Produces: `satellite_names.MACHINE_WORDS: Dict[str, Tuple[str, ...]]`; `satellite_names.plain(said: str) -> str`; `satellite_names.matching(said: str, satellites: Dict[str, Tuple[Optional[str], List[str]]]) -> List[str]` (keyed by satellite name, value `(machine, page names)`); `satellite_names.spoken(name: str, machine: Optional[str], called: List[str]) -> str` ("the PC"); `SatelliteRooms.called(name) -> List[str]`; `SatelliteRooms.set_called(name: str, called: List[str])` (raises `OSError` if it can't be saved); `Brain.find_satellite(said: str) -> Tuple[Optional[str], Optional[str]]` (name, or None and the sentence to say); `Brain.names_satellite(said: str) -> bool`; `Brain.spoken_name(name: str) -> str`.

- [ ] **Step 1: Write the failing tests**

`test_satellite_names.py`: `test_plain_drops_articles_and_satellite` ("The PC" / "my pc" / "the PC satellite" / "p.c." → "pc"; "home-assistant" → "home assistant"); `test_machine_words_find_it` ({"home-assistant": ("pi", []), "pc": ("pc", [])}: "my pi" and "pie" → ["home-assistant"], "the computer" → ["pc"]); `test_its_own_name_finds_it` ("home assistant" → ["home-assistant"]); `test_page_names_win_over_machine_words` (pc called ["computer"], a Mac also present → "computer" → ["pc"]); `test_two_matches_are_both_given` ("computer" with a PC and a Mac and no page names → both); `test_nothing_matches` ("phone" → []); `test_spoken` (pi → "the Pi", pc → "the PC", mac → "the Mac", page names ["Desk"] → "Desk", linux with no page names → its name).

`test_satellite_rooms.py`: `test_called_is_kept_across_a_restart`, `test_called_defaults_to_none`.

`test_brain.py`: `test_find_satellite_by_machine_word`; `test_find_satellite_ambiguous_says_which` (→ `(None, "Which one: the PC or the Mac?")`); `test_find_satellite_none_lists_them` (→ None and a sentence naming the satellites there are, e.g. "There's no satellite called the phone. There's the Pi and the PC."); `test_find_satellite_includes_disconnected_ones`.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Matching tiers: first the satellite's own name and page names; only if none match, machine words. Candidates are every satellite the brain lets in (`Brain.satellites()`), connected or not.
- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.
- [ ] **Step 5: Commit** — "Know a satellite by what it is or what he calls it"

### Task 4: The settings page: what each satellite is, and what it's also called

**Files:**
- Modify: `voice_assistant/settings_page.py` (`_satellites`, new `set_called`, route `/api/satellites/called`), `voice_assistant/web/settings.js` (the Satellites row, near `travelsBox` at line 938)
- Test: `tests/test_settings_page.py`, `tests/web/settings_rows.test.mjs` (or whichever page test file covers the Satellites rows)

**Interfaces:**
- Consumes: Task 2's `SatelliteRooms.machine`/`saves_notes`, Task 3's `called`/`set_called`/`satellite_names.plain`.
- Produces: each satellite in `/api/state` gains `"machine": Optional[str]`, `"called": List[str]`, `"saves_notes": bool`; `SettingsPage.set_called(name, called, who) -> Dict` taking the text box's string.

- [ ] **Step 1: Write the failing tests**

`test_settings_page.py`: `test_satellites_say_machine_called_and_notes`; `test_set_called_saves_trimmed_names` ("Desk, , computer " → ["Desk", "computer"]); `test_set_called_refuses_too_many_or_too_long` (6 names; a 31-character name → `success: False` and a sentence, nothing saved); `test_set_called_refuses_a_name_another_has` (the Pi's own name or another's page name, compared through `plain` → refused); `test_set_called_refuses_unknown_satellite_and_odd_types`; `test_set_called_empty_clears`.

Page test: the Satellites row shows "Raspberry Pi", "PC" or "Mac" for those machines (nothing for unknown or linux), "Saves notes to its Downloads" when `saves_notes`, and an "Also called" text box holding the names joined with ", " which posts `{name, called}` to `/api/satellites/called` on change. Keep to the existing page-test patterns; no equality asserts on DOM nodes or large objects.

- [ ] **Step 2: Run them and see them fail.** Page tests: `~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs`, and `node --check voice_assistant/web/settings.js`.
- [ ] **Step 3: Implement.** Mirror `set_travels` (type checks, `_satellite_names`, `SATELLITES_UNREADABLE`, log line with `who`).
- [ ] **Step 4: Run them and see them pass**, then the page tests, the whole Python suite in both zones and ruff.
- [ ] **Step 5: Commit** — "Show what each satellite is on the page, and let him give it other names"

### Task 5: Handing the music from one player to another

**Files:**
- Modify: `voice_assistant/navidrome.py` (`NavidromePlayer`)
- Test: `tests/test_navidrome.py`

**Interfaces:**
- Produces: `NavidromePlayer.has_music() -> bool` (a queue with a song at or after the index); `NavidromePlayer.is_playing() -> bool` (has music and isn't paused, or was playing before a hold); `NavidromePlayer.hand_over() -> Optional[Dict]` returning `{"queue", "unshuffled", "index", "position": float seconds, "repeat", "shuffle"}` and stopping and emptying this player, or None with no music; `NavidromePlayer.take_over(state: Dict) -> Dict` (`{"success": True, "message": "Playing"}`), replacing its queue and playing from `position` seconds into the song at `index`, paused until the end of a reply if one is under way (as `_start` does).

- [ ] **Step 1: Write the failing tests**

`test_hand_over_gives_queue_place_and_modes`; `test_hand_over_with_nothing_is_none`; `test_hand_over_empties_and_stops_this_player` (its playback stopped, `has_music()` False, a later `resume_after_speech` plays nothing); `test_take_over_starts_at_the_place` (the decode for the first song is asked to start at the offset: assert the ffmpeg arguments hold `-ss` and the seconds after `-i pipe:0`; the next song starts at 0); `test_hand_over_twice_counts_the_first_offset` (taken over at 60 s, 10 s played → hand_over gives 70); `test_take_over_during_a_reply_waits` (`pause_for_speech` first → stays paused until `resume_after_speech`); `test_paused_music_plays_once_taken_over`; `test_skip_after_take_over_starts_the_next_at_zero`.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** The place is the offset the current playback began at plus `playback.position / playback.sample_rate` (a speaker without these counts 0). `_decode(song, start: float = 0.0)` adds `-ss <start>` after the input when start > 0; the offset is used only by the song the take-over jumped to and cleared by any other jump.
- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.
- [ ] **Step 5: Commit** — "Let one Navidrome player hand its music to another, from the same place"

### Task 6: Moving music and playing on another satellite

**Files:**
- Modify: `voice_assistant/brain.py` (`move_music`), `voice_assistant/session.py` (wiring), `voice_assistant/actions.py` (`move_music` tool, `on` for `play_music` and `play_playlist`), `voice_assistant/intents.py` (the move rule)
- Test: `tests/test_brain.py`, `tests/test_session.py`, `tests/test_actions.py`, `tests/test_intents.py`

**Interfaces:**
- Consumes: Task 3's `find_satellite`, `names_satellite`, `spoken_name`; Task 5's `has_music`, `is_playing`, `hand_over`, `take_over`.
- Produces: `Brain.move_music(spoken_at: str, said: str) -> Dict`; `ActionExecutor(..., mover: Optional[Callable[[str], Dict]] = None, elsewhere: Optional[Callable[[str], Tuple[Optional["ActionExecutor"], Optional[str]]]] = None)`; tool `move_music(to: str)` with `needs=("music", "mover")`; `play_music(query=None, search_type=None, on=None)` and `play_playlist(name, on=None)`; `LocalIntents(..., names_satellite: Optional[Callable[[str], bool]] = None)`.

- [ ] **Step 1: Write the failing tests**

`test_brain.py` (fake sessions with fake players): `test_move_from_the_satellite_spoken_to`; `test_move_from_the_room_when_here_has_none`; `test_move_from_the_one_playing_anywhere`; `test_move_with_no_music_says_so` ("There's no music to move."); `test_move_to_itself_says_already` ("It's already on the PC."); `test_move_to_a_disconnected_one_stays` ("The PC isn't connected, so the music's staying on the Pi."); `test_move_ambiguous_moves_nothing`; `test_move_with_spotify_says_navidrome_only`; `test_move_success_says_moved` ("Moved to the PC.", source `hand_over` then target `take_over` with that state, after the source's `music.wait()`).

`test_session.py`: `test_move_during_a_reply_waits_for_it_and_the_source_stays_quiet` (Pi and PC in one room, conversation at the Pi moves to the PC: the PC's music stays paused until the conversation ends, then plays; the Pi plays nothing after); `test_elsewhere_gives_the_named_sessions_executor`.

`test_actions.py`: `test_move_music_tool_calls_the_mover`; `test_play_music_on_another_satellite_plays_there` (the target executor's `play_music` gets the query; this player untouched); `test_play_music_on_with_no_query_moves`; `test_play_music_on_this_satellite_plays_here`; `test_play_music_on_unknown_says_why`; `test_play_playlist_on_another_satellite`.

`test_intents.py` (with `names_satellite` true for "pc", "the pc", "my pi", "mac"): `test_move_phrases` ("play it on my pi", "move this to the pc", "switch to the pc", "change it to the pc", "move the music over to the pc", "transfer the song to the pc", "take it to my pi" → `Action("move_music", {"to": ...})` with the satellite words); `test_move_words_that_name_no_satellite_fall_through` ("switch to the next song", "change it to 50" → not move_music); `test_send_and_put_phrases_still_send_the_reply` ("send it to my mac", "put it on my phone", "put that on my mac" → their existing actions even though "mac" names a satellite); `test_bare_play_still_resumes`; `test_switch_on_the_pc_is_still_power` ("switch on the pc", "switch on my pc" → `pc_power` on, with "pc" naming a satellite); `test_without_names_satellite_nothing_moves`.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** The intent rule goes before the send-to-phone and send-to-device rules, with verbs move, switch, change, swap, transfer, play, take; an optional "it", "this", "that", "the music", "music", "the song", "this song"; then "to", "onto" or "over to" (and "on" only after "play", so "switch on my PC" and "turn on the PC" stay the PC's power rules); then up to four words, handled only when `names_satellite` says they name one. Tool descriptions: `move_music` for "play it on my Pi", "move this to the PC"; `on` only when the user names somewhere other than here. The session gives `mover=lambda said: brain.move_music(self.satellite.name, said)` and an `elsewhere` returning the named connected session's `actions`, or None and `find_satellite`'s sentence, or "<Spoken> isn't connected." for one away.
- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.
- [ ] **Step 5: Commit** — "Move the music to another satellite, or play something there, by voice"

### Task 7: Notes by voice at a computer

**Files:**
- Modify: `voice_assistant/actions.py` (`save_note` tool), `voice_assistant/session.py` (`_save_note`, wiring `notes` only when `satellite.saves_notes`)
- Test: `tests/test_actions.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: Task 2's `RemoteSatellite.save_note`, `saves_notes`; Task 3's `spoken_name`.
- Produces: `ActionExecutor(..., notes: Optional[Callable[[str, str], Dict]] = None)`; tool `save_note(title: str, text: str)` with `needs="notes"`, both required.

- [ ] **Step 1: Write the failing tests**

`test_actions.py`: `test_save_note_offered_only_with_notes` (no `notes` → not among the tools offered); `test_save_note_passes_title_and_text`; `test_save_note_description_keeps_the_list_apart` (the description says "add X to my notes" is the list, and the 400-word limit).

`test_session.py`: `test_save_note_reply_names_the_file_without_extension` ("Saved to your Downloads as Bins."); `test_save_note_error_is_said` ("I couldn't save it on the PC: There's no Downloads folder to save it in."); `test_save_note_no_answer` ("The PC didn't answer, so the note wasn't saved."); `test_pi_session_has_no_notes_tool`; `test_add_to_my_notes_is_still_the_list_at_the_pc` (local intent → `add_to_list`, never `save_note`).

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Tool description: save a note as a text file in the Downloads folder of the computer the user is at, for "make a note", "take a note", "write this down", "write me notes on ..."; `text` is the user's own words when they dictate, or plain-text notes of at most about 400 words when they ask for notes on something; `title` a few words; "add X to my notes" goes on the notes list instead. The session checks `len(text) <= MOST_TEXT` before sending (the same sentence as the satellite's) and strips the extension for the reply.
- [ ] **Step 4: Run them and see them pass**, then the whole suite in both zones and ruff.
- [ ] **Step 5: Commit** — "Save a note in the computer's Downloads folder by voice"

### Task 8: The record

**Files:**
- Create: `docs/decisions/0032-music-that-moves-and-notes-on-the-computer.md`
- Modify: `docs/release-checklist.md` (a new numbered hand-check section), `README.md` (the features list, one line each)

- [ ] **Step 1: Write the decision record** in the shape of 0026 (Context, Decision, The calls, Not done), from the spec and the commits; it first noted that 0029 was another branch's, and at the merge it was renumbered 0032.
- [ ] **Step 2: Add the hand checks:** move music Pi → PC and back mid-song (same place, same queue, the source silent); "play Drake on the PC" from the Pi; moving to the PC while it's asleep; "computer" with page names; a note at the PC (file in Downloads, opens in Notepad, CRLF), a second with the same title, "add milk to my notes" still the list; the page's "Also called" box.
- [ ] **Step 3: Commit** — "Record decision 0030 and its hand checks" (renumbered 0032 at the merge)
