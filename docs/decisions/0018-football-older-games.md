# 0018: Football, round four: older games

16 November 2026 · Agreed with the user in chat · Built

(0017 is kept for the listening stats, whose branch had 0016 when football's reached `main` first.)

**Context.** Round three is live (decision 0016). Asked "what happened last time Liverpool played Newcastle", Nova said it could only check recent results. They last met on 23 August 2026, a 2-2 at St James' Park, this season: Home Assistant's script keeps only each team's last five results (`KEPT`), and Nova asks for no more. The user would still want, for an older game, at least the score, who was at home and the ground.

What a look on 16 November showed, all read-only:

- ESPN's schedule of a team in every competition, the one the script already reads (`all/teams/<id>/schedule`), takes `?season=<year>` (the year a season starts in) and gives that season's results in every competition: Liverpool's 2024 season has 61, its League Cup final against Newcastle among them. Each is about 600 KB and came back in under a second. The answer's `requestedSeason.year` says which season was given.
- For a national side, ESPN ignores the season and gives the same list, about the last 18 months (England's goes back to June 2025).
- A past day's scoreboard still has its matches' `details` (Liverpool at Newcastle, 25 August 2025), so an older game has its scorers and red cards, as a recent one does.
- BBC Sport's pages, for South Shields' league matches, go a month at a time.

**What the user wants.** "When did Liverpool last play Newcastle?" and a team's match on a day further back than its last few, with the score, the date, who was at home, the ground, and the rest where it's there. Five seasons back, his choice of my suggestion.

**Approaches.** A search over the team's own seasons, newest first, stopping at the first meeting, was chosen: one request a season, for a meeting this season none beyond the current list. Set aside: ESPN's head-to-head list in a match summary, which only comes with a match the two have coming or just played; and keeping whole seasons in the cache file, which the sensor reads and writes every minute (a season is about 600 KB from ESPN).

**Decision.**

1. **The `match` question takes `against`, the other team.** With it, the answer is the latest match between the two: the one in play, else the latest result, in this season's schedule, then each season before, back five, stopping at the first season with one. The answer says how many seasons were looked through (`seasons`), so a match not found is said with how far back it was looked for. For a national side, whose schedule has no seasons, the one list is all there is, and `seasons` is 1.
2. **A day further back.** Given a day with no match among a team's kept results, the season holding that day is read (a season starts in July), and the team's match on that day is the answer.
3. **The same answer as for a recent match.** The chosen match is taken from its day's scoreboard, as now, for its events, and ESPN's notes are added from its summary, as now.
4. **Nothing of it is kept.** A season's schedule is read for the one ask and never written to the cache file. The day's scoreboard and the summary are kept, as now.
5. **Time.** An ask stops reading seasons ten seconds in, and its answer is then stale, as it couldn't look as far as it says. Nova waits 20 seconds for an ask.
6. **BBC.** South Shields' league matches are only as far back as the pages the sensor keeps; their cup matches come from ESPN, as above. Nova says what it has.
7. **Nova.** `get_football` gains `against` for `match`. A team and another team, with no day, is the last time they played; a day given wins over `against`. With nothing found, Nova says no match between the two is listed in the last N seasons (or on that day).
8. **What Nova's build settled.** Everything is in `get_football`'s `against` and the words of `football_words.py`.
   - `against` is cut down as a name is (letters, digits, spaces and . & / -, at most 60 characters) in `HomeAssistant.football_ask`, the one place anything reaches a command line, and is sent only for `match` and only where something is left of it; every other question goes to the command exactly as in rounds one to three. `describe` cuts it down the same way (`football_name`) before it decides anything, so an `against` with nothing left of it is no other team, and so is one which is the team's own name once both are cut down.
   - A day wins: with a date, `against` is dropped before Home Assistant is asked, so the answer is the day's match and, with none, "No match is listed for Liverpool on that day."
   - The same team as `team`, however cased or spaced, is the team's match alone: a team has no match against itself, and Nova keeps no list of names to tell two ways of saying one team apart by. `against` with no team is "Name a team for its match.", asked of nobody and with the sensor unread, as a followed team's match is not the one of two teams.
   - An answer to a `match` asked with `against` which doesn't say `seasons` (or says one outside 1 to 6) or `against` (the team found, read as `team` is, with a name; its `id` may be any text, as a BBC team's begins "bbc:"), is "I can't get football right now.": without it the match could be the team's own latest, from a script which didn't know it was asked for another team's. A stale or error answer, or a command which fails (a script from before round four rejecting `--against`), is said as for any answer.
   - Nothing found: "No match between Liverpool and Newcastle United is listed in the last 6 seasons." (2 and up), and "in the results ESPN lists" for 1, where a national side's single list has no seasons in it. Never that the two haven't played. The other team is named as the answer's `against` has it (its full name where given), not as it was asked for, so that a wrong pick by ESPN's search shows.
   - A match of an earlier year is said with its year ("on Sunday the 16th of March 2025"); a past day of the year it is said in, and a fixture, are said without it.

**The contract, round four.** Rounds one to three stand, with these additions, word for word the same in both plans:

- `shell_command.football_ask` takes `against` (a team's name), for `what` = `match` only. The `match` answer, asked with `against`, carries `seasons` (an integer from 1 to 6: how many seasons were looked through) and `against` (the other team as found, shaped as `team` is: `id`, `name`, `full_name` where it differs, `followed`), with `match` only where one was found; `unknown` lists `against` where that team isn't found. Asked with a `date`, `against` is not used.

**Not in this round.** Head-to-head records ("how many times have they won"); seasons further back than five; BBC Sport's older months.
