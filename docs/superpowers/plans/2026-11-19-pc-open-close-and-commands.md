# Opening and Closing Things on the PC, and PC Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At the PC (or naming it from the Pi), Nova opens apps, games, web pages and Google searches, closes programs and the front window, takes bare power words, and sets volume and mute on the named device, else the one spoken to.

**Architecture:** The Windows satellite builds its own list of what it can open (Start menu, Epic, Steam) and of programs with open windows, sends both up, and carries out `computer` requests by id. The brain keeps each satellite's last list, matches what was said (local intents first, Claude tools for other wordings), resolves which satellite is meant, and sends the request. Power stays on HASS.Agent via `PCPower`; a sleep/lock/screen-off press runs after the reply is said.

**Tech Stack:** Python 3.13 (PC) / 3.11 (brain, Pi), ctypes for Win32 windows, Core Audio through `pycaw` + `comtypes` (pinned), PowerShell `Get-StartApps`, the existing line-of-JSON protocol, vanilla JS settings page.

**Spec:** `docs/superpowers/specs/2026-11-19-pc-open-close-and-commands-design.md` (read it first; this plan argues from it).

## Global Constraints

- A spoken word never becomes a shell command or a path: the PC opens only an item of its own current list, by that item's id, or an `https://` URL with no user:password part, at most 2,000 characters.
- Names starting "Uninstall" are never in the list. Never terminate a process; close is `WM_CLOSE` only. Never close Nova's own process, `explorer.exe`, the desktop or the taskbar.
- Power words act only as the whole request, only at a satellite whose `machine` is `pc` (or with the PC named, as today's `put my PC to sleep` shapes). Shut down and restart keep the existing ask-first flow.
- Volume and mute: named device, else the one spoken to. At the PC: Windows' master volume. At the Pi: its own volumes, music only on its own player. Alarm category is always Nova's alarm volume. While something rings or Nova is speaking, "mute" stays a stop word.
- The browser is Windows' default, never named in code. Search site setting `computer.search_site`: `google` (default), `duckduckgo`, `bing`.
- New hello fields are optional both ways: an old brain or satellite sees the hello it always did.
- App names from his PC never go in the repo (tests use Fortnite, Discord, Steam, Calculator and made-up names). URLs are logged by host only.
- House style: match the surrounding code's docstrings and comment density; British spelling, no em dashes. One commit per task, regression tests that fail on the old code. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- CT 109 load limits: check `/proc/loadavg`; tests only through `NOVA_TREE=/home/nathaniel/work/Home-Assistant-open NOVA_CHECK=pc-open bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh <args>` (runs on the Pi), one suite at a time, no repeat runs; anything local under `nice -n 19 timeout 600`. Never `ps` on the Pi. No `git stash`.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/pc-open && /tmp/nova-check-pytest/bin/ruff check .'` after a pi_test run.
- Nothing is pushed, deployed or run on the PC without his named job.

## Review Focus

1. "Start a timer for 5 minutes", "open the garage", "close the garage door", "close the blinds" at the PC keep their present meaning. Tests in Task 6 (`test_fall_through_untouched`).
2. "I'm going to sleep after this", "restart the song", "lock the front door" never touch the PC, at the Pi or the PC. Tests in Task 6 (`test_power_words_only_whole`).
3. The PC disconnects mid-request (asleep): the brain's wait ends at once with "Your PC's off." / "Nova isn't running on your PC.", no hang. Tests in Task 2 (`test_ask_ends_when_lost`) and Task 5 (`test_where_pc_asleep`).
4. A name in two catalogues or twice in one ("Calendar" twice, "Overwatch" vs "Overwatch 2", "Fortnite" vs "LEGO Fortnite Content"): exact wins, duplicates collapse, content entries dropped. Tests in Task 3 and Task 8.
5. "Mute" said while an alarm rings on the Pi and the PC is the one spoken to stops the alarm, does not mute Windows. Test in Task 7 (`test_mute_while_ringing_stops`).

---

### Task 0: Throwaway check on the PC that an opened window comes to the front (his named job)

No repo code. Needs his named job ("run the foreground test on the PC"). Proves the method Task 9 uses.

- [ ] **Step 1:** Write a scratch script (scratchpad, not the repo) that, run from Task Scheduler as a windowless `pythonw` in his session (as the satellite runs), calls `AllowSetForegroundWindow(ASFW_ANY)` then `os.startfile("https://example.com")`, waits up to 3 s for Firefox's top-level window, and raises it (`ShowWindow(SW_RESTORE)` if minimised, then `SetForegroundWindow`; if refused, the `AttachThreadInput` to the foreground thread fallback). Same for `shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App`.
- [ ] **Step 2:** Run it with Firefox behind another window and with Firefox minimised. Record which call made it work. Remove the scratch task and script from the PC.
- [ ] **Step 3:** Write the finding (which call, any caveat) as one paragraph at the top of Task 9's notes in this plan and commit.

### Task 1: Protocol: new hello fields and messages

**Files:**
- Modify: `voice_assistant/protocol.py` (module docstring message lists; `greet`; `admit`)
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces: `greet(..., opens: bool = False, system_volume: bool = False)`; `admit()` result gains `"opens": bool`, `"system_volume": bool` (False if left out or not a bool, with a warning, never a refusal). Docstring lists: satellite→brain `apps` (payload: JSON list of `{"id","name","kind"}`), `windows` (payload: JSON list of names), `computer_done {"id", "said"?, "error"?, "level"?, "muted"?}`; brain→satellite `computer {"id", "action": "open"|"open_url"|"close"|"close_front"|"volume", "item"?, "url"?, "name"?, "direction"?, "step"?, "level"?, "mute"?}`.
- Produces: `COMPUTER_ACTIONS = ("open", "open_url", "close", "close_front", "volume")`, `APP_KINDS = ("app", "epic", "steam")`.

- [ ] **Step 1: Write the failing tests**
  - `test_hello_opens_and_volume_sent_only_when_true`: `greet(..., opens=True, system_volume=True)` sends both. With defaults, neither key is present.
  - `test_admit_reads_opens_and_volume`: both True come back True. Missing keys give False.
  - `test_admit_bad_opens_is_false_not_refused`: `"opens": "yes"` is admitted, with `opens` False.
- [ ] **Step 2:** Run `pi_test.sh tests/test_protocol.py`. Expected: the new tests fail.
- [ ] **Step 3:** Implement following the `saves_notes` pattern; update the module docstring lists.
- [ ] **Step 4:** Run `pi_test.sh tests/test_protocol.py`. Expected: pass.
- [ ] **Step 5:** Commit `Say in the hello whether a satellite opens things and has system volume`.

### Task 2: The brain's side of a satellite: lists in, requests out

**Files:**
- Modify: `voice_assistant/remote.py` (`RemoteSatellite.__init__`, handlers, `_lost`)
- Test: `tests/test_remote.py` (or the file holding `save_note` tests; grep `save_note` in tests/)

**Interfaces:**
- Consumes: Task 1's hello keys and message names.
- Produces on `RemoteSatellite`:
  - `opens: bool`, `system_volume: bool`
  - `on_apps: Optional[Callable[[str, List[Dict]], None]]` and `on_windows: Optional[Callable[[str, List[str]], None]]`, set by the brain, called with `(satellite name, list)`.
  - `ask_computer(action: str, wait: float = COMPUTER_WAIT, **fields) -> Optional[Dict]` returns the `computer_done` fields minus `id`, or None on no answer or disconnect. `COMPUTER_WAIT = 5.0`.
- Validation: `apps` payload must decode to a list of at most `MOST_APPS = 3000` dicts, each with str `id` (≤ 64), str `name` (1..200 chars) and `kind` in `APP_KINDS`. Bad entries are dropped with one warning; a payload that isn't a list is ignored. `windows`: a list of at most 200 str names (≤ 200 chars). Both are ignored with a warning from a satellite that didn't say `opens`.

- [ ] **Step 1: Write the failing tests**
  - `test_apps_passed_on_validated`: a payload with one good and one bad (`kind` "exe") entry calls `on_apps("pc", [good])`.
  - `test_apps_ignored_without_opens`.
  - `test_ask_computer_answered`: a `computer_done` with the same id returns `{"said": "Opening Fortnite."}`.
  - `test_ask_ends_when_lost`: `_lost()` during the wait returns None at once (wait 30 s; assert it returns in under 1 s).
  - `test_ask_computer_late_answer_ignored`.
- [ ] **Step 2:** Run the file. Expected: fail.
- [ ] **Step 3:** Implement. Generalise `_NoteWait` / `_note_waits` to serve both notes and computer requests (one dict, ids `note-N` / `computer-N`). `_note_saved` behaviour stays the same.
- [ ] **Step 4:** Run the file plus `tests/test_notes*.py`. Expected: pass.
- [ ] **Step 5:** Commit `Take a satellite's app and window lists, and ask it to do things on the computer`.

### Task 3: Matching names and keeping each computer's lists

**Files:**
- Create: `voice_assistant/computers.py`
- Modify: `voice_assistant/brain.py` (make `self.computers = Computers()`; set `on_apps` / `on_windows` where the satellite connects, next to `self.rooms.seen(...)` near line 1161; call `computers.gone(name)` where a session ends)
- Test: `tests/test_computers.py`

**Interfaces:**
- Produces:
  - `plain(name: str) -> str`: lower-case; drop ™ ® © and the replacement character; punctuation to spaces; collapse spaces. "EA SPORTS FC™ 25" → "ea sports fc 25".
  - `@dataclass Match: item: Optional[Dict]; choices: List[str]` (choices are the names when ambiguous; both empty for no match).
  - `match_app(said: str, items: List[Dict], aliases: Dict[str, str]) -> Match`. Order: an alias whose `plain` equals `plain(said)` maps to its app name. Then exact `plain` equality, where the first item wins (the list is already de-duplicated by the satellite). Then items whose plain words include every word said; exactly one is a match, and two or more are `choices` (at most 4, sorted). Words "the", "app", "game" are ignored in said.
  - `match_window(said: str, names: List[str]) -> Optional[str]`: same rules without aliases, returning a single name or None.
  - `class Computers` (thread-safe):
    - `set_apps(name, items)`, `apps(name) -> List[Dict]` (kept after `gone`)
    - `set_windows(name, names)`, `windows(name) -> List[str]` (cleared by `gone`)
    - `gone(name)`
    - `known() -> List[str]`: satellites whose apps were ever set since start.

- [ ] **Step 1: Write the failing tests**
  - `test_plain_drops_marks`.
  - `test_exact_beats_partial`: items Overwatch and Overwatch 2; "overwatch" matches Overwatch.
  - `test_every_word_one_match`: "rocket league" finds "Rocket League®".
  - `test_ambiguous_gives_choices`: "fortnite" against "Fortnite Festival" and "Fortnite Creative" (no exact) gives 2 choices.
  - `test_alias`: `{"gta": "Grand Theft Auto V Enhanced"}` with "GTA".
  - `test_no_match`.
  - `test_apps_kept_windows_dropped_on_gone`.
- [ ] **Step 2:** Run. Expected: fail (no module).
- [ ] **Step 3:** Implement and wire into the brain.
- [ ] **Step 4:** Run `tests/test_computers.py tests/test_brain*.py`. Expected: pass.
- [ ] **Step 5:** Commit `Keep each computer's apps and windows, and match what's said to them`.

### Task 4: Settings: the switch, app names and the search site

**Files:**
- Modify: `voice_assistant/satellite_rooms.py` (two fields per satellite: `opens_things` default True, `app_names` default `{}`)
- Modify: `voice_assistant/settings_page.py` (satellite payload; two endpoints; EDITABLE entry)
- Modify: `voice_assistant/config.py` (`CHOICES[("computer","search_site")] = ("google","duckduckgo","bing")`)
- Modify: `config.yaml` (new section `computer:` with `search_site: google` and a comment)
- Modify: `voice_assistant/web/settings.js` (Satellites row: switch, count line, "Apps also called" box), and `page-preview` stub if it lists satellites
- Test: `tests/test_satellite_rooms.py`, `tests/test_settings_page.py`, `tests/web/*.test.mjs`

**Interfaces:**
- Produces:
  - `SatelliteRooms.opens_things(name) -> bool`, `set_opens_things(name, on: bool)`
  - `SatelliteRooms.app_names(name) -> Dict[str, str]` (plain alias → app name as listed), `set_app_names(name, names: Dict[str, str])`
  - `SettingsPage.set_opens(name, on, who) -> Dict` at `/api/satellites/opens` `{name, on}`
  - `SettingsPage.set_app_names(name, text, who) -> Dict` at `/api/satellites/app-names` `{name, text}`. The text is `GTA = Grand Theft Auto V Enhanced, FC = EA SPORTS FC 25`. Each right side must `match_app` exactly one item of that satellite's list (else "I can't find X on Nathaniel's PC."). At most 30 pairs, alias ≤ 40 chars.
  - The page's satellite dict gains `"opens"` (hello says it can, from `Computers.known()`), `"opens_on"`, `"apps_found"` (len of list) and `"app_names"`.
  - EDITABLE `("computer", "search_site", "Search site for 'search for' on a computer", "now")`.
  - `SatelliteRooms.pc_device(name) -> Optional[str]`, `set_pc_device(name, device)` and `/api/satellites/pc` `{name, device}`: which HASS.Agent PC (device id from `PCPower.overview()["pcs"]`) a satellite is. The page shows the choice only when `overview()` lists more than one PC; with one, it is that PC, whatever is saved. Task 6's power words and Task 5's "Your PC's off." pass that PC's name to `PCPower.act` as `name`.
- Page copy (exact):
  - switch label "Opens apps and pages"
  - count line "Found {n} apps and games."
  - box label "Apps also called"
  - placeholder "GTA = Grand Theft Auto V Enhanced"
  - Shown only for satellites with `opens`.

- [ ] **Step 1: Write the failing tests**
  - `test_opens_things_default_on_and_saved`.
  - `test_app_names_saved_and_checked`: an unknown app is refused and nothing is saved.
  - `test_search_site_choice_refuses_other`.
  - JS: `opens row shown only when satellite.opens`.
- [ ] **Step 2:** Run those files. Expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, then the JS tests (`~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs` and `node --check` each script). Expected: pass. Then draw the page with `NOVA_TREE=/home/nathaniel/work/Home-Assistant-open bash ~/work/Home-Assistant/.superpowers/tools/page-preview/shoot.sh` and look at the Satellites shots.
- [ ] **Step 5:** Commit `Settings for opening things on a computer, its app names and the search site`.

### Task 5: Which computer, and Claude's tools

**Files:**
- Modify: `voice_assistant/brain.py`, `voice_assistant/session.py` (pass `computer=` to `ActionExecutor`), `voice_assistant/actions.py`
- Create: functions in `voice_assistant/computers.py`
- Test: `tests/test_computers.py`, `tests/test_actions*.py` (the file with `save_note` tool tests)

**Interfaces:**
- Consumes:
  - Task 2's `ask_computer`.
  - Task 3's `Computers`, `match_app`, `match_window`.
  - Task 4's `opens_things` and `app_names`.
  - `Brain.find_satellite(said) -> (name|None, problem)`, `Brain.spoken_name(name)`, `Brain.session_for(name)`.
  - `PCPower.act("status")`.
- Produces:
  - `safe_url(url: str) -> Optional[str]`: adds `https://` to a bare domain shape. Refuses other schemes, user:password, longer than 2,000, control characters and spaces. Returns the URL or None.
  - `search_url(site: str, words: str) -> str` for `google` (`https://www.google.com/search?q=`), `duckduckgo` (`https://duckduckgo.com/?q=`) and `bing` (`https://www.bing.com/search?q=`), with `urllib.parse.quote_plus`.
  - `Brain.computer_for(spoken_at: str, where: Optional[str]) -> Tuple[Optional[str], Optional[str]]`, returning (satellite name, None) or (None, sentence):
    - `where` given: `find_satellite(where)`. The satellite must be in `Computers.known()` with `opens_things`, else "{Name} can't open things."
    - No `where`: the spoken-at satellite if known and `opens_things`. Otherwise the only known one that opens things. None known: (None, None), meaning fall through.
    - The chosen one not connected: if `PCPower.act("status")` says off, "Your PC's off."; otherwise "Nova isn't running on {spoken name}."
  - `class ComputerControl` (in `computers.py`, one per session, built from the brain and the satellite name):
    - `open(name, where=None) -> Dict`
    - `open_url(url, where=None) -> Dict`
    - `search(words, where=None) -> Dict`
    - `close(name, where=None) -> Dict` (`name == "this"` sends `close_front`)
    - `volume(where, **change) -> Dict`, used by Task 7.
    - Each returns `{"success", "message"}`. Messages (exact):
      - "Opening {App}." plus " on {spoken name}" when not the spoken-at satellite
      - "Which one: A, B or C?"
      - "I can't find {said} on {spoken name}."
      - "Opening {host}."
      - "Searching for {words}."
      - "Closing {App}."
      - "{Said} isn't open."
      - "I couldn't do that on {spoken name}." when the answer is None
      - the satellite's `error` sentence when it gives one
      - "That isn't a web address I can open." for a refused URL
  - Tools on `ActionExecutor`, `needs="computer"`:
    - `open_on_computer(name, where?)`
    - `open_web_page(url, where?)`
    - `search_on_computer(words, where?)`
    - `close_on_computer(name, where?)`, where name may be "this"
    - Offered only while `Computers.known()` has one with `opens_things` (the `_available` check).
    - Descriptions say "What happened is said for you, so just reply 'Okay.'"
    - On no match, `open_on_computer` adds "Closest: A, B and C." for Claude.

- [ ] **Step 1: Write the failing tests**
  - `test_safe_url`: `bbc.co.uk` becomes `https://bbc.co.uk`. Refused: `http://x`, `file:///c`, `https://u:p@x`, `javascript:`, and 2,001 characters.
  - `test_search_url_each_site`.
  - `test_where_spoken_at_pc`.
  - `test_where_named_from_pi`.
  - `test_where_only_one_known`.
  - `test_where_none_known_falls_through`.
  - `test_where_pc_asleep`: known but not connected, with status off, gives "Your PC's off.".
  - `test_where_switch_off`.
  - `test_open_sends_id_never_target`.
  - `test_open_ambiguous_asks`.
  - `test_tools_offered_only_when_known`.
  - `test_url_logged_by_host_only` (caplog).
- [ ] **Step 2:** Run. Expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the two files. Expected: pass.
- [ ] **Step 5:** Commit `Open, close and search on a computer, at it or by its name`.

### Task 6: Local intents for opening, closing, searching and power words

**Files:**
- Modify: `voice_assistant/intents.py`, `voice_assistant/session.py` (pass the new hooks)
- Test: `tests/test_intents.py`

**Interfaces:**
- Consumes: Task 5's `ComputerControl` via hooks, Task 3's matchers.
- Produces: `LocalIntents(..., computer: Optional[ComputerHooks] = None)` where `ComputerHooks` (a small dataclass in `computers.py`) has:
  - `at_pc: Callable[[], bool]`: the spoken-at satellite's machine is `pc`.
  - `names_app: Callable[[str, Optional[str]], bool]`: said X names an app on the target from `computer_for`.
  - `names_window: Callable[[str, Optional[str]], bool]`.
  - `opens_somewhere: Callable[[], bool]`.
- New rules, all before the `(?:open|close) {THING}` Assist rule and the lists, as the PC rules are now. Each returns None to fall through when its hook says no:
  - `(?:open|launch|start|load up) (?P<app>.+?)(?: on (?P<where>MOVE_WHERE))?`: `names_app` gives `Action("open_on_computer", {"name", "where"?})`.
  - `(?:open|go to) (?P<site>[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?: on ...)?`: an app of that name wins, else `open_web_page`.
  - `(?:close|quit|exit) (?:this|that|this window|the window|it)(?: on ...)?`: `close_on_computer {"name": "this"}`, at a PC or with a where that names one.
  - `(?:close|quit|exit) (?P<app>.+?)(?: on ...)?`: `names_window`.
  - `(?:search|google)(?: google)? for (?P<words>.+?)(?: on ...)?|google (?P<words2>.+?)|look up (?P<words3>.+?) on google`: `opens_somewhere`.
  - At `at_pc()` only:
    - `go to sleep|sleep` gives `pc_power sleep`
    - `lock|lock (?:it|the pc|the computer|my pc|my computer)` gives `lock`
    - `(?:turn )?(?:off )?the screen off|screen off|turn off the screen` gives `screen_off`
    - `shut down|shut it down|power off` gives `off`
    - `restart|reboot|restart it` gives `restart`
- Response "Okay." as the existing `_pc`.

- [ ] **Step 1: Write the failing tests**
  - `test_open_app_at_pc`.
  - `test_open_on_pc_from_pi`.
  - `test_open_unknown_falls_through`.
  - `test_open_domain`.
  - `test_open_youtube_app_beats_site`.
  - `test_close_this_at_pc_only`: at the Pi without where, gives None.
  - `test_close_open_window`.
  - `test_search_forms`.
  - `test_fall_through_untouched`: "start a timer for 5 minutes" still gives `set_timer`. "open the garage" and "close the garage door" still go to Assist. "close the blinds" too.
  - `test_power_words_only_whole`: "go to sleep" at the PC gives sleep. At the Pi it gives None. "I'm going to sleep after this", "restart the song" and "lock the front door" give no `pc_power`.
  - `test_shut_down_bare_asks`: via `pc_power off`, whose ask is tested in test_pc.
- [ ] **Step 2:** Run `tests/test_intents.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_intents.py tests/test_pc.py`. Expected: pass.
- [ ] **Step 5:** Commit `Open, close, search and the PC's power words, said at the PC`.

### Task 7: Volume and mute on the named device, else the one spoken to

**Files:**
- Modify: `voice_assistant/actions.py` (`set_volume` gains `where` and `mute`; `_change_music_volume` acts on this satellite's player only), `voice_assistant/intents.py` (volume shapes gain an optional device; mute/unmute rule), `voice_assistant/brain.py`, `voice_assistant/session.py`
- Test: `tests/test_actions*.py` (volume tests), `tests/test_intents.py`, `tests/test_conversation*.py`

**Interfaces:**
- Consumes: Task 5's `ComputerControl.volume`, `Brain.find_satellite`, Task 2's `system_volume`.
- Produces:
  - `set_volume(category=None, level=None, direction=None, amount=None, where=None, mute=None)`:
    - `where` resolves via `find_satellite`; it is None for the spoken-at satellite.
    - The target has `system_volume` and the category is not `alarm`: `ComputerControl.volume(where, direction|level|mute, step=self.volume.step)`. The satellite answers `level` and `muted`. The message is "Volume {level} percent." or "Muted." / "Unmuted.", plus a beep on the target.
    - Otherwise, when the target is another satellite: `Brain.session_for(target).actions.set_volume(...)` without `where`, with its beep played by that session.
    - Otherwise: as today, but music changes only `self.music`, not `_playing_in_room()`.
    - `mute=True/False` at a non-system-volume target sets that satellite's music player to 0 and back, remembering the level on its `ActionExecutor` (`_level_before_mute`).
  - Intents:
    - Existing volume shapes plus `(?: on| for) (?P<where>MOVE_WHERE)` or a leading `(?P<where>...) volume`, when `names_satellite(where)`.
    - `(?P<un>un)?mute(?: (?:the )?(?P<where>...))?|mute (?:the )?sound`. "mute" alone keeps its stop meaning while something rings: check how `Conversation.handle` runs stop words before intents and make sure that order still holds, then add the test.
  - Decision note: room-wide volume (0026) is replaced. Pause and stop stay room-wide.

- [ ] **Step 1: Write the failing tests**
  - `test_volume_up_at_pc_goes_to_windows`.
  - `test_volume_on_pc_from_pi`.
  - `test_volume_on_pi_from_pc_beeps_on_pi`.
  - `test_volume_at_pi_changes_only_pi_music`: the PC is also playing in the room and is untouched.
  - `test_alarm_volume_at_pc_stays_nova`.
  - `test_claude_set_volume_at_pc_goes_to_windows`.
  - `test_mute_at_pc`.
  - `test_mute_at_pi_mutes_music_only_and_unmute_restores`.
  - `test_mute_while_ringing_stops`.
  - `test_tv_volume_still_assist`.
- [ ] **Step 2:** Run. Expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the volume, intents and conversation files. Expected: pass.
- [ ] **Step 5:** Commit `Volume and mute on the device named, else the one spoken to`.

### Task 8: Sleep, lock and screen off after the reply is said

**Files:**
- Modify: `voice_assistant/actions.py` (`ActionResult.after_said: Optional[Callable[[], Dict]]`, read in `from_reply`), `voice_assistant/conversation.py` (`Reply.after: List[Callable]`, gathered from results), `voice_assistant/session.py` (run each `after` once `_say` returns, talked over or not; log failures), `voice_assistant/pc.py` (`act(..., defer: bool = False)`: for sleep, lock and screen_off with `defer`, the success reply carries `after_said` that does the press; `pc_power` passes `defer=True` when said at a PC satellite)
- Test: `tests/test_pc.py`, `tests/test_session*.py`

**Interfaces:**
- Consumes: Task 6's power rules.
- Produces: `ActionResult.after_said` and `Reply.after`, available for any later action.

- [ ] **Step 1: Write the failing tests**
  - `test_sleep_at_pc_pressed_after_reply`: a fake speaker records the order "say" then "press".
  - `test_sleep_from_pi_pressed_at_once`: unchanged.
  - `test_after_runs_when_talked_over`.
  - `test_after_failure_logged_not_raised`.
- [ ] **Step 2:** Run. Expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/test_pc.py` and the session tests. Expected: pass.
- [ ] **Step 5:** Commit `Put the PC to sleep once Nova has said so`.

### Task 9: The Windows satellite: list, open, close, front, volume

**Notes from Task 0:** (filled in by Task 0)

**Files:**
- Create:
  - `voice_assistant/pc_apps.py` (catalogue)
  - `voice_assistant/pc_windows.py` (windows, close, raise)
  - `voice_assistant/pc_volume.py` (Core Audio)
- Modify: `satellite.py` (hello flags on Windows; `computer` handler; background refresh threads), `requirements-pc.txt` or wherever the PC's pins live (grep `winrt` to find it) adding pinned `pycaw` and `comtypes`, `scripts/install.ps1` only if the pin file isn't already installed by it
- Test: `tests/test_pc_apps.py`, `tests/test_pc_windows.py`, `tests/test_satellite.py`

**Interfaces:**
- Produces:
  - `pc_apps.build(start_apps: Callable[[], List[Dict]], epic_dir: str, steam_dirs: Callable[[], List[str]]) -> List[Dict]`. Items are `{"id","name","kind","target"}`, where `target` stays on the PC. Behaviour:
    - Uninstall names are dropped.
    - Duplicates by `plain` name are dropped, keeping the first: Start, then Epic, then Steam.
    - Epic keeps only items with `bIsApplication` true and `AppCategories` containing "games" or "applications". It drops names ending " Content".
    - Epic is parsed leniently: on a JSON error, unescaped backslashes are doubled and it is retried. An item still failing is skipped with a warning.
    - Steam reads `libraryfolders.vdf` paths and each `appmanifest_*.acf` `name` and `appid`. It skips names starting "Steamworks" and "Proton", and the "Steam Linux Runtime".
    - Each id is `sha1(kind + target)[:12]`.
  - `pc_apps.start_apps() -> List[Dict]`: runs `powershell -NoProfile -Command "Get-StartApps | ConvertTo-Json -Compress"` with a 20 s timeout. Its launch target is `shell:AppsFolder\{AppID}`.
  - `pc_apps.epic_target(item)`: `com.epicgames.launcher://apps/{CatalogNamespace}%3A{CatalogItemId}%3A{AppName}?action=launch&silent=true`.
  - `pc_apps.steam_target(appid)`: `steam://rungameid/{appid}`.
  - `pc_windows.open_windows() -> List[str]`: names of programs with a visible, titled, non-tool top-level window. The name is the exe's FileDescription, else the exe stem. It excludes the satellite's own pid, `explorer.exe`, `ShellExperienceHost.exe`, `SearchHost.exe`, `StartMenuExperienceHost.exe` and `TextInputHost.exe`.
  - `pc_windows.close(name) -> Optional[str]`: `PostMessage(WM_CLOSE)` to each such window whose program matches. Returns the name, or None.
  - `pc_windows.close_front() -> Optional[str]`: the same rule for the foreground window.
  - `pc_windows.bring_to_front(started_after: float, pid_hint=None)`: Task 0's method.
  - `pc_volume.change(direction=None, step=None, level=None, mute=None) -> Dict` returns `{"level": int, "muted": bool}`. A level or direction change also unmutes.
  - `Satellite`:
    - Hello sends `opens` and `system_volume` on `sys.platform == "win32"` when imports succeed.
    - A thread sends `apps` (without `target`) at start and every 600 s when changed.
    - A thread sends `windows` every 3 s when changed.
    - `computer` handler: `open` looks up the id in the current list ("That's not on this PC any more." if missing), then `os.startfile(target)` and `bring_to_front`. `open_url` re-checks with `safe_url`, then the same. `close`, `close_front` and `volume` work as above.
    - It answers `computer_done` with `said` or `error`, plus `level`/`muted` for volume.
    - Errors are logged by type only.

- [ ] **Step 1: Write the failing tests** (pure-Python with fakes; Windows calls behind small functions the tests replace)
  - `test_build_drops_uninstall_and_duplicates`.
  - `test_epic_lenient_parse_and_content_dropped`: a fixture manifest with `D:\Gaming\...` backslashes.
  - `test_steam_manifests_read`.
  - `test_targets_never_sent`.
  - `test_open_unknown_id_refused`.
  - `test_open_url_rechecked`.
  - `test_close_never_explorer_or_self`.
  - `test_close_front_never_desktop`.
  - `test_hello_flags_only_on_windows`.
- [ ] **Step 2:** Run the three files. Expected: fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, then the whole suite once (London), then the touched files with `TZ_OVERRIDE=UTC`, then ruff. Expected: all pass, ruff clean.
- [ ] **Step 5:** Commit `The PC lists, opens and closes things, brings them to the front and sets its volume`.

### Task 10: Decision record, checklist and live checks

**Files:**
- Create: `docs/decisions/0034-opening-things-and-commands-at-the-computer.md`, following 0032/0033's shape. It covers his calls, my calls (copied from the spec's "My calls" list), what replaces 0026's room-wide volume, and the anti-goals.
- Modify: `docs/release-checklist.md` (or wherever 35-46 live; grep "35") to add hand checks:
  - "open Fortnite" at the PC.
  - "open Calculator on my PC" at the Pi.
  - "open bbc.co.uk" (Firefox tab to the front).
  - "search Google for Stevenage fixtures".
  - "close Calculator", and "close this" on a Notepad window with unsaved text (Notepad asks to save).
  - "go to sleep" at the PC (the reply is heard, then sleep), and "I'm going to sleep" (nothing).
  - "shut down" (asks) and "no".
  - "volume up" at the PC (Windows changes, Pi doesn't), "volume down on the Pi" at the PC, and "mute" / "unmute" at both.
  - Mark the ones that make sound in his room.
- Also check once, read-only, that HASS.Agent's `media_player.windows_pc` `volume_level` is still wrong while Windows is not at 0 (the spec's reason for Core Audio) and write the result into 0034.
- [ ] **Step 1:** Write both docs.
- [ ] **Step 2:** Run the whole suite London and UTC once each, and ruff.
- [ ] **Step 3:** Commit `Decision 0034: opening things and commands at the computer`.
- [ ] **Step 4:** Hand over his named jobs, in order: push to GitHub main and restart `nova-backend`; the Pi's `bash update.sh`; the PC's `scripts/deploy_pc.sh`. Then the hand checks, warning before sound.
