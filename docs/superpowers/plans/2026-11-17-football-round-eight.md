# Football Round Eight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A followed team's postponed, cancelled or abandoned match today or tomorrow is said when asked about, and announced once at the time the 15-minute warning would have come.

**Architecture:** `football_words.py` (how a match that is off is said in a team's lines and the overview), `football_watch.py` (one announcement for a match that is off). Home Assistant's side keeps the match in the sensor (its own plan).

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0024-football-called-off.md` (decision 2 is this plan's). Home Assistant's plan: the vault's `projects/homelab/2026-11-17-football-round-eight-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one to seven stand, with this addition.

- A followed team's called-off match (phase "off") which kicks off today or tomorrow is in the sensor, as the team's `now` (today) or at the head of its `next` (tomorrow), and in `rows`, with its `clock` the source's word for it ("Postponed", "Cancelled", "Abandoned"); one further off or past is not.

## Global Constraints

- Rounds one to seven's constraints stand (British spelling, no em dashes; Nova never calls ESPN or BBC and keeps no list of teams; football only from what Home Assistant returned; no digits joined by a dash; the repository is public).
- Work in `~/work/Home-Assistant-football`, branch `football-8` from f07c847 (GitHub's main and the brain, with other sessions' clean-songs work).
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. The overview and the team's answer with `now` a match that is off today: "South Shields v Harborough Town, tonight at 7:45 PM, is postponed." (in plain words, never "off (Postponed)"), and no "isn't playing"; tomorrow's at the head of `next`: said as tomorrow's, postponed.
2. The watcher: a match off at the warning time (15 minutes before kick-off): "South Shields' match against Harborough Town tonight is postponed." said once, under the "starts in 15 minutes" switch, the quiet hours and the user being home; found off only after kick-off, the same day: said once when first seen; never a 15-minute warning, a kick-off or anything else for it; said once across restarts (the record on disk).
3. Cancelled and abandoned said as such ("is cancelled", "was abandoned"); a clock of another word said as "is off".
4. A match that goes off after it started (abandoned in play): said once as abandoned, never "Full time".
5. Nothing said with a dash between digits.

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_watch.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite once each at the end.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check .'`

---

### Task 1: A called-off match said on Nova's side

**Files:** `voice_assistant/football_words.py`, `voice_assistant/football_watch.py`, tests in `tests/test_football_words.py` and `tests/test_football_watch.py`; README's Football part; decision 0024's line under the title ("Built"); `docs/release-checklist.md` 31a: on a day a followed team's match is postponed, Nova says so at the warning time and when asked.

- [ ] Tests first for each Review Focus line; implement; run the touched files in London and UTC; the whole suite once each; ruff; node. Commit as "Say a followed team's match that is called off".

### Task 2: Putting it on Nova (the controller's)

- [ ] Stop for Nathaniel (Home Assistant's side live first): "Deploy football-8 to the brain: push football-8 to GitHub's main, pull as nova and restart nova-backend."
