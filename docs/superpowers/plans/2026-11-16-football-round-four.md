# Football Round Four Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "When did Liverpool last play Newcastle?" and a team's match on any day of the last five seasons, said as a recent match is.

**Architecture:** Home Assistant's script searches the team's seasons (its own plan). Nova passes `against` with `match`, and says how far back it looked when it finds nothing.

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0018-football-older-games.md`. Home Assistant's plan is in the vault, `projects/homelab/2026-11-16-football-round-four-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one to three stand, with these additions.

- `shell_command.football_ask` takes `against` (a team's name), for `what` = `match` only. The `match` answer, asked with `against`, carries `seasons` (an integer from 1 to 6: how many seasons were looked through) and `against` (the other team as found, shaped as `team` is: `id`, `name`, `full_name` where it differs, `followed`), with `match` only where one was found; `unknown` lists `against` where that team isn't found. Asked with a `date`, `against` is not used.

## Global Constraints

- Rounds one to three's constraints stand: British spelling, no em dashes or emojis; Nova's docstring style; Nova never calls ESPN or BBC; every service call through `_call_service`; football only said from what Home Assistant returned, "I can't get football right now." when it can't be got; no digits joined by a dash in anything Nova builds; the repository is public.
- Work in `~/work/Home-Assistant-football`, branch `football-4` (from 4f4acc5, `spoken-fixes`, which is on b9c969d..ee4f707, GitHub's main). `main` locally holds another session's unpushed listening-stats work: never merge it into this branch, and never push it.
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. "When did Liverpool last play Newcastle?" goes to `match` with `against`, never to `matches` or `team` (the tool's words, Task 1).
2. Nothing found: "No match between Liverpool and Newcastle is listed in the last 6 seasons." (and for `seasons` 1, "in the results ESPN lists"), never a claim they've never played (Task 1).
3. `against` the same team as `team`, or `against` with no team: asked of nobody, "Name a team for its match." or the team alone (Task 1).
4. Against the live script, which doesn't know `--against`: Home Assistant fails the command, and Nova says "I can't get football right now." (Task 1, a test of the error path).
5. A day given with `against`: the day wins, as the Contract says (Task 1).

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite once each at the end of Task 1.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check .'`

---

### Task 1: `against`, said and documented

**Files:** `voice_assistant/home_assistant.py` (`ask_football` takes `against`, cleaned as `name` is), `voice_assistant/football.py` (`Football.ask` passes it), `voice_assistant/football_words.py` (`describe`, `_question`, `_said_answer` for `match` with `seasons`), `voice_assistant/actions.py` (`get_football`'s `against` and its words), tests in `tests/test_home_assistant.py`, `tests/test_football.py`, `tests/test_football_words.py`, `tests/test_actions.py`; `README.md`, decision 0018's line under the title ("Built", with what the build settled), `docs/release-checklist.md` (27a "Hey Nova, when did Liverpool last play Newcastle?": 23 August 2026, 2 2 at St James' Park, with the scorers; 27b "Hey Nova, what happened in the Liverpool match on the 16th of March 2025?": the League Cup final; 27c a team never met: how many seasons were looked through).

- [ ] Tests first, for each Review Focus line; the tool's words asserted ("against: the other team, for 'match': the last time the team played them, back five seasons"; "When did Liverpool last play Newcastle?" as an example). Run London and UTC on the touched files; then the whole suite once in London and once in UTC; ruff; node. Commit as "Ask when two teams last played".

### Task 2: Putting it on Nova (the controller's)

- [ ] Stop for Nathaniel (Home Assistant's side live first): "Deploy football-4 to the brain: push football-4 to GitHub's main, pull as nova and restart nova-backend." (GitHub's main is ee4f707, which `football-4` builds on through `spoken-fixes`, so it is a fast-forward and deploys those fixes too.)
