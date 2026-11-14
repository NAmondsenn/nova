# 0014: Football, through Home Assistant

13 November 2026 · Agreed with the user in chat · Built

**Context.** Home Assistant has had football since 12 November 2026, from ESPN's free feed (no key, unofficial, so its shape can change). A script, `football.py`, is run every minute by a command-line sensor in a package, `football.yaml`; both live in the user's own Home Assistant configuration, which is kept in a private repository, not this one. It began as a whole Football view, with a calendar of every league's matches and the tables. On 13 November the user had that taken out - "i just need the small widget" - and what is left is small on purpose: a card on the Home views when one of his teams plays today or tomorrow, and behind it:

- `sensor.football_teams` - the followed teams, each with its match today (`now`), its next three fixtures (`next`) and last three results (`last`), and beside them `live`, `next_kickoff`, `unknown` (names ESPN couldn't find), `stale` and, only when a run failed, `error`;
- `binary_sensor.football_live`, `sensor.football_next_match` and `sensor.football_soon`;
- `input_text.football_teams` - the followed teams, as names separated by commas, which ESPN's search turns into teams. Changing it has the sensor run again at once. No view shows this box any more.

The script now knows followed teams only: today's matches by league, the tables and the extra leagues went with the view, and are in that repository's history.

A match is a dict with `id`, `kickoff` (ISO, UTC), `state` (`pre`, `in` or `post`), `home`, `away`, `home_id`, `away_id`, `league`, `slug` and, only when they exist, `clock`, `home_score` and `away_score`. Fields with nothing in them are left out. Scores are up to a minute behind ESPN. The sensor's attributes are trimmed to stay under the 16 KB Home Assistant's recorder allows. `sensor.football_soon` and the `soon` and `rows` attributes are the card's, so nothing here reads them.

What a look at the live sensors, ESPN's feed and Nova showed on 13 November, all read-only:

- Team names are ESPN's short ones - "Man Utd", "C Palace", "Sheffield Wed" - which suit a dashboard and not a voice.
- A match settled on penalties reads 2-2 with the clock "FT-Pens", and nothing says who won.
- ESPN's feed holds more than the script keeps: each match's ground and its city; bookmakers' odds for some competitions (the Premier League and the Champions League had them, League One and the Nations League didn't); which side won, and a shoot-out's score; a cup's rounds, and which one it's on. Its scoreboard takes a whole month in one request (`dates=YYYYMM`), though never a range of days.
- Writing the teams box with `input_text.set_value` has Home Assistant run the teams sensor again within about five seconds.
- Nova's own calls to Home Assistant go through one allow-list, `SERVICES` in `home_assistant.py`, which has neither `input_text` nor `shell_command`. Home Assistant already has other shell commands, so one more needs a reload, not a restart.
- "Is Liverpool playing?" has the shape Nova hands to Assist first, as a question about a media player.
- Claude's part of a turn takes 1.07 s typically and 2.3 s on a bad day (766 turns on the brain).
- No match was in play, and Home Assistant's history starts after 12 November's matches, so what the feed says at half time and in the second half hasn't been seen yet.

**What the user wants, and why.** "I want to be able to watch the football scores live. Well, listen to them." He wants to ask Nova about football as he'd open a scores app, for any team or competition and not only his own: "what time do Liverpool play tonight?", "what is the Stevenage score right now?", "did Stevenage win?", "who are England playing?", "is anyone playing today?", "where are Liverpool in the table?", "what matches are going on in the Premier League right now?", "what minute is Barcelona versus Real Madrid in?", "what competition is Celtic versus Rangers playing in?", "is the Champions League on next week?", "what teams are left in the Carabao Cup?", "in what stadium are Bayern Munich and PSG playing tonight?", "what are the win and draw probabilities for Liverpool versus Arsenal tonight?". He wants to follow and unfollow a team by voice and on the settings page. And he wants Nova to speak up by itself about his own teams' matches, each kind of announcement switched on or off for each team.

His rules for it: Nova gets football through Home Assistant and never calls ESPN itself; the followed teams are found as Nova runs, never kept in a list of Nova's own; anything he'd change later is on the settings page; and Nova says plainly what it can't know.

**Approaches.** Three ways of getting the data to Nova were looked at.

