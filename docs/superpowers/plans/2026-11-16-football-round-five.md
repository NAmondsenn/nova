# Football Round Five Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova's side of the known gaps: odds said only where looked for, a national side's not-found line, the same team twice, a goal ruled out only once it holds two minutes, and competitions named as they are properly called.

**Architecture:** Small changes in `football.py` (`Match` reads `no_odds`), `football_words.py`, `football_watch.py` (`_goals`) and `actions.py` (the tool's words). Home Assistant's side has its own plan.

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0019-football-gaps.md` (decisions 2 to 5 and 1's last sentence are this plan's). Home Assistant's plan is in the vault, `projects/homelab/2026-11-16-football-round-five-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one to four stand, with this addition.

- A match to come in an answer to a question (never the sensor), taken from a day's scoreboard, carries `odds` where ESPN lists them or `no_odds: true` where it lists none; a match from a team's schedule alone carries neither.

## Global Constraints

- Rounds one to four's constraints stand: British spelling, no em dashes or emojis; Nova's docstring style; Nova never calls ESPN or BBC and keeps no list of teams, leagues or abbreviations; football only said from what Home Assistant returned; no digits joined by a dash in anything Nova builds; the repository is public.
- Work in `~/work/Home-Assistant-football`, branch `football-5` from 1cf63fc (GitHub's main and the brain).
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. A team answer with five fixtures, the first two looked up (one with odds, one with `no_odds`) and three not: "No odds are listed for it." after the second only.
2. `no_odds` of the wrong type, or beside `odds`: the answer is refused as any wrong shape is.
3. The watcher: a lower score seen at 0 s, again at 60 s, again at 125 s: "Goal ruled out." at 125 s only; the higher score back at 60 s drops it; a reading of unknown time confirms nothing (as now).
4. `against` whose found id is the team's own: "Liverpool and Liverpool FC are the same team.", nothing of a match said.
5. Against Home Assistant's live script (no `no_odds` yet): nothing is said of odds for a fixture without them, rather than the old sentence (Home Assistant is deployed first, but say so).

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite once each at the end of Task 1.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check .'`

---

### Task 1: The gaps on Nova's side

**Files:** `voice_assistant/football.py` (`Match.no_odds`, read in `Match.from_home`), `voice_assistant/football_words.py` (the odds sentence, the not-found line for `seasons` 1, the same team), `voice_assistant/football_watch.py` (`_goals`: `RULED_OUT_AFTER = 120` seconds), `voice_assistant/actions.py` (the tool: "Name a competition as it is properly called where you know it, such as the Carabao Cup for the EFL Cup"), tests in `tests/test_football.py`, `tests/test_football_words.py`, `tests/test_football_watch.py`, `tests/test_actions.py`; README's Football part; decision 0019's line under the title ("Built", with what the build settled); `docs/release-checklist.md` 28a "Hey Nova, who won the Euros?" (the last European Championship), 28b "Hey Nova, who's left in the EFL Cup?" (the Carabao Cup).

- [ ] Tests first for each Review Focus line, with the exact sentences of decision 0019 ("No match between England and Brazil is among ESPN's results for England.", "Liverpool and Liverpool FC are the same team."). Run the touched files in London and UTC; then the whole suite once in London and once in UTC; ruff; node. Commit as "Close football's known gaps on Nova's side".

### Task 2: Putting it on Nova (the controller's)

- [ ] Stop for Nathaniel (Home Assistant's side live first): "Deploy football-5 to the brain: push football-5 to GitHub's main, pull as nova and restart nova-backend."
