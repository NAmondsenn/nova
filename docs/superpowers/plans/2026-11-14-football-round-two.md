# Football Round Two Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova answers "what's the starting 11 for Liverpool?" from Home Assistant, says the followed teams it can get when one can't be got, says nothing that names ESPN where Home Assistant's football now has two sources, and reads and announces South Shields' matches from BBC Sport with no other change.

**Architecture:** Home Assistant does the work (its own plan): BBC Sport fills in the matches ESPN doesn't list, as ordinary Contract matches, and `shell_command.football_ask` answers a fifth question, `lineups`. Nova's side allows that question through, puts its answer into words for Claude, and offers it through `get_football`'s `about`. Everything else is round one's.

**Tech Stack:** Python 3.13 on the brain, requests, pytest on the Pi.

**Spec:** `docs/decisions/0015-football-round-two.md` (decisions 7 to 10 are this plan's), with `0014-football.md` for round one. Home Assistant's side has its own plan, in the vault (`projects/homelab/2026-11-14-football-round-two-plan.md`); the Contract below is word for word the same in both.

## Contract

Round one's Contract stands as it is. This round adds three things.

- A match from BBC Sport has an `id` and a `slug` beginning `bbc:`. Of its two sides, the followed team's carries the team's ESPN id; the other's begins `bbc:`. It never has `venue` or `odds`.
- `shell_command.football_ask` takes `what` = `lineups`, with `name` the team. The answer: `team` (`id`, `name`, `full_name`?, `followed`), `match`? (the team's match in play; else its next if it kicks off within 24 hours; else its last if that was within the last 24 hours; else its next), `sides`? (each `team` (short name), `team_name`?, `formation`?, `starters` (names, in ESPN's order), `bench` (names)), with `stale`, `error` and `unknown` as every answer has them. `sides` holds only the sides whose line-up is out.
- The sensor's `rows` are today's and tomorrow's matches, then each team's next match within the card's days.

## Global Constraints

- Round one's constraints all stand (British spelling, no em dashes or emojis; Nova's docstring style; Nova never calls ESPN or BBC and keeps no list of teams or leagues; every service call through `_call_service`; announcements as decision 5 of 0014 has them; a fixture, score, result, table or line-up only ever said from what Home Assistant returned, and "I can't get football right now." when it can't be got; the repository is public).
- Work in the worktree `~/work/Home-Assistant-football`, branch `football-2` (made from `football` at 9558a00). Never touch `~/work/Home-Assistant`, branch `satellite`, or the worktree `~/work/Home-Assistant-merge`.
- `SERVICES` in `home_assistant.py` doesn't change: `shell_command.football_ask` already takes `what`; only `FOOTBALL_QUESTIONS` gains `lineups`.
- CT 109 load limits: check `/proc/loadavg` (wait while the first number is above 3), `nice -n 19 timeout 600`, one suite at a time, no repeat runs. Reviewers are on Opus, read diffs and run nothing.
- Pushes, deploys and restarts of `nova-backend` wait for Nathaniel to name the exact job.
- One commit per task, in plain words, ending `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Review Focus

1. A line-up with one side out and the other not: the side that's out is said, and the other is said as not out yet (Task 1, `test_one_side_out`).
2. Line-ups asked with no team named and no followed team playing today (Task 1, `test_lineups_with_no_team_and_nobody_playing`).
3. A match from BBC (ids and slug beginning `bbc:`) read, said and announced, the followed side known by its ESPN id (Task 3, `test_a_bbc_match_is_read_and_said`, `test_a_goal_in_a_bbc_match_is_announced`).
4. BBC down for days, so South Shields is stale while the other teams aren't: "is anyone playing today?" says the others and names South Shields; every team stale, or a sensor too old to mark teams, still "I can't get football right now." (Task 2, `test_the_overview_names_a_team_it_cant_get`, `test_the_overview_with_every_team_stale`).
5. A line-ups answer of the wrong shape (sides not a list, a starter not a name, a bench missing): "I can't get football right now." (Task 1, `test_a_lineups_answer_of_the_wrong_shape_is_refused`).

---

## Test commands

- One file: `nice -n 19 timeout 600 bash ~/work/pi_test_football.sh tests/test_football_words.py`, then the same with `TZ_OVERRIDE=UTC` in front. The whole suite (no arguments) only in Task 4.
- Ruff, after a test run has copied the tree: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-football && /tmp/nova-check-pytest/bin/ruff check <files>'`