- *Everything in sensors.* Grounds, odds, a week ahead, tables and cup survivors added as polled sensors, with Nova only reading states. The recorder's 16 KB holds only the followed teams and a few leagues, so "Celtic versus Rangers" and "when do Arsenal play next?" would still fail, more would be fetched every minute for questions asked once a week, and it would grow back what the user has just had cut down.
- *Asked on demand.* One new Home Assistant service runs the same `football.py` with a question - a team, a competition, some days - and gives the answer back. Any team or competition ESPN knows, on any date, with nothing new polled and nothing new shown. Today's sensor stays for the card and for the announcements.
- *Nova asks ESPN itself.* Ruled out by the user, and the football logic would then live in two places.

The user picked the second on 13 November. For the wording, Claude says every answer from what Home Assistant returns, through one tool: the questions are too many and too loosely worded for fixed sentences, and Claude puts right a misheard name ("Stephen JC" for Stevenage). The alternative, local commands with sentences built in code, was dropped for that reason. Announcements are the other way round: fixed sentences built in code, with no Claude.

**Decision.**

1. **Two parts and one contract.** Home Assistant's side grows a little (2 and 3); Nova's side is new (4 to 10). They meet in the contract below, and the names in it are fixed in Nova's code, not settings: `sensor.football_teams`, `input_text.football_teams` and `shell_command.football_ask`. Nova's own football code is three new files: `voice_assistant/football.py` reads Home Assistant, and follows and unfollows; `voice_assistant/football_words.py` turns what was read into words, including `describe`, which the `get_football` tool calls; and `voice_assistant/football_watch.py` is the watcher and the saved switches. The tools are in `actions.py`, the rule that keeps football from Assist in `intents.py`, the calls to Home Assistant in `home_assistant.py`, and the page in `settings_page.py` and `web/`. Nova's side can be built and tested against the contract before Home Assistant's side is live. Home Assistant's side adds no sensor, no view and nothing to a dashboard: the sensor's matches gain fields, and the script gains a command.

2. **Home Assistant: more in each match.** `football.py` adds, wherever it gives a match:
   - `home_name` and `away_name` - the team's full name ("Manchester United"), only where it differs from the short one;
   - `winner` - `home` or `away`, once ESPN says who won; left out for a draw;
   - `shootout` - `[home, away]`, when it went to penalties; and `extra_time: true` when it ended after extra time;
   - `phase` - where a match that has started is: `first_half`, `half_time`, `second_half`, `extra_time`, `penalties` or `full_time`, and `off` for one postponed, abandoned or cancelled. Worked out from ESPN's own status, so that nothing in Nova reads ESPN's words. A status the script doesn't know gives no `phase`.

   And in `sensor.football_teams`:
   - each team carries `asked`, the entry in the teams box it was found by ("South Shields FC" for South Shields), so that unfollowing takes out exactly that entry;
   - each team carries `stale`, `true` when an old copy stood in for any of its own fetches and `false` when none did, so that each team says for itself whether what is shown of it is old. The sensor's own `stale` stays as it is, true when any team's did;
   - a match still in play at midnight stays the team's `now` until it ends, rather than vanishing for the night.

3. **Home Assistant: `shell_command.football_ask`.** It runs `football.py` with a question and returns what the script prints, as the shell command's response. Nothing calls it but Nova, and only when asked something. Finding a league by name, the tables and a league's matches come back from the script's history where they fit; a month's matches come in one request. It takes `what`, `name`, `date` (`YYYYMMDD`, today if left out) and `days` (1 if left out, 31 at most):
   - `what: team` - any team, by name or ESPN id: the team as found (`id`, `name`, `full_name`, `followed`), its match today, its next five fixtures and last five results, and its row in each table it's in (`league`, `group`, `rank`, `of`, `played`, `won`, `drawn`, `lost`, `gd`, `points`);
   - `what: competition` - any league or cup by name: its matches from `date` for `days`, and for a cup its current round's name;
   - `what: table` - a competition's table, every group;
   - `what: left` - who is still in a cup: the current round's name, its matches, and the teams left - the winner of each match of the round that has finished and both sides of each that hasn't, or the winners of the round before while the next hasn't been drawn. For a competition with no rounds it says so.

   A match in these answers also has `venue` (`name` and `city`) and, before kick-off and only where ESPN lists odds, `odds`: `home`, `draw` and `away` as whole percentages which add up to 100, worked out from the bookmaker's prices, and `source`, the bookmaker's name. Every answer has `stale`, as the sensors do, `error` when the run failed, and `unknown` with the name asked for when ESPN found nothing by it.

