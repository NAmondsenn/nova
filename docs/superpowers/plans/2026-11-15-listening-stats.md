# Listening Stats and Wrapped Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova answers listening questions by voice, Home Assistant gets a Music view like SoulSync's Stats page, and Nova makes a monthly and a yearly Wrapped: a tap-through story page with an ntfy notification.

**Architecture:** One reader, `voice_assistant/listening.py`, fetches SoulSync's ready-made stats (`/api/stats/cached`, `/api/stats/year`, `/api/stats/library-disk-usage`) every 30 minutes into an in-memory snapshot, with the top artists' pictures. Claude's new tools, a new MQTT device (`music_device.py`) and the Wrapped maker (`wrapped.py`) all read that snapshot, never SoulSync directly. Wrappeds are saved as JSON plus pictures under the brain's data folder and served by the settings page's own server at `/wrapped`.

**Tech Stack:** Python 3 on the brain (CT 107), requests, paho-mqtt (MQTT image entities), pytest on the Pi, plain HTML/CSS/JS for the page (no build step, like `settings.js`), Home Assistant storage-mode dashboard YAML with apexcharts-card, bubble-card and card-mod.

**Spec:** `docs/superpowers/specs/2026-11-15-listening-stats-design.md` (read it first; this plan argues from it).

## Global Constraints

- British spelling, no em dashes, no emojis, in code, comments, UI text and commit messages. Nova's comment style: long plain-English docstrings and "why" comments (read `usage_device.py` and `soulsync.py` first).
- The repo is public: no real listening data in tests or fixtures. Fixtures use made-up artists, songs and albums ("Artist One", "Song A").
- Every number comes from SoulSync; nothing is counted by Nova. What SoulSync doesn't supply is left out, never built from a play log of Nova's own.
- Nothing on the reply path waits on SoulSync: tools, the device and the page read the snapshot. Only `Listening.refresh()` talks to SoulSync, on its own thread.
- Changeable values go on the settings page (`EDITABLE`), read live with `config.section(...)`.
- SoulSync's errors never put the key in a log line: log only the kind of error, as `SoulSync._send` does.
- CT 109 load limits: `nice`, `timeout`, one suite at a time, no repeat runs; check `/proc/loadavg` first.
- Worktree `~/work/Home-Assistant-stats`, branch `listening-stats` (from `main` b9c969d). Never touch `~/work/Home-Assistant` or another worktree. No `git stash`.
- Lab writes (HA config copy and dashboard saves, `nova-backend` restart, deploy to the brain) and every `git push` wait for Nathaniel to name the exact job; stop and give him the sentence.
- One commit per task; messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. SoulSync down, its key refused, or its answer changed shape after a `:latest` update: the voice tool says so in one sentence, the device keeps the last good snapshot, a Wrapped due now is tried again an hour later, nothing raises (Task 1 `test_refresh_keeps_last_good`, `test_unknown_shape_is_unavailable`; Task 5 `test_retries_after_failure`).
2. No plays in a window (a quiet week, a fresh install): "You haven't listened to anything in the last 7 days", HA shows 0 and no pictures, and no Wrapped is made for an empty month (Task 2 `test_empty_window`, Task 5 `test_empty_month_not_made`).
3. Nova restarts across midnight on the 1st, or is off all that day: the Wrapped is made at the next look that day or later in the month, and never twice (Task 5 `test_made_once_after_restart`, `test_made_late_if_missed`).
4. A picture SoulSync can't give (404, not an image, too big, a URL on another host): that artist has no picture; nothing else fails (Task 1 `test_picture_refused`).
5. Odd input to the page's API: `/api/wrapped?key=../x`, an unknown key, a picture index past the end: a 404, never a file outside the Wrapped folder (Task 6 `test_wrapped_key_checked`).

---

## Test commands

