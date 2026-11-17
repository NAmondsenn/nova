# Football Round Six Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova's side of football's last gaps: last night's match with no team named, a goal ruled out just before full time, and BBC Sport named where its results were searched.

**Architecture:** `football_words.py` (`_team_playing` for `match`, the not-found line), `football.py` (`bbc_since` read), `football_watch.py` (the full-time reading confirms a waiting lower score). Home Assistant's side has its own plan.

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0021-football-last-gaps.md` (decisions 2's first sentence, 4 and 6 are this plan's). Home Assistant's plan: the vault's `projects/homelab/2026-11-16-football-round-six-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one to five stand, with this addition.

- The `match` answer asked with `against` or a `date`, for a followed team whose league matches come from BBC Sport, may carry `bbc_since` (YYYY-MM): BBC Sport's month pages were read back to that month.

## Global Constraints

- Rounds one to five's constraints stand: British spelling, no em dashes or emojis; Nova's docstring style; Nova never calls ESPN or BBC and keeps no list of teams, leagues or abbreviations; football only said from what Home Assistant returned; no digits joined by a dash; the repository is public.
- Work in `~/work/Home-Assistant-football`, branch `football-6` from 55c75cf (GitHub's main and the brain).
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. "What happened in the match?" at 00:30 after a followed team's match that kicked off at 19:45 and ended at 21:40: that match; at 09:00 the next day: that match; at 08:00 two days on: "Name a team for its match." (past the twelve hours, which the build counts from the match's end, taken as three hours after kick-off: 15 hours from kick-off, to 10:45 after one at 19:45); a match in play always first.
2. The watcher: a lower score noted at 89' and the full-time reading showing it: "Goal ruled out." once, then "Full time. ..." with the lower score; the full-time reading showing the higher score again: no ruled-out line; a waiting note with no full-time reading time known: nothing said of it.
3. Not found with `bbc_since` "2021-08": "No match between South Shields and Spennymoor Town is listed in the last 6 seasons, nor in BBC Sport's results since August 2021."; a "bbc:" other team: "No match between South Shields and Spennymoor Town is among BBC Sport's results for South Shields since August 2021."; a side only BBC Sport could know, never met, which Home Assistant answers as `unknown` with `bbc_since`: "No match between South Shields and Nowhere Rovers is among BBC Sport's results for South Shields since August 2021, and ESPN found nothing called Nowhere Rovers." (as before, ESPN's line, without `bbc_since`); `bbc_since` that isn't YYYY-MM: refused, found or not.
4. A match found with `bbc_since`: said as any match is (nothing of `bbc_since`).
5. Nothing said with a dash between digits ("August 2021", never "2021-08").

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite once each at the end of Task 1.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check .'`

---

### Task 1: The last gaps on Nova's side

**Files:** `voice_assistant/football.py`, `voice_assistant/football_words.py`, `voice_assistant/football_watch.py`, tests in `tests/test_football.py`, `tests/test_football_words.py`, `tests/test_football_watch.py`; README's Football part; decision 0021's line under the title ("Built", with what the build settled); `docs/release-checklist.md` 29a "Hey Nova, what happened in the match?" the morning after a followed team's evening match.

- [ ] Tests first for each Review Focus line; implement; run the touched files in London and UTC; then the whole suite once in London and once in UTC; ruff; node. Commit as "Close football's last gaps on Nova's side".

### Task 2: Putting it on Nova (the controller's)

- [ ] Stop for Nathaniel (Home Assistant's side live first): "Deploy football-6 to the brain: push football-6 to GitHub's main, pull as nova and restart nova-backend."
