# 0024: Football, round eight: a called-off match is said

17 November 2026 · Agreed with the user in chat ("yes to both") · Built

**Context.** South Shields v Harborough Town (17 November, 19:45) was postponed. Home Assistant's script dropped the match as soon as BBC Sport marked it, so the card on the Home views lost it, no warning was given (rightly), and asked about South Shields at 20:17 Nova said they weren't playing and named their next match: nothing said it was postponed. Nova already says a match that is off ("off (Postponed)") where Home Assistant gives one; the script never gives one for a followed team's day.

**Decision.**

1. **Home Assistant keeps a called-off match for today and tomorrow.** A followed team's match that is off (postponed, cancelled or abandoned, from ESPN or BBC Sport) and kicks off today or tomorrow (local) stays in the sensor: in the team's `now` (today) or at the head of its `next` (tomorrow), and in the card's rows, with its phase "off" and its clock ESPN's or BBC's word ("Postponed", "Cancelled", "Abandoned"). The card shows the word where a score would go. A called-off match further off, or past, is dropped as now. Asks give the same (a team's answer is built from the same view).
2. **Nova says it.** Asked about the team, Nova says the match is postponed (or cancelled, or abandoned) in plain words. And it announces it once, at the time the 15-minute warning would have come ("South Shields' match against Harborough Town tonight is postponed."), under the same switch as that warning ("starts in 15 minutes"), the same quiet hours and the same rule about the user being home; a match found off only after its kick-off is announced once when first seen, if still today.

**The contract, round eight.** Rounds one to seven stand, with this addition, word for word the same in both plans:

- A followed team's called-off match (phase "off") which kicks off today or tomorrow is in the sensor, as the team's `now` (today) or at the head of its `next` (tomorrow), and in `rows`, with its `clock` the source's word for it ("Postponed", "Cancelled", "Abandoned"); one further off or past is not.