- **Nova (on the Pi):** make a copy of the test script for this worktree once:
  `sed -e 's#^cd /home/nathaniel/work/Home-Assistant$#cd /home/nathaniel/work/Home-Assistant-stats#' -e 's#ct-work#ct-work-stats#g' ~/work/Home-Assistant/.superpowers/tools/pi_test.sh > ~/work/pi_test_stats.sh`
  Run: `nice -n 19 timeout 600 bash ~/work/pi_test_stats.sh tests/test_listening.py`, and again with `TZ_OVERRIDE=UTC` in front. The full suite (no args) only at the end of Task 7.
- **Ruff:** `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-stats && /tmp/nova-check-pytest/bin/ruff check .'`
- **Page script:** `~/tools/node/bin/node --check voice_assistant/web/wrapped.js`
- **Read-only checks on the brain:** `ssh nova 'sudo -n -u nova env HOME=/home/nova /home/nova/Home-Assistant/venv/bin/python -I -' < script.py`

---

### Task 1: Reading SoulSync's stats

**Files:**
- Modify: `voice_assistant/soulsync.py` (add `_get`, `stats`, `picture`)
- Create: `voice_assistant/listening.py`
- Create: `tests/fixtures/soulsync_stats_cached.json`, `tests/fixtures/soulsync_stats_year.json`, `tests/fixtures/soulsync_disk_usage.json` (made-up names)
- Test: `tests/test_listening.py`; `tests/test_soulsync.py` additions

**Interfaces:**
- Produces, in `soulsync.py`:
  - `SoulSync._get(path: str, timeout: float, params: Dict) -> Tuple[int, Any]`: as `_send` but a GET with query params; same error handling and logging.
  - `SoulSync.stats(path: str, **params) -> Dict`: `_read(*self._get(path, REQUEST_TIMEOUT, params))`.
  - `SoulSync.picture(url: str) -> Optional[Tuple[bytes, str]]`: a path starting `/` is fetched from SoulSync with the key; an absolute URL only if its scheme, host and port are SoulSync's; anything else, a non-200, a content type not in `("image/jpeg", "image/png", "image/webp")`, or over `PICTURE_MOST = 512 * 1024` bytes gives `None`. Never raises.
