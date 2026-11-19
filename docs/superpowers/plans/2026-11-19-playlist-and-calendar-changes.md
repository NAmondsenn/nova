# Playlist and Calendar Changes by Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova adds the playing song to named playlists, takes it off, makes playlists, likes and unlikes songs, removes, moves and renames calendar events (one, or from this one on), and "undo that" reverses the last such change.

**Architecture:** Navidrome writes go in `navidrome.py` beside the New Music ones; SoulSync's answers about remade and mirrored playlists in `soulsync.py`; the rules for a playlist change in a new `playlist_changes.py` held by the brain; calendar changes in `google_calendar.py` through one send-once write path; a one-slot `LastChange` in `undo.py` held by each satellite's `ActionExecutor`, kept and cleared in `ActionExecutor.execute`. Each change returns its reply dict with an `"undo"` callable.

**Tech Stack:** Python 3, requests, pytest (run on the Pi), Subsonic API, SoulSync REST API, Google Calendar API v3.

**Spec:** `docs/superpowers/specs/2026-11-19-playlist-add-and-calendar-remove-design.md` (read it first; every reply quoted there is exact copy).

## Global Constraints

- Worktree `/home/nathaniel/work/Home-Assistant-addremove`, branch `add-and-remove`. No `git stash`. One commit per task, message ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; no model names anywhere else.
- Test command, called `$T` below (runs on the Pi, never on CT 109): `cat /proc/loadavg` first (wait if the first number is above 3), then `NOVA_TREE=/home/nathaniel/work/Home-Assistant-addremove NOVA_CHECK=addremove nice -n 19 timeout 600 bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh <pytest args>`. One run at a time, no repeat runs for flakiness. Never `ps` on the Pi.
- Ruff after each task: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/addremove && /tmp/nova-check-pytest/bin/ruff check .'` (after a `$T` run has copied the tree).
- Code style as the files around it: full-sentence block comments above code saying why, with quoted examples ("add this to Gym"); British spelling; spaced " - " dashes; Google-style Args:/Returns:/Raises: docstrings; no comments on obvious lines; keep each file's line endings.
- The repo is public: tests use made-up songs, playlists and calendars, never his real ones; no addresses, keys or names of his.
- Every reply the spec quotes is used word for word. Song names in replies are the song's `title` as Navidrome has it; playlist names as Navidrome has them; several joined "A, B and C".
- Every write to Google is sent once (never again on an unclear answer) with `sendUpdates=none`. Nova only changes events on calendars it can write, never an invite.
- A reply that changed nothing ("Endless is already on Gym.", "Endless isn't on Gym.", "You've already liked Endless.", "Dentist was already gone.") has `"success": False`, so it leaves the last change undoable (Task 3's rule). An offer to make a playlist is a success, as the download offer is, since a yes is only listened for after one.
- Nothing new on the settings page; no new dependencies; no live machine touched by any task.

## Review Focus

1. **The song changes before the yes.** "Add this to Jim" offers to make Jim; the next song starts; "yes" must make Jim with the song that was playing when asked, not the new one. Test in Task 5.
2. **The closest playlist is a remade one.** "Add this to my daily mix" must be refused for Daily Mix 1, never fall through to a weaker match such as "Mix Tape". Test in Task 4.
3. **Repeating events kept by `COUNT`, and all-day ones, cut from this one on.** The cut rule must end the series just before this one in both cases, and the new series of a move must have only the times that were left. Tests in Tasks 6 and 7.
4. **"Undo that" after a failed change, after a read, and after a yes.** A failed change and a read leave the slot as it was; a change run on a yes is kept. Tests in Task 3 and Task 5.
5. **An event already gone in Google** (deleted from the phone meanwhile: 404 or 410) when removing or undoing: said plainly, nothing else sent. Test in Task 6.

---

### Task 1: Navidrome playlist writes, likes and the song playing

**Files:**
- Modify: `voice_assistant/navidrome.py`
- Test: `tests/test_navidrome.py`

**Interfaces:**
- Produces (module level): `same_song(a: Dict, b: Dict) -> bool`; `closest_playlist(playlists: List[Dict], name: str) -> Optional[Dict]`.
- Produces (`NavidromeLibrary`): `all_playlists() -> List[Dict]` (Navidrome's `getPlaylists` entries owned by `self.username`, as `playlist_with_id` checks ownership); `playlist_entries(playlist_id: str) -> List[Dict]` (`getPlaylist` entries in order); `add_songs(playlist_id: str, song_ids: List[str]) -> None`; `remove_entries(playlist_id: str, indexes: List[int]) -> None`; `delete_playlist(playlist_id: str) -> None`; `set_starred(song_id: str, starred: bool) -> None`; `is_starred(song_id: str) -> bool`. Existing `make_playlist(name) -> str` is reused. All raise as `_call` does (`requests.RequestException`, `NavidromeError`, `ValueError`).
- Produces (`NavidromePlayer`): `song_at(which: str) -> Optional[Dict]`, `which` "this" or "last".

- [ ] **Step 1: Write the failing tests**

```python
def test_the_same_song_is_its_id_or_its_title_and_artist():
    song = {"id": "1", "title": "Made Up", "artist": "Band"}
    assert same_song(song, {"id": "1", "title": "Other", "artist": "Other"})
    assert same_song(song, {"id": "2", "title": "Made Up (Clean)", "artist": "Band"})
    assert same_song(song, {"id": "3", "title": "made up", "artist": "band"})
    assert not same_song(song, {"id": "4", "title": "Made Up (Live)", "artist": "Band"})
    assert not same_song(song, {"id": "5", "title": "Made Up Love", "artist": "Band"})
    assert not same_song(song, {"id": "6", "title": "Made Up", "artist": "Someone Else"})

def test_the_closest_playlist_is_found_as_playing_one_finds_it():
    playlists = [{"id": "p1", "name": "Gym Bangers"}, {"id": "p2", "name": "Car Songs"}]
    assert closest_playlist(playlists, "gym")["id"] == "p1"
    assert closest_playlist(playlists, "car")["id"] == "p2"
    assert closest_playlist(playlists, "jim") is None

def test_only_the_users_own_playlists_are_changed(make_library):
    # getPlaylists answers p1 owned by "nova" and p2 owned by "someone"
    assert [p["id"] for p in library.all_playlists()] == ["p1"]

def test_songs_are_added_to_a_playlist_by_id(make_library):
    library.add_songs("p1", ["7"])
    assert ("updatePlaylist", {..., "playlistId": "p1", "songIdToAdd": ["7"]}) in requests_without_sign_in

def test_entries_are_removed_by_their_place_last_first(make_library):
    library.remove_entries("p1", [0, 3])
    # one updatePlaylist with songIndexToRemove == [3, 0]

def test_a_playlist_is_deleted_by_its_id(make_library): ...           # ("deletePlaylist", {"id": "p1"})
def test_a_song_is_liked_and_unliked(make_library): ...               # ("star", {"id": "7"}), ("unstar", {"id": "7"})
def test_a_liked_song_is_known_from_navidrome(make_library): ...      # getSong {"song": {"id": "7", "starred": "2026-..."}} -> True; no "starred" -> False

def test_the_song_playing_and_the_one_before_are_found(make_player):
    # play SONGS, skip once: song_at("this")["id"] == "2", song_at("last")["id"] == "1"
    # before skipping: song_at("last") is None; with nothing queued both are None
```

Strip the signing parameters (`u`, `t`, `s`, `v`, `c`, `f`) when comparing requests, as the file's existing tests do.

- [ ] **Step 2: Run them to see them fail**

Run: `$T tests/test_navidrome.py -k "same_song or closest_playlist or own_playlists or added_to_a_playlist or removed_by_their_place or deleted_by or liked or song_playing"`
Expected: FAIL (ImportError or AttributeError for each new name).

- [ ] **Step 3: Implement**

`same_song`: the same non-empty `id`, else the same `(_own_words(title), version, normalise(artist))` where `version` is `version_of(title)` with any of `CLEAN_VERSIONS` read as None, and `_own_words(title)` isn't empty. `closest_playlist`: the matching `_find_playlist` already does for a spoken name (`distinctive` then `best` on `"distinctive"`); have `_find_playlist` call it so the two can't drift. `add_songs` in chunks of `PLAYLIST_SONGS_AT_ONCE` with `updatePlaylist songIdToAdd`; `remove_entries` one `updatePlaylist` with `songIndexToRemove` sorted descending; `delete_playlist` is `deletePlaylist id`; `set_starred` is `star` or `unstar` with `id`; `is_starred` reads `getSong` and is truthy `song.starred`. `song_at` reads `_queue` and `_index` under `self._lock`.

- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_navidrome.py` whole. Expected: PASS.

- [ ] **Step 5: Commit** `git add voice_assistant/navidrome.py tests/test_navidrome.py && git commit -m "Let Navidrome add to, take off, make and delete playlists, and like songs"`

---

### Task 2: SoulSync's remade and mirrored playlists

**Files:**
- Modify: `voice_assistant/soulsync.py`
- Test: `tests/test_soulsync.py`

**Interfaces:**
- Produces (`SoulSync`): `remade_playlists() -> List[str]` (`GET /api/personalized/playlists`, answer `{"playlists": [{"name": ...}, ...]}`, the names); `mirrors() -> List[Dict]` (`GET /api/mirrored-playlists`, a JSON list; each item reduced to `{"id": int, "server_playlist_id": Optional[str], "sync_name": str, "sync_mode": Optional[str]}`); `set_append(mirror_id: int) -> None` (`PATCH /api/mirrored-playlists/{id}/preferences`, JSON `{"sync_mode": "append"}`). All raise `SoulSyncError` when SoulSync can't be reached, refuses the key (401/403, `KEY_REFUSED`) or answers another shape; all use `REQUEST_TIMEOUT`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_playlists_soulsync_remakes_are_named(soulsync, http): ...
    # pages[f"{URL}/api/personalized/playlists"] = FakeResponse(200, {"playlists": [{"name": "Daily Mix 1", "kind": "daily_mix"}, {"name": "Hidden Gems"}]})
    # -> ["Daily Mix 1", "Hidden Gems"], asked with the key as a bearer token

def test_the_mirrors_say_which_navidrome_playlist_each_writes(soulsync, http): ...
    # a list answer with extra keys -> [{"id": 3, "server_playlist_id": "p1", "sync_name": "Gym", "sync_mode": None}]

def test_a_mirror_is_switched_to_append_only(soulsync, http): ...
    # http.patched == [(f"{URL}/api/mirrored-playlists/3/preferences", {"sync_mode": "append"}, headers)]

def test_a_refused_key_or_an_odd_answer_is_an_error(soulsync, http): ...
    # 401 on each -> SoulSyncError with KEY_REFUSED; a dict where the mirrors' list should be -> SoulSyncError

def test_soulsync_out_of_reach_is_an_error_for_each(soulsync, http): ...
    # http.fail = requests.ConnectionError() -> SoulSyncError for all three
```

Add `patch(url, json=None, timeout=None)` to `FakeSoulSync`, recording `(url, json, headers)` in `self.patched` and answering from `self.patches.get(url, (200, {"success": True}))`.

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_soulsync.py -k "remakes or mirrors or append or odd_answer or out_of_reach_is"` - FAIL.

- [ ] **Step 3: Implement** with `_ask(self._session.get | .patch, ...)`. `_read` refuses a list, so `mirrors` checks its own answer: a 2xx list of dicts, else `SoulSyncError`.

- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_soulsync.py`. PASS.

- [ ] **Step 5: Commit** `"Ask SoulSync which playlists it remakes and mirrors, and switch a mirror to append only"`

---

### Task 3: The last change, and "undo that"

**Files:**
- Modify: `voice_assistant/undo.py`, `voice_assistant/actions.py`
- Test: `tests/test_last_change.py` (new), `tests/test_actions.py`

**Interfaces:**
- Produces (`undo.py`): `UNDO_CHANGE_KEPT = 600.0`; `NOTHING_TO_UNDO = "There's nothing to undo."`; `SOMETHING_ELSE_SINCE = "The last thing I did wasn't a playlist or calendar change, so I've left it."`; `TOO_LATE = "That was too long ago to undo."`; `class LastChange` with `__init__(keep_for: float = UNDO_CHANGE_KEPT, clock: Callable[[], float] = time.monotonic)`, `keep(undo: Callable[[], Dict]) -> None`, `note_other() -> None`, `take() -> Union[Callable[[], Dict], str]` (the undo, which empties the slot, or one of the three sentences). Thread-safe with a lock, in memory only.
- Produces (`ActionExecutor`): attribute `last_change: LastChange`; tool `undo_last_change()`. A reply dict whose `"success"` is true may carry `"undo": Callable[[], Dict]`; `ActionResult.from_reply` ignores it.
- Rule in `execute`, after the tool runs: tool `undo_last_change` touches nothing more; a success with a callable `"undo"` is kept; any other success of a tool that isn't `reads` calls `note_other()`; a failure leaves the slot as it was.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_last_change.py
def test_nothing_kept_says_theres_nothing_to_undo(): assert LastChange().take() == NOTHING_TO_UNDO
def test_the_change_kept_is_given_once(): ...           # keep(f); take() is f; take() == NOTHING_TO_UNDO
def test_a_change_kept_too_long_is_too_late(): ...      # clock moves 601 s -> TOO_LATE, and again NOTHING_TO_UNDO
def test_something_else_since_leaves_the_change_alone(): ...  # keep(f); note_other(); take() == SOMETHING_ELSE_SINCE
def test_a_new_change_replaces_the_last(): ...          # keep(f); keep(g); take() is g

# tests/test_actions.py
def test_undo_that_runs_the_last_changes_undo_and_says_what_it_says(): ...
    # a stand-in tool reply {"success": True, "message": "Done.", "undo": lambda: {"success": True, "message": "Undone."}}
    # executed, then Action("undo_last_change") -> ok, say "Undone."; again -> NOTHING_TO_UNDO
def test_another_action_since_means_undo_leaves_the_change(): ...     # a timer set between -> SOMETHING_ELSE_SINCE
def test_a_failed_action_or_a_read_since_leaves_the_change_undoable(): ...  # Review Focus 4
```

For the executor tests, register a stand-in change tool on a subclass of `ActionExecutor` with `@tool(...)`, as the file does elsewhere if it has such a pattern, else monkeypatch one change tool's target to return the dict above.

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_last_change.py tests/test_actions.py -k "undo or last_change"` - FAIL.
- [ ] **Step 3: Implement** `LastChange` (one slot: the callable, when it was kept, and whether something else ran since) and the rule in `execute`. `undo_last_change` is offered to the model (`needs=None`) with a description saying it reverses Nova's last playlist or calendar change - adding, removing, moving, renaming, a new playlist or a like - for "undo that", "put it back" or "take that back", and that what it did is said for it. Its reply is the undo's reply, or `{"success": False, "message": <sentence>}`.
- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_actions.py`. PASS.
- [ ] **Step 5: Commit** `"Keep the last playlist or calendar change for ten minutes, for undo that"`

---

### Task 4: The rules of a playlist change

**Files:**
- Create: `voice_assistant/playlist_changes.py`
- Test: `tests/test_playlist_changes.py`

**Interfaces:**
- Consumes: Task 1's `NavidromeLibrary` methods, `same_song`, `closest_playlist`; Task 2's `SoulSync` methods; `means_liked_songs` from `music.py`; `normalise` from `navidrome.py`.
- Produces: `class PlaylistChanges` with `__init__(soulsync: Callable[[], Optional[SoulSync]], new_music_id: Callable[[], Optional[str]])` and, each returning `{"success", "message"}` plus `"undo"` when something changed and `"ask_first"` when it offers to make a playlist:
  - `add(library, song: Dict, names: List[str]) -> Dict`
  - `remove(library, song: Dict, names: List[str]) -> Dict`
  - `make(library, name: str, song: Optional[Dict]) -> Dict`
  - `like(library, song: Dict, liked: bool) -> Dict`
  - `ask_first` is `Action("make_offered_playlist", {"name": name, "song": {"id", "title", "artist"} of the song})`, only when exactly one name had no playlist.

The order for each name, for add and remove (the spec's Goals 1-7 have every reply):
1. `means_liked_songs(name)`: like or unlike instead (`like`).
2. `closest_playlist(library.all_playlists(), name)`; none: missing (add offers to make it, remove says "You haven't got a playlist called Jim.").
3. The playlist found is remade: its id is `new_music_id()`, or its normalised name is one of `soulsync().remade_playlists()`: refused, never the next-closest one.
4. It's a mirror (`server_playlist_id` equal to its id, else, for a mirror without one, the same normalised `sync_name`): add switches a mirror whose `sync_mode` isn't "append" with `set_append`, and says so the first time; remove is refused.
5. Add: already on it (`same_song` with any entry) is said; else `add_songs`. Remove: every entry `same_song` with the song goes, by `remove_entries`; none: "Endless isn't on Gym."

`soulsync()` is called once per request: None (not set up) skips steps 3's SoulSync part and 4; a `SoulSyncError` from it refuses every name that needed the check with "I can't reach SoulSync to check Gym, so I haven't changed it." A Navidrome failure says `library._failure_message(e)`, keeping the undo of what was already done. Undo replies (exact): "Took Endless back off Gym and Car." (the last entry with the id added, per playlist), "Put Endless back on Gym." (the first removed entry's id, added at the end), "Deleted Chill.", "Unliked Endless.", "Liked Endless again." `make`: an empty name says "I didn't catch the playlist's name."; a name meaning liked songs says "Your liked songs aren't a playlist. Say 'like this' to add a song to them."; the same normalised name as one of his says "You've already got Chill." and adds the song to it if given; otherwise "Made Chill." or "Made Chill with Endless on it."

- [ ] **Step 1: Write the failing tests** with a fake library (a class holding playlists, entries and starred ids, recording calls) and a fake SoulSync. One test per rule, named for what he'd see:

```python
def test_the_song_goes_on_each_playlist_named_and_the_reply_names_them(): ...  # "Added Song 1 to Gym Bangers and Car."
def test_a_song_already_on_a_playlist_isnt_added_again(): ...                   # same title, other id counts
def test_a_name_with_no_playlist_offers_to_make_it_with_the_song(): ...         # ask_first carries the song
def test_two_names_missing_offer_nothing(): ...
def test_the_closest_playlist_being_remade_is_refused_not_the_next(): ...       # Review Focus 2
def test_new_music_is_refused_by_its_id(): ...
def test_a_mirror_is_switched_to_append_only_once_and_says_so(): ...
def test_taking_a_song_off_a_mirror_is_refused(): ...
def test_soulsync_out_of_reach_changes_nothing_that_needed_it(): ...
def test_without_soulsync_set_up_navidrome_alone_is_changed(): ...
def test_every_copy_of_the_song_is_taken_off(): ...
def test_liked_songs_named_as_a_playlist_like_the_song(): ...
def test_a_song_already_liked_says_so(): ...
def test_making_a_playlist_that_exists_adds_to_it(): ...
def test_each_change_is_undone_as_the_spec_says(): ...                          # add, remove, make, like
def test_navidrome_failing_half_way_keeps_the_undo_of_what_was_done(): ...
```

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_playlist_changes.py` - FAIL (module missing).
- [ ] **Step 3: Implement `PlaylistChanges`** as above. Module docstring: what it does and why the SoulSync checks exist (a mirror's sync puts back or takes off what Navidrome alone changed; decision 0030).
- [ ] **Step 4: Run them to see them pass** - PASS.
- [ ] **Step 5: Commit** `"Decide each playlist change: remade and mirrored playlists, likes, new playlists and their undo"`

---

### Task 5: Playlist tools for Claude, wired up

**Files:**
- Modify: `voice_assistant/actions.py`, `voice_assistant/brain.py`, `voice_assistant/session.py`, `voice_assistant/intents.py`
- Test: `tests/test_actions.py`, `tests/test_intents.py`, `tests/test_conversation.py`

**Interfaces:**
- Consumes: Task 4's `PlaylistChanges`; Task 1's `song_at`; Task 3's undo rule.
- Produces (`ActionExecutor.__init__`): keyword `playlist_changes=None`. Tools, all `needs="music"`:
  - `add_to_playlists(playlists: Optional[List[str]] = None, song: Optional[str] = None)` - `playlists` an array of names as said; `song` enum `["this", "last"]`, default "this".
  - `take_off_playlists(playlists=None, song=None)`
  - `make_playlist(name: Optional[str] = None, with_song: Optional[bool] = None, song: Optional[str] = None)`
  - `like_song(like: Optional[bool] = None, song: Optional[str] = None)` (like defaults to True)
  - `make_offered_playlist(name: Optional[str] = None, song: Optional[Dict] = None)`, `for_model=False`, run only on a yes.
- Before each: `_music_offline()`; the player is `self._playing_in_room()[0]`; not Navidrome: "Changing playlists only works with Navidrome."; `song_at` None: "Nothing's playing." or "There isn't a last song."
- `brain.py` makes `self.playlist_changes = PlaylistChanges(lambda: SoulSync.from_config(self.config, os.getenv("SOULSYNC_API_KEY")), lambda: self.new_music_playlist.playlist_id((self.config.section("new_music").get("playlist") or "").strip()))`; `session.py` passes it.
- `intents.py`: an item that only points at a song - "the last song", "the song before", "the previous track", "this song" - is never a to-do list's (`_listed` returns None, so Claude has it).

Tool descriptions tell Claude: pass each playlist name roughly as said; what was done is said for it, so reply "Okay."; "liked songs" is a name it may pass; never make a playlist unless the user says new or make.

- [ ] **Step 1: Write the failing tests**

```python
# test_actions.py, with a fake player whose name is "Navidrome" and a fake PlaylistChanges recording calls
def test_add_this_to_playlists_gives_the_song_playing_and_the_names(): ...
def test_the_last_song_is_the_one_before(): ...
def test_nothing_playing_or_no_last_song_says_so(): ...
def test_playlists_on_spotify_say_they_need_navidrome(): ...
def test_the_offered_playlist_is_made_with_the_song_asked_about_not_the_one_playing_now(): ...  # Review Focus 1
def test_the_offer_is_never_offered_to_claude(): ...      # make_offered_playlist absent from tool_schemas()

# test_conversation.py
def test_a_yes_to_making_the_playlist_is_kept_for_undo(): ...  # Review Focus 4: ask_first run, then "undo that" deletes it

# test_intents.py
def test_a_song_pointed_at_is_never_put_on_a_to_do_list(): ...
    # "add the last song to gym", "take the song before off car", "add this song to gym" -> None (Claude's)
    # "add milk to my shopping list" is still the list's
```

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_actions.py tests/test_intents.py tests/test_conversation.py -k "playlist or song_pointed or offered or last_song or nothing_playing or yes_to_making"` - FAIL.
- [ ] **Step 3: Implement** the tools, the wiring and the intents rule.
- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_actions.py tests/test_intents.py tests/test_conversation.py tests/test_brain.py tests/test_session.py`. PASS.
- [ ] **Step 5: Commit** `"Let Claude add the song playing to playlists, take it off, make playlists and like songs"`

---

### Task 6: Removing calendar events, and one way to write to Google

**Files:**
- Modify: `voice_assistant/google_calendar.py`
- Test: `tests/test_google_calendar.py`

**Interfaces:**
- Produces:
  - `GoogleCalendar._send_once(method: str, url: str, body: Optional[Dict] = None) -> Optional[Dict]` - the only write to Google, `self._session.request(method, url, params={"sendUpdates": "none"}, json=body, headers=..., timeout=REQUEST_TIMEOUT)`, with `_insert`'s retry-only-on-401, `_NotSure`, `SignInNeeded` and network rules; returns Google's JSON, None for a 204. `_insert` keeps its job through it and returns the new event's id.
  - `events(first, days, calendar_ids=None, leave_out=frozenset(), whole: bool = False)` - `whole=True` adds to each `"id"`, `"recurring_id"` (`recurringEventId` or None), `"invite"` (an `organizer` whose `self` isn't true), `"writable"` (its calendar's), and `"event"` (Google's own dict).
  - `GoogleCalendar._to_change(search: str, day: Optional[date], at: Optional[str], following: bool, today: date) -> Union[Dict, str]` - the one event to change, or what to say.
  - `GoogleCalendar.remove(search: str, day: Optional[date] = None, at: Optional[str] = None, following: bool = False, today: Optional[date] = None) -> Dict`
  - `_cut_rules(rules: List[str], before: Union[date, datetime]) -> List[str]` (module level).
  - `add()` gains `"undo"`: deletes the event it added, "Took Dentist back off your calendar."

`_to_change`: words matched as `find` matches them; the window is the day given (one day), else today over `SEARCH_DAYS`; `at` ("HH:MM") keeps the events starting at exactly that time, else those in that hour, and no all-day one; with `following` and every match one series, the earliest; several: "I found 3: " + each as `describe_found` names one, at most 3 then "and N more", + " Which one?"; none: "I can't find the dentist on your calendar on Thursday." (without " on Thursday" when no day was given); not writable: "I can't change that, Google only lets me change your own calendars."; an invite: "That's an invite from someone else. Change it in Google Calendar."

`remove`: one event: `DELETE` it (an occurrence by its own id removes that day only), "Removed Dentist, Thursday at 3."; undo is `PATCH` `{"status": "confirmed"}` on the same id, else (a 4xx) a `POST` of a copy (summary, description, location, start, end, reminders, colorId, transparency, visibility), "Put Dentist back, Thursday at 3.". `following`: read the series (`GET .../events/{recurring_id}`); this one its first (`originalStartTime` equals the series' `start`): `DELETE` the series; else `PATCH` the series `{"recurrence": _cut_rules(rules, this start)}`, "Removed Gym from Tuesday on."; undo puts the old `recurrence` back (or confirms the deleted series). A 404 or 410 on the delete: "Dentist was already gone.", no undo. `_NotSure`: "I can't tell whether Dentist was removed." no undo.

`_cut_rules`, exactly:

```python
# Each RRULE loses its COUNT and UNTIL and ends just before `before`: a second before it, in UTC,
# for a timed series ("UNTIL=20261124T085959Z"), the day before for an all-day one
# ("UNTIL=20261123"). EXDATE and RDATE lines are kept as they are.
```

- [ ] **Step 1: Write the failing tests.** Give `FakeGoogle` a `request(method, url, params=None, json=None, headers=None, timeout=None)` that records each write in `self.sent` (`{"method", "url", "params", "json"}`), also in `posted`/`asked` for a POST so the add tests keep passing, and answers from `self.answers[f"{method} {url}"]` (falling back to `url` for a POST). Rewrite `test_nova_never_changes_or_deletes_an_event` as `test_every_write_to_google_is_sent_once_and_tells_nobody`: the source has `.request(` once (in `_send_once`) and `.post(` once (the token), no `.patch(`, `.put(` or `.delete(`; every `sent` in this file's tests has `params == {"sendUpdates": "none"}` (check it in a fixture finaliser).

```python
def test_an_event_is_removed_and_said_back(for_changing): ...
def test_several_matches_remove_nothing_and_ask_which(for_changing): ...
def test_nothing_found_says_where_it_looked(for_changing): ...
def test_a_time_picks_the_event_at_it(for_changing): ...
def test_an_event_on_a_calendar_nova_cant_write_isnt_removed(for_changing): ...
def test_an_invite_is_left_alone(for_changing): ...
def test_one_day_of_a_repeating_event_is_removed_by_its_own_id(for_changing): ...
def test_from_this_one_on_cuts_the_series_just_before_it(for_changing): ...
def test_from_the_first_one_on_removes_the_whole_series(for_changing): ...
def test_cut_rules_end_a_count_series_and_an_all_day_series_just_before(): ...   # Review Focus 3
def test_an_event_already_gone_is_said_plainly(for_changing): ...              # Review Focus 5 (404 and 410)
def test_an_unclear_answer_is_never_sent_again(for_changing): ...
def test_a_removed_event_is_put_back_as_it_was(for_changing): ...              # PATCH status confirmed
def test_a_removed_event_google_wont_confirm_is_put_back_as_a_copy(for_changing): ...
def test_an_event_added_can_be_taken_back_off(for_adding): ...
```

`for_changing` is a fixture like `for_adding` (Personal and Work writable, Uni read-only) whose events lists answer from given events with ids, `recurringEventId`, `originalStartTime` and `organizer` as Google gives them.

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_google_calendar.py -k "remove or removed or cut_rules or invite or already_gone or unclear_answer or put_back or taken_back or every_write or several_matches or nothing_found or a_time_picks"` - FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_google_calendar.py`. PASS.
- [ ] **Step 5: Commit** `"Remove calendar events, one day or from this one on, and put them back"`

---

### Task 7: Moving and renaming calendar events

**Files:**
- Modify: `voice_assistant/google_calendar.py`
- Test: `tests/test_google_calendar.py`

**Interfaces:**
- Consumes: Task 6's `_to_change`, `_send_once`, `_cut_rules`, the `for_changing` fixture.
- Produces: `GoogleCalendar.change(search: str, day: Optional[date] = None, at: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None, summary: Optional[str] = None, following: bool = False, today: Optional[date] = None) -> Dict`. `start` and `end` as `add` takes them ("YYYY-MM-DD HH:MM" or "YYYY-MM-DD").

Rules: neither `start` nor `summary`: "I didn't catch what to change." New times: a timed `start` keeps the event's length (an hour, `DEFAULT_LENGTH`, for one that was all day) unless `end` is given; a date alone on a timed event keeps its time of day; a date on an all-day event keeps its number of days. A day already gone: `add`'s own sentence. One event: one `PATCH` of `start`/`end` and/or `summary`. Replies: "Moved Dentist to Friday at 4.", "Renamed Dentist to Dentist check-up, Thursday at 3.", both: "Moved Dentist to Friday at 4 and renamed it Dentist check-up." Undo `PATCH`es the old values back: "Moved Dentist back to Thursday at 3.", "Renamed it back to Dentist.", both: "Put Dentist back as it was, Thursday at 3."

`following`: a new day other than this one's: "I can only change the time for all of them. Move the days in Google Calendar." This one the series' first: one `PATCH` of the series (its own first date with the new time, and/or `summary`). Otherwise: `PATCH` the series with `_cut_rules`, then `POST` a new series from this one: the series' fields as Task 6's copy, the change applied, `recurrence` its rules with an `UNTIL` kept as it is and a `COUNT` lowered by the times before this one (counted with `GET .../events/{recurring_id}/instances`, `timeMax` this one's start). The `POST` failing: `PATCH` the old rules back once and say "I couldn't change them, so I've left Gym as it was."; `_NotSure` on the `POST`: put the old rules back once and say "I can't tell whether the new Gym was added, so check Google Calendar." Replies: "Moved Gym to 6 from Tuesday on.", "Renamed Gym to Gym class from Tuesday on."; undo deletes the new series and puts the old rules back: "Put Gym back as it was."

- [ ] **Step 1: Write the failing tests**

```python
def test_an_event_moves_keeping_its_length(for_changing): ...
def test_an_all_day_event_stays_all_day_on_a_new_day(for_changing): ...
def test_a_date_alone_keeps_a_timed_events_time(for_changing): ...
def test_an_event_is_renamed(for_changing): ...
def test_moved_and_renamed_at_once(for_changing): ...
def test_a_day_gone_isnt_moved_to(for_changing): ...
def test_a_change_is_undone_to_the_old_times_and_name(for_changing): ...
def test_all_of_them_only_changes_the_time(for_changing): ...
def test_from_the_first_one_on_changes_the_series_itself(for_changing): ...
def test_from_this_one_on_splits_the_series_with_the_times_left(for_changing): ...   # Review Focus 3: COUNT 10, 4 before -> COUNT 6
def test_a_split_that_fails_puts_the_old_series_back(for_changing): ...
def test_a_split_is_undone_whole(for_changing): ...
```

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_google_calendar.py -k "moves or moved or renamed or day_gone or all_of_them or from_the_first or splits or split"` - FAIL.
- [ ] **Step 3: Implement `change`.**
- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_google_calendar.py`. PASS.
- [ ] **Step 5: Commit** `"Move and rename calendar events, one day or from this one on"`

---

### Task 8: Calendar tools for Claude

**Files:**
- Modify: `voice_assistant/actions.py`
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: Task 6's `remove`, Task 7's `change`, Task 3's undo rule.
- Produces tools, `needs="calendar"`, `required=["search"]`:
  - `remove_from_calendar(search: Optional[str] = None, day: Optional[str] = None, time: Optional[str] = None, following: Optional[bool] = None)`
  - `change_calendar_event(search=None, day=None, time=None, new_start=None, new_end=None, new_name=None, following=None)`
- `day` "YYYY-MM-DD" (unreadable: "I didn't catch which day you meant."), `time` "HH:MM". Descriptions: `search` as `get_calendar`'s is (a word from its name, misheard words put right); `day` and `time` only when the user said them, for the event as it is now; `new_start`/`new_end` as `add_to_calendar` takes them, worked out from today's date; `following` only for "all of them", "every week" or "from now on"; what was done is said for it, so reply "Okay."

- [ ] **Step 1: Write the failing tests** with a fake calendar recording calls:

```python
def test_remove_from_calendar_passes_the_day_time_and_from_this_one_on(): ...
def test_change_calendar_event_passes_the_new_times_and_name(): ...
def test_an_unreadable_day_is_said(): ...
def test_a_calendar_change_is_kept_for_undo(): ...   # "undo" from the fake reaches last_change
```

- [ ] **Step 2: Run them to see them fail** - `$T tests/test_actions.py -k "calendar"` - FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass**, then `$T tests/test_actions.py`. PASS.
- [ ] **Step 5: Commit** `"Let Claude remove, move and rename calendar events"`

---

### Task 9: The record, the checklist and the whole suite

**Files:**
- Create: `docs/decisions/0030-changing-playlists-and-the-calendar.md` (0030 because the Nova view tiles branch holds 0029; take the next free number at merge)
- Modify: `docs/decisions/0008-home-assistant-first.md` (the "Adds only; never changes or deletes" row: add "- changed by 0030"), `docs/release-checklist.md`, `README.md` (its list of what Nova does, if it has one)

- [ ] **Step 1: Write the decision record** in the shape of `0028-the-page-as-a-list.md`: the ask in his words, his three choices (remove straight away with undo; stop SoulSync mirroring the three, append for later mirrors; "all of them" is from this one on), why SoulSync is asked, the calendar's one write path and `sendUpdates=none`, what isn't done (the spec's non-goals), and the one-off at deploy.
- [ ] **Step 2: Add release-checklist lines** under Music and Everything else, numbered from the next free number (34a if free): add, already on, missing then yes, remade refused, take off, make, like, undo; calendar remove, several, move, rename, from this one on, undo. Each as a sentence to say and what should happen, like the lines around it.
- [ ] **Step 3: Run the whole suite in both zones and ruff**, one at a time: `$T` then `TZ_OVERRIDE=UTC` with `$T`, then ruff. Expected: all pass, ruff clean. Paste the counts into the commit message body.
- [ ] **Step 4: Commit** `"Record decision 0030 and the hand checks for playlist and calendar changes"`

---

## After the build (each needs his OK, not part of any task)

- **Live checks, silent writes:** a throwaway playlist "Nova check" (make it, add a song, add again, take it off, undo, delete it); a throwaway event "Nova check" tomorrow on his primary calendar (add, move, rename, remove, undo - check undo brings back the same event id), and a daily throwaway series for five days (remove from day 3 on, undo; move day 3 on to another time, undo; delete it). Nothing plays or speaks.
- **His named job at deploy:** push `add-and-remove` to GitHub's main, pull as nova, restart nova-backend; then the one-off: read SoulSync's three mirrors, check each `server_playlist_id` is his Navidrome playlist, and `DELETE /api/mirrored-playlists/<id>` for each; read Navidrome's playlists after to confirm all six are still there.
