# Update Once, Everything Updates - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One action from him (the settings page's Update Nova button, or "Nova, update yourself" with his approval) brings the brain to GitHub's latest main and every satellite to exactly the brain's commit, with machines that were away catching up as they reconnect, and every machine falling back to the code it had if the new code won't start.

**Architecture:** The brain asks a separate updater unit (`nova-update.service`, started by a root-installed path unit when the brain drops a request file in its own data folder) to fast-forward its clone, install packages and models and run the settings check; the brain then exits 75 into the new code, with a take-back marker that a root-installed `OnFailure=` unit acts on if it won't start. Every satellite says its commit in its hello; on each connection the brain sends one that differs its own commit (a git bundle to the Pi, a zip to the PC) in chunks, with an HMAC bound to that connection's handshake nonces, and the satellite verifies it before anything is written, waits until it is free, and hands it to a detached installer from its running code which installs, restarts, waits for the new code to reconnect, and takes it back if it doesn't.

**Tech Stack:** Python 3 standard library (hmac, hashlib, zipfile, subprocess), git (bundle, archive), bash and systemd (system units on the brain, `systemd-run --user` on the Pi), Windows PowerShell 5.1 and Task Scheduler on the PC, pytest on the Pi.

**Spec:** `docs/superpowers/specs/2026-11-26-update-everywhere-design.md`. Read it first; its decisions are settled and this plan argues from it. Where the spec left a choice to the builder, the choice is in "Builder's choices" below and goes into decision 0040.

## Global Constraints

- He starts every update. Nothing updates on a schedule, and nothing checks GitHub by itself. A routine can't start one.
- Always GitHub's `main`, fast-forward only. Every machine ends on the brain's commit; no machine is ever ahead of the brain (a satellite ahead is moved back to it).
- The brain's lock stays: `nova-backend.service` keeps `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` and its two `ReadWritePaths`. The brain only asks for the updater; it never writes its own code.
- A satellite never installs code it can't prove came from its brain unaltered: HMAC-SHA256 keyed with that satellite's password, over the archive and the commit id, bound to this connection's two handshake nonces. Checked before anything is written. The check is logged as pass or fail only; no proof, password or nonce is ever logged.
- No machine restarts during a recording, a reply, a conversation, a ringing alarm or timer, or an approval in progress. Music is not "in use": it is noted and resumes where it was.
- Never a dead satellite and never a dead brain: each keeps the previous code and goes back to it if the new code won't start, by itself.
- Never silent: every outcome shows on the settings page and in the brain's log, with why; "needs you" always carries the one command to run.
- The update messages are frozen for ever: the hello's `version`, `updates` and `update_format`, `update_offer`, `update_chunk`, `update_end`, `update_installing` and `update_result`. A test pins their shape.
- What Nova says (exact): "Updating. I'll be back in a minute." on starting; "Already up to date; updating {names}." when only satellites are behind (names as `Brain.spoken_name` gives them, joined "the Pi and the PC"); "Already up to date." when nothing is behind; "I'm already updating." on a second start; "I couldn't update, as I was busy for {n} minutes. Ask again when I'm free." ("1 minute" when n is 1); "The update didn't work, so I'm still on the old version. The settings page says why."; "The update needs you: the settings page says what to run."; "I'm updated." at the satellite he asked at, once it is back (with " One thing needs you on the settings page." added when there is a needs-you note).
- The approval description (exact): "Update Nova on every machine".
- Setting (section `updates`, used straight away, on the page): `idle_wait_minutes` 10, a whole number from 1 to 60, labelled "Minutes to wait for Nova to be free before updating".
- Constants: `protocol.UPDATE_CHUNK = 256 * 1024` (inside `MAX_AUDIO`, 320,000, which caps every payload), `protocol.UPDATE_MOST = 64 * 1024 * 1024`, `protocol.MOST_UPDATE_ERROR = 300`, confirm wait 180 s on the Pi and the PC, the brain counted as started after 60 s, music resumed only within 600 s, the brain's wait for its updater 30 minutes.
- No new third-party packages. The repo is public: nothing personal or secret, no hostnames beyond what the repo already has.
- Code style (memory notes working-style, design-principles): long explanatory Google-style docstrings and full-sentence why-comments in plain British English, spaced " - " dashes (never em dashes), the file's existing conventions; one commit per task; a test that fails on the old code for every rule.
- Tests run on the Pi only, never locally. "RUN <args>" in this plan means, in order: `cat /proc/loadavg` (wait while the 1-minute load is above 3); `ssh -o BatchMode=yes pi 'pgrep -f "python -m pytest"'` prints nothing (never `ps` on the Pi); then `NOVA_TREE=/home/nathaniel/work/Home-Assistant-update NOVA_CHECK=update nice -n 19 timeout 600 bash ~/work/Home-Assistant/.superpowers/tools/pi_test.sh <args>`. One run at a time, never in the background, no repeat runs of a passing command. "RUN-UTC <args>" is the same with `TZ_OVERRIDE=UTC` in front. Ruff, after a RUN has copied the tree: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/update && /tmp/nova-check-pytest/bin/ruff check .'`.
- No `git stash`. Don't touch any other worktree's code. Reviewers read diffs and run nothing.
- Nothing is pushed, deployed or restarted: the plan ends with the branch ready. The bootstrap (push; root's `update.sh` on the brain; the Pi's `update.sh`; `deploy_pc.sh`) is his named jobs, and anything that makes sound in his room or touches his PC waits for his go.

## Builder's choices (record them in decision 0040)