4. **Asking: one tool, read by Claude.** `get_football` takes a `team`, a `competition`, `about` (`matches`, `table` or `left`), a `date` and `days`, all optional, and Claude answers from what it returns.
   - With no team and no competition it reads the sensor, with no call to the script: each followed team's match today with its score, minute and phase, its next fixture and last result. That answers "what's the score?", "is anyone playing today?" and "did they win?" said of nobody in particular. Matches of teams he doesn't follow need a team or a competition named.
   - With a team or a competition it asks `football_ask`.
   - What Claude is given is text Nova builds, not Home Assistant's JSON: full names, kick-off times already as local days and times, the ground, the odds as percentages with the bookmaker named, who won and how. Claude never works out a time zone.
   - The tool's description names the followed teams, read from Home Assistant as Nova runs, so that a misheard name is put right to one of them. It tells Claude to answer fixtures, scores, results and tables only from what the tool returns, never from what it remembers, and to say so when the tool hasn't got something.
   - Football is never handed to Assist: a request naming a followed team, or with "football" in it, which would otherwise have gone to Assist goes to Claude instead. What Nova's own commands take first is unchanged. A team is named by its short name, its full name or its entry in the teams box, said with its accents or without them. "Is Liverpool playing?", "is the football on?" and "is Stevenage on?" must reach `get_football`.

5. **Saying plainly what it can't know.**
   - "I can't get football right now." when the sensor has an `error`, is unavailable, or was last written by Home Assistant more than the `stale_after` setting ago, five minutes unless changed (its `last_reported`: `stale` stays false if Home Assistant has stopped running the script at all); when, asked of nobody in particular, any followed team is stale; when an answer from `football_ask` is stale, has an error, can't be read or doesn't come in time; and Nova's usual sentences when Home Assistant itself can't be reached.
   - A team is stale when its own `stale` is true, or when the sensor as a whole says `stale` and no team carries the key at all - a Home Assistant side from before teams were marked, which can't tell whose data is old. A sensor which says `stale` while every team says it isn't leaves every team fresh. One team's old data never stops another's from being said, except in the answer about nobody in particular, which is said whole or not at all.
   - A team with no league table and no league fixtures: Nova says that ESPN, where Home Assistant's football comes from, doesn't seem to cover its league, so only its cup ties show. This is how South Shields are answered, worked out from the data each time, with no team named in Nova.
   - Odds are bookmakers' prices turned into percentages, and Nova says so; with none listed, it says ESPN lists no odds for that match.
   - Claude is told that live scores can be up to a minute behind, and says so when asked how fresh a score is - not after every score.
   - Scorers, cards, line-ups and anything else the answer doesn't hold: Nova says Home Assistant's football doesn't have that. "Search the web for..." works as it always has.

6. **Following and unfollowing.** By voice ("follow Arsenal", "unfollow Norway"), through a second tool, `follow_team`, and on the settings page; both go the same way.
   - To follow, Nova first asks `football_ask` for the team, so it knows what ESPN makes of the name. Found nothing: "I couldn't find a team called X.", and nothing is written. Already followed: "You're already following Arsenal." Otherwise it adds a name to the teams box with `input_text.set_value`, waits for `sensor.football_teams` to show the team, and says "You're now following Arsenal.", with the full name ESPN found, so a wrong match is heard at once.
   - To unfollow, Nova finds the followed team meant and takes its `asked` entry out of the box: "You've stopped following Norway."
   - A follow, from the page or by voice, is usually a few seconds and can take up to about a minute, because Home Assistant looks the team up.
   - The box holds 255 characters. A name which wouldn't fit isn't written: "Home Assistant's list of teams is full."
   - `SERVICES` gains exactly two things: `input_text.set_value` for `input_text.football_teams` and nothing else, and `shell_command.football_ask`, whose answer is asked for. What Nova sends as a name is cut down to letters, digits, spaces and a few marks before it goes, and Home Assistant strips quotes again.

