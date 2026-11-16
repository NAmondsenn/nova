# 0016: Football, round three: what happened in a match

16 November 2026 · Agreed with the user in chat · Built

**Context.** Football is live (decisions 0014 and 0015). Asked "what happened in the England match yesterday", Nova said England beat Spain "3-2", which the speech read as "three dash two", and that Home Assistant's football has no scorers. Asked about a competition on a day ("what happened in the Nations League yesterday"), it already lists every score and who won.

What a look on 16 November showed, all read-only:

- ESPN's scoreboard, which Home Assistant's script already reads for every match it says, lists each match's goals, penalties, own goals and cards in `details`: the minute ("45'", "90'+3'"), the kind ("Goal", "Goal - Header", "Penalty - Scored", "Own Goal", "Red Card", "Yellow Card"; and from the match summary, "Penalty - Saved") and the player. Spain 2, England 3 on 15 November: Kane's penalty (45'), Konsa (53'), White (57'), Anderson's red card (63'), Oyarzabal's penalty (65'), Williams (75'). The script drops it today.
- ESPN's match summary (about 350 KB) adds a minute-by-minute commentary. Across 30 recent matches it named penalties conceded ("Penalty conceded by Pau Cubarsí (Spain) after a foul in the penalty area."), VAR decisions ("VAR Decision: No Goal France 1-0 Türkiye."), penalties saved, shots against the woodwork, own goals and second yellow cards. It never named an error leading to a goal.
- BBC Sport's pages, which fill in South Shields' matches, list each side's goals and own goals with the scorer and minute (`actions`).

**What the user wants.** To ask what happened in any team's match: the scorers, penalties, red cards, a late winner or equaliser, and a mistake that cost the game. And after a round of matches in a competition (the Champions League, a World Cup, the Euros, the Premier League), "what happened in ... today / yesterday", with the scores and who played. Not old seasons.

**Approaches.** Home Assistant's script gives the events (Nova never calls ESPN or BBC, decision 0014): from the scoreboards it already reads, so a round-up costs nothing more, and from one match summary only when one match is asked about. Reading every match's summary for a round-up was set aside: about 350 KB a match. Nova saying itself what was a mistake was set aside: it would be made up; only what ESPN's commentary records is said.

**Decision.**

1. **Events on every match in an answer.** A match in an answer to a question (never the sensor) carries its `events` once it has started: goals, penalties scored, own goals, penalties missed or saved, and red cards, each with its minute, the side it counts for and the player, in the order they happened. Yellow cards are left out. ESPN's come from the scoreboard's `details`; BBC's from its `actions`, using only the kinds seen in BBC's own data.

   An own goal is listed under the side it counts for, which is the contract's `side`, on both sources: ESPN's detail names that side's team, the scorer being on the other, and BBC lists the action under it. ESPN's kinds are `Goal` and any `Goal - ...`, `Penalty - Scored`, `Own Goal`, `Penalty - Saved` and `Penalty - Missed` (both a penalty missed) and `Red Card`, and a detail whose `redCard` is true is a red card whatever its text, as a sending-off for a second yellow can come with another. BBC's are `Goal`, `Penalty`, `Own Goal`, `Red Card` and `Two Yellow Cards`, which is a sending-off and so a red card; any other type is left out.

   The kick of a shoot-out is never an event. ESPN can list the kicks among a match's details, marked by a `shootout` flag; a shoot-out under way (the scoreboard's period 5) has no score yet; and in a summary's commentary the kicks are clocked from 91' on and carry no flag. So a detail with the flag is left out, and in a match settled on penalties, or with a shoot-out under way, so is a penalty scored or missed which is clocked past 90' ("90'+3'" is not past it). That also loses the rarer penalty of extra time in such a match, which was accepted over saying a kick of the shoot-out as a goal.

   There is no event without a minute. The script leaves out a detail or an action with none, and Nova refuses an answer which has one, as it could not say when it happened; the words are then "I can't get football right now.".

   A match with goals and nothing listed has no `events`, rather than none. The key is left out where the source gave no list, or one which cannot be read, and where it gave an empty list for a match which has goals, as a source which lists none of them has not given its events; an empty list is kept only for a match which has started and has no goals. Nova then says the match's line alone, with nothing of what happened.

   Names are as the source gives them. Nova finds no surname without a list of players, which it does not keep ("Vinícius Júnior", "Son Heung-min"), so it does not shorten a name, and Claude, which says the answer, shortens them as people do.

