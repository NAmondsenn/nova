# Football Round Three Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova says what happened in a team's match (goals with minutes and the score after each, penalties, own goals, red cards, ESPN's notes), adds the scorers to a competition's round-up, and never has a score spoken as "three dash two".

**Architecture:** Home Assistant's script gives each match `events`, and a sixth question, `match`, with `notes` (its own plan). Nova reads `events` into `Match`, says them in a round-up and in a new `said_match_report`, and offers `about: "match"` through `get_football`.

**Tech Stack:** Python 3.13 on the brain, pytest on the Pi.

**Spec:** `docs/decisions/0016-football-what-happened.md` (decisions 2 to 5 are this plan's). Home Assistant's plan is in the vault, `projects/homelab/2026-11-16-football-round-three-plan.md`; the Contract below is word for word the same in both.

## Contract

Rounds one and two stand, with these additions.

- A match in an answer to a question may have `events` (never in the sensor): each `minute` (the source's label, e.g. "45'", "90'+3'"), `kind` (`goal`, `penalty`, `own_goal`, `penalty_missed` or `red_card`), `side` (`home` or `away`: the side a goal, penalty or own goal counts for; the player's own side for a penalty missed or a red card) and `player`? (the name the source gives). In the order they happened.
- `shell_command.football_ask` takes `what` = `match`, with `name` the team and `date` (YYYYMMDD) if a day was asked about. The answer: `team` (as for `lineups`), `match`? (with `events`), `notes`? (each `minute` and `text`, ESPN's sentence), with `stale`, `error` and `unknown` as every answer has them.

## Global Constraints

- Rounds one and two's constraints stand: British spelling, no em dashes or emojis; Nova's docstring style; Nova never calls ESPN or BBC; every service call through `_call_service`; football only said from what Home Assistant returned, "I can't get football right now." when it can't be got; the repository is public.
- Work in `~/work/Home-Assistant-football`, branch `football-3` (from b9c969d, what is live). `main` also holds another session's unpushed listening-stats work: never merge it into this branch, and never push it.
- No sentence Nova builds has a score as digits joined by a dash.
- CT 109 load limits; reviewers on Opus read diffs and run nothing; one commit per task ending `Co-Authored-By: Claude <noreply@anthropic.com>`; pushes and deploys on his named job.

## Review Focus

1. An own goal said as the other side's goal, naming the player ("an own goal by C. Sykes"), with the score right after it (Task 1, `test_an_own_goal`).
2. A match with no events (not started, or a feed without them): said as before, nothing raises; events of the wrong shape: "I can't get football right now." (Task 1).
3. A round-up of 18 matches: every match's scorers, still one line a match (Task 1, `test_a_round_up_with_events`).
4. `about="match"` with no team named: the followed team playing now, else today's, else asks which team (Task 2).
5. A match in play: its events so far, and LAG last (Task 1).

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then with `TZ_OVERRIDE=UTC` in front. The whole suite only in Task 3.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check <files>'`

---

### Task 1: Events read and said

**Files:** `voice_assistant/football.py` (`Match`), `voice_assistant/football_words.py`, tests in `tests/test_football.py`, `tests/test_football_words.py`.

**Interfaces:** `MatchEvent(minute: str, kind: str, side: str, player: Optional[str])` (frozen); `Match.events: Tuple[MatchEvent, ...]` (empty by default; read in `Match.from_home`, refusing a wrong shape or a `kind`/`side` outside the Contract with `ValueError`). `said_events(match) -> List[str]`: one sentence per event in order, e.g. "45': a penalty for England, scored by Harry Kane. Spain 0, England 1.", "63': a red card for Elliot Anderson of England.", "32': an own goal by C. Sykes, for Oxford City. Oxford City 1, South Shields 0.", "a penalty for X, missed by Y." (score unchanged, so none after it); a goal with no player says "a goal for England". The running score is worked out from the goals in order, starting at 0, 0; if the last running score doesn't match the match's own score, the events are said without the running scores (so nothing false is said). `said_competition` puts each started match's goals after it on the same line, compactly: "Goals: Harry Kane 45' (pen), Ezri Konsa 53', Ben White 57' for England; Mikel Oyarzabal 65' (pen), Nico Williams 75' for Spain. Red card: Elliot Anderson (England) 63'." (names as the source gives them: no rule finds a surname without a list) `said_match_report(answer, now) -> str` for the `match` answer: the match line (`said_match`), then `said_events`, then each note "Note at 41': Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area.", then LAG if in play; with no match, "No match is listed for {name}." (or "on that day").

- [ ] Tests first, with the exact sentences above for Spain 2, England 3 and the South Shields own goal; `test_an_own_goal`; `test_a_round_up_with_events` (18 matches, each still one line); a match in play; events of the wrong shape refused; a running score that doesn't add up says no running scores. Run London and UTC; ruff; commit as "Say a match's goals, penalties and red cards".

### Task 2: The question, and no dashes

**Files:** `voice_assistant/home_assistant.py` (`FOOTBALL_QUESTIONS` gains `match`), `voice_assistant/football_words.py` (`_question`, `_said_answer`, `describe`), `voice_assistant/actions.py` (`get_football`), tests.

- [ ] `about="match"` with a team (and `date`, passed as YYYYMMDD) asks `match`; with no team named, the followed team in play, then to come today, then over today (as line-ups' choice, with a BBC match no longer ranked last, since BBC matches have events), else "Name a team for its match." The tool's description: "'match' for what happened in a team's match - its goals, penalties, red cards and ESPN's notes"; "what happened in the Champions League yesterday?" is a competition with a date; and "Say a score as 'Spain 2, England 3' or in words, never as digits joined by a dash, which is read out as 'dash'." Tests for each; the tool's words asserted. Commit as "Ask what happened in a match".

### Task 3: Docs, the whole suite, and putting it on Nova

- [ ] `README.md` (the Football part: what happened in a match, scorers in round-ups); decision 0016's line under the title ends "Built", with what the build settled; release checks 26a to 26c: "what happened in the England match on Sunday?" (goals with minutes, the red card, a note of a penalty conceded); "what happened in the Nations League on Sunday?" (every score with its scorers); a score spoken as "Spain 2, England 3" or "three-two", never "dash".
- [ ] The whole suite once in London and once in UTC; ruff; node. Commit as "Say what football's third round adds and how to check it".
- [ ] Stop for Nathaniel (Home Assistant's side live first): "Merge football-3 into main on GitHub and deploy it to the brain: push football-3 to GitHub's main, pull as nova and restart nova-backend." (GitHub's main is b9c969d, which `football-3` builds on, so the push is a fast-forward and leaves the local main, with the other session's unpushed work, alone.)