7. **Announcements.** A watcher on the brain reads `sensor.football_teams` every 30 seconds and says, for each followed team's match:
   - **Starting** - once, when the match is first seen with no more than the lead time to go, 15 minutes unless changed: "Stevenage play Notts County in 15 minutes." The minutes said are those really left. Not for a match which is `off`.
   - **Goals** - when either side's score has gone up since the last look: "Goal for Stevenage. Notts County 0, Stevenage 1." The opponent's goals are said too, so the score heard is never behind. A score which has gone down is said, once the lower score has been seen on two readings from Home Assistant running (Nova looks more often than Home Assistant writes the sensor, so two looks are often one reading): "Goal ruled out. Notts County 0, Stevenage 0." Until then the higher score stands.
   - **Half time** - when `phase` becomes `half_time`: "Half time. Notts County 0, Stevenage 1."
   - **Second half** - when `phase` becomes `second_half`: "The second half has started. Notts County 0, Stevenage 1."
   - **Full time** - when `phase` becomes `full_time`: "Full time. Notts County 1, Stevenage 2.", with "after extra time" or "Stevenage won 4-3 on penalties" when that's how it ended. Never for a match which is `off`.

   Each is said on the default satellite, after the usual announcement sound, in full names. An announcement goes ahead only if that kind is switched on for that team; it isn't quiet hours (ntfy's, 23:00 to 07:00 unless changed), which are asked before each announcement and not once for the look, as saying several can run into the night; and Home Assistant doesn't say the user is out (if it can't say, it's said). One which isn't said - quiet hours, the user out, the satellite away - is dropped and logged: never held for later, never sent to the phone. When two followed teams play each other it's said once, if either team's switch is on.

   Whether the user is out is asked of Home Assistant's person as the battery watcher asks it for a device which is nobody's: the first person there is.

   What the watcher will not say, because said unprompted a false or repeated announcement is worse than one missed:
   - Anything on a look which goes backwards: an earlier state or phase than the last look showed, or a score known before and now gone. That is taken for an old copy of the match, and the match is left as it was.
   - Anything for a match which is `off` (postponed, cancelled or abandoned). One which comes back from `off` is taken as it stands.
   - Anything for a match which kicked off more than a day ago: it isn't looked at.

   What the watcher has seen and said for each match is kept on the brain, so a restart neither repeats an announcement nor makes up for lost time. A match first seen already in play is taken as it stands, with nothing said for the goals so far. After more than five minutes without a good look at a team - Nova restarted, Home Assistant down, that team's data stale - whatever changed in its match meanwhile isn't announced. A team whose data is stale is passed over on its own; the others carry on. A match it shares with a followed team whose data is fresh is still looked at, as that team has it, and the switches of both teams count.

8. **The settings page gets a Football section.** It follows Batteries, and lists the followed teams, read from Home Assistant, each with five switches - Starting, Goals, Half time, Second half, Full time - and Remove; and a box and a Follow button to add a team. The minutes before kick-off for Starting are not in the section but in the Football group of Settings, with the page's other settings. A name Home Assistant couldn't find is shown with Remove, so a bad entry typed in Home Assistant can be cleared. A team Home Assistant can't read just now, because what it shows of the team is an old copy, says so under its name. When football can't be read, the section says so.

   ```
   Football
     Stevenage    Starting [on]  Goals [on]   Half time [on]  Second half [on]  Full time [on]   Remove
     Liverpool    Starting [on]  Goals [off]  Half time [on]  Second half [on]  Full time [on]   Remove
     Add a team [______________] Follow

   Settings, Football
     Minutes before kick-off [15]
   ```

   A switch changed is kept on the brain for that team, by its ESPN id, and used straight away. A team never changed has everything on. Remove asks nothing first, and an unfollowed team's switches are kept, so that a team followed again has the switches chosen for it. Home Assistant's sensor catches up a few seconds after its box changes, so after a follow or an unfollow the page looks again eight seconds later, as well as at its usual look. With Home Assistant's Football view gone, this section and the voice are where teams are followed.

9. **Defaults.** A new `football` section in the repository's `config.yaml`: how often the watcher looks (30 seconds), how old the sensor may be (300 seconds), and the minutes before kick-off (15), which is also on the page and used straight away. Each has limits, so that every value allowed leaves the announcements working: `check_every` 10 to 60 seconds, `kickoff_minutes` 2 to 120, `stale_after` 120 to 300 seconds. With Home Assistant set up but no football in it, `get_football` says football isn't set up in Home Assistant, and the watcher says nothing.

10. **At the first deploy** Liverpool's Goals switch is set off through the settings page's own API, as the user asked on 13 November ("all notifications on but Liverpool for goals"). It's a choice saved on the brain like any he makes later, never a default in code.

**Goals.**
- Any question in the list above gets a true answer from Home Assistant's data, or a plain "I haven't got that".
- The user hears a goal for one of his teams within about two minutes of it, without asking.
- Teams are followed, unfollowed and switched from the settings page or by voice, with no file edited.

