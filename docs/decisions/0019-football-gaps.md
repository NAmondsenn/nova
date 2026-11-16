# 0019: Football, round five: the known gaps

16 November 2026 · Asked for by the user in chat · Approved, being built

**Context.** Rounds one to four are live (decisions 0014 to 0016 and 0018). The gaps parked along the way were listed for the user, and he asked for all of them to be fixed except three: the Women's Super League's name, "what happened in the match?" with no team named just after a match that ended past midnight, and old non-league league matches beyond the BBC Sport pages Home Assistant keeps, which stay as they are.

What a look on 16 November showed, all read-only:

- ESPN's list of competitions calls the European Championship "EURO" (`uefa.euro`), so "Euros" finds nothing, and "the Euro" would. It calls the EFL Cup the "Carabao Cup" (`eng.league_cup`) and nothing else: no name in the list holds "EFL Cup".
- Home Assistant looks a team's odds up for its first two fixtures only, and Nova says "No odds are listed for it." of any fixture without them, the later ones included.
- Two of Home Assistant's readings a minute apart can both carry one old copy of a match from ESPN, so a score that has gone down and is seen again on the next reading can still be the old copy, said as "Goal ruled out".

**Decision.** For each gap:

1. **Competitions by their usual names.** Home Assistant's script also tries a name without a plural "s" ("Euros" finds EURO), and keeps a short list of names ESPN doesn't use at all, each with the competition it means: "EFL Cup" is `eng.league_cup`. Claude is told to name a competition as it is properly called where it knows it.
2. **Odds only where they were looked for.** A match to come which Home Assistant took from a day's scoreboard carries `odds` where ESPN lists them, or `no_odds` where it lists none; a later fixture, never looked up, carries neither, and Nova says nothing of its odds.
3. **A national side not found.** "No match between England and Brazil is among ESPN's results for England.", in place of "is listed in the results ESPN lists".
4. **The same team twice.** Asked against itself under another name ("Liverpool FC" against Liverpool), Nova sees the two ids are one and says "Liverpool and Liverpool FC are the same team." BBC's names lose a closing "FC" or "AFC" before they are compared, as ESPN's search already does, so "Oxford City FC" finds BBC's "Oxford City".
5. **A goal ruled out is waited for.** A lower score is said as a goal ruled out only when a reading two minutes or more after the first one that showed it still shows it; one in between changes nothing.
6. **Two small ones of Home Assistant's.** A match at period 5 is in its shoot-out whatever ESPN's status calls it, so a kick is never a note; and the other team's full name is read inside an ask's ten seconds, not after them.

**The contract, round five.** Rounds one to four stand, with this addition, word for word the same in both plans:

- A match to come in an answer to a question (never the sensor), taken from a day's scoreboard, carries `odds` where ESPN lists them or `no_odds: true` where it lists none; a match from a team's schedule alone carries neither.

**Not in this round.** The Women's Super League's name; a match that ended past midnight with no team named; BBC Sport's older months.
