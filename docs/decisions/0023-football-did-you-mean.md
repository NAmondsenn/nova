# 0023: Football, round seven: "did you mean"

17 November 2026 · Agreed with the user in chat ("yes") · Approved, being built

**Context.** Asked "When did South Shields last play Chorley?", the speech model heard "crawle", then "Corley". Home Assistant answered both truthfully (ESPN's search took "crawle" for Crawley Town, never met; "Corley" is no team), and Claude, given nothing better, guessed Carlisle United and Corby Town. Asked with "Chorley", the 15 August 2026 match is found in a second. Among the teams South Shields have played, Chorley is the closest name to both mishearings ("Corley" 0.92, "crawle" 0.62 by Python's difflib).

**Decision.**

1. **Home Assistant gives the nearest names.** A `match` answer asked with `against` which found no meeting carries `nearest`: up to three names of the team's opponents, from every match the ask looked through (the kept matches, ESPN's seasons read, BBC Sport's pages read), whose names are closest to the other team as it was asked (difflib's ratio of the plain names, "FC" cut, 0.6 or more), closest first, each as the opponent's full name where it has one; never the team found as the other team itself. None close enough: no `nearest`.
2. **Nova has Claude ask.** Nova's not-found lines end "The nearest name among South Shields' opponents is Chorley." (or "names ... are A, B and C."), and the tool tells Claude to ask the user whether they meant one of them rather than guess another team; a "yes" asks again with that name, which the history keeps (decision of 16 Nov's fix: reads are kept).
3. **Not the speech.** The speech model mishearing names is not fixed here; a larger one needs the AI machine's GPU (pve2 has none).

**The contract, round seven.** Rounds one to six stand, with this addition, word for word the same in both plans:

- A `match` answer asked with `against` which found no meeting may carry `nearest`: a list of one to three names (non-empty text), the team's opponents in the matches looked through whose names are closest to the other team as asked, closest first.
