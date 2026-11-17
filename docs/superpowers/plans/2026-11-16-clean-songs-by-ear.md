# Clean songs found by ear, and swapped - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova decides whether a song is clean by listening to it against its lyrics, swaps clean copies for explicit ones itself (after downloads and in a library check), and drops its label-based clean checks; plus 0012's music leftovers.

**Architecture:** Pure judging rules (`clean_ear.py`); lyrics from Navidrome or LRCLIB (`lyrics.py`); a low-priority worker process with its own Nemotron (`listener.py`, `listen_worker.py`); file moves inside the music folder (`music_folder.py`); a JSON store of verdicts, lists and swaps in progress (`clean_store.py`); the swap and the library check (`clean_swap.py`, `clean_check.py`) hooked into `downloads.py`; the page's Music settings and a Clean songs section.

**Tech Stack:** Python 3 (Nova's brain venv), requests, numpy, sherpa-onnx (already installed), ffmpeg (already on the brain), Navidrome Subsonic and native APIs, SoulSync REST (key) and its admin session, LRCLIB, pytest on the Pi.

**Spec:** `docs/decisions/0022-clean-songs-by-ear.md` (extends `docs/decisions/0012-music-fetched-by-voice.md`).

## Global Constraints

- Code and comment style as the repo has it: British spelling, no em dashes, comments above the code saying why with concrete quoted examples, Google-style Args:/Returns: docstrings, no comments on obvious lines. Keep each file's line endings.
- Small commits, one per change, each ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Each fix has a test that fails on the old code. Never push. Never `git stash`.
- Tests: `bash .superpowers/tools/pi_test.sh <tests...>` from the worktree root, and the whole suite once before the task's last commit, in London and again with `TZ_OVERRIDE=UTC`. That script copies `~/work/Home-Assistant`; for this worktree pass it the worktree with `NOVA_TREE=$PWD` if Task 1 adds that (see Task 1). Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work && /tmp/nova-check-pytest/bin/ruff check .'` after a pi_test run. CT 109 limits: `cat /proc/loadavg` first (wait above 3), one suite at a time, no repeat runs. Never `ps` on the Pi.
- Nothing names the user's hosts, holds a key, or prints a key, password, token or Subsonic signed URL (log a requests error by its class name).
- Settings the user may change go on the settings page through `SETTINGS` in `settings_page.py`, with a `LIMITS` entry in `config.py` and a default in `config.yaml`. Secrets go under Keys (`KEYS` in `settings_page.py`).
- Defaults, verbatim from the spec: lyric minimum 3 swears; explicit at 35% heard or more; clean at 15% or less; tries to swap 2; music folder `/mnt/music`; holding folder `.nova-held` inside it, with a blank `.ndignore`; SoulSync admin password key `SOULSYNC_ADMIN_PASSWORD`.
- The swear pattern, verbatim: `\b(fuck\w*|motherfuck\w*|shit\w*|bitch\w*|nigga\w*|nigger\w*|pussy|pussies|dick\w*|hoes?|ass)\b`, case-insensitive; heard = matches in the transcript, not capped.
- Never delete a file; never overwrite one; never move anything outside the music folder or anywhere but `.nova-held/`.
- Tests fake Navidrome, SoulSync, LRCLIB, the worker process and the file system (tmp_path). The model is never loaded in tests.

## Review Focus

1. A song Navidrome lists whose real path is absolute, climbs out with `..`, or is a symlink out of the music folder: refused, never moved. Test in Task 4.
2. A swap whose re-fetch never lands: the request comes off SoulSync's wishlist and the set-aside copy goes back; if a new file now sits at that path, both stay and the list says so. Test in Task 7.
3. A restart mid-swap: the saved swap carries on (watch while inside its limit, else restore). Test in Task 7.
4. The listening worker dying, hanging or the decode failing mid-song: the song is "not judged" with the reason, a download says "couldn't check", and the next song still works. Test in Task 3 and Task 7.
5. A library check that meets a song already judged with the same size and modified time, or one that only came down clean: skipped. Test in Task 8.

---

### Task 0 (controller, live, read-only): confirm SoulSync's calls

Not for a subagent. With SoulSync's key, read-only:
- [ ] `GET /api/v1/library/tracks?title=...&artist=...` gives `id` and `file_path` (`/app/Transfer/<relative path>`), confirmed 16 Nov; note the id's form.
- [ ] The wishlist's own DELETE route (`DELETE /api/v1/wishlist/<id>`): read its code for the id it takes (the Deezer track id sent in `track_data`?) and its auth.
- [ ] `POST /api/auth/login` and `DELETE /api/library/track/<id>`: read their code for the session cookie name and the 401/403 shapes. Not called (no profile yet).
- [ ] Write what was found into the ledger; adjust Task 5's Interfaces if any differs.

### Task 1: the judging rules and their settings

**Files:**
- Create: `voice_assistant/clean_ear.py`, `tests/test_clean_ear.py`
- Modify: `config.yaml` (new `clean_check:` section), `voice_assistant/config.py` (LIMITS), `.superpowers/tools/pi_test.sh` (optional `NOVA_TREE`)

**Interfaces:**
- Produces:
  - `SWEARS: re.Pattern` (the pattern above)
  - `def count_swears(text: str) -> int`
  - `@dataclass Verdict: kind: str` ("explicit" | "clean" | "unsure" | "too_few" | "not_judged"), `expected: int`, `heard: int`, `reason: str = ""`
  - `@dataclass Thresholds: minimum: int = 3, explicit_share: float = 0.35, clean_share: float = 0.15` and `Thresholds.from_config(config) -> Thresholds` reading `clean_check.lyric_minimum`, `.explicit_share`, `.clean_share`
  - `def judge(lyrics: Optional[str], transcript: Optional[str], thresholds: Thresholds) -> Verdict`
- config.yaml `clean_check:` keys: `lyric_minimum: 3`, `explicit_share: 0.35`, `clean_share: 0.15`, `swap_tries: 2`, `music_folder: "/mnt/music"`, `soulsync_admin: ""` (the SoulSync profile Nova signs in as), each with a comment pointing at decision 0022.
- LIMITS: `("clean_check", "swap_tries"): Limit("number", 0, 5, True)`, `("clean_check", "lyric_minimum"): Limit("number", 1, 20, True)`, `("clean_check", "explicit_share")` and `("clean_check", "clean_share")`: `Limit("number", 0, 1)`. Music folder and admin profile take free text (check how the file's other free-text keys are held).

- [ ] **Step 1: Failing tests** in `tests/test_clean_ear.py`:
  - `count_swears("Oh my God, what the fuck? Why these niggas") == 2`; `count_swears("Pass the class") == 0`; `count_swears("hoes and a hoe, asshole") == 2` ("asshole" isn't "ass", as the pattern's `\b` gives).
  - With `lyrics6 = "fuck shit bitch fuck shit bitch"` (6 expected) and default thresholds `T`: heard 3 ("fuck shit bitch", 50%) -> "explicit"; heard 2 (33%) -> "unsure"; heard 1 (17%) -> "unsure"; heard 0 -> "clean". `judge("fuck shit", "", T).kind == "too_few"`; `judge(None, "x", T)` is "not_judged" with reason "no lyrics"; `judge(lyrics6, None, T)` is "not_judged" with reason "not heard". Exactly at a threshold counts for that side: 20 expected, 7 heard (35%) -> explicit; 20 expected, 3 heard (15%) -> clean.
  - Endless-shaped: 16 expected, 1 heard -> clean; 14 expected, 7 heard -> explicit; 72 expected, 49 heard -> explicit.
  - `Thresholds.from_config` reads the three keys and falls back to the defaults.
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_clean_ear.py`
- [ ] **Step 3: Implement.** Exactly at the thresholds counts as that side: `heard / expected >= explicit_share` is explicit, `<= clean_share` is clean.
- [ ] **Step 4: pi_test.sh**: if `NOVA_TREE` is set, copy that tree instead of `~/work/Home-Assistant` (one line, default unchanged). Then run the tests: pass. Then the whole suite once, London and UTC, and ruff.
- [ ] **Step 5: Commit** "Judge a song clean or explicit by how many of its lyric swears are heard".

### Task 2: lyrics, and what Navidrome says of a song's file and sound

**Files:**
- Create: `voice_assistant/lyrics.py`, `tests/test_lyrics.py`
- Modify: `voice_assistant/navidrome.py` (NavidromeLibrary gains three methods), `tests/test_navidrome.py`

**Interfaces:**
- Produces:
  - `NavidromeLibrary.song_file(song_id: str) -> Dict` with `{"path": str (relative), "size": int, "updated": str}` from the native API (`POST <url>/auth/login` with `{"username", "password"}` -> `token`; `GET <url>/api/song/<id>` with header `x-nd-authorization: Bearer <token>`; one sign-in per NavidromeLibrary, again on a 401). Raises NavidromeError (message without the URL) on failure.
  - `NavidromeLibrary.audio(song_id: str) -> bytes` (`/rest/stream`, `format=raw`, signed as `_call` signs, timeout 120 s).
  - `NavidromeLibrary.lyrics(song_id: str) -> Optional[str]` (`getLyricsBySongId`, the `structuredLyrics` lines joined with newlines; None when there are none or Navidrome refuses).
  - `class Lyrics: __init__(self, library: NavidromeLibrary, session=None)`, `def for_song(self, song: Dict) -> Tuple[Optional[str], str]` returning the lyrics and their source ("navidrome" | "lrclib" | "none"). LRCLIB: `GET https://lrclib.net/api/search?track_name=<title>&artist_name=<first artist>`, timeout 15 s, take the result with `plainLyrics` whose `duration` is nearest the song's (`song["duration"]`, seconds) and within 10 s; `User-Agent: Nova (https://github.com/NAmondsenn/nova)`.

- [ ] **Step 1: Failing tests** with fake sessions: song_file signs in once for two songs and again after a 401; its error text holds no token or password; audio returns the bytes; lyrics joins lines, None for an empty list and for a refusal; Lyrics takes Navidrome first, then LRCLIB's nearest-duration result, skips one 11 s off, gives (None, "none") when LRCLIB fails or times out (no exception).
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_lyrics.py tests/test_navidrome.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Fetch a song's lyrics, file and sound for listening".

### Task 3: the listening worker

**Files:**
- Create: `voice_assistant/listener.py`, `voice_assistant/listen_worker.py`, `tests/test_listener.py`

**Interfaces:**
- Consumes: `voice_assistant.nemotron.Nemotron` (start, add, finish), `voice_assistant.paths.MODELS_DIR`, the model folder named by `stt.model` in config.
- Produces:
  - `def decode(audio: bytes) -> np.ndarray`: ffmpeg `-v error -i pipe:0 -ac 1 -ar 16000 -f f32le pipe:1`; raises `ListenError("couldn't decode the song")` on a non-zero exit.
  - `class ListenError(Exception)` carrying the reason said in lists.
  - `class Listener: __init__(self, model_folder: str, threads: int = 2, idle: float = 300, command: Optional[List[str]] = None)`; `def transcribe(self, samples: np.ndarray, timeout: float) -> str`; `def close(self)`.
  - The worker: `python -m voice_assistant.listen_worker <model_folder> <threads>` run under `nice -n 19` (`os.nice(19)` in the child as well, belt and braces), reading requests from stdin as a 4-byte little-endian length then that many float32 bytes, answering each with one JSON line `{"text": ...}` or `{"error": ...}` on stdout. It feeds Nemotron one-second pieces from start to end (the spike's method) and loads the model once.
  - The parent starts the worker on first use, sends one song at a time (a lock), restarts it after an error or a timeout (timeout = 3 x the song's length + 60 s, the caller passes it), and closes it after `idle` seconds without work.

- [ ] **Step 1: Failing tests** with `command` set to a small fake worker script written in tmp_path (echoes the sample count as text; or exits; or sleeps forever): transcribe returns the fake's text; a worker that exits mid-request raises ListenError and the next transcribe starts a fresh worker and succeeds; a worker that hangs raises ListenError after the timeout and is killed; close kills it; the idle timer closes it; decode of a fake ffmpeg failure raises ListenError (patch subprocess.run).
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_listener.py`
- [ ] **Step 3: Implement.** No real model in tests.
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Listen to a song in a worker of its own, at the lowest priority".

### Task 4: the music folder and its holding folder

**Files:**
- Create: `voice_assistant/music_folder.py`, `tests/test_music_folder.py`

**Interfaces:**
- Produces:
  - `HELD = ".nova-held"`, `SIDECARS = (".lrc", ".txt")`
  - `@dataclass Held: original: str` (relative), `held: str` (relative inside `.nova-held`), `sidecars: List[Tuple[str, str]]`
  - `class MusicFolder: __init__(self, root: str)`; `def usable(self) -> Optional[str]` (None when the folder exists and Nova can write to it, else the reason); `def set_aside(self, rel: str) -> Held`; `def put_back(self, held: Held) -> str` ("put back" | "a new copy is there" | the error); `def held_count(self) -> int` (audio files under `.nova-held`).
  - Raises `MusicFolderError(reason)` for a refused or failed move.
- Rules: `rel` must be relative, without `..`, and resolve (realpath) inside the root to a file not under `.nova-held`; the move is `os.link` then `os.unlink`, the sidecars with the same stem move with it, and the row is rolled back if any part fails (a failed rollback is named in the error); a taken name in `.nova-held` gets " (2)", " (3)" before the extension; `.nova-held/` and a blank `.ndignore` in it are made on first use; put_back never overwrites (link fails with EEXIST -> "a new copy is there", both kept).

- [ ] **Step 1: Failing tests** in tmp_path: set_aside moves the audio and its .lrc and .txt but not cover.jpg, and makes `.ndignore`; a second song of the same name gets " (2)"; "../x.mp3", "/etc/passwd", a symlink pointing outside, and a path under `.nova-held` are refused with nothing moved; a failing second link rolls the first back; put_back restores all three files; put_back with a file now at the original path returns "a new copy is there" and changes nothing; usable() gives a reason for a missing or read-only folder; held_count counts audio only.
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_music_folder.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Set a song aside inside the music folder, and put it back".

### Task 5: SoulSync forgets a song, and a wish is taken back

**Files:**
- Modify: `voice_assistant/soulsync.py`, `tests/test_soulsync.py`

**Interfaces:**
- Consumes: Task 0's findings.
- Produces:
  - `SoulSync.find_song(title, artist, album: Optional[str] = None)`: with an album, pick_track is tried first on the candidates whose album normalises to the same name, then on all of them (the spec's "preferring a SoulSync candidate on the same album"). Test both.
  - `SoulSync.unwish(track_id: str) -> bool` (`DELETE /api/v1/wishlist/<id>`, the key; False and a warning on failure, never raises).
  - `class SoulSyncAdmin: __init__(self, url: str, profile: str, password: str, session=None)`; `def forget(self, soulsync: SoulSync, title: str, artist: str, rel_path: str) -> bool`: finds SoulSync's row with `GET /api/v1/library/tracks?title=&artist=` (through `soulsync`, the key) whose `file_path` ends with "/" + rel_path, signs in with `POST /api/auth/login` (once, again after a 401 or 403), and removes it with `DELETE /api/library/track/<id>` (no `delete_file`, no `blacklist`). True when no row is left for that path (already none counts). Raises `SoulSyncError(reason)` for a refused sign-in ("SoulSync didn't accept Nova's admin profile, so SOULSYNC_ADMIN_PASSWORD under Keys needs checking.") or a failed delete.
  - `SoulSyncAdmin.from_config(config, password) -> Optional[SoulSyncAdmin]` (None without `clean_check.soulsync_admin`, the address, or the password).

- [ ] **Step 1: Failing tests** with a fake session: forget signs in once, deletes the matching row only (two rows, one other path), sends no `delete_file`; a row already gone gives True without a delete; a 401 sign-in raises SoulSyncError with the key sentence and no password in it; a 403 on the delete signs in again once; unwish returns True on 200 and False on 404 without raising.
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_soulsync.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Let Nova make SoulSync forget a song it set aside".

### Task 6: the store

**Files:**
- Create: `voice_assistant/clean_store.py`, `tests/test_clean_store.py`
- Modify: `voice_assistant/paths.py` (`CLEAN_SONGS_FILE = os.path.join(DATA_DIR, "clean_songs.json")`)

**Interfaces:**
- Produces `class CleanStore: __init__(self, path: str)` (thread-safe, written atomically through a temporary file and `os.replace`, a corrupt file is renamed aside with a warning and the store starts empty) with:
  - `verdict(song_id) -> Optional[Dict]` and `set_verdict(song_id, size, updated, verdict: Verdict, name: str)`; `known(song_id, size, updated) -> bool` (same size and updated).
  - `listed(kind) -> List[Dict]` for kinds "swapped", "only_clean", "unsure", "couldnt", and `add(kind, name, reason="", **extra)` (newest first, one entry per name and kind, at most 200 each).
  - `swaps() -> List[Dict]`, `save_swap(swap: Dict)`, `end_swap(key: str)` (a swap is a dict with `key`, `name`, `title`, `artist`, `album`, `held` (Held as dict), `tries`, `track_id`, `asked_at`, `limit`).
  - `check() -> Dict` / `save_check(state: Dict)` for the library check's progress (`running`, `done`, `total`, `current`, `queue` of song ids, `last_finished`).

- [ ] **Step 1: Failing tests**: round trip of each part through a reload; `known` false after a size change; a corrupt file starts empty and is kept as `clean_songs.json.bad`; two threads adding at once lose nothing; the 200 cap.
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_clean_store.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Keep what Nova has heard of each song, and its swaps".

### Task 7: judge each download, and swap a clean one

**Files:**
- Create: `voice_assistant/clean_swap.py`, `tests/test_clean_swap.py`
- Modify: `voice_assistant/downloads.py`, `tests/test_downloads.py`, `voice_assistant/brain.py` (wiring, and `stop()` closing the listener)

**Interfaces:**
- Consumes: Tasks 1 to 6.
- Produces:
  - `class Ears: __init__(self, config, library_factory, listener: Listener, lyrics_factory, store: CleanStore)`; `def hear(self, song: Dict) -> Verdict`: song_file (for size and updated), lyrics, decode(audio), transcribe (timeout 3 x duration + 60), judge; stores the verdict; any NavidromeError, ListenError or requests error gives `not_judged` with a plain reason. One song at a time (shares the Listener's lock).
  - `class Swapper: __init__(self, config, ears: Ears, folder_factory, soulsync_factory, admin_factory, library_factory, store, say)`; `def swap(self, song: Dict, wanted: Wanted, satellite: Optional[str], quiet: bool) -> str` returning the outcome kind ("swapped" | "only_clean" | "couldnt") and recording it in the store's lists; `def resume(self)` carrying on saved swaps after a restart (started from Downloads' first use, in a thread).
  - The swap, per the spec section 4: set_aside -> save_swap -> start_scan, then look until Navidrome no longer lists that song id (at most 2 minutes, else put back and "couldnt": "Navidrome still lists it") -> admin.forget -> soulsync.find_song(title, artist, album) + request_song -> watch with Downloads' own look and limit -> hear the new copy: explicit or too_few -> "swapped"; clean -> while tries remain, set it aside too and fetch again; on the last try the clean copy that landed stays in the library and the outcome is "only_clean" (earlier copies stay held); unsure -> kept, "unsure"; not landed in time -> unwish + put back the last held copy + scan -> "couldnt" with the reason.
  - Downloads: after a song lands (`_landed`), hear it before speaking: explicit or too_few -> as now; unsure -> "X has been downloaded, but I couldn't tell if it's the clean version."; not_judged -> "X has been downloaded, but I couldn't check it for a clean version."; clean -> Swapper.swap (not quiet), then "X has been downloaded." for swapped, "X only came down clean, so I've kept it." for only_clean, "X has been downloaded, but it's the clean version, and I couldn't swap it: <reason>." for couldnt. Albums: each new track heard; clean ones swapped one by one; one sentence for the album naming how many were swapped and how many only came down clean.
  - Downloads stops using `is_clean` for what landed (`_song_outcome`, `_clean_ones`, `_album_tracks`' explicit-first order) and uses verdicts for what's had (`_as_had`, `_in_navidrome`'s offer, `_none_queued`): a song with a stored clean verdict is "only a clean version"; everything else is had.
  - Without a usable music folder or admin (folder.usable() or SoulSyncAdmin None), a clean song isn't swapped: it's listed under "couldnt" with that reason, and the download says so as above.

- [ ] **Step 1: Failing tests** (fakes for every part; the Downloads tests keep their FakeSoulSync and FakeNavidrome style):
  - a landed explicit song says "X has been downloaded." as before; unsure and not_judged say their sentences;
  - a landed clean song is swapped: set aside, scanned, forgotten, fetched again, the new copy explicit -> "X has been downloaded." and listed as swapped;
  - every try clean -> tries+1 fetches in all, "X only came down clean, so I've kept it.", the copies set aside by earlier tries stay held, the last copy is in the library;
  - a re-fetch that never lands -> unwish called with its id, the held copy put back, scanned, "couldnt" with the reason (Review Focus 2), and with a new file at that path both kept and the reason says so;
  - a restart mid-swap: a saved swap inside its limit is watched to the end; one past its limit is restored (Review Focus 3);
  - listening that fails mid-song -> "couldn't check" and the next download still heard (Review Focus 4);
  - no admin set -> listed "couldnt" with "SoulSync admin profile isn't set up", nothing moved;
  - "download SICKO MODE" with a stored explicit verdict on a "(clean)" album -> "You've already got ..."; with a stored clean verdict -> the explicit offer.
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_clean_swap.py tests/test_downloads.py`
- [ ] **Step 3: Implement.** Remove the label-based landing checks and their now-dead tests in the same task.
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** in two or three commits (Ears, Swapper, Downloads wiring).

### Task 8: the library check

**Files:**
- Create: `voice_assistant/clean_check.py`, `tests/test_clean_check.py`
- Modify: `voice_assistant/brain.py` (one CleanCheck per brain; started and stopped with Downloads)

**Interfaces:**
- Consumes: Ears, Swapper, CleanStore, NavidromeLibrary (`getAlbumList2` pages of 500, `_album_songs`).
- Produces `class CleanCheck: __init__(self, ears, swapper, store, library_factory)`; `def start(self) -> Dict` ({"success", "message"}; refuses while running or without Navidrome); `def stop(self) -> Dict` (ends after the current song); `def state(self) -> Dict` (`running`, `done`, `total`, `current`, `last_finished`, and the four lists, and `held` from MusicFolder.held_count()); `def resume(self)` (a check stopped by a restart carries on from its saved queue).
- The check: list every song; queue those not `known` with the same size and updated, leaving out songs listed only_clean; for each, hear it (lyrics first, so songs with too few swears are passed over without listening: Ears.hear does lyrics before audio), and swap a clean one quietly; save progress after each song. A download's hearing goes first: the check waits while Ears is busy with a download (a shared priority flag).

- [ ] **Step 1: Failing tests**: a check over five fake songs (one known with the same size, one known with a new size, one only_clean, one clean, one explicit) hears three and swaps one; stop ends after the current song; resume carries on from the saved queue; start while running refuses; nothing is said aloud (`say` never called) (Review Focus 5).
- [ ] **Step 2: Run, see them fail.** `bash .superpowers/tools/pi_test.sh tests/test_clean_check.py`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff.**
- [ ] **Step 5: Commit** "Check the library for clean songs in the background".

### Task 9: the settings page

**Files:**
- Modify: `voice_assistant/settings_page.py`, `voice_assistant/web/settings.js`, `voice_assistant/web/settings.html` and `settings.css` if needed, `tests/test_settings_page.py`, the node checks for settings.js (memory note node-for-page-tests: `~/tools/node`, `--max-old-space-size=768 --test --test-concurrency=1`, no DOM equality asserts)

**Interfaces:**
- SETTINGS rows (Music group, "now"): `("clean_check", "swap_tries", "Tries to swap a clean song", "now")`, `("clean_check", "music_folder", "Music folder", "now")`, `("clean_check", "soulsync_admin", "SoulSync admin profile", "now")`. KEYS: `("SOULSYNC_ADMIN_PASSWORD", "SoulSync admin")`.
- `SettingsPage.clean_state() -> Dict` (CleanCheck.state()) served at `GET /api/clean`; ACTIONS `"/api/clean/check": page.start_clean_check(who)` and `"/api/clean/stop": page.stop_clean_check(who)`.
- The page: a "Clean songs" block in the Music section, as the spec's sketch: Check library / Stop buttons, "Last check", progress line, the four lists, the holding folder count with "Emptying it is up to you".

- [ ] **Step 1: Failing tests**: the three settings save and are refused out of limits; the key is saved and never sent back; `/api/clean` gives the state; check and stop call CleanCheck and log who; node tests render the block from a fake state (progress line, list items, held count) and the buttons post to the right paths.
- [ ] **Step 2: Run, see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, pass; whole suite London and UTC; ruff; node.**
- [ ] **Step 5: Commit** "Show the clean songs on the settings page, and check the library from it".

### Task 10: the music leftovers

**Files:**
- Modify: `voice_assistant/navidrome.py`, `voice_assistant/soulsync.py`, `voice_assistant/downloads.py` and their tests

One commit each, each with a test failing on the old code:
- [ ] "demo" in `navidrome.VERSIONS`; check play's matching with a test that "play Song" doesn't pick "Song (Demo)" when the plain one exists, and still plays the demo when it's all there is.
- [ ] Remove the watch's dead "done" and "failed" branches (`_follow`'s progress handling, `_scan_due`'s done_at) and their tests; the scan before the limit stays.
- [ ] A dashed part drops its own brackets before it is read, in soulsync's `_parts`: `pick_track([track "Song - Extended Mix (feat. X)"], "Song", artist)` is None (the old code takes it), and `pick_track([track "Song - Extended Mix (feat. X)"], "Song Extended Mix", artist)` still takes it.
- [ ] A wider title isn't had for a download: "download Endless" with only "Endless Love" in Navidrome is requested; play's partial titles unchanged (test "play Sweet Dreams" still finds "Sweet Dreams (Are Made of This)").
- [ ] `request_album` doesn't call `process_wishlist` when `queued` is 0.
- [ ] "clean lyrics" and "clean advance copy" read as clean versions when picking (`version_of("X (clean lyrics)")` is clean; pick_track refuses an album "Y (Clean Advance Copy)").
- [ ] Whole suite London and UTC; ruff.

### Task 11: docs

**Files:**
- Modify: `README.md` (music section), `docs/troubleshooting.md`, `docs/release-checklist.md` (new checks 25a to 25f: a clean download swapped; every try clean; a re-fetch that never lands; restart mid-swap; the library check and its lists; the holding folder count), `docs/decisions/0012-music-fetched-by-voice.md` (a line at the top of section 4 pointing to 0022), the roadmap if it lists music.
- [ ] Every fixed sentence in the docs matches the code word for word; no em dashes (`git diff -U0 -- README.md docs/ | grep -nP '\x{2014}'` finds nothing); set-up says: the music folder share (pve2 `mp1`), the SoulSync admin profile and its password under Keys, and that `.nova-held` is emptied by hand.
- [ ] Commit "Document clean songs found by ear".

### After the build (controller, his named jobs)

Merge into main, push, then: the CT 107 share (pve2 `pct set 107 -mp1 /mnt/library/media/music,mp=/mnt/music` and a restart of the container), the SoulSync admin profile (by him in SoulSync's page, or by the controller if SoulSync's API allows on his word), the password under Keys (by him), deploy (git pull as nova, restart nova-backend), then the library check from the page.