- Produces, in `listening.py`:
  - `WINDOWS = {"week": "7d", "month": "30d", "year": "12m", "all": "all"}` and `WINDOW_WORDS = {"7d": "the last 7 days", "30d": "the last 30 days", "12m": "the last 12 months", "all": "all time"}`.
  - `class ListeningUnavailable(Exception)`: its message is a sentence to say.
  - `class Listening(config, key: Optional[str], clock=time.time)`: makes `SoulSync.from_config(config, key)` at each refresh, so a new address on the settings page is used at once.
    - `refresh() -> None`: fetches `/api/stats/cached?range=` for each of the four windows, `/api/stats/year` and `/api/stats/library-disk-usage`, plus the pictures of each window's top 5 artists and the year's top 5 artists and 5 discoveries (by URL, each fetched once per refresh), then swaps in a new `Snapshot` in one assignment. On any failure it logs once, keeps the old snapshot and records `problem` (the sentence). Raises nothing.
    - `snapshot() -> Snapshot` (frozen dataclass): `windows: Dict[str, Dict]` (the cached answer per range, keys as SoulSync gives them: `overview`, `previous`, `top_artists`, `top_albums`, `top_tracks`, `timeline`, `genres`, `clock`, `rhythm`), `year: Dict` (`period`, `totals`, `months`, `top_artists`, `top_albums`, `top_tracks`, `discoveries`, `peak_day`, `top_hour`), `health: Dict` (the cached answer's `health`), `disk: Dict`, `pictures: Dict[str, Tuple[bytes, str]]` (by image URL), `at: Optional[float]`, `problem: Optional[str]`.
    - `require() -> Snapshot`: the snapshot, or raises `ListeningUnavailable(problem or NOT_YET)` when no refresh has ever worked. `NOT_YET = "I haven't got your listening stats from SoulSync yet."`
    - `start()` / `stop()`: a daemon thread named `listening` that refreshes at once, then every `REFRESH_EVERY = 30 * 60` seconds, on a `threading.Event` like `FootballWatcher`.
  - Shape check: a cached answer missing `overview` or `top_artists`, or a year answer missing `totals`, is a failure with `problem = "SoulSync's stats have changed shape, so Nova can't read them."`; a `:latest` update is the likely cause.

- [ ] **Step 1: Check the live API (read-only), then write the fixtures.** On the brain, a script reads `SOULSYNC_API_KEY` from `/home/nova/.config/nova/.env` and `soulsync.url` from its config, GETs `/api/stats/cached?range=7d`, `/api/stats/year`, `/api/stats/library-disk-usage` and one `image_url`, and prints only status codes, key names, types and the `image_url` form (never values, never the key). Expected: 200s, and the keys in the Interfaces above. If the key gets 401/403 on `/api/stats`, stop and tell Nathaniel (the spec's step 0: he makes an admin key). Write the three fixtures with those keys and made-up values.
- [ ] **Step 2: Write the failing tests** with a fake `requests` session:
  - `test_stats_get_sends_key_and_params`: `stats("/api/stats/cached", range="7d")` GETs `.../api/stats/cached?range=7d` with the bearer header.
  - `test_picture_from_soulsync_path`, `test_picture_refused` (another host, 404, `text/html`, 600 KB: all `None`).
  - `test_refresh_reads_all_windows`: four cached calls, year, disk; `snapshot().windows["30d"]["overview"]["total_plays"]` is the fixture's.
  - `test_pictures_fetched_once_per_url`: two windows sharing a top artist fetch its picture once.
  - `test_refresh_keeps_last_good`: one good refresh, then SoulSync down: the snapshot is the old one with `problem == "SoulSync isn't answering."`.
  - `test_unknown_shape_is_unavailable`: `require()` before any good refresh with a shapeless answer raises with the "changed shape" sentence.
  - `test_new_address_used`: changing `soulsync.url` between refreshes calls the new address.
- [ ] **Step 3: Run, expect failures:** `nice -n 19 timeout 600 bash ~/work/pi_test_stats.sh tests/test_listening.py tests/test_soulsync.py`
- [ ] **Step 4: Implement** to the interfaces. Module docstring: why SoulSync is the one source (spec Approaches), the rolling windows, and that names never go to the log.
- [ ] **Step 5: Run, expect PASS** in London and UTC; ruff clean.
- [ ] **Step 6: Commit** "Read SoulSync's listening stats every half hour, for Nova to answer from"

### Task 2: Asking Nova

**Files:**
- Create: `voice_assistant/listening_words.py`
- Modify: `voice_assistant/actions.py` (`ActionExecutor.__init__` gains `listening=None`; `NEEDS` gains `"listening": "Listening stats need SoulSync."`; new tool), `voice_assistant/session.py` (pass `listening=brain.listening`), `voice_assistant/brain.py` (`self.listening = Listening(config, os.getenv("SOULSYNC_API_KEY"))`, started in `run()` and stopped in `stop()` beside the football watcher)
- Test: `tests/test_listening_words.py`, `tests/test_actions.py` additions

**Interfaces:**
- Consumes: `Listening.require()`, `WINDOWS`, `WINDOW_WORDS` from Task 1.
- Produces: `describe(listening, period: Optional[str], about: Optional[str]) -> Dict`, returning `{"success": True, "message": <facts for Claude to reply from>}` or `{"success": False, "message": <sentence>}`. `period` is a key of `WINDOWS` (default `"week"`); `about` is one of `"summary"` (default), `"artists"`, `"songs"`, `"albums"`, `"genres"`, `"when"`.
  - `summary`: plays, listening time in hours and minutes (from `total_time_ms`), artists, change in plays against the period before as a whole percentage (none for `all`), top 3 artists with plays.
  - `artists` / `songs` / `albums`: top 5 with plays (songs and albums with their artist).
  - `genres`: top 3 with percentages; `when`: the peak weekday and hour from `clock.peak`, longest streak and busiest day from `rhythm`.
  - Tool on `ActionExecutor`: `get_listening(period=None, about=None)`, `@tool(..., needs="listening", reads=True, period={"type": "string", "enum": ["week", "month", "year", "all"], ...}, about={"type": "string", "enum": [...], ...})`. Description says the windows are rolling ("this week" is the last 7 days), to answer only from what it gets, briefly, and that it can't answer for a calendar month or for today.

- [ ] **Step 1: Write the failing tests:**
  - `test_summary_week`: with the fixture snapshot, the message holds the plays, "10 hours 30 minutes" for 37,800,000 ms, "up 50%" and the first three artists in order.
  - `test_empty_window`: zero plays gives "You haven't listened to anything in the last 7 days." and no top lists.
  - `test_unavailable`: `require()` raising gives `{"success": False, "message": <its sentence>}`.
  - `test_songs_name_artist`, `test_when_uses_peak_and_streak`.
  - `test_tool_listed_only_with_listening`: `get_listening` is offered with `listening` set, not without.
- [ ] **Step 2: Run, expect failures:** `... tests/test_listening_words.py tests/test_actions.py`
- [ ] **Step 3: Implement** `listening_words.py`, the tool and the wiring.
- [ ] **Step 4: Run, expect PASS** (London and UTC), plus `tests/test_session.py tests/test_brain.py` unchanged; ruff clean.
- [ ] **Step 5: Commit** "Let Nova say who and what you've listened to most"

### Task 3: The music device in Home Assistant

**Files:**
- Create: `voice_assistant/music_device.py`
- Modify: `voice_assistant/brain.py` (make, start and stop it beside `usage_device`)
- Test: `tests/test_music_device.py`; `tests/test_ha_device.py` unchanged

**Interfaces:**
- Consumes: `Listening.snapshot()` (never `refresh`, as `_state` runs every second).
- Produces: `class MusicDevice(NovaDevice)` built like `UsageDevice` (name "Nova music", `mdl` "Nova brain", node id `nova_music_<hostname>`, subscribes to `HA_STATUS` only), `from_config(config, password, listening)`.
  - Sensors, one per window `w` in `7d`, `30d`, `12m`, `all`: control `plays_<w>`, name "Plays <7 days|30 days|12 months|all time>", entity id `sensor.nova_music_plays_<7_days|30_days|12_months|all_time>`, state `overview.total_plays`; attributes `minutes`, `artists`, `albums`, `tracks`, `change` (dict of the five overview figures' percentage changes against `previous`, `None` where `previous` is 0 or the window is `all`), `top_artists`, `top_albums`, `top_tracks` (first 10, each `name`, `artist` where it has one, `plays`), `timeline`, `clock` (`grid` and `peak`), `rhythm`, `genres` (first 5).
  - `library`: entity `sensor.nova_music_library`, state `health.total_tracks`; attributes `unplayed_count`, `unplayed_percentage`, `hours` (from `total_duration_ms`), `formats`, `disk_bytes` and `disk_formats` from the disk answer, `refreshed` (ISO time of the snapshot) and `problem`.
  - Images: control `artist_<w>_<n>` for n 1 to 5, platform `image`, `image_topic`, `content_type` from the picture, entity `image.nova_music_artist_<7_days|...>_<n>`, payload the picture's bytes. With no picture, publish an empty payload so HA shows none rather than a stale one.
  - Sizes: attributes stay under 16 KB per sensor (HA's recorder limit); the package in Task 4 excludes them from the recorder anyway.
- [ ] **Step 1: Write the failing tests** (reuse `test_usage_device.py`'s fake client): `test_discovery_lists_sensors_and_images` (5 sensors, 20 images, unique ids `nova_music_<host>_<control>`), `test_state_from_snapshot`, `test_change_none_when_previous_zero`, `test_picture_bytes_published_once` (unchanged bytes aren't republished), `test_no_snapshot_yet` (states `None`, nothing raises), `test_attributes_under_16kb` with 25-item fixtures.
- [ ] **Step 2: Run, expect failures:** `... tests/test_music_device.py tests/test_ha_device.py`
- [ ] **Step 3: Implement.** If `NovaDevice.publish_state` needs a change for bytes payloads, keep it to that and say why in a comment.
- [ ] **Step 4: Run, expect PASS** (London and UTC); ruff clean.
- [ ] **Step 5: Commit** "Show listening stats and top artists' pictures in Home Assistant"

### Task 4: The Music view (`~/work/ha-dashboard`, branch `main`)

**Files:**
- Create: `homeassistant/packages/music_stats.yaml`
- Modify: `config.yaml` (new view after AI: `title: Music`, `path: music`), `phone.yaml` (the same view, laid out for a phone, after Home), `README.md` (the package and the view)

**Interfaces:**
- Consumes: the entity ids from Task 3; `input_select.music_period` options "7 days", "30 days", "12 months", "All time".
- Produces: the view, top to bottom: period switch; five glass tiles (plays, listening time, artists, albums, tracks) each with its change as SoulSync shows it ("up 34% on the 7 days before", green up, red down); listening by day (apexcharts column chart from `timeline`); when you listen (apexcharts heatmap from `clock.grid`, Monday to Sunday, 24 hours) with peak slot, current and longest streak and busiest day under it; top artists (the five images in a row with names and plays, then rows 6 to 10); top songs and top albums (5 each); a library block (tracks, hours, unplayed share, size, formats bar); a "Your Wrapped" button opening `<settings page address>/wrapped` (Task 6). Look: `theme: glass`, `card_mod: class: glass`, grey monochrome like the AI view, the accent only on the chart bars.
- [ ] **Step 1:** Write the package: the input select, `recorder: exclude:` for the five `sensor.nova_music_*` and the 20 `image.nova_music_*` entities. Check YAML with `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))"` (the `!secret`-free file loads plainly).
- [ ] **Step 2:** Write both views. The cards pick the window from `input_select.music_period` with templates (as the AI view's cards read `ai_usage`'s switch).
- [ ] **Step 3: Stop for Nathaniel's named job** to copy the package into HA's `/config`, check the configuration, reload, and save both dashboards over the websocket; then read both back and compare.
- [ ] **Step 4:** Screenshot both views at deviceScaleFactor 2 (desktop 1920x1080, phone as Pixel 7) with the deployed Nova feeding them (after Task 7's deploy) and send them to him.
- [ ] **Step 5: Commit** in `ha-dashboard`: "Add a Music view with Nova's listening stats, on the desktop and the phone"

### Task 5: Making a Wrapped

**Files:**
- Create: `voice_assistant/wrapped.py`
- Modify: `voice_assistant/ntfy.py` (`send` and `send_soon` gain `click: str = ""`, sent as ntfy's `click`), `config.yaml` (new `wrapped:` section), `voice_assistant/settings_page.py` (`EDITABLE` rows), `voice_assistant/brain.py` (make, start and stop the watcher)
- Test: `tests/test_wrapped.py`, `tests/test_ntfy.py` addition, `tests/test_settings_page.py` addition

**Interfaces:**
- Consumes: `Listening.require()`, `Snapshot.pictures`.
- Produces:
  - `config.yaml` `wrapped:` with comments: `enabled: true`, `monthly: true`, `yearly_month: 12` (made on the 1st of that month), `link: ""` (the settings page's address through NPM, for the notification's link; empty sends no link).
  - `EDITABLE` rows, all `"now"`: `("wrapped", "enabled", "Make a Wrapped of your listening")`, `("wrapped", "monthly", "A smaller one each month")`, `("wrapped", "yearly_month", "Month the yearly one is ready, on the 1st (12 for December)")`, `("wrapped", "link", "This page's address, for the link in Wrapped's notification")`.
  - `make(snapshot, kind: str, now: datetime) -> Optional[Dict]`, `kind` `"year"` or `"month"`; `None` when the period had no plays. Keys: `key` (`"2026"` or `"2026-11"`, the month being covered, so a 1 December monthly is `"2026-11"`), `kind`, `made` (ISO), `period` (`start`, `end` dates: the year answer's `period` for a year; for a month, `now - 30 days` to the day before `now`), `minutes`, `plays`, `artists`, `albums`, `tracks`, `active_days`, `top_artists` (5, `name`, `plays`, `picture` index or `None`), `top_tracks` (5, `name`, `artist`, `plays`), `top_albums` (5, `name`, `artist`, `plays`), `top_genre` (name and percentage, or `None`), `time_of_day` (from the peak hour: 5 to 11 "an early bird", 12 to 17 "an afternoon listener", 18 to 22 "an evening listener", 23 to 4 "a night owl"), `peak_hour`, `longest_streak`, `busiest_day` (`date`, `plays`). Year only: `months` (`label`, `minutes`, `top_artist`), `discoveries` (5, `name`, `plays`, `picture`). Year from `snapshot.year` plus the `12m` window's `genres`, `clock` and `rhythm`; month from the `30d` window (`active_days` from its `rhythm`).
  - `class WrappedStore(folder: str)`: `save(wrapped: Dict, pictures: List[Tuple[bytes, str]])` writes `<key>.json` and `<key>-<n>.<jpg|png|webp>` atomically with `storage.save_text` / a temp file and rename; `load(key) -> Optional[Dict]`; `keys() -> List[str]` newest first; `picture(key, n) -> Optional[Tuple[bytes, str]]`. A key must match `^\d{4}(-\d{2})?$` or it's `None`. Keeps every yearly one and the newest `KEEP_MONTHS = 24` monthly ones. Folder: `os.path.join(paths.DATA_DIR, "wrapped")`.
  - `class WrappedWatcher(listening, store, ntfy, settings: Callable[[], Dict], clock=time.time)`: a daemon thread named `wrapped`, looking once an hour (`LOOK_EVERY = 3600`) and once at start. On a look, for the month just ended (and, in `yearly_month`, the year) whose Wrapped isn't in the store and whose 1st was this month: `make`, `save`, then `ntfy.send(phone_topic, "Your <November|2026> Wrapped is ready.", title="Wrapped", click=<link>/wrapped?key=<key>)`. Not made when `enabled` is off (`monthly` off skips months only), when `make` gives `None`, or when `listening.require()` raises (tried again next look). Only looks after 00:05 local, so SoulSync's last half-hour rebuild of the month is in.
- [ ] **Step 1: Write the failing tests:** `test_make_year_from_fixture`, `test_make_month_covers_last_30_days`, `test_time_of_day_edges` (hours 4, 5, 11, 12, 17, 18, 22, 23), `test_empty_month_not_made`, `test_store_round_trip_and_key_checked` (`load("../x")` is `None`), `test_store_keeps_24_months_and_every_year`, `test_made_once_after_restart` (a new watcher over the same store on the same day sends nothing), `test_made_late_if_missed` (first look on the 9th still makes it), `test_retries_after_failure`, `test_not_made_when_off`, `test_year_only_in_yearly_month`, `test_ntfy_click_sent`, `test_no_link_no_click`, and in `test_settings_page.py` that the four rows show and save.
- [ ] **Step 2: Run, expect failures:** `... tests/test_wrapped.py tests/test_ntfy.py tests/test_settings_page.py`
- [ ] **Step 3: Implement** to the interfaces; a London/UTC-safe "now" as `clock.py` does.
- [ ] **Step 4: Run, expect PASS** (London and UTC); ruff clean.
- [ ] **Step 5: Commit** "Make a Wrapped of each month's and year's listening, and say when it's ready"

### Task 6: The Wrapped page, and "what's my Wrapped?"

**Files:**
- Create: `voice_assistant/web/wrapped.html`, `wrapped.js`, `wrapped.css`
- Modify: `voice_assistant/settings_page.py` (`FILES` gains `/wrapped`, `/wrapped.js`, `/wrapped.css`; `do_GET` gains `/api/wrapped` and `/api/wrapped/picture`), `voice_assistant/actions.py` (new tool), `voice_assistant/session.py` (pass the store)
- Test: `tests/test_settings_page.py` additions, `tests/test_wrapped.py` additions

**Interfaces:**
- Consumes: `WrappedStore` from Task 5 (the brain holds it as `brain.wrapped_store`).
- Produces:
  - `GET /api/wrapped` → `{"keys": [...], "wrapped": <newest or ?key=>}`; an unknown or malformed key → 404. `GET /api/wrapped/picture?key=&n=` → the bytes with their type, or 404. Both behind the same login as `/api/state` (only `FILES` are open before one).
  - The page: full-screen cards, one at a time, tap right for next and left for back, swipe too, a progress bar of segments at the top like Spotify's, Outfit font, the settings page's dark look with a stronger accent per card. Cards in order: intro ("Your 2026 Wrapped" / "Your November"), minutes listened, top artist with picture, top five artists, top song, top album, top genre, when you listen (`time_of_day`, peak hour), longest streak and busiest day, each month's top artist (year), discoveries (year), the period in numbers (a summary card listing plays, minutes, artists, albums, tracks). A card whose data is missing is skipped. A menu on the last card lists the other saved Wrappeds by key. Not logged in: a message with a link to `/` to log in first.
  - Tool `get_wrapped(which=None)`: `which` is `"year"`, `"month"` or a key; reads the store and returns facts for Claude to say in four or five sentences (top artist, top song, minutes, time of day, one more). None saved yet: "Your first Wrapped is made on the 1st of next month."
- [ ] **Step 1: Write the failing tests:** `test_wrapped_api_newest`, `test_wrapped_key_checked` (`../x`, `2026-13x`, unknown, picture `n` past the end: 404), `test_wrapped_needs_login`, `test_wrapped_files_served`, `test_get_wrapped_says_top_artist`, `test_get_wrapped_none_yet`.
- [ ] **Step 2: Run, expect failures:** `... tests/test_settings_page.py tests/test_wrapped.py tests/test_actions.py`
- [ ] **Step 3: Implement** the routes and the tool, then the page. The page's script stays inside the page's CSP (`SAFETY_HEADERS`): no inline script, no outside fonts or images.
- [ ] **Step 4: Run, expect PASS** (London and UTC); ruff clean; `node --check` on `wrapped.js`.
- [ ] **Step 5:** Render the page with Playwright from a saved fixture Wrapped (a tiny local server, not the brain), phone size (Pixel 7), screenshot three cards at deviceScaleFactor 2 and send them to him for the look.
- [ ] **Step 6: Commit** "Show each Wrapped as a story to tap through, and let Nova read it out"

### Task 7: Records, the full suite and the deploy

**Files:**
- Create: `docs/decisions/0017-listening-stats.md` (decision record in the style of 0014: SoulSync as the one source and why, rolling windows, the device, the Wrapped and where it's kept)
- Modify: `README.md` (one paragraph under what Nova can do), `CHANGELOG` if the repo keeps one

- [ ] **Step 1:** Write the decision record and the README paragraph.
- [ ] **Step 2:** Check `/proc/loadavg`, then the full suite once in London and once in UTC: `nice -n 19 timeout 600 bash ~/work/pi_test_stats.sh` and `TZ_OVERRIDE=UTC ...`. Expected: all pass. Ruff clean.
- [ ] **Step 3: Commit** "Record the listening stats decision"
- [ ] **Step 4:** Whole-branch review by a fresh Opus reviewer; fix what it finds, one commit each, re-run only the affected test files.
- [ ] **Step 5: Stop for Nathaniel's named jobs:** merge `listening-stats` into `main`, deploy `main` to the brain and restart `nova-backend`; set `wrapped.link` on the settings page; then the HA job of Task 4 step 3. After the restart, a read-only check that the device's sensors have values and the voice tool answers.
- [ ] **Step 6:** His Last.fm import (spec, "His answers" 1): before it, note `/api/stats/cached?range=7d`'s `total_plays`; he enters his Last.fm username and API key in SoulSync's settings and runs its import; after it, the same figure must be unchanged (no double counting) and `all`'s should have grown by the older plays. Tell him both numbers.
- [ ] **Step 7:** Vault: the dated log entry in `projects/jarvis/log.md`, the status line in `index.md`, the memory note for this feature; commit and push the vault (pull first, stage named files only).
