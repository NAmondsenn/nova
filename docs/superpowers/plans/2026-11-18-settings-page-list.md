# Settings Page List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova's settings page opens on a short list of its sections, each with a line on how it is, and shows one section at a time, with each feature's settings inside its section, what needs attention on top, and a search.

**Architecture:** Every section keeps its `<section>` and its drawing code in `settings.js`; which one shows is an attribute on `<body>` and CSS. The rules of the list (which section a setting belongs to, each row's line, what needs attention, search) are plain functions with no DOM in a new file, `voice_assistant/web/settings_list.js`, tested with Node. `settings.js` builds one plain `view` object from what it has loaded and hands it to them.

**Tech Stack:** HTML, CSS and JavaScript as the page has them (no framework, no build step, no dependency); Python for one field of `/api/state`; `node:test` for the page's tests; pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-settings-page-list-design.md`. Read it first; this plan argues from it.

## Contract

**Sections** (id, name, group, icon symbol, the starts of the settings keys it holds), in this order:

| id | Name | Group | Icon | Settings |
| --- | --- | --- | --- | --- |
| `status` | Status | Now | `i-pulse` | |
| `music` | Music | Now | `i-music` | `music.` `navidrome.` `soulsync.` `spotify.` `clean_check.` `new_music.` `wrapped.` |
| `clock` | Timers and alarms | Now | `i-alarm` | |
| `devices` | Devices | Home | `i-bulb` | |
| `batteries` | Batteries | Home | `i-battery` | |
| `displays` | Clock displays | Home | `i-display` | `display.` |
| `journeys` | Journeys | Daily | `i-car` (new) | `journeys.` |
| `football` | Football | Daily | `i-football` | `football.` |
| `calendars` | Calendars and lists | Daily | `i-calendar` | |
| `routines` | Routines | Daily | `i-routine` | |
| `satellites` | Satellites | Nova | `i-microphone` | |
| `sound` | Sound and voice | Nova | `i-sound` | `voice.` `sounds.` `thresholds.` |
| `claude` | Claude | Nova | `i-reply` | `web_search.` `conversation.` `usage.` |
| `notifications` | Notifications | Nova | `i-bell` | `ntfy.` |
| `memory` | Memory | Nova | `i-memory` | |
| `keys` | Keys | Nova | `i-key` | |
| `else` | Everything else | Nova | `i-settings` | `home_assistant.` `mqtt.` `logging.` and any key nothing else claims |

Sub-headings inside a section which holds more than one of the old groups: Music (`wrapped.` under "Wrapped", the rest under "Music"); Sound and voice (`voice.` "Voice", `sounds.` "Sounds", `thresholds.` "Listening"); Everything else (`home_assistant.` and `mqtt.` "Home", `logging.` "Privacy", the unclaimed "Other"). Other sections' settings have no sub-heading.

**The view**, a plain object `settings.js` builds and `settings_list.js` reads. `null` means not loaded or couldn't be read:

```js
{ loaded: true,                       // /api/state was read
  tiles: [{ name: "Claude", good: true, words: "Claude can't be reached" }],  // Status's tiles, in page order; words is what the tile says when it isn't good
  problems: [{ at: 1795010880, level: "problem" }],   // newest first; level "problem" or "warning"; at in epoch seconds
  restart: false, settingsProblem: "",
  music: { title: "Song", artist: "Artist", playing: true },
  clock: { timers: 0, reminders: 1, alarms: 1 },
  lights: { on: 2, total: 5 },
  batteries: [{ name: "Tablet", percent: 14, low: true }],
  displays: { connected: 1, total: 1 },
  journeys: { requests: 21, cap: 3000 },
  football: { teams: 2 },
  google: "in", spotify: "off",      // "in", "out" (set up but needs signing in to) or "off" (not set up)
  routines: 2,
  satellites: [{ name: "bedroom", connected: true, isDefault: true }],
  voice: 60, webSearch: true,
  quiet: { from: "23:00", until: "07:00" },   // false for none
  memories: 12, keys: { set: 6, total: 11 } }
```

**Each row's line** (`""` when its part of the view is `null`, or for Status when `loaded` is false):

- `status`: the `words` of the first tile whose `good` is false; else `A problem at 14:08` for the newest problem of level `"problem"` within 3,600 seconds of now, the time local as HH:MM; else `All well`.
- `music`: `Song, Artist` while playing (the title alone with no artist); else `Nothing playing`.
- `clock`: the non-zero counts joined with a comma and a space, in the order timers, reminders, alarms: `1 timer`, `2 reminders`, `1 alarm`; all zero `Nothing set`.
- `devices`: `All off`, `1 light on`, `3 lights on`; with `total` 0 `No lights`.
- `batteries`: the lowest, `Tablet at 14%`, marked as a warning when its `low` is true; none `None to show`.
- `displays`: `1 connected`; with `total` 0 `None added`.
- `journeys`: `21 of 3,000 asks used this month` (the cap with thousands' commas).
- `football`: `No teams followed`, `1 team followed`, `2 teams followed`.
- `calendars`: `Signed in to Google`; `Sign in to Google` marked as a warning; `Google isn't set up`.
- `routines`: `No routines`, `1 routine`, `2 routines`.
- `satellites`: `2 of 2 connected`; none `None added`.
- `sound`: `Voice at 60%`.
- `claude`: `Web search on`, `Web search off`.
- `notifications`: `Quiet from 23:00 to 07:00`, `No quiet hours`.
- `memory`: `Nothing remembered`, `1 thing remembered`, `12 things remembered`.
- `keys`: `6 of 11 set`.
- `else`: always `""`.

**What needs attention**, in this order, each `{ section, text }`:

1. `restart` true: `status`, `Nova needs a restart to use what's been saved`.
2. `settingsProblem` not empty: `else`, the problem's own words.
3. The default satellite not connected: `satellites`, `The bedroom satellite isn't connected`.
4. Each battery whose `low` is true: `batteries`, `Tablet is at 14%`.
5. `google` is `"out"`: `calendars`, `Sign in to Google`.
6. `spotify` is `"out"`: `music`, `Sign in to Spotify`.
7. A problem of level `"problem"` within the hour: `status`, `A problem at 14:08`.

## Global Constraints

- British spelling, no em dashes, no emojis. Comments in the three web files' own style: full sentences which say why, above what they explain.
- The repo is public: names in tests and docs are made up ("bedroom", "Tablet", "Song").
- No new dependency, no framework, no build step. The page's Content-Security-Policy lets only its own files run: a new script is served by `settings_page.py` (`FILES`) and counted in `PAGE_FILES`.
- Nothing a control does, and nothing Nova's API takes or gives, changes, but for Task 1's one field.
- The look is kept: new pieces use the page's existing classes, colours (the `:root` tokens in `settings.css`) and icon style (24 by 24, stroked, as the `<symbol>`s in `settings.html`).
- One commit per task, with tests that fail on the old code where the task has logic. Commit messages in the repo's style, ending `Co-Authored-By: Claude <noreply@anthropic.com>`. Named files only; no `git stash`; no pushes or deploys.
- Tests never compare DOM nodes or large objects, only small strings and numbers.
- CT 109 load limits: check `cat /proc/loadavg` first and wait if the first number is above 3; one test run at a time; no repeat runs; pytest never runs on CT 109.

## Review Focus

1. The 20-second refresh arriving while a section is open and one of its fields is changed and unsaved: the field keeps its value, the section stays open, the page doesn't scroll (Tasks 3 and 4).
2. The page opened at `#nonsense`, at `#music` while locked, and at `#music` after logging in: the list; the log-in alone; then Music (Task 3).
3. Back from a section reached by a link or a reload goes to the list, and from the list leaves the page; Back never leaves the page from inside a section (Task 3).
4. A setting whose key no section claims, and a section none of whose settings exist on this Nova: the first shows under Everything else, the second shows no empty sub-heading (Tasks 2 and 4).
5. A search of only spaces, of `.*(`, or of 500 characters: no error; treated as plain words, or as no search (Tasks 2 and 5).

## Test commands

- Page tests, from the worktree `/home/nathaniel/work/Home-Assistant-page`: `nice -n 19 timeout 300 ~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/`
- Syntax: `~/tools/node/bin/node --check voice_assistant/web/settings.js` (and `settings_list.js`).
- Python, on the Pi: `NOVA_TREE=$PWD NOVA_CHECK=page nice -n 19 timeout 600 bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh tests/test_settings_page.py`; the whole suite is the same without a file, then again with `TZ_OVERRIDE=UTC` in front.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/page && /tmp/nova-check-pytest/bin/ruff check .'` after a Python run has copied the tree.

---

### Task 1: How many journeys were asked of Google, in the page's state

**Files:**
- Modify: `voice_assistant/journeys.py` (one method on `Journeys`), `voice_assistant/settings_page.py` (`state()`)
- Test: `tests/test_journeys.py`, `tests/test_settings_page.py`

**Interfaces:**
- Produces: `Journeys.used() -> Dict[str, int]`: `{"requests": n, "cap": n}`, the count for this calendar month (0 when the stored month is another, or the stored count is unusable) and the `monthly_requests` setting as it is now. `/api/state` gains `"journeys"`: that dict, or `None` when the brain has no journeys.

- [ ] **Step 1: Write the failing tests:** in `tests/test_journeys.py`, `test_the_months_asks_and_the_cap_are_given` (after two looks: `{"requests": 2, "cap": 3000}`; with the store's month set to last month: `requests` 0; with a cap setting of 50: `cap` 50). In `tests/test_settings_page.py`, `test_the_state_has_how_many_journeys_were_asked` (with journeys: the dict; without: `None`), following how the file's other state tests build a page.
- [ ] **Step 2: Run both files on the Pi**; expected: the two fail.
- [ ] **Step 3: Implement.** `used()` reads the same store and month rule `look` counts by, without changing the store.
- [ ] **Step 4: Run both files**; expected: all pass.
- [ ] **Step 5: Commit**: "Tell the page how many journeys were asked of Google this month".

### Task 2: The list's rules, with no page

**Files:**
- Create: `voice_assistant/web/settings_list.js`, `tests/web/settings_list.test.mjs`
- Modify: `voice_assistant/settings_page.py` (`FILES`, `PAGE_FILES`), `voice_assistant/web/settings.html` (one `<script src="settings_list.js" defer>` before `settings.js`'s)
- Test: `tests/test_settings_page.py` (the new file is served and is in the page's version)

**Interfaces:**
- Produces: one global, `NovaList`, set on `globalThis` so it is there in the page and in Node's `vm`:
  - `NovaList.GROUPS`: `[{ id: "now", name: "Now" }, { id: "home", name: "Home" }, { id: "daily", name: "Daily" }, { id: "nova", name: "Nova" }]`
  - `NovaList.SECTIONS`: the Contract's table as `[{ id, name, group, icon, starts: [...] }]`, in its order.
  - `NovaList.sectionOf(key) -> id` (`"else"` for a key nothing claims) and `NovaList.subheadingOf(key) -> string | null` (the Contract's sub-headings).
  - `NovaList.lines(view, now) -> { [id]: { text, warn } }`, every section present, `now` in epoch seconds.
  - `NovaList.attention(view, now) -> [{ section, text }]`.
  - `NovaList.words(query) -> [string]`: the query lower-cased and split on white space, at most 8 words of at most 40 characters, empty for only spaces.
  - `NovaList.search(query, entries) -> [{ section, hits: [{ label, target }] }]`: `entries` is `[{ section, kind: "name" | "line" | "setting" | "control", text, target }]`. A section is found when every word is in the text of some entry of it (plain text, never a pattern); its `hits` are its `setting` and `control` entries which hold every word, in the order given, at most 6. Sections come back in the Contract's order. No words: `[]`.
  - `NovaList.fromHash(hash) -> id | null`: `"#music"` gives `"music"`; anything which isn't a section's id gives `null`.

- [ ] **Step 1: Write the failing tests** in `tests/web/settings_list.test.mjs`, loading the script into a `vm` context as `tests/web/settings_clean.test.mjs` loads `settings.js`, with no DOM at all. One `test()` per bullet of the Contract's lines (each wording, each singular and plural, each `null`), per attention rule and their order together, and for: every key start in the Contract's table mapping to its section and sub-heading; `"made.up"` mapping to `"else"` under "Other"; `words("  Quiet   HOURS ")` being `["quiet", "hours"]`; `search(".*(", ...)` and a query of 500 characters returning without throwing; a search finding a section by its line, by a setting's label, and by a control, with the hits it lists; `fromHash("#nonsense")` and `fromHash("")` being `null`. Times are made from `now` so the tests hold in any time zone (compare with the same `Date` formatting the code uses, or check the line with a pattern for the hour and minute).
- [ ] **Step 2: Run the page tests**; expected: the new file fails on the missing script.
- [ ] **Step 3: Implement `settings_list.js`** with a comment at its top saying what it is and that it never touches the page, and serve it.
- [ ] **Step 4: Run the page tests, `node --check` on the new file, and `tests/test_settings_page.py` on the Pi**; expected: all pass.
- [ ] **Step 5: Commit**: "Work out the page's list, its lines and what needs attention, apart from the page".

### Task 3: One section at a time

**Files:**
- Modify: `voice_assistant/web/settings.html`, `voice_assistant/web/settings.css`, `voice_assistant/web/settings.js`
- Create: `tests/web/settings_sections.test.mjs`

**Interfaces:**
- Consumes: `NovaList.SECTIONS`, `GROUPS`, `fromHash`.
- Produces, in the page:
  - Every section is `<section id="section-ID" data-section="ID">`. New, with their heading and icon: `journeys`, `satellites`, `claude`, `notifications`, `else`. The Satellites block (its heading, list, the chips choosing which the page looks after, the password and renew panels, the add form) and the microphone's mute move from Status into `satellites`. Sound's heading becomes "Sound and voice". The old Settings section's heading goes; its form stays for Task 4.
  - `<nav id="list" aria-label="Nova's sections">` before the sections, holding `<div id="attention">`, `<input type="search" id="find">` and `<div id="rows">`; Task 5 fills them. In this task `#rows` gets one plain row per section (icon, name, an empty line) in its group under the group's name, drawn once from `NovaList`.
  - `<button type="button" id="back">All of Nova</button>`, shown at the top of the open section.
  - `document.body.dataset.open`: the open section's id, absent on the list. CSS shows the list alone without it and that section alone with it, under 1,180 px; at 1,180 px and over the list is a left column 340 px wide and the open section beside it, Status when none is open.
  - `openSection(id, how)` and `closeSection()` in `settings.js`: opening scrolls to the top, puts the focus on the section's heading and, unless it came from the browser's Back or the first load, pushes a history entry whose address ends `#id`; closing goes back in the history if the section was pushed there, else replaces the address with the bare page, and puts the list back where it was scrolled to with the focus on that section's row. `popstate` and the address on load (`fromHash`) open or close to match. While the page is locked nothing opens, and the address is looked at again once it unlocks.
- The section a setting or control is found in must not depend on this task: nothing else in `settings.js` changes what it draws.

- [ ] **Step 1: Write the failing tests** in `tests/web/settings_sections.test.mjs`, running `settings_list.js` then `settings.js` in a `vm` over a stand-in page as `settings_clean.test.mjs` does (extend its stand-in only as far as needed: `body.dataset`, `location.hash`, `history.pushState`, `replaceState` and `back` recorded in a list, `scrollTo`, `focus`): opening a row sets `body.dataset.open`, pushes one entry ending `#music` and focuses Music's heading; Back (`popstate` with no section) removes it, restores the list's scroll and focuses Music's row; loading at `#music` opens it with nothing pushed, and closing then replaces the address rather than going back; loading at `#nonsense` leaves the list; while locked, `#music` opens nothing, and opens Music on unlocking; a `load()` of new state while Music is open leaves `body.dataset.open` and calls no scroll. And one test reading `settings.html` as text: every id in `NovaList.SECTIONS` has exactly one `id="section-ID"`, and no other `data-section` exists.
- [ ] **Step 2: Run the page tests**; expected: the new file fails.
- [ ] **Step 3: Implement.** The CSS goes in a block of its own in `settings.css` with a comment saying how the list and the open section share the screen; the old board's grid rules which no longer apply are removed, not left overridden. Add the `i-car` symbol.
- [ ] **Step 4: Run the page tests and `node --check voice_assistant/web/settings.js`**; expected: all pass, the two older test files included.
- [ ] **Step 5: Commit**: "Show Nova's page as a list, and one section at a time".

### Task 4: A feature's settings in its own section, and the one save bar

**Files:**
- Modify: `voice_assistant/web/settings.js` (`GROUPS`, `showSettings`, `countChanges`, `showNight`, `showSettingsProblem` and what they call), `voice_assistant/web/settings.html`, `voice_assistant/web/settings.css`
- Create: `tests/web/settings_settings.test.mjs`

**Interfaces:**
- Consumes: `NovaList.sectionOf`, `subheadingOf`, Task 3's sections.
- Produces:
  - Each section which holds settings has `<div class="settings-of" data-settings="ID">` as its last block; `showSettings` draws each setting's field into its section's block, under the Contract's sub-heading where there is one. A section whose settings don't exist on this Nova shows no block and no sub-heading. `GROUPS` is gone.
  - Every setting's input belongs to the one form, `#settings` (the `form` attribute), which is now only the save bar: fixed to the bottom of the screen above the phone's home bar while anything is changed and unsaved or a restart is offered, hidden otherwise. Its count ("2 changes to save") counts every section's; saving saves them all, as now.
  - The quiet hours (`showNight`) are drawn in Notifications.
  - That Nova's settings file can't be read (`showSettingsProblem`) is said at the top of every `settings-of` block, and kept where Task 5 can read it for the attention row.
  - `showSettings` still redraws nothing while any setting anywhere is changed or has the focus.

- [ ] **Step 1: Write the failing tests** in `tests/web/settings_settings.test.mjs`, over the stand-in page: a state with `football.kickoff_minutes`, `voice.rate`, `sounds.reminder_enabled` and `made.up` draws the first in `football`'s block with no sub-heading, the next two in `sound`'s under "Voice" and "Sounds", and the last in `else`'s under "Other"; no block is drawn for `claude` when the state has no setting of its; each input's `form` is `settings`; changing one in Football and one in Sound makes the bar say 2 changes and shows it, and a new state then redraws neither; with a settings problem its words are at the top of each block drawn.
- [ ] **Step 2: Run the page tests**; expected: the new file fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the page tests and `node --check`**; expected: all pass.
- [ ] **Step 5: Commit**: "Put each feature's settings in its own section, under one save bar".

### Task 5: The list alive: its lines, what needs attention, and search

**Files:**
- Modify: `voice_assistant/web/settings.js`, `voice_assistant/web/settings.html`, `voice_assistant/web/settings.css`
- Create: `tests/web/settings_rows.test.mjs`

**Interfaces:**
- Consumes: `NovaList.lines`, `attention`, `search`, `words`; `openSection`; `/api/state`'s `journeys` (Task 1).
- Produces:
  - `currentView()` in `settings.js`: the Contract's view, built from what the page last loaded (the state, and the music's, calendars', home's, batteries' and football's own loads). A part whose load hasn't answered, or failed last time, is `null`.
  - After every load of any of those, the rows' lines and the attention rows are drawn again from `NovaList`, only where their words changed, and never while a row is pressed. A warning line and an attention row use the page's warning colour. An attention row opens its section.
  - Journeys' section says `21 of 3,000 asks of Google used this month` above its settings, and "Places are your zones in Home Assistant." under it.
  - Typing in `#find`: the rows are replaced by the sections found and, under each, its hits; a tap on a hit opens the section, scrolls the hit's element into view and gives it the class `found` for 2 seconds; a tap on a section's own row opens it. With words and nothing found: "Nothing on Nova's page matches that." Emptying the box puts the list back. The entries searched are made when the box is typed in: each section's name and line, each setting's label (its target the field), and each section's own words on the page: its headings, labels, button texts and placeholders (their target the element).

- [ ] **Step 1: Write the failing tests** in `tests/web/settings_rows.test.mjs`, over the stand-in page: after a state with a restart needed, a low battery and a song playing, Music's row says the song, Batteries' row says the lowest and is marked a warning, and the attention rows are the restart's then the battery's, each opening its section when pressed; a later state with nothing wrong removes them; a row being pressed (`:active`) is not redrawn; a failed music load leaves Music's line empty; typing "quiet" lists Notifications with the quiet hours' hit, pressing the hit opens Notifications and marks the field, and emptying the box restores every row; "zzz" gives the nothing-matches line; `.*(` throws nothing.
- [ ] **Step 2: Run the page tests**; expected: the new file fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the page tests and `node --check`**; expected: all pass.
- [ ] **Step 5: Commit**: "Say how each section is on its row, what needs attention, and find things".

### Task 6: The record, and every suite

**Files:**
- Create: `docs/decisions/0028-the-page-as-a-list.md` (check `docs/decisions/` for the next free number first and use that)
- Modify: `README.md` (where the settings page is described), `docs/release-checklist.md` (one entry, in its format, for trying the page on a phone: the list, a section and Back, a setting changed in two sections and saved, search for "mute", the wide screen)

- [ ] **Step 1: Write the record** in the format of the latest files in `docs/decisions/`: the status line `18 November 2026 · Agreed with the user in chat ("yes b") · Built`, **Context.**, **Decision.** (numbered: the list and its groups; one section at a time and Back; settings with their feature; Satellites; the lines; attention; search; the wide screen; what was kept), **Approaches.**, **My calls, not the user's** (from the spec's marked calls and its builder-check answers), **Hardware.** (none).
- [ ] **Step 2: Fetch and merge `origin/main`**, then run the page tests, `node --check` on both scripts, the whole Python suite in London and in UTC, and ruff (Test commands). Expected: everything passes; `test_home_assistant`'s 3000-deep script walk is known to fail now and then in London: if it alone fails, say so and don't rerun.
- [ ] **Step 3: Commit**: "Record the page as a list".
