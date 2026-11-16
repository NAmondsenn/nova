# 0016: Football, round three: what happened in a match

16 November 2026 · Agreed with the user in chat · Approved, being built

**Context.** Football is live (decisions 0014 and 0015). Asked "what happened in the England match yesterday", Nova said England beat Spain "3-2", which the speech read as "three dash two", and that Home Assistant's football has no scorers. Asked about a competition on a day ("what happened in the Nations League yesterday"), it already lists every score and who won.

What a look on 16 November showed, all read-only:

- ESPN's scoreboard, which Home Assistant's script already reads for every match it says, lists each match's goals, penalties, own goals and cards in `details`: the minute ("45'", "90'+3'"), the kind ("Goal", "Goal - Header", "Penalty - Scored", "Own Goal", "Red Card", "Yellow Card"; and from the match summary, "Penalty - Saved") and the player. Spain 2, England 3 on 15 November: Kane's penalty (45'), Konsa (53'), White (57'), Anderson's red card (63'), Oyarzabal's penalty (65'), Williams (75'). The script drops it today.
- ESPN's match summary (about 350 KB) adds a minute-by-minute commentary. Across 30 recent matches it named penalties conceded ("Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."), VAR decisions ("VAR Decision: No Goal France 1-0 Türkiye."), penalties saved, shots against the woodwork, own goals and second yellow cards. It never named an error leading to a goal.
- BBC Sport's pages, which fill in South Shields' matches, list each side's goals and own goals with the scorer and minute (`actions`).

**What the user wants.** To ask what happened in any team's match: the scorers, penalties, red cards, a late winner or equaliser, and a mistake that cost the game. And after a round of matches in a competition (the Champions League, a World Cup, the Euros, the Premier League), "what happened in ... today / yesterday", with the scores and who played. Not old seasons.

**Approaches.** Home Assistant's script gives the events (Nova never calls ESPN or BBC, decision 0014): from the scoreboards it already reads, so a round-up costs nothing more, and from one match summary only when one match is asked about. Reading every match's summary for a round-up was set aside: about 350 KB a match. Nova saying itself what was a mistake was set aside: it would be made up; only what ESPN's commentary records is said.

**Decision.**

1. **Events on every match in an answer.** A match in an answer to a question (never the sensor) carries its `events` once it has started: goals, penalties scored, own goals, penalties missed or saved, and red cards, each with its minute, the side it counts for and the player, in the order they happened. Yellow cards are left out. ESPN's come from the scoreboard's `details`; BBC's from its `actions`, using only the kinds seen in BBC's own data.
2. **A sixth question, `match`.** For a team: its match in play; else its match on the day given; else its last. The answer has the match with its events and, for a match ESPN carries, `notes`: ESPN's own commentary sentences, verbatim, for penalties conceded, VAR decisions, penalties saved or missed, the woodwork, own goals and second yellow cards. Nova never says a mistake cost a game unless a note says what happened; Claude may say a goal was a late winner or equaliser from the minutes and the score.
3. **Round-ups.** A competition's matches on a day carry their events too, from the same scoreboards, so "what happened in the Premier League yesterday" gives every score with its scorers and red cards.
4. **Nova's words.** The events are said in order, each with its minute and the score after it. `get_football` gains `about: "match"`. Claude is told to say a score as words or as "Spain 2, England 3", never digits with a dash between, which the speech reads as "dash"; Nova's own sentences never use one.
5. **Built and deployed as rounds one and two were:** a branch on each side from what is live (`football-3` from b9c969d, `football-ask-3` from f50aa1c), each task reviewed on Opus, Home Assistant first on the user's named job.

**The contract, round three.** Rounds one and two stand, with these additions, word for word the same in both plans:

- A match in an answer to a question may have `events` (never in the sensor): each `minute` (the source's label, e.g. "45'", "90'+3'"), `kind` (`goal`, `penalty`, `own_goal`, `penalty_missed` or `red_card`), `side` (`home` or `away`: the side a goal, penalty or own goal counts for; the player's own side for a penalty missed or a red card) and `player`? (the name the source gives). In the order they happened.
- `shell_command.football_ask` takes `what` = `match`, with `name` the team and `date` (YYYYMMDD) if a day was asked about. The answer: `team` (as for `lineups`), `match`? (with `events`), `notes`? (each `minute` and `text`, ESPN's sentence), with `stale`, `error` and `unknown` as every answer has them.

**Not in this round.** Old seasons; yellow cards; line-ups of players who came on; commentary beyond the listed kinds; a judgement of mistakes of Nova's own.