1. **How the brain starts its updater: a path unit, not polkit or sudoers.** `NoNewPrivileges=yes` stops the brain running sudo (setuid), and polkit needs `polkitd`, which the brain's container doesn't install. So root installs `nova-update.path`, which starts `nova-update.service` when the file `~/.local/share/nova/update/request` appears in the brain's own writable data folder. That unit is the narrow rule: the brain can start that one job and nothing else, and the job only fast-forwards from GitHub.
2. **The brain restarts itself by exiting 75** once the updater says the new code is in place; the updater (as `nova`) can't restart a system unit either.
3. **Start-failure take-back on the brain:** `nova-backend.service` gains `StartLimitIntervalSec=600`, `StartLimitBurst=3` and `OnFailure=nova-takeback.service`. Today's `RestartSec=10` never trips the default limit, so without this `OnFailure=` would never fire. The take-back unit's fixed text (installed by root, so not writable by `nova`) resets the clone to the commit in `update/previous` as `nova`, then, as root, `systemctl reset-failed` and starts the brain again, so an ordinary crash loop, or four restarts in ten minutes from the page, carries on as today after one extra pause.
4. **A brain commit needing root still restarts into the new code** and reports "needs you" with `sudo bash /home/nova/Home-Assistant/update.sh --no-pull` (the path is the clone's). Root-needing means the range changed a unit template (`nova-backend.service`, `nova-update.service`, `nova-update.path`, `nova-takeback.service`) or the body of `install_system_packages` in `setup.sh`. The take-back covers a new commit that can't run without them.
5. **Formats:** the Pi gets a git bundle (thin, from its own commit, when that is an ancestor of the brain's), so its clone stays a clone; the PC gets `git archive --format=zip`, whose zip comment is the commit id. The hello says which a satellite takes, as `update_format` ("bundle" with a `.git` folder, "zip" on Windows with `app\COMMIT`; the Mac says none until it has an installer).
6. **"Started" on a satellite means the new code completed a handshake with the brain within 180 s.** The helper that installs and takes back is the running (old) code's copy; the install steps (`update.sh --no-pull`, `install.ps1`) are the new code's.
7. **A version that failed on a satellite isn't tried again on every reconnect** (which would loop): the satellite remembers it and answers its offer with the failure, unless the offer says `retry` - which only a press of Update Nova (or the voice command) sends.
8. **The idle wait limit applies before the brain starts its updater.** Once new code is on disk the restart waits for idle without a limit, as giving up would leave new code for the next crash to start. Satellites wait for idle without a limit.
9. **Music resume:** only Navidrome music that was playing (`hand_over`), kept for 600 s, resumed with `take_over` as the satellite's session is caught up. Spotify plays on the satellite's own Spotify Connect speaker and isn't stopped by either restart.
10. **The PC's rollback:** the helper snapshots `app` (without the venv), `venv\Scripts\Nova.exe` and the two marks every time, and the whole venv only when `requirements-pc.txt` changed; it restores them and starts the old code if the new one doesn't confirm. The helper runs in a third task, "Nova update", which `install.ps1` registers (not from inside an update), so it outlives the two tasks it stops.
11. **The Pi's install runs detached** with `systemd-run --user --collect`, so stopping `voice-assistant` doesn't stop it. Local changes or a diverged clone are "needs you".
12. **The page button is on both of the page's addresses**, behind its login (the spec's call: the login covers it).

## Review Focus

1. The new satellite code starts but can't reach the brain within 180 s (the brain restarting, a network blip): it must be taken back, show "failed" with why, and be updated by the next press (Task 6 `test_no_confirmation_takes_the_code_back_and_restarts`; Task 8 `test_a_retry_offer_installs_a_version_that_failed_before`).
2. The connection dropping part way through a transfer: nothing partial is installed, and the next connection starts afresh (Task 5 `test_a_new_offer_drops_the_one_before`; Task 8 `test_a_transfer_cut_off_installs_nothing`).
3. A second start (page then voice, or two presses) while one runs: one update only (Task 10 `test_a_second_start_while_updating_doesnt_start_another`).
4. A satellite connecting while the brain's clone has moved but the brain hasn't restarted yet: it must not be offered the half-state (Task 4 `test_no_offer_while_the_brains_code_has_moved_under_it`).
5. A satellite back hours later must not suddenly play music kept for it (Task 11 `test_music_kept_longer_than_ten_minutes_isnt_resumed`).

---

### Task 0: Baseline

**Files:** none.

- [ ] **Step 1:** In `/home/nathaniel/work/Home-Assistant-update` (branch `update-everywhere`), `git status` is clean apart from this plan's commit.
- [ ] **Step 2:** RUN (whole suite, no args). Expected: all pass. Note the count in the ledger; later tasks compare against it.

---

### Task 1: The hello says the commit, and hands back the nonces

**Files:**
- Create: `voice_assistant/code_version.py`
- Modify: `voice_assistant/protocol.py` (module docstring's message list, constants, `greet`, `admit`), `voice_assistant/satellite_link.py` (`SatelliteLink.__init__`, `_connect`, new `nonces` property)
- Test: `tests/test_protocol.py`, `tests/test_code_version.py` (new), `tests/test_satellite_link.py`

**Interfaces:**
- Produces:
  - `protocol.COMMIT = re.compile(r"[0-9a-f]{40}")`, `protocol.UPDATE_FORMATS = ("zip", "bundle")`, `UPDATE_CHUNK`, `UPDATE_MOST`, `MOST_UPDATE_ERROR` (values in Global Constraints).
  - `protocol.greet(..., conversations=False, version: Optional[str] = None, update_format: Optional[str] = None) -> Tuple[str, str]`: returns `(satellite_nonce, brain_nonce)`. The hello carries `version`, `updates: true` and `update_format` only when both are given; otherwise the hello is exactly as today.
  - `protocol.admit(...)` returns today's dict plus `"version"` (str, or None unless it fullmatches `COMMIT`), `"updates"` (bool, via `_flag`; forced False when version or format is None), `"update_format"` (one of `UPDATE_FORMATS` or None) and `"nonces"` (`(satellite_nonce, brain_nonce)`). Bad values are logged and taken as absent, never refused.
  - `code_version.running_version(project: str = paths.PROJECT_DIRECTORY) -> Optional[str]`: the stripped contents of `project/COMMIT` if it fullmatches `COMMIT`, else `git -C project rev-parse HEAD` (timeout 10 s), else None.
  - `code_version.update_format(project: str = paths.PROJECT_DIRECTORY) -> Optional[str]`: "bundle" if `project/.git` is a folder; "zip" if `sys.platform == "win32"` and `project/COMMIT` exists; else None.
  - `code_version.commit_date(project: str, version: Optional[str]) -> Optional[str]`: `git log -1 --format=%cs <version>` ("2026-11-26"), None if unknown.
  - `code_version.short(version: Optional[str]) -> str`: the first 7 characters, or "unknown".
  - `SatelliteLink(..., conversations=False, version=None, update_format=None)`, and `SatelliteLink.nonces -> Optional[Tuple[str, str]]`: this connection's, set in `_connect` before `connected` is True, None while not connected.

- [ ] **Step 1: Write the failing tests:**
  - `test_hello_says_the_version_and_updates_only_with_both`: `_hello_sent(version="a"*40, update_format="bundle")` has `version == "a"*40`, `updates is True`, `update_format == "bundle"`; `_hello_sent(version="a"*40)` and `_hello_sent()` have none of the three keys.
  - `test_admit_reads_the_version_and_format`; `test_admit_takes_a_nonsense_version_or_format_as_none_not_refused` (parametrised: `"A"*40`, `"a"*39`, `123`, format `"tar"`; each admits, with `version` None or `updates` False).
  - `test_both_sides_get_the_same_nonces`: through `_handshake`, the satellite's return value equals `outcome["brain"]["nonces"]`, and the two nonces differ.
  - Update `test_the_handshake_admits_a_satellite_which_knows_its_password` for the new keys (`version: None, updates: False, update_format: None`, `nonces` a pair) and greet's return.
  - `test_the_update_messages_are_frozen`: a literal table of every frozen message and field (hello's three; `update_offer {id, version, format, size, chunks, proof, retry}`; `update_chunk {id, index}` + payload; `update_end {id}`; `update_installing {version}`; `update_result {version, ok, error?, needs_you?}`) asserted against `protocol.FROZEN_UPDATE_MESSAGES`, a dict the module defines from the same table, with a comment that changing it breaks every satellite in the house.
  - `tests/test_code_version.py`: a COMMIT file wins over git; a tmp git repo gives its HEAD; neither gives None; a COMMIT file holding junk falls back to git; `update_format` is "bundle" with `.git`, None on Linux without it; `short(None) == "unknown"`.
  - `test_satellite_link.py`: `test_the_link_says_its_version_and_keeps_the_nonces` with the file's existing fake brain.
- [ ] **Step 2:** RUN `tests/test_protocol.py tests/test_code_version.py tests/test_satellite_link.py`. Expected: the new tests fail.
- [ ] **Step 3:** Implement. Add the new hello fields and the update messages to `protocol.py`'s docstring under a heading saying they are frozen, and why (any old updating satellite can always be updated by any newer brain).
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Say each satellite's commit in its hello, and keep the handshake's nonces".

---

### Task 2: The archive's proof, and making and checking archives

**Files:**
- Create: `voice_assistant/update_archive.py`
- Modify: `voice_assistant/protocol.py` (`update_proof`)
- Test: `tests/test_protocol.py`, `tests/test_update_archive.py` (new)

**Interfaces:**
- Consumes: `protocol.COMMIT`, `UPDATE_FORMATS` (Task 1).
- Produces:
  - `protocol.update_proof(password: str, satellite_nonce: str, brain_nonce: str, version: str, format: str, archive: bytes) -> str`: hex HMAC-SHA256 keyed with `password.encode()` over `f"update|{satellite_nonce}|{brain_nonce}|{version}|{format}|{len(archive)}|".encode()` followed by the archive (fed to the hmac in pieces, not concatenated).
  - `update_archive.ArchiveError(Exception)`.
  - `make_zip(project: str, version: str) -> bytes`: `git archive --format=zip -o <temp file> <version>` in `tempfile.gettempdir()` (the brain's PrivateTmp), read and removed.
  - `make_bundle(project: str, version: str, base: Optional[str]) -> bytes`: raises `ArchiveError` unless `git rev-parse HEAD` is `version` (a bundle needs a ref, so it is always `HEAD`); `git bundle create <temp> HEAD ^<base>` when `base` is a strict ancestor of HEAD (`merge-base --is-ancestor` and not equal), else `git bundle create <temp> HEAD`.
  - `zip_commit(archive: bytes) -> Optional[str]`: the zip's comment as text if it fullmatches `COMMIT`, else None (also None for anything that isn't a zip).
  - `unpack_zip(archive: bytes, into: str) -> None`: empties `into`, then extracts; raises `ArchiveError` before writing anything if any member name is absolute, has a drive letter, a backslash, or a `..` part.

- [ ] **Step 1: Write the failing tests** (`tests/test_protocol.py`):
  - `test_an_update_proof_checks_with_the_same_everything`.
  - `test_a_tampered_archive_doesnt_prove`: one byte changed anywhere (first, middle, last), one byte added, one removed: each proof differs.
  - `test_another_commit_doesnt_prove`; `test_another_format_doesnt_prove`.
  - `test_a_proof_from_another_connection_doesnt_prove`: the same archive and commit with the nonces of another `_handshake` run, and with the two nonces swapped: each differs.
  - `test_an_update_proof_is_never_a_handshake_proof`: for the same password and nonces, `update_proof(...)` differs from `proof(password, "brain", a, b)` and `proof(..., "satellite", ...)`.
  - `test_another_password_doesnt_prove`.
- [ ] **Step 2: Write the failing tests** (`tests/test_update_archive.py`, each with a tmp git repo of two or three commits made with `git -c user.name=t -c user.email=t@t`):
  - `test_a_zip_carries_its_commit` (`zip_commit(make_zip(repo, c)) == c`), `test_a_zip_of_an_older_commit_is_that_commit`.
  - `test_a_thin_bundle_from_an_ancestor_fetches_into_a_clone_at_that_ancestor`: clone at commit 1, bundle of HEAD (commit 3) with base 1 is smaller than a full one, `git bundle verify` passes in the clone, and `git fetch <bundle> HEAD` gives `FETCH_HEAD` = commit 3.
  - `test_a_base_the_brain_doesnt_have_gives_a_full_bundle`; `test_a_base_ahead_of_the_brain_gives_a_full_bundle`.
  - `test_a_bundle_is_refused_while_head_isnt_the_version`.
  - `test_unpack_refuses_unsafe_names_and_writes_nothing` (parametrised: `/etc/x`, `../x`, `a/../../x`, `C:/x`, `a\\x`), built with `zipfile`; `into` is still empty after.
  - `test_unpack_replaces_what_was_there`; `test_not_a_zip_has_no_commit`.
- [ ] **Step 3:** RUN `tests/test_protocol.py tests/test_update_archive.py`. Expected: the new tests fail.
- [ ] **Step 4:** Implement both.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Prove an update came from the brain, bound to the connection it came on".

---

### Task 3: The brain's link sends an update, and hears how it went

**Files:**
- Modify: `voice_assistant/remote.py` (`RemoteSatellite.__init__`, handlers, `send_update`)
- Test: `tests/test_remote.py`

**Interfaces:**
- Consumes: Task 1's admit keys; `protocol.UPDATE_CHUNK`, `COMMIT`, `MOST_UPDATE_ERROR`.
- Produces:
  - `RemoteSatellite.version: Optional[str]`, `.updates: bool`, `.update_format: Optional[str]`, `.nonces: Optional[Tuple[str, str]]` from the hello (None/False when absent).
  - `RemoteSatellite.on_update_result: Optional[Callable[[str, Dict], None]]`, called with (name, `{"version", "ok", "error"?, "needs_you"?}`) for an `update_result` whose `version` fullmatches `COMMIT` and `ok` is a bool; `error` kept only if a string, cut to `MOST_UPDATE_ERROR`; `needs_you` kept only as a list of at most 3 `{"why": str, "command": str}`, each cut to 200 characters. Anything else is logged and dropped.
  - `RemoteSatellite.on_update_installing: Optional[Callable[[str, str], None]]`, called with (name, version) for a valid `update_installing`.
  - `RemoteSatellite.send_update(version: str, format: str, archive: bytes, proof: str, retry: bool) -> bool`: id `f"update-{next(_update_ids)}"` (a module counter like `_note_ids`); sends `update_offer` (with `size = len(archive)`, `chunks` = ceil(size / `UPDATE_CHUNK`)), then each `update_chunk {id, index}` with at most `UPDATE_CHUNK` bytes, then `update_end {id}`. False as soon as any send fails.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_update_goes_in_chunks_inside_a_payload`: a 600 KiB archive arrives as offer, three chunks (indexes 0, 1, 2, none over `UPDATE_CHUNK`), end; the chunks joined are the archive.
  - `test_a_lost_satellite_stops_the_update_part_way` (returns False, nothing after the failure).
  - `test_an_update_result_reaches_the_brain`, `test_a_nonsense_update_result_is_dropped` (parametrised: bad version, `ok` not a bool, `needs_you` not a list), `test_a_long_error_is_cut`.
  - `test_installing_reaches_the_brain`.
  - `test_the_hello_s_version_and_nonces_are_kept`.
- [ ] **Step 2:** RUN `tests/test_remote.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Send a satellite an update in chunks, and hear how it went".

---

### Task 4: The brain offers its commit on every connection

**Files:**
- Create: `voice_assistant/updates.py`
- Modify: `voice_assistant/brain.py` (`__init__`, `_serve_satellite`, new `offer_all`)
- Test: `tests/test_updates.py` (new), `tests/test_brain.py`

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces `class Updates` in `updates.py`:
  - `Updates(project: str, version: Optional[str], password_for: Callable[[str], Optional[str]], folder: str)`, `folder` = `os.path.join(paths.DATA_DIR, "update")` from the brain. `version` is what the brain runs, read once at start with `code_version.running_version()`.
  - `paused() -> bool`: True while `running_version(project)` (read now) isn't `version` - the updater has moved the clone and the brain hasn't restarted.
  - `seen(satellite: RemoteSatellite) -> None`: notes its version, updates, format and that it is connected, in `folder/satellites.json` (`storage.save_json`), so the page lists it when it's away.
  - `gone(name: str) -> None`.
  - `offer(satellite: RemoteSatellite, retry: bool = False) -> bool`: False (nothing sent) unless the satellite `updates`, its format is one of `UPDATE_FORMATS`, its version isn't the brain's, the brain has a version, `paused()` is False and its password is known; else makes the archive (a zip cached once; a bundle per base, the last four kept), proves it with `protocol.update_proof` and the satellite's `nonces`, marks it "updating" and `send_update`s it. Logs "Offered the {name} satellite {short}" and never the proof.
  - `result(name: str, message: Dict) -> None`: state "current" when ok and the version is the brain's; "needs_you" when ok with `needs_you`; "failed" with the error otherwise. Logs each outcome (a failure as a warning, with why).
  - `installing(name: str, version: str) -> None`: state "updating".
  - `behind() -> List[str]`: connected satellites that `updates` and differ from the brain.
  - `versions(known: Iterable[str]) -> Dict`: `{"brain": {"version", "short", "date"}, "satellites": [{"name", "version", "short", "date", "connected", "state", "reason", "command"}]}`, state one of "current", "behind", "updating", "failed", "needs_you", "old" ("old" for one whose hello had no `updates`, with reason "Update by hand once"), in `known`'s order.
- `Brain.updates: Updates`; `Brain.offer_all(retry: bool = True) -> List[str]`: offers each connected session's satellite, returning the names offered.
- In `_serve_satellite`: set `satellite.on_update_result = self.updates.result` and `on_update_installing` before the reading thread starts; once `_catch_up` returns True, `self.updates.seen(satellite)` and start `self.updates.offer` on a daemon thread named `update-{name}`; `self.updates.gone(name)` in `_forget`.

- [ ] **Step 1: Write the failing tests** (`tests/test_updates.py`, a tmp git repo as `project` and fake satellites with `send_update` recorded):
  - `test_a_satellite_on_another_commit_is_offered_the_brains`; the proof sent equals `update_proof(password, *nonces, brain_version, "zip", archive)`.
  - `test_a_satellite_on_the_brains_commit_isnt_offered`; `test_an_old_satellite_isnt_offered_and_shows_as_old`.
  - `test_no_offer_while_the_brains_code_has_moved_under_it` (commit in the repo after `Updates` was made).
  - `test_a_bundle_is_thin_from_the_satellites_commit`.
  - `test_results_show_as_states` (current, needs you with its command, failed with its reason); `test_a_satellite_away_is_still_listed`.
- [ ] **Step 2: Write the failing test** (`tests/test_brain.py`): `test_a_satellite_behind_is_sent_the_brains_commit_as_it_connects` - with `Updates` monkeypatched to a tmp repo, the test `Satellite` (extend its `__init__` with `version` and `update_format`, passed to `greet`, keeping greet's nonces) receives `update_offer`, chunks and `update_end`, and verifies the proof with its own nonces.
- [ ] **Step 3:** RUN `tests/test_updates.py tests/test_brain.py`. Expected: new tests fail.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Offer every satellite the brain's own commit as it connects".

---

### Task 5: A satellite receives and verifies an update, and keeps what happened

**Files:**
- Create: `voice_assistant/update_receive.py`, `voice_assistant/update_state.py`
- Test: `tests/test_update_receive.py` (new), `tests/test_update_state.py` (new)

**Interfaces:**
- Consumes: `protocol.update_proof`, constants (Tasks 1, 2).
- Produces:
  - `update_receive.UpdateRefused(Exception)` with `.reason: str`. Reasons (exact, as reported in `update_result.error`): `NOT_FROM_THE_BRAIN = "The update wasn't from the brain, so it wasn't installed."`, `TOO_BIG = "The update was bigger than a satellite takes."`, `CUT_SHORT = "The update arrived cut short or out of order."`, `WRONG_FORMAT = "The update wasn't in a form this satellite installs."`.
  - `Verified = NamedTuple("Verified", [("version", str), ("format", str), ("archive", bytes), ("retry", bool)])`.
  - `UpdateReceiver(password: str, update_format: str)`: `offer(message: Dict, nonces: Tuple[str, str]) -> None` (drops any transfer before; refuses a bad version, a format not its own, a size above `UPDATE_MOST` or a chunk count that doesn't fit `size`); `chunk(message: Dict, payload: bytes) -> None` (only the current id, indexes in order, never past `size`); `end(message: Dict) -> Verified` (all chunks there and the proof checks with `hmac.compare_digest`, else raises). The archive is kept in memory only; nothing is written. Logs "The update's proof checked out" or "The update's proof didn't check out", nothing more.
  - `update_state.UpdateState(folder: str)`, all files in `folder` via `storage`:
    - `failed_before(version: str) -> Optional[str]`: the error noted for that version in `failed.json`, else None.
    - `begin(version: str, previous: Optional[str]) -> None`: writes `pending.json` `{version, previous, at}`.
    - `confirm(running: Optional[str]) -> bool`: if `pending.json` names `running`, writes `confirmed` (the version, for the helper to see), writes `result.json` `{version, ok: True, needs_you}` (needs-you lines read from `notes`, each "why<TAB>command", at most 3), removes `pending.json` and `notes`, and returns True.
    - `note_result(result: Dict) -> None`: writes `result.json`; a failure is also kept in `failed.json` (the latest one only).
    - `result_to_send() -> Optional[Dict]`, `sent() -> None` (removes `result.json`).
    - `clear_pending() -> None`.

- [ ] **Step 1: Write the failing tests** (`tests/test_update_receive.py`, offers built with `update_proof` and fixed nonces):
  - `test_a_whole_update_from_the_brain_verifies`.
  - `test_a_tampered_archive_is_refused_as_not_from_the_brain` (one chunk's byte flipped).
  - `test_another_commit_is_refused_as_not_from_the_brain` (the offer's version changed after proving).
  - `test_a_replay_on_another_connection_is_refused`: a whole offer proved with connection A's nonces, received with B's: `NOT_FROM_THE_BRAIN`.
  - `test_too_big_is_refused_before_any_chunk`; `test_chunks_out_of_order_or_too_many_are_refused`; `test_an_end_before_every_chunk_is_refused`; `test_another_format_is_refused`.
  - `test_a_new_offer_drops_the_one_before`.
  - `test_the_proof_is_never_logged` (caplog holds no proof, password or nonce).
- [ ] **Step 2: Write the failing tests** (`tests/test_update_state.py`): `test_confirm_only_for_the_pending_version`, `test_confirm_carries_the_needs_you_notes`, `test_a_failure_is_remembered_for_its_version`, `test_a_result_stays_until_sent`.
- [ ] **Step 3:** RUN `tests/test_update_receive.py tests/test_update_state.py`. Expected: fail (modules missing).
- [ ] **Step 4:** Implement both.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Take an update only once it proves it came from the brain on this connection".

---

### Task 6: The Pi's installer, with its take-back

**Files:**
- Create: `voice_assistant/update_install.py` (bundle part), `scripts/update_satellite.sh`
- Modify: `setup.sh` (`needs_you`), `update.sh` (the asound.conf warning through `needs_you`)
- Test: `tests/test_update_install.py` (new), `tests/test_update_scripts.py` (new)

**Interfaces:**
- Produces:
  - `update_install.InstallRefused(Exception)` with `.reason: str` and `.needs_you: Optional[Dict]` (`{"why", "command"}`).
  - `install_bundle(project: str, folder: str, version: str, archive: bytes, run: Callable = subprocess.run) -> None`: writes `folder/nova.bundle`; `git -C project bundle list-heads <bundle>` must list `version` for `HEAD`, else `InstallRefused("The update's code isn't the commit it said.")`; copies the running code's `scripts/update_satellite.sh` to `folder/update_satellite.sh`; starts `systemd-run --user --collect --quiet --unit nova-update-<first 12 of version> /bin/bash <folder>/update_satellite.sh <project> <version> <bundle> <folder>`; a non-zero `systemd-run` is `InstallRefused`.
  - `setup.sh needs_you "<why>" "<command>"`: prints it as `problem`, and appends `why<TAB>command` to `$NOVA_UPDATE_NOTES` when that is set.
  - `scripts/update_satellite.sh <project> <version> <bundle> <folder>` (bash, `set -uo pipefail`), with `CONFIRM_WAIT=${NOVA_CONFIRM_WAIT:-180}`:
    1. Local changes (`git status --porcelain --untracked-files=no` not empty): result needs you, why "The Pi's code has local changes, so it wasn't updated.", command `cd <project> && git status`.
    2. `git fetch -q <bundle> HEAD`; `FETCH_HEAD` must be `version`.
    3. `previous=$(git rev-parse HEAD)`. Version a descendant: `git merge -q --ff-only <version>`; an ancestor (the Pi is ahead): `git reset -q --keep <version>`; otherwise needs you, why "The Pi's code has gone its own way, so it wasn't updated.", command `cd <project> && git log --oneline -3`.
    4. `NOVA_UPDATED_FROM=$previous NOVA_UPDATE_NOTES=<folder>/notes bash <project>/update.sh --no-pull`. On a non-zero exit with HEAD still `version` (it failed after stopping the service), take back as in 6.
    5. Wait up to `CONFIRM_WAIT` for `<folder>/confirmed` to hold `version`.
    6. Not confirmed: `git reset -q --keep $previous`, `systemctl --user restart voice-assistant`, result failed "The new code didn't reconnect to the brain within 3 minutes, so it was taken back.".
    Results are written to `<folder>/result.json` by `python3 -I -c` with the text as arguments (never interpolated into code); a failure's error ends with the last line of `update.sh`'s output, cut to 200 characters. `bundle` is removed at the end whatever happened.

- [ ] **Step 1: Write the failing tests** (`tests/test_update_install.py`, tmp repos): `test_a_bundle_of_the_right_commit_starts_the_detached_install` (the `run` stand-in sees `systemd-run --user --collect` and the copied script path), `test_a_bundle_of_another_commit_is_refused_and_starts_nothing`.
- [ ] **Step 2: Write the failing tests** (`tests/test_update_scripts.py`, the script run with `bash` against a tmp clone whose committed `update.sh` is a stub, a stand-in `systemctl` on PATH logging its calls, `NOVA_CONFIRM_WAIT=2`, and a stub that writes `confirmed` or doesn't):
  - `test_a_newer_commit_is_fast_forwarded_installed_and_confirmed` (no result written, HEAD at version, `update.sh` saw `--no-pull` and `NOVA_UPDATED_FROM`).
  - `test_a_pi_ahead_of_the_brain_is_moved_back_to_it`.
  - `test_local_changes_need_you_and_change_nothing`; `test_a_clone_gone_its_own_way_needs_you`.
  - `test_a_bundle_of_another_commit_changes_nothing`.
  - `test_a_failed_update_sh_is_reported_with_its_last_line`.
  - `test_no_confirmation_takes_the_code_back_and_restarts` (HEAD back at previous, `systemctl --user restart voice-assistant` logged, result failed with the exact text).
  - `test_needs_you_goes_to_the_notes_file` (sourcing `setup.sh`, as `run_setup` does).
- [ ] **Step 3:** RUN `tests/test_update_install.py tests/test_update_scripts.py`. Expected: fail.
- [ ] **Step 4:** Implement. In `update.sh`, the asound.conf `problem` becomes `needs_you` with the command `bash <project>/setup.sh` (its sentence unchanged).
- [ ] **Step 5:** RUN the same, then RUN `tests/test_config.py tests/test_setup_script.py` (update.sh's own tests). Expected: pass. Commit: "Install an update on the Pi from its own clone, and take it back if it doesn't reconnect".
- Hand check (Task 15's list): `systemd-run --user` outlives `voice-assistant` stopping, on the real Pi.

---

### Task 7: The PC's installer, its helper and its rollback

**Files:**
- Create: `scripts/windows/update_pc.ps1`, `scripts/windows/stop_nova.ps1`
- Modify: `voice_assistant/update_install.py` (zip part), `scripts/windows/install.ps1` (`-FromUpdate`, the "Nova update" task), `scripts/deploy_pc.sh` (stops Nova through `stop_nova.ps1`)
- Test: `tests/test_update_install.py`, `tests/test_pc_scripts.py`

**Interfaces:**
- Consumes: `update_archive.zip_commit`, `unpack_zip` (Task 2); `InstallRefused` (Task 6).
- Produces:
  - `install_zip(project: str, folder: str, version: str, previous: Optional[str], archive: bytes, run: Callable = subprocess.run) -> None`: `zip_commit(archive) == version` else `InstallRefused("The update's code isn't the commit it said.")`; `unpack_zip` into `folder/new` (its `ArchiveError` becomes `InstallRefused`); copies the running code's `scripts/windows/update_pc.ps1` and `stop_nova.ps1` into `folder`; writes `folder/job.json` `{version, previous, source}`; runs `schtasks /Run /TN "Nova update"` (with `CREATE_NO_WINDOW` on Windows). A failure is `InstallRefused` with needs you: why "The PC has no update task yet, so it needs one deploy by hand.", command `bash scripts/deploy_pc.sh` (run on the machine that deploys).
  - `stop_nova.ps1`: defines `Find-Running` (today's, from `deploy_pc.sh`) and `Stop-Nova` (stops both tasks, then the processes, up to 20 tries, throwing if Nova is still running).
  - `update_pc.ps1` (plain ASCII, PowerShell 5.1, no params; `$confirmWait = 180`), from the folder it sits in: `Start-Transcript` to `update_pc.log`; read `job.json`; dot-source `stop_nova.ps1`; `Stop-Nova`; snapshot (`robocopy app previous\app /MIR /XD <venv> venv`; `venv\Scripts\Nova.exe`, `requirements-pc.sha256` and `nova-exe.sha256` copied to `previous\venv-files`; the whole venv to `previous\venv` only when the new `requirements-pc.txt`'s hash isn't the mark); `& <source>\scripts\windows\install.ps1 -Source <source> -FromUpdate`; `app\COMMIT` = version; `Start-ScheduledTask 'Nova start'`; wait for `confirmed` holding the version. On an install that throws or no confirmation: `Stop-Nova`, restore all of the snapshot, `app\COMMIT` = previous, start 'Nova start', write `result.json` `{version, ok: false, error}` (error "The new code didn't reconnect to the brain within 3 minutes, so it was taken back." or "The install failed: <message>", cut to 200). Removes `new` at the end.
  - `install.ps1 -FromUpdate` (a `[switch]`): everything as today, but the "Nova update" task is registered only without it: no trigger, the same principal and settings as "Nova start", action `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "<data>\update\update_pc.ps1"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_update_install.py`): `test_a_zip_of_the_right_commit_is_unpacked_and_the_task_run`, `test_a_zip_of_another_commit_is_refused_and_runs_nothing`, `test_no_update_task_needs_a_deploy_by_hand`.
- [ ] **Step 2: Write the failing tests** (`tests/test_pc_scripts.py`, text only, with the file's `statements` helper): `test_install_registers_the_update_task_except_from_an_update`; `test_the_helper_snapshots_before_it_installs`; `test_the_helper_installs_from_update_and_writes_the_commit`; `test_the_helper_restores_and_restarts_without_confirmation`; `test_the_helper_snapshots_the_venv_only_when_the_packages_change`; `test_deploy_and_the_helper_stop_nova_the_same_way`; the new scripts are plain ASCII; `deploy_pc.sh`'s command still fits `CMD_LONGEST`.
- [ ] **Step 3:** RUN `tests/test_update_install.py tests/test_pc_scripts.py`. Expected: fail.
- [ ] **Step 4:** Implement. In `deploy_pc.sh`, expand the archive first and dot-source `$src\scripts\windows\stop_nova.ps1`, noting `$firstInstall` and `$wasRunning` before `Stop-Nova` as today.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Install an update on the PC from a task that outlives Nova, and roll it back if it doesn't reconnect".
- Hand checks (Task 15's list): the helper outlives both tasks; the window it shows; the rollback on the real PC.

---

### Task 8: The satellite takes an update when it's free

**Files:**
- Modify: `satellite.py` (start-up version, `SatelliteLink` arguments, handlers, `_busy`, `_install_when_free`, `_on_connected`, `_approve` counting)
- Test: `tests/test_satellite.py`

**Interfaces:**
- Consumes: Tasks 1, 5, 6, 7.
- Produces (all on `Satellite`):
  - `self.version = code_version.running_version()`, `self.update_format = code_version.update_format()`, passed to `SatelliteLink`; `self.update_state = UpdateState(os.path.join(paths.DATA_DIR, "update"))`.
  - Handlers `update_offer`, `update_chunk`, `update_end` on an `UpdateReceiver` made per offer with `self.link.nonces` (no receiver without an `update_format`). An `UpdateRefused` is answered with `update_result {version, ok: False, error: reason}`. A verified update whose version `failed_before` and isn't `retry` is answered with that error and not installed. Only one install at a time: an offer while one is under way is logged and ignored.
  - `_busy() -> Optional[str]`: "recording" while `_recording` is set, "speaking" while any `_playing` is of a kind other than "music", "ringing" while `ringer.ringing`, "conversation" while `_conversing`, "approval" while `_approving` (a counter `_approve` raises around `hello.ask`); else None.
  - `_install_when_free(verified: Verified)` on a daemon thread: looks every `UPDATE_LOOK = 2.0` s until `_busy()` is None; sends `update_installing {version}`; `update_state.begin(version, self.version)`; calls `install_bundle` or `install_zip` by format; an `InstallRefused` is noted (`note_result`), sent as `update_result` (with `needs_you` when it has one) and `clear_pending()`.
  - `_on_connected(True)`: `update_state.confirm(self.version)`, then sends `result_to_send()` as `update_result` and calls `sent()` only when the send returned True.

- [ ] **Step 1: Write the failing tests** (with the file's `FakeBrain` and `make`; installers monkeypatched to record):
  - `test_a_verified_update_installs_once_free` (busy "speaking" first: nothing installed until the reply ends; then `update_installing` is sent before the installer runs).
  - `test_music_doesnt_hold_an_update_back`.
  - `test_a_refused_update_is_reported_and_not_installed` (tampered: `update_result ok False` with `NOT_FROM_THE_BRAIN`).
  - `test_a_version_that_failed_before_is_answered_not_installed`; `test_a_retry_offer_installs_a_version_that_failed_before`.
  - `test_a_transfer_cut_off_installs_nothing` (the connection lost after one chunk, a new connection's offer completes: one install, of the second).
  - `test_the_new_code_confirms_and_reports_as_it_connects`; `test_a_result_unsent_is_sent_on_the_next_connection`.
  - `test_the_hello_says_the_satellites_commit`.
- [ ] **Step 2:** RUN `tests/test_satellite.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Take an update at the satellite once it's free, and say how it went".

---

### Task 9: The brain's updater, its unit and its start-failure take-back

**Files:**
- Create: `scripts/update_brain.sh`, `nova-update.service`, `nova-update.path`, `nova-takeback.service` (templates beside `nova-backend.service`)
- Modify: `update.sh` (`--no-restart`), `setup.sh` (`install_service` for the brain), `nova-backend.service` (`[Unit]` start limit and `OnFailure=`)
- Test: `tests/test_update_scripts.py`, `tests/test_setup_script.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `update.sh --no-restart`: everything up to and including the settings check and `steps_finished`, then `done_ "Ready, for Nova to restart into"` and exit 0, before "Restarting".
  - `scripts/update_brain.sh` (run by `nova-update.service` as the clone's owner, from the clone), status in `${NOVA_DATA:-$HOME/.local/share/nova}/update/brain.json` as `{state, from, to, error?, needs_you?: [{why, command}], at}` written with the venv's python `-I -c` and arguments. First removes `update/request`. States in order: "checking"; then one of "current" (FETCH_HEAD is HEAD), "needs_you" (not on `main`: why "The brain's code is on {branch}, not main."; local changes; diverged - each with its command, and the clone untouched), "failed" (update.sh's non-zero exit, with its last line; the code is back at `from`, checked and reset with `--keep` if not), or "ready" (fast-forwarded with `git merge --ff-only FETCH_HEAD`, `bash update.sh --no-pull --no-restart` passed with `NOVA_UPDATED_FROM`, `update/previous` written holding only `from`, and `needs_you` set per Builder's choice 4).
  - `nova-update.path`: `PathExists=__HOME__/.local/share/nova/update/request`, `Unit=nova-update.service`, `WantedBy=multi-user.target`.
  - `nova-update.service`: `Type=oneshot`, `User=__USER__`, `WorkingDirectory=__PROJECT_DIRECTORY__`, `ExecStart=/bin/bash __PROJECT_DIRECTORY__/scripts/update_brain.sh`, `UMask=0077`, `NoNewPrivileges=yes`, `TimeoutStartSec=1800`; no `ProtectSystem` or `ProtectHome` (it writes the clone and venv: that is its job), with a comment saying so.
  - `nova-takeback.service`: `Type=oneshot`; `ExecStart=-/usr/sbin/runuser -u __USER__ -- /bin/sh -c '<inline>'`, the inline script reading `update/previous`, refusing anything not 40 hex digits, `git -C __PROJECT_DIRECTORY__ reset -q --keep "$from"`, writing `update/taken_back` with the commit and removing `previous`; then `ExecStart=/usr/bin/systemctl reset-failed nova-backend.service` and `ExecStart=/usr/bin/systemctl start --no-block nova-backend.service`. Runs as root (the unit file is root's, the git step as `__USER__`).
  - `nova-backend.service` `[Unit]`: `StartLimitIntervalSec=600`, `StartLimitBurst=3`, `OnFailure=nova-takeback.service`, with a comment on why (Builder's choice 3).
  - `setup.sh install_service` (brain): fills and installs the three new templates beside the brain's, `daemon-reload`, `enable -q nova-backend`, `enable -q --now nova-update.path`, `mkdir -p` the `update` folder as the user.

- [ ] **Step 1: Write the failing tests** (`tests/test_update_scripts.py`, a bare repo as `origin`, a clone on `main` whose committed `update.sh` is a stub logging its arguments and environment, `NOVA_DATA` a tmp folder):
  - `test_the_brain_already_on_main_is_current` (and the request file is gone).
  - `test_a_newer_main_is_fast_forwarded_installed_and_ready` (`previous` holds the old commit; `update.sh` saw `--no-pull --no-restart` and `NOVA_UPDATED_FROM`).
  - `test_a_failed_install_is_reported_and_leaves_the_old_code`.
  - `test_local_changes_need_you_and_change_nothing`; `test_a_branch_other_than_main_needs_you`; `test_a_diverged_clone_needs_you`.
  - `test_a_changed_unit_file_is_ready_and_needs_you_for_root`; `test_a_changed_system_package_list_needs_you_for_root`.
- [ ] **Step 2: Write the failing tests** (unit text and setup): `test_the_updater_starts_only_on_its_request_file`; `test_the_updater_is_outside_the_brains_sandbox_but_never_root`; `test_the_brain_takes_back_after_three_failed_starts` (the three `[Unit]` lines); `test_the_take_back_only_takes_a_commit_id_and_starts_the_brain_again`; in `tests/test_setup_script.py`, `test_the_brains_install_puts_the_updater_in_place` (`run_setup` as root, role brain: the stand-in `systemctl` logs `enable -q --now nova-update.path`, and each template is written); in `tests/test_config.py`, `test_no_restart_stops_after_the_settings_check` (the block between `steps_finished` and `step "Restarting"` exits under `--no-restart`).
- [ ] **Step 3:** RUN `tests/test_update_scripts.py tests/test_setup_script.py tests/test_config.py`. Expected: new tests fail.
- [ ] **Step 4:** Implement. Reuse `update.sh` (`--no-pull --no-restart`) for the packages, models, settings check and take-back rather than copying its steps.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Give the brain an updater of its own, and take a brain that won't start back".
- Hand checks (Task 15's list): the path unit fires as `nova`; three failed starts trip `OnFailure=`; the take-back starts the brain again.

---

### Task 10: The brain's update, from start to "I'm updated"

**Files:**
- Create: `voice_assistant/brain_update.py`
- Modify: `voice_assistant/brain.py` (`busy_reason`, `restart_for_update`, `_say_update`, wiring in `__init__`, `run`, `_catch_up`), `voice_assistant/approvals.py` (`waiting`)
- Test: `tests/test_brain_update.py` (new), `tests/test_brain.py`, `tests/test_approvals.py`

**Interfaces:**
- Consumes: `Updates.behind`, `Brain.offer_all` (Task 4); `brain.json`, `previous`, `taken_back` (Task 9).
- Produces:
  - `Approvals.waiting() -> bool`: any request still open.
  - `Brain.busy_reason() -> Optional[str]`: "talking at {name}" for a connected session whose `activity` isn't "idle", "a conversation" while `open_conversation.satellite` is set, "something ringing" while `clock.is_ringing()`, "an approval" while `approvals.waiting()`; else None.
  - `Brain.restart_for_update() -> bool`: as `restart()` (exit 75 through `restarting` and `stop` after `RESTART_DELAY`), False when not under systemd (`INVOCATION_ID` unset).
  - `Brain._say_update(satellite: Optional[str], message: str) -> None`, modelled on `_say_download`; nothing said for None.
  - `class BrainUpdate(brain, version: Optional[str], folder: str, settings: Callable[[], Dict])` in `brain_update.py`, `brain.update`:
    - `start(asked_at: Optional[str]) -> str`: "I'm already updating." while one runs; else starts `_run` on a thread and returns "Updating. I'll be back in a minute.".
    - `running -> bool`.
    - `_run`: wait for `busy_reason()` to be None, looking every `IDLE_LOOK = 2.0` s, up to `idle_wait_minutes` (from `settings()`, default 10); else say the busy sentence and stop. Then write `request`, and read `brain.json` every 2 s for up to `UPDATER_LONGEST = 1800` s. "current": `offer_all()` and say "Already up to date; updating …" or "Already up to date."; "failed" or "needs_you": say the matching sentence, keep the outcome; "ready": wait for idle (no limit), `brain.keep_music_for_update()` (Task 11; a no-op until then), save `asked.json` `{satellite}`, `restart_for_update()`.
    - `after_start() -> None` (from `Brain.run` once "brain is ready" is logged): `taken_back` present: outcome failed, "The new code didn't start, so it was taken back.", the file removed; `previous` present with the running version not equal to it: outcome done (from, to, needs_you from `brain.json`), and `previous` removed after `STARTED_AFTER = 60` s of running.
    - `caught_up(name: str) -> None` (from `_catch_up`, once settled): says "I'm updated." (plus the needs-you sentence) at the satellite in `asked.json` when it is this one, once.
    - `outcome() -> Dict`: `{"state", "at", "from", "to", "reason", "command"}`, kept in `folder/outcome.json`.

- [ ] **Step 1: Write the failing tests** (`tests/test_brain_update.py`, a fake brain recording what's said, offered and restarted; `brain.json` written by the test as the updater would; the waits shortened by monkeypatching):
  - `test_nothing_is_asked_of_the_updater_while_busy`; `test_busy_too_long_gives_up_and_says_so`.
  - `test_already_current_updates_the_satellites_behind` ("Already up to date; updating the Pi."); `test_already_current_with_none_behind`.
  - `test_ready_restarts_into_the_new_code_once_idle` (music kept, `asked.json` saved, restart once).
  - `test_a_failed_update_says_so_and_doesnt_restart`; `test_needs_you_says_so`.
  - `test_a_second_start_while_updating_doesnt_start_another`.
  - `test_back_on_the_new_code_says_im_updated_where_he_asked` (only at that satellite, once).
  - `test_a_brain_taken_back_shows_it_failed`; `test_the_marker_stays_for_the_first_minute`.
- [ ] **Step 2: Write the failing tests:** `test_brain.py` `test_busy_reason_names_what_holds_an_update` (a session's activity, ringing); `test_approvals.py` `test_waiting_while_a_request_is_open`.
- [ ] **Step 3:** RUN `tests/test_brain_update.py tests/test_brain.py tests/test_approvals.py`. Expected: new tests fail.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** RUN the same. Expected: pass. Commit: "Update the brain when it's free, restart into the new code, and say so when it's back".

---

### Task 11: Music carries on after an update

**Files:**
- Create: `voice_assistant/music_resume.py`
- Modify: `voice_assistant/brain.py` (`keep_music_for_update`, resume in `_catch_up`, `on_update_installing` also keeps that satellite's music)
- Test: `tests/test_music_resume.py` (new), `tests/test_brain.py`

**Interfaces:**
- Produces:
  - `MusicResume(path: str)` (`update/music.json`): `keep(name: str, state: Dict) -> None`, `take(name: str) -> Optional[Dict]` (removes it; None when kept more than `RESUME_WITHIN = 600` s ago).
  - `Brain.keep_music_for_update(names: Optional[Iterable[str]] = None) -> None`: for each connected session (or those named) whose `music_player` has `hand_over` and `is_playing()`, `hand_over()` after `music.wait()`, kept. A failure is logged and skipped.
  - In `_catch_up`, once settled: a kept state for this satellite is `take_over`n by its session's player; a failure is logged.
  - `Updates.installing` callback path (Task 4's `on_update_installing`) becomes a brain method that keeps that satellite's music and then calls `updates.installing`.

- [ ] **Step 1: Write the failing tests:** `test_kept_music_comes_back_once`, `test_music_kept_longer_than_ten_minutes_isnt_resumed` (clock monkeypatched), `test_music_kept_survives_a_restart` (a new `MusicResume` on the same file); in `test_brain.py`, `test_music_playing_before_an_update_plays_on_where_it_was` (a stub player with `hand_over`/`take_over`; paused music isn't kept) and `test_a_satellite_installing_keeps_its_music_for_its_return`.
- [ ] **Step 2:** RUN `tests/test_music_resume.py tests/test_brain.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Carry the music on where it was after an update".

---

### Task 12: The settings page's Updates section

**Files:**
- Modify: `voice_assistant/settings_page.py` (`updates_state`, `start_update`, `ACTIONS`, `EDITABLE`, `state`), `voice_assistant/config.py` (limit), `config.yaml` (section `updates`), `voice_assistant/web/settings_list.js` (section, attention), `voice_assistant/web/settings.html` (the section), `voice_assistant/web/settings.js` (drawing it, the button), `voice_assistant/web/settings.css` if needed
- Test: `tests/test_settings_page.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Updates.versions` (Task 4), `BrainUpdate.start/outcome/running` (Task 10).
- Produces:
  - `SettingsPage.updates_state() -> Dict`: `{"versions": brain.updates.versions(brain.satellites()), "last": brain.update.outcome(), "running": brain.update.running}`, under `"updates"` in `state()`.
  - `SettingsPage.start_update(who: str) -> Dict`: logs "Settings page: {who} started an update"; `{"success": True, "message": brain.update.start(None)}`.
  - `ACTIONS["/api/update"]`.
  - `EDITABLE` gains `("updates", "idle_wait_minutes", "Minutes to wait for Nova to be free before updating", "now")`; `config.LIMITS[("updates", "idle_wait_minutes")] = Limit("number", 1, 60, True)`; `config.yaml` `updates: idle_wait_minutes: 10` with a comment in the file's manner.
  - `settings_list.js` `SECTIONS`: `{ id: "updates", name: "Updates", group: "nova", icon: "i-server", starts: ["updates."] }`; `attention` lists a machine "failed" or "needs_you" ("The {name} needs you for its update").
  - The section: the brain then each satellite by name, each with its short commit, date and state in words ("Up to date", "Behind", "Updating", "Failed: {reason}", "Needs you: {command}" with the command in a copyable code line, "Old: update by hand once"); the Update Nova button; "Last updated {time}".

- [ ] **Step 1: Write the failing tests:** `test_the_updates_section_lists_every_machine_and_its_state`; `test_update_nova_starts_one_and_says_who`; `test_update_nova_works_through_the_proxy_with_its_login` (the NPM route with a login cookie); `test_the_idle_wait_is_a_setting_used_straight_away`; `test_the_updates_section_is_in_the_list` (reading `settings_list.js` as the file's other section tests do); `test_config.py` `test_the_idle_wait_is_held_to_one_to_sixty`.
- [ ] **Step 2:** RUN `tests/test_settings_page.py tests/test_config.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Show every machine's version on the settings page, with an Update Nova button".

---

### Task 13: Preview the Updates section for his OK

**Files:** none in the repo. The preview tool (`~/work/Home-Assistant/.superpowers/tools/page-preview/`) is untracked tooling: change only `stub.js` (an `updates` state with the brain current, the Pi "Behind", the PC "Needs you" with a command, and an "old" satellite) and `shoot.sh`'s `SHOTLIST` (`phone-updates|400|860||updates`, `wide-updates|1300|900||updates`, `phone-updates-tall|400|1500||updates`).

- [ ] **Step 1:** `cat /proc/loadavg` (wait while above 3), then `NOVA_TREE=/home/nathaniel/work/Home-Assistant-update SHOTS_ONLY='updates' nice -n 19 timeout 600 bash ~/work/Home-Assistant/.superpowers/tools/page-preview/shoot.sh`. Expected: the three shots in `shots/`, and `shots/console.txt` empty of errors.
- [ ] **Step 2:** Look at each shot: phone width has no sideways scroll, the command line wraps or scrolls within itself, light and dark both read.
- [ ] **Step 3:** Stop and hand the shots to the controller to show him (memory note preview-before-saving). Changes he asks for go back into Task 12's files as a new commit, and Steps 1 to 3 again. Nothing goes further until he says it looks right.

---

### Task 14: "Nova, update yourself", with his approval

**Files:**
- Modify: `voice_assistant/intents.py` (rule, `_update_nova`), `voice_assistant/actions.py` (`updater` parameter, `update_nova` tool), `voice_assistant/session.py` (wiring)
- Test: `tests/test_intents.py`, `tests/test_actions.py`

**Interfaces:**
- Consumes: `BrainUpdate.start` (Task 10); `ActionExecutor._approved` (decision 0037).
- Produces:
  - Intent rule, whole utterance, before the Assist rules: `(?:update|upgrade) (?:yourself|nova|everything)|update all (?:of )?your (?:devices|satellites)` giving `Action("update_nova")` with response "Okay.".
  - `ActionExecutor(..., updater: Optional[Callable[[], Dict]] = None)`; `ActionExecutor.update_nova(self)` as a `@tool` (description: update Nova on every machine, the brain from GitHub then each satellite, for "update yourself" or "update Nova"; it asks his OK first; what is said is said for you, so reply "Okay."), `needs="updater"`, `not_in_routines="A routine can't update Nova: ask me yourself."`; body `self._approved("Update Nova on every machine", self.updater, ActionExecutor.update_nova)`.
  - In `session.py`: `updater=lambda: {"success": True, "message": brain.update.start(self.satellite.name)}`.

- [ ] **Step 1: Write the failing tests:** `test_update_yourself_is_nova_s_own_command` (each phrase, with capitals and a trailing full stop, gives `update_nova`; "update my shopping list" and "update me on the football" don't); `test_update_nova_asks_his_ok_first` (with a fake approvals: `gate` called with "Update Nova on every machine"; the updater runs only once approved); `test_update_nova_with_the_trust_window_open_starts_at_once`; `test_a_routine_cant_update_nova`; `test_without_an_updater_the_tool_isnt_offered`.
- [ ] **Step 2:** RUN `tests/test_intents.py tests/test_actions.py`. Expected: new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** RUN the same. Expected: pass. Commit: "Update Nova by voice, once he approves".

---

### Task 15: Decision 0040, the hand checks, and the branch ready

**Files:**
- Create: `docs/decisions/0040-update-once-everything-updates.md`
- Modify: `docs/release-checklist.md` (a section "Updating everything at once", checks 142 onwards; a line under "Putting a branch on Nova" that main is now updated with Update Nova, and a branch still by hand), `README.md` only where it tells how to update

- [ ] **Step 1:** Write the decision in 0037's form: date, asked for by him, design link, "Built; his named bootstrap and hand checks 142 to 152 to come"; context; what he decided (the spec's list); what was built (each task in a short paragraph); the Builder's choices, numbered as above, each with its why; the bootstrap as his named jobs.
- [ ] **Step 2:** Write the checks, marking those that make sound or restart things:
  - 142. After the bootstrap: the page's Updates section lists the brain, the Pi and the PC, all up to date on one commit. On the brain `systemctl status nova-update.path` is active and `systemctl show nova-backend -p OnFailure -p StartLimitBurst` says `nova-takeback.service` and 3.
  - 143. Update Nova with nothing new: "Already up to date." on the page, nothing restarts.
  - 144. Restarts the brain and both satellites; makes sound. A real update (a small commit on main): press Update Nova with music playing on the Pi; the brain is back within about a minute, then the Pi and the PC update themselves, the page shows all three on the new commit, and the Pi's music carries on from where it was.
  - 145. Makes sound. "Hey Nova, update yourself" at the Pi: "That needs your OK. Check your phone." (or the PC); approve; "Updating. I'll be back in a minute."; later "I'm updated." at the Pi.
  - 146. Makes sound. Busy: with a timer ringing, press Update Nova: nothing happens until it's dismissed. With the idle wait set to 1 and the timer left ringing: "I couldn't update, as I was busy for 1 minute. Ask again when I'm free." (put the setting back to 10).
  - 147. The PC asleep during an update: wake it, and within a minute of Nova reconnecting it updates itself; `data\update\update_pc.log` shows the snapshot, the install and the confirmation; note how the helper's window looked.
  - 148. The Pi's detached install: during 144, `journalctl --user -u 'nova-update-*'` on the Pi shows the install running on past `voice-assistant` stopping.
  - 149. Restarts the brain; needs his OK. Satellite rollback: right after the page shows the Pi and the PC "Updating", stop the brain for four minutes (`sudo systemctl stop nova-backend`), then start it: both took their code back, run the old commit, and show "Failed: The new code didn't reconnect ..."; press Update Nova again: both update.
  - 150. Restarts the brain; needs his OK. The brain's take-back: as root, write the current commit's parent into `/home/nova/.local/share/nova/update/previous` and run `systemctl start nova-takeback.service`: the brain comes back on the parent and the page says it was taken back; then Update Nova brings it forward again.
  - 151. A local change on the Pi (touch a tracked file): Update Nova shows the Pi "Needs you" with `git status`; undo the change and press again: it updates.
  - 152. The log: `journalctl -u nova-backend` shows each offer and outcome, the satellites' logs show the proof as checked out, and no proof, password or nonce appears anywhere.
- [ ] **Step 3:** RUN (whole suite). Expected: all pass, the count above Task 0's. Then RUN-UTC (whole suite). Expected: all pass. Then ruff. Expected: no findings.
- [ ] **Step 4:** Commit: "Decision 0040 and the hand checks for updating everything at once".
- [ ] **Step 5:** Hand back to the controller: the branch is ready, nothing pushed or deployed. The bootstrap, as his named jobs, in order: push `update-everywhere` merged into main; on the brain, root runs `bash /home/nova/Home-Assistant/update.sh` (installs the updater, its path unit, the take-back and the new start limit); on the Pi, `bash ~/Home-Assistant/update.sh`; on CT 109, `bash scripts/deploy_pc.sh` (registers the "Nova update" task). Then checks 142 onwards.