## File structure

- `voice_assistant/home_assistant.py`: `FOOTBALL_QUESTIONS`.
- `voice_assistant/football_words.py`: `said_lineups`, the question chosen, `describe` for line-ups, the two sentences that named ESPN.
- `voice_assistant/actions.py`: `get_football`'s description and `about`.
- `tests/test_home_assistant.py`, `tests/test_football_words.py`, `tests/test_actions.py`, `tests/test_football_watch.py`.
- `README.md`, `docs/release-checklist.md`, `docs/decisions/0015-football-round-two.md`.

---

### Task 1: Line-ups through Home Assistant, in words

**Files:**
- Modify: `voice_assistant/home_assistant.py` (`FOOTBALL_QUESTIONS` at 127, `football_ask`'s docstring), `voice_assistant/football.py` (`Football.ask`'s docstring), `voice_assistant/football_words.py` (`_question` at 530, `_said_answer` at 550, `describe` at 578, new `said_lineups`)
- Test: `tests/test_home_assistant.py`, `tests/test_football_words.py`

**Interfaces:**
- Produces:
  - `FOOTBALL_QUESTIONS = ("team", "competition", "table", "left", "lineups")`
  - `said_lineups(answer: Dict, now: datetime) -> str`; raises `KeyError`, `TypeError` or `ValueError` for an answer of the wrong shape, as the other `said_*` do.
  - `_question(team, competition, about)`: `("lineups", team)` when `about` is "lineups" and a team is given; otherwise as now.
  - `describe(..., about="lineups")` with no team: the followed team whose `now` is in play, else the first whose `now` is today (by kick-off), from `football.read()`; with none, `{"success": True, "message": NO_LINEUP_TEAM}`.
  - `NO_LINEUP_TEAM = "None of the followed teams is playing today. Name a team for its line-up."`

- [ ] **Step 1: Write the failing tests.**

```python
AT = datetime(2026, 11, 13, 12, 0)


def _lineups_answer(**fields):
    """A football_ask answer for a team's line-ups, as the Contract has it, with no match unless a test gives one."""
    return {"team": {"id": "364", "name": "Liverpool", "followed": True}, "stale": False, **fields}


LIVERPOOL_SIDE = {"team": "Liverpool", "formation": "4-2-3-1", "starters": ["Alisson", "Frimpong", "Konate", "Van Dijk",
                  "Kerkez", "Gravenberch", "Mac Allister", "Salah", "Szoboszlai", "Gakpo", "Isak"], "bench": ["Mamardashvili", "Gomez"]}
ARSENAL_SIDE = {"team": "Arsenal", "starters": ["Raya", "White", "Saliba", "Gabriel", "Calafiori", "Rice", "Zubimendi",
                "Odegaard", "Saka", "Martinelli", "Gyokeres"], "bench": []}


def test_both_sides(london):
    lines = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[ARSENAL_SIDE, LIVERPOOL_SIDE]),
                         AT.astimezone()).split("\n")
    assert lines[0] == said_match(_m(**SUNDAY_AWAY), AT.astimezone())
    # The team asked about comes first, whatever order the answer has
    assert lines[1] == ("Liverpool's starting eleven (4-2-3-1): Alisson, Frimpong, Konate, Van Dijk, Kerkez, "
                        "Gravenberch, Mac Allister, Salah, Szoboszlai, Gakpo and Isak. Bench: Mamardashvili and Gomez.")
    assert lines[2].startswith("Arsenal's starting eleven: Raya,")
    assert lines[2].endswith("Bench: none listed.")


def test_one_side_out(london):
    said = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY), sides=[LIVERPOOL_SIDE]), AT.astimezone())
    assert said.split("\n")[-1] == "Arsenal's line-up isn't out yet."


def test_none_out_yet(london):
    said = said_lineups(_lineups_answer(match=_found_match(**SUNDAY_AWAY)), AT.astimezone())
    assert said.split("\n")[-1] == "Line-ups aren't out yet; they usually come about an hour before kick-off."


def test_no_lineups_for_a_match_over_or_from_bbc(london):
    ...  # a played match with no sides, and a match to come whose id begins "bbc:": the last line is
         # "No line-ups are listed for it."


def test_no_match_at_all(london):
    assert said_lineups(_lineups_answer(), AT.astimezone()) == "No match is listed for Liverpool."


def test_lineups_are_asked_of_home_assistant(london):
    ...  # describe(about="lineups", team="Liverpool") asks football.ask("lineups", "Liverpool", None, 1)


def test_lineups_with_no_team_and_nobody_playing(london):
    ...  # describe(about="lineups") with a reading where no team has a now: {"success": True, "message": NO_LINEUP_TEAM}


def test_lineups_with_no_team_ask_for_the_team_playing(london):
    ...  # a reading where Liverpool's now is in play and Stevenage's is later today: asks for Liverpool by its name


def test_lineups_of_a_competition_is_its_matches(london):
    ...  # describe(about="lineups", competition="Premier League"): asks "competition", as before


@pytest.mark.parametrize("changes", [{"sides": "Liverpool"}, {"sides": [{"team": "Liverpool", "starters": [7], "bench": []}]},
                                     {"sides": [{"team": "Liverpool", "starters": []}]}, {"match": "today"}])
def test_a_lineups_answer_of_the_wrong_shape_is_refused(london, changes):
    ...  # describe through a stand-in Football giving the answer with changes: {"success": False, "message": CANT_GET}
```

  `SUNDAY_AWAY`, `_found_match`, `_m` and `said_match` are the file's own. In `tests/test_home_assistant.py`: `test_the_football_command_asks_for_lineups` (the POST carries `"what": "lineups"`), and the existing refusal test keeps refusing `"weather"`.