2. **A sixth question, `match`.** For a team: its match in play; else its match on the day given; else its last. The answer has the match with its events and, for a match ESPN carries, `notes`: ESPN's own commentary sentences, verbatim, for penalties conceded, VAR decisions, penalties saved or missed, the woodwork, own goals and second yellow cards. Nova never says a mistake cost a game unless a note says what happened; Claude may say a goal was a late winner or equaliser from the minutes and the score.

   A `match` answer is stale when its scoreboard read failed, whether an old copy of the scoreboard stood in or none could be had: the match would be an old one, or the team's schedule's, which lists no events and would pass for a match with none. It is "I can't get football right now." instead, as it is for an old copy of ESPN's summary, which the notes come from, standing in. The notes are only for a match ESPN carries which has started (BBC has no summary), and a line of commentary with no minute is left out.

   With no team named, `match` is the followed team whose match is in play; else the one whose match on the day (today by default) is over, the latest to kick off first. A match still to come, or off, is left out, as nothing has happened in it: at six in the evening, with Stevenage's match at three over and Liverpool's at eight not begun, it is Stevenage's. A match from BBC ranks as any other, as BBC's have events. For a day other than today, a team's match is the one in its `now` or its `last` which kicked off on that day, as the sensor has them. A stale team is passed over; where none which is not stale has such a match and one which is stale may, it is "I can't get football right now."; with no team followed, "No teams are followed."; with none having one, "Name a team for its match.". Line-ups keep the order of decision 7 of 0015: the match in play, else one still to come today, then one over today, BBC's after the rest.

3. **Round-ups.** A competition's matches on a day carry their events too, from the same scoreboards, so "what happened in the Premier League yesterday" gives every score with its scorers and red cards.
4. **Nova's words.** The events are said in order, each with its minute and the score after it. `get_football` gains `about: "match"`. Claude is told to say a score as words or as "Spain 2, England 3", never digits with a dash between, which the speech reads as "dash"; Nova's own sentences never use one.

   A score or a formation with a dash between digits is said without one, as the speech reads the dash out whichever it is: a shoot-out as "Liverpool won on penalties, 4 to 3" (the winner's kicks first, however the sides were listed), in an answer and in the announcement of full time, which had said "won 4-3 on penalties"; a score in one of ESPN's notes ("VAR Decision: No Goal France 1-0 Türkiye.") as "1 to 0", with the rest of the note as ESPN wrote it; and a formation in a line-up as "4 2 3 1". The score after each goal is counted in order from 0, 0 and said only where the last is the match's own score, so that a list which is short of a goal, or has one too many, says what happened without a score rather than a false one.

   The tool's words tell Claude to say what happened in a match only from what Home Assistant gave it, as it does the rest, never from memory, and that Home Assistant's football has no yellow cards; to say a mistake cost a game only where one of ESPN's notes says what happened, though a late winner or equaliser may be worked out from the minutes and the score; and to say a score as "Spain 2, England 3" or in words, and never to write digits joined by a dash, a formation as well as a score.

5. **Built and deployed as rounds one and two were:** a branch on each side from what is live (`football-3` from b9c969d, `football-ask-3` from f50aa1c), each task reviewed on Opus, Home Assistant first on the user's named job.

**The contract, round three.** Rounds one and two stand, with these additions, word for word the same in both plans:

- A match in an answer to a question may have `events` (never in the sensor): each `minute` (the source's label, e.g. "45'", "90'+3'"), `kind` (`goal`, `penalty`, `own_goal`, `penalty_missed` or `red_card`), `side` (`home` or `away`: the side a goal, penalty or own goal counts for; the player's own side for a penalty missed or a red card) and `player`? (the name the source gives). In the order they happened.
- `shell_command.football_ask` takes `what` = `match`, with `name` the team and `date` (YYYYMMDD) if a day was asked about. The answer: `team` (as for `lineups`), `match`? (with `events`), `notes`? (each `minute` and `text`, ESPN's sentence), with `stale`, `error` and `unknown` as every answer has them.

**Not in this round.** Old seasons; yellow cards; line-ups of players who came on; commentary beyond the listed kinds; a judgement of mistakes of Nova's own.
