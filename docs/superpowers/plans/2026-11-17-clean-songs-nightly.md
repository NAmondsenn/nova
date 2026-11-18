# Clean songs: nightly check, weekly note, only explicit swapped in - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Section 8 of decision 0022: Nova runs the library check itself every night, sends a weekly note to the phone, and swaps in a new copy only when it's heard explicit.

**Architecture:** One change in `clean_swap.py` (the last try's outcome), list entries stamped with when they were added (`clean_store.py`), and one watcher, `CleanSchedule` in a new `clean_schedule.py`, looking once a minute: it starts CleanCheck at the night's time and sends the weekly note through Ntfy, recording what it has done in the store. Settings rows on the page; brain wiring.

**Tech Stack:** Python 3, the existing CleanCheck, CleanStore, Swapper, Ntfy; tests on the Pi.

**Spec:** `docs/decisions/0022-clean-songs-by-ear.md` section 8 (and 3 to 6).

## Global Constraints
- Everything in `.superpowers/sdd/2026-11-16-clean-songs-by-ear/common.md` still applies (style, tests on the Pi in London and UTC, ruff, never delete or overwrite, secrets, load limits, commits with the Co-Authored-By line, no push).
- Defaults verbatim: "Check new songs every night" on; "Nightly check at" 05:30; "Send a weekly note" on; "Weekly note on" Monday; weekly note "at" 09:30.
- Local time as the system has it (the brain's zone is London), as following.py does.

## Review Focus
1. A night's check already running or paused when its time comes: carried on, never two at once. Test in Task 2.
2. Nova off at the night's time: no make-up run at start; the next night runs. Test in Task 2.
3. A weekly note due in quiet hours: sent when they end, once. Test in Task 2.
4. A last-try new copy that is unsure or not heard: set aside too, original back, nothing deleted. Test in Task 1.
5. A week with nothing new: no note. Test in Task 2.

---

### Task 1: only an explicit new copy is swapped in, and list entries say when

**Files:** `voice_assistant/clean_swap.py`, `voice_assistant/clean_store.py`, `tests/test_clean_swap.py`, `tests/test_clean_store.py`

- [ ] In Swapper, when a try's new copy is heard unsure or not judged (and no copy of that try is explicit): set the new copy(ies) aside into `.nova-held` (as clean copies are, saved in the swap), put the original clean copy back (`held[0]`), end the swap, and list it "unsure" (or "couldnt" with the hearing's reason). Explicit or too_few stays "swapped" as now. Earlier tries' clean copies stay held.
- [ ] Tests: last try unsure -> the original is back in the library, the unsure copy is in .nova-held, listed unsure; last try not_judged -> the same, listed couldnt with the reason; explicit -> unchanged behaviour. A restart mid-way through this put-back is resumed (the swap is saved before each move).
- [ ] CleanStore.add stamps each entry with `at` (epoch seconds, from an injectable clock, default time.time) unless the caller passed one; entries loaded without `at` count as old. Test.
- [ ] Docs: README's clean-songs part and troubleshooting say an unsure new copy isn't kept.
- [ ] Commits; covering tests, then the whole suite London and UTC, ruff.

### Task 2: CleanSchedule - the nightly check and the weekly note

**Files:** create `voice_assistant/clean_schedule.py`, `tests/test_clean_schedule.py`; modify `config.yaml` (clean_check: nightly, nightly_time, weekly_note, weekly_day, weekly_time), `voice_assistant/config.py` (LIMITS: nightly_time and weekly_time "time" form as ntfy's quiet times use; weekly_day one of Monday..Sunday - add a form if none fits), `voice_assistant/settings_page.py` (SETTINGS rows in the Music group, "now"), `voice_assistant/brain.py` (make, start in run(), stop in _clean_up before clean_check.close), `tests/test_config.py`, `tests/test_settings_page.py`, node check of settings.js if its Music group needs the rows listed.

**Interfaces:** `CleanSchedule(clean_check, store, ntfy, settings: Callable[[], Dict], page_url: Callable[[], str], clock=time.time)`, `start()`, `stop()`, `look()` (one look; the thread looks once a minute).
- Nightly: when `nightly` is on and local time has reached `nightly_time` today and today's night hasn't been done (store records the date done), call clean_check.start() (a check running or paused is carried on by start() as it is); record today done whatever start() says, logging its message. At first start, today counts as done if the time has already passed (no make-up run).
- Weekly: when `weekly_note` is on, today is `weekly_day`, local time has reached `weekly_time`, this week's note isn't recorded as sent, and it's not ntfy's quiet hours (ntfy.quiet()) - count list entries with `at` after the last note's time (first note: the last 7 days); if any, send one message to the phone topic: e.g. "This week Nova swapped 3 clean songs for explicit ones; 1 only comes down clean, 2 are unsure, 1 couldn't be swapped. 4 files wait in .nova-held. See the settings page." (leave out zero parts; the held count only if above 0); title "Clean songs"; click = the page's address when known. Record the week sent (and its time) whether or not anything was sent; with no ntfy or no phone topic, record it and log.
- Store: CleanStore gets `schedule()` / `save_schedule(dict)` for {night_done: "YYYY-MM-DD", week_sent: "YYYY-Www", note_at: epoch}, kept like the check part.
- [ ] Tests for Review Focus 1, 2, 3, 5, plus: switches off -> nothing; the time read live from the settings; the sentence leaves out zero parts; a failed send is logged and the week still recorded (no retry storm).
- [ ] Commits; covering tests, then the whole suite London and UTC, ruff, node.

### Task 3: docs
- [ ] README clean-songs part, troubleshooting and the release checklist (13m: nightly check runs and hears only new songs; 13n: weekly note arrives, outside quiet hours) describe section 8 word for word with the code. No em dashes. Commit.