**Not part of this.**
- Scorers, cards, line-ups, commentary and statistics.
- Announcements sent to the phone, or a sound of their own.
- Switching a team's announcements by voice; that's the page's.
- Anything new on Home Assistant's dashboards, or any new sensor there: the user wants only the small card.
- Football on the clock display.
- Local commands for football which answer without Claude.
- A women's side by name: the script's search puts men's sides first, and a women's side is followed by its ESPN id.
- A kind of its own on the AI usage view; football counts under Other.

**Must not happen.**
- A fixture, score, result or table said from Claude's memory, or guessed.
- Old data read out as live.
- The same thing announced twice, a run of announcements catching up after a gap, or an announcement at night.
- A list of teams, leagues or abbreviations kept in Nova.
- Nova calling ESPN.
- Nova able to write any other text box, or run any other shell command.

**Constraints.**
- Everything of Nova's runs on the brain; nothing is added to the satellite.
- Nova's token in Home Assistant is an administrator's, so `SERVICES` is the only guard on what it can call. The two new entries are exact.
- An announcement is one to two minutes behind the match: up to a minute in Home Assistant, up to 30 seconds in Nova, and ESPN's own delay.
- A football answer costs two calls to Claude and, for a named team or competition, one run of the script: about a second or two more than an ordinary answer.
- Home Assistant's football was cut down to the card on 13 November, in the same files this changes. Home Assistant's side starts from that, keeps the card working as it is, and keeps the sensor's attributes under the recorder's 16 KB.
- This repository is public: no hosts, keys or anything of the user's own in the code, the tests or the docs.
- Changing Home Assistant's configuration, and updating and restarting the brain, each need the user's word. Home Assistant goes first: what it adds only adds.
- Tests on the build machine run one suite at a time, under its load limits.

**Left to the builder.**
- How ESPN's statuses map to `phase`, checked against a match in play before anything is built on it; and what `off` covers.
- How who's left is worked out for a two-legged round, checked against one that has finished. Where the feed can't say who went through, the answer names the round's teams and says so.
- How Home Assistant runs the command without a shell, or makes the name safe if it can't.
- How many matches a competition's answer may hold, and what's cut first - never the followed teams' matches.
- What Nova and the page say of a followed team the sensor has dropped to fit the recorder's 16 KB (`trimmed`), which can happen once many are followed.
- How long Nova waits for `football_ask` before saying it can't get football, and how `SERVICES` lets through a command which has nothing to act on.
- The text Claude is given, and the tools' descriptions.
- How the followed teams' names reach the rule which keeps football from Assist, and the tool's description, without asking Home Assistant at every sentence.
- Which name goes in the box when a team is followed - one that finds the same team again, or its ESPN id if none does - and how long Nova waits for the sensor to show it.
- How the Football section looks, in the page's own style, at 390 and 1440 wide.
- Where the watcher's record is kept in the data folder, and when a match is dropped from it.
- Tests for `football.py`'s new parts, which has none today, from saved copies of ESPN's answers.
- The words, beyond those fixed above.

**My calls, not the user's** (for him to check).
- One team's stale data silences that team alone, not all of football. He asked for "can't get football right now" when the sensor is stale; as the script has it, one league failing at ESPN would then stop every team's answers and announcements.
- Scores are said home side first.
- The minute's lag is said when he asks how fresh a score is, not with every score.
- An announcement which isn't said is dropped, and nothing is announced for what happened during a gap of more than five minutes.
- "Goal ruled out" comes under the Goals switch.
- The watcher looks every 30 seconds.
- A question naming nobody is answered from the sensor; one naming a team or a competition goes through `football_ask`, followed team or not.
- Following says the full name ESPN found, rather than asking first.
- The names of Home Assistant's entities and its command are fixed in the code.
- The minutes before kick-off are on the page; how often the watcher looks isn't.
- The words of every announcement but the goal's.

**Tests** fake Home Assistant, as they do now, with matches shaped as the contract has them: each kind of announcement, each reason for staying quiet, a restart mid-match, a gap, two followed teams playing each other, a shoot-out, following a name ESPN can't find. Nothing in the repository names the user's hosts or holds a key.

**Hardware.** None. The brain reads one sensor every 30 seconds, and a football answer is one more call to Claude.

**Order of work.** Two plans: Home Assistant's side (2 and 3), kept with the user's Home Assistant configuration, and Nova's side (4 to 10), here. Home Assistant's is deployed first; then the brain; then the hand checks, which need a match in play.
