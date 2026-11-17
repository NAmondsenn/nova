# Football Round Seven Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Where no meeting is found, Nova says the nearest names among the team's opponents and Claude asks "Did you mean ...?" instead of guessing.

**Architecture:** `football_words.py` reads `nearest` and adds it to the not-found lines; `actions.py`'s tool words. Home Assistant's side has its own plan.

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0023-football-did-you-mean.md` (decision 2 is this plan's). Home Assistant's plan: the vault's `projects/homelab/2026-11-17-football-round-seven-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one to six stand, with this addition.

- A `match` answer asked with `against` which found no meeting may carry `nearest`: a list of one to three names (non-empty text), the team's opponents in the matches looked through whose names are closest to the other team as asked, closest first.

## Global Constraints

- Rounds one to six's constraints stand (British spelling, no em dashes; Nova never calls ESPN or BBC and keeps no list of teams; football only from what Home Assistant returned; no digits joined by a dash; the repository is public).
- Work in `~/work/Home-Assistant-football`, branch `football-7` from ca8c4f3 (GitHub's main and the brain, with another session's clean-songs work).
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. "Corley": `{"team": South Shields, "unknown": ["Corley"], "bbc_since": "2021-08", "nearest": ["Chorley"]}`: "No match between South Shields and Corley is among BBC Sport's results for South Shields since August 2021, and ESPN found nothing called Corley. The nearest name among South Shields' opponents is Chorley."
2. "crawle" found as Crawley Town, not met, with `nearest` ["Chorley"]: the not-found line, then the nearest sentence; three names said "A, B and C".
3. `nearest` of the wrong shape (not a list, empty, more than three, a name not text or empty): refused as any wrong shape is; absent: the lines as before; never said beside a match found.
4. The tool's words: where nearest names are given, ask whether the user meant one of them; never guess another team.
5. Nothing said with a dash between digits.

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite once each at the end.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check .'`

---

### Task 1: "Did you mean" on Nova's side

**Files:** `voice_assistant/football_words.py`, `voice_assistant/actions.py`, tests in `tests/test_football_words.py`, `tests/test_actions.py`; README's Football part; decision 0023's line under the title ("Built"); `docs/release-checklist.md` 30a "Hey Nova, when did South Shields last play Corley?": Nova asks whether you meant Chorley, and "yes" gives the August match.

- [ ] Tests first for each Review Focus line; implement; run the touched files in London and UTC; the whole suite once each; ruff; node. Commit as "Ask did you mean, with the nearest names among a team's opponents".

### Task 2: Putting it on Nova (the controller's)

- [ ] Stop for Nathaniel (Home Assistant's side live first): "Deploy football-7 to the brain: push football-7 to GitHub's main, pull as nova and restart nova-backend."