- [ ] **Step 2: Run** `tests/test_football_words.py` and `tests/test_home_assistant.py`. Expected: the new tests fail.

- [ ] **Step 3: Implement.** `said_lineups` lines, in order: `said_match(match, now)` for the match (else only `f"No match is listed for {name}."`, where `name` is the team's full name or name); each side that is out, the asked team's first: `f"{side name}'s starting eleven ({formation}): {starters}. Bench: {bench}."` (no brackets without a formation; the side's name is `team_name` or `team`; names joined as the other `said_*` join them, with "and"; "none listed" for an empty bench); then, for a match to come or in play whose id doesn't begin `bbc:`, `f"{name}'s line-up isn't out yet."` for a side not out where the other is, or the one "Line-ups aren't out yet; ..." line where neither is; for a match over, or one from BBC, with no sides, "No line-ups are listed for it." The other side's name comes from the match.

- [ ] **Step 4: Run** both files, London and UTC. Expected: all pass. Ruff on the changed files.

- [ ] **Step 5: Commit** as "Say a team's line-ups from Home Assistant".

---

### Task 2: The overview says the teams it can get

**Files:**
- Modify: `voice_assistant/football_words.py` (`describe` at 610-615, `said_followed`)
- Test: `tests/test_football_words.py`

**Interfaces:**
- Produces: `said_followed(reading, now)` as now; `describe` with no team and no competition says it for the teams that aren't stale, then `f"I can't get {names} right now."` naming the stale ones (their names as `said_followed` gives them, joined with "and"). With every team stale, or the sensor stale with no team carrying the key (the round-one rule for an older Home Assistant script), `{"success": False, "message": CANT_GET}` as before.

- [ ] **Step 1: Write the failing tests.**

```python
def test_the_overview_names_a_team_it_cant_get(london):
    ...  # a reading of Liverpool (fresh, playing now) and South Shields (stale): the message is said_followed of a
         # reading with Liverpool alone, then the line "I can't get South Shields right now."; success True


def test_the_overview_with_every_team_stale(london):
    ...  # both stale: {"success": False, "message": CANT_GET}
```

  `test_one_stale_team_stops_the_overview` (round one) changes to the first of these; `test_a_sensor_which_is_stale_with_no_team_marked_stops_the_overview` stays as it is.

- [ ] **Step 2: Run** `tests/test_football_words.py`. Expected: the new tests fail.

- [ ] **Step 3: Implement.** No line of a stale team is said: the stale teams are left out of the reading before `said_followed` sees it, so its "no teams" and "playing today" lines are about the fresh teams only.

- [ ] **Step 4: Run** the file, London and UTC. Expected: all pass. Ruff on the changed file.

- [ ] **Step 5: Commit** as "Say the followed teams that can be got, and name the ones that can't".

---

### Task 3: The tool, the words that named ESPN, and BBC's matches

**Files:**
- Modify: `voice_assistant/actions.py` (`get_football` at 1084-1103), `voice_assistant/football_words.py` (235 and its docstring at 228; 357 and its docstring at 326)
- Test: `tests/test_actions.py`, `tests/test_football_words.py`, `tests/test_football_watch.py`

**Interfaces:**
- Produces: `about`'s enum `["matches", "table", "left", "lineups"]`.

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_actions.py
def test_get_football_offers_lineups():
    ...  # the tool's about enum holds "lineups", its description names line-ups as something it reads ("starting
         # eleven"), and "line-ups" is no longer among what it says Home Assistant hasn't got


# tests/test_football_words.py
def test_no_odds_listed(london):  # changed: the sentence names no source
    ...  # "... at 4:30 PM. No odds are listed for it."


def test_a_team_home_assistant_doesnt_cover(london):  # was test_a_team_espn_doesnt_cover
    ...  # the note is "Home Assistant's football doesn't seem to cover the league South Shields play in, so only their cup ties show."


def test_a_bbc_match_is_read_and_said(london):
    ...  # a found match with id "bbc:abc", slug "bbc:national-league-north", home_id "bbc:x", away_id the followed
         # team's ESPN id, no venue or odds: Match.from_home reads it, said_match says it, and a match to come says
         # "No odds are listed for it."


# tests/test_football_watch.py
def test_a_goal_in_a_bbc_match_is_announced():
    ...  # events() for a BBC match whose away_id is the followed team's id, the away score going 0 to 1:
         # "Goal for South Shields. Worksop Town 0, South Shields 1."
```

  Every other test asserting "ESPN lists no odds for it." changes to the new sentence (five in `tests/test_football_words.py`); the round-one note test is the one renamed above.

- [ ] **Step 2: Run** the three files. Expected: the new and changed tests fail; the two BBC tests may already pass, which is what they are there to show (say so in the report).

- [ ] **Step 3: Implement.** The tool's description gains line-ups ("'what's the starting 11 for Liverpool?'"; "'lineups' with a team for its starting eleven and bench; say the side asked for, or both") and drops them from what Home Assistant hasn't got; scorers stay there.

- [ ] **Step 4: Run** the three files, London and UTC. Expected: all pass. Ruff on the changed files.

- [ ] **Step 5: Commit** as "Offer line-ups to Claude, and say football's gaps without naming one source".

---

### Task 4: The docs, the whole suite, and putting it on Nova

**Files:**
- Modify: `README.md`, `docs/release-checklist.md`, `docs/decisions/0015-football-round-two.md`

- [ ] **Step 1: Docs.**
  - `README.md`: the Football part gains line-ups ("what's the starting 11 for Liverpool?") and says Home Assistant fills in from BBC Sport the matches ESPN doesn't list, such as non-league clubs' league games.
  - `docs/decisions/0015-football-round-two.md`: the line under the title ends "Built", and what the build settled that the record left open is added where it belongs.
  - `docs/release-checklist.md`, a new group after 24, in the list's own style:
    - 25a. "Hey Nova, what's the starting 11 for Liverpool?" within an hour of a Liverpool kick-off: their starting eleven and bench, and the other side's if asked; earlier in the day, when the match is and that line-ups usually come about an hour before.
    - 25b. "Hey Nova, when do South Shields play next?": their next league match, from BBC Sport.
    - 25c. During a South Shields league match: the card shows the score and minute, and a goal is announced.
    - 25d. On a day with no followed match today or tomorrow but one within 14 days, the card shows that match; England's next match shows only once it is within 14 days.

- [ ] **Step 2: The whole suite**, once in London and once in UTC: `nice -n 19 timeout 900 bash ~/work/pi_test_football.sh` and the same with `TZ_OVERRIDE=UTC`. Then ruff over the tree and `node --check` on the page's script. Expected: everything passes (4257 passed, 1 skipped before this work) and ruff is clean.

- [ ] **Step 3: Commit** the docs as "Say what football's second round adds and how to check it".

- [ ] **Step 4: Stop for Nathaniel.** Home Assistant's side must be live first (its plan's Task 7). Then give him this sentence, which covers round one too if it isn't live yet: "Merge football-2 into main, push main to GitHub, and deploy it to the brain: pull as nova and restart nova-backend." The Pi needs nothing.

- [ ] **Step 5: After the deploy.** As round one's Task 12 Step 5 (the brain's log; Liverpool's Goals switch off if it isn't yet). Then the hand checks 24a to 24g and 25a to 25d are his.
