# 0021: Football, round six: the last gaps

16 November 2026 · Asked for by the user in chat ("fix the 7 gaps actually") · Approved, being built

**Context.** Round five (decision 0019) closed most of football's known gaps and left seven: three the user had chosen to leave and four found while building it. He asked for all seven to be fixed.

What a look on 16 November showed, all read-only:

- ESPN's list calls the Women's Super League "Women's Super League" (`eng.w.1`). "WSL" is in no exact name, and as part of a name it is in "NWSL" (the American league) and "WSL Players Cup" (`eng.w.league_cup`), so a partial match finds the wrong one.
- BBC Sport's month pages for a team (`.../teams/<team>/scores-fixtures/YYYY-MM`) go back years (South Shields' to 2021 at least), each about 350 KB, in under half a second.

**Decision.** For each gap:

1. **"WSL".** Home Assistant's short list of names ESPN doesn't use (decision 0019) gains "WSL" for `eng.w.1`, and the list is now looked at after ESPN's exact names and before its partial ones, since a partial match finds a wrong competition for it. "EFL Cup" is found as before.
2. **What happened, just after midnight.** With no team named and no day given, Nova's choice for `match` also takes a followed team's match which is over and kicked off within the last twelve hours (the latest first), after one in play: "what happened in the match?" at half past midnight, or the next morning, is last night's match. And Home Assistant counts a match in play as today's when today is the day asked about, whatever day it kicked off.
3. **South Shields' older league matches.** For a followed team whose league matches come from BBC Sport, `match` with `against`, or with a day further back than the pages the sensor keeps, also reads BBC Sport's month pages for the team, newest first, as far back as five seasons or until the ask's ten seconds are spent, and stops at the first meeting (or the day's match). Nothing of the older months is kept. The answer says how far back BBC Sport was read.
4. **A goal ruled out just before full time.** A lower score waiting to be confirmed when the full-time reading arrives is confirmed by that reading if it shows the same lower score: a reading at full time is never an old copy of the match in play. "Goal ruled out." is said once, before "Full time.".
5. **A year at the front, or a season with an en dash.** A competition's name loses a year at its front ("the 2026 World Cup") as it loses one at its end, and a season written with an en dash ("2025–26") or short ("25/26") as one with a hyphen or slash.
6. **BBC Sport named where it was searched.** The not-found line names BBC Sport where its results were read: "... is listed in the last 6 seasons, nor in BBC Sport's results since August 2021." for an ESPN team, and "... is among BBC Sport's results for South Shields since August 2021." for a side only BBC Sport knows.
7. **Period 5 and an over status.** A match in play at period 5 is in its shoot-out only where ESPN's status names a half or isn't one the script knows; a status ESPN names as over or called off keeps that meaning.

**The contract, round six.** Rounds one to five stand, with this addition, word for word the same in both plans:

- The `match` answer asked with `against` or a `date`, for a followed team whose league matches come from BBC Sport, may carry `bbc_since` (YYYY-MM): BBC Sport's month pages were read back to that month.

**Not in this round.** Nothing of the seven is left.
