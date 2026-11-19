# Release checklist

Run through this whenever a branch is put on Nova, before relying on it. It takes about 15 minutes. Jot down roughly what time anything odd happens, so it can be matched against the log.

## Putting a branch on Nova

Almost everything below runs on the brain - the hearing, Claude, the voice, music, timers and Home Assistant - and a satellite only hears the wake word and plays what it's sent. So the branch goes on the brain first, then on each satellite.

On the brain, as your own account (`nova`'s home is closed to your account, so every path is a full one):

```bash
sudo -u nova -H git -C /home/nova/Home-Assistant fetch
sudo -u nova -H git -C /home/nova/Home-Assistant switch <branch>
sudo bash /home/nova/Home-Assistant/update.sh
```

Then on each satellite, from its clone:

```bash
git fetch && git switch <branch> && bash update.sh
```

`update.sh` brings the branch up to date, installs the packages (`requirements-backend.txt` on the brain, `requirements.lock` on a satellite), fetches any new models, rewrites the service file and restarts Nova, then shows the end of the log. The brain's should show `Nova's brain is ready` and `Listening for satellites on port 10400`, and a satellite's `Satellite '<name>' ready, for its brain at ...` and `Listening for 'Hey Nova'...`, with no errors above. To go back, switch to the previous branch or tag and run `bash update.sh --no-pull`, which uses the code as it is - a tag can't be pulled.

## Hearing you

1. In one breath: "Hey Nova what time is it". The whole question is answered, with the time said like "8:15 PM".
2. "Hey Nova", wait for the blip, then "what's the date?"
3. Straight after the blip, quickly: "set a timer for two minutes".
4. With a pause mid-sentence: "Hey Nova, remind me in ten minutes... to check the oven". It shouldn't cut you off at the pause.
5. Quietly, from across the room.
6. With a fan, a tap or instrumental music on: it stops listening about a second after you finish.
7. "Hey Nova", then say nothing. After a few seconds there's a falling blip, and it's done.
7a. Ask something with a two-sentence answer ("how far away is the moon?"): it starts speaking noticeably sooner than it used to, and the second sentence follows without a gap.
7b. The moment a reply finishes, "Hey Nova" again: it's heard first time.
7c. Part way through a long answer, "Hey Nova, stop": it stops talking almost at once - in well under a quarter of a second - and says nothing more. "Hey Nova, what's the date?" part way through another one: it stops, and answers that instead.
8. Talk normally nearby for a few minutes without saying "Hey Nova". Note any false wake-ups.

## Music

With Navidrome (`music.player: navidrome`), and again with Spotify if you still use it:

8a. "Hey Nova, play Fleetwood Mac" - "Playing Fleetwood Mac" is said first, then the music starts. "Play the Rumours album", "play my liked songs", "play some jazz".
9. "Hey Nova, pause the music" - chime, and it pauses.
10. While it plays: "Hey Nova, skip this song".
11. While it plays: "Hey Nova, what time is it?" - the music pauses, the answer, the music comes back. Said at your normal volume from where you'd usually be, "Hey Nova" is heard over the music (echo cancelling, Navidrome only).
12. While it plays: "Hey Nova, turn it down" - the music, not Nova, gets quieter.
13. While it plays, let a reminder go off - the music pauses for it.

With Navidrome, and SoulSync set up (its address in the page's Music section, its key under Keys, MCP on in SoulSync for albums). Pick a song and an album Navidrome hasn't got:

13a. "Hey Nova, download" the song, "by" its artist - a beep and "... requested.", and later another beep and "... has been downloaded." It isn't played.
13b. "Hey Nova, play" something Navidrome hasn't got - "I haven't got ... Do you want me to download it?" Say "thanks": nothing is requested. Ask for it again and say "yes" - it's requested, as in 13a.
13c. "Hey Nova, download" a song Navidrome has - "You've already got ...", and nothing is requested. Then "download the album ..." for one Navidrome has only some tracks of - "... requested.", and later "... has been downloaded." with the rest of it in Navidrome - and for one it has every track of - after a few seconds, a beep and "You've already got ...". If there's an album Navidrome has only a clean version of, "download the album" it and say "yes" to the explicit one - note what SoulSync does.
13d. "Hey Nova, download everything by" an artist - "I only download songs and albums, not a whole artist."
13e. "Hey Nova, download the album ... by ..." - "... requested.", then a beep and "... has been downloaded." or, within 30 minutes, "Only N of the M tracks of ... came down."
13f. "Hey Nova, download" another song, then switch its satellite off before it's done - the outcome arrives on the phone, as a "Music download" notification.

With clean songs set up as well (the README's "Clean songs, found by ear": the music folder shared with the brain, its path on the page, the SoulSync admin profile and its password under Keys). These need a song SoulSync brings clean, so watch the log for `came down clean`, or pick one whose clean edit is common. Each run takes minutes: listening is on the processor, about 35 seconds a song.

13g. A download which lands clean: nothing is said until it's heard. The clean file, and its `.lrc` and `.txt`, are in `.nova-held` in the music folder at the same path, Navidrome stops listing it, and the new copy, heard explicit, is said as "... has been downloaded." The page's Clean songs block lists it under "Clean and swapped". A download which lands explicit is said as "... has been downloaded." half a minute or so after it lands, with nothing in `.nova-held`.
13h. A song which only ever comes down clean (set "Tries to swap a clean song" to 0 to make this likely): "... only came down clean, so I've kept it.", the original copy back at its own path and in Navidrome, each try's copy in `.nova-held`, and the song under "Only comes down clean".
13i. A re-fetch which never lands: set "Minutes to wait for a song to download" to 1 and download a song which lands clean. Once the log says `Asked SoulSync for ... again, try 1`, stop SoulSync's downloading (pause its downloads, or the Soulseek client it fetches through), but leave SoulSync itself running: Nova has had it forget the old copy's row by then, and needs it again to take the request off its wishlist. "... has been downloaded, but it's the clean version, and I couldn't swap it: SoulSync didn't bring another copy in time." The old copy is back at its own path and in Navidrome, the request is off SoulSync's wishlist, and the song is under "Couldn't swap" with the reason. Put the setting and SoulSync's downloading back.
13j. A restart in the middle of a swap: restart the brain after the clean copy is in `.nova-held` and before the new one lands. Its log says it carried the swap on, and the song ends as in 13g or 13i. Nothing is left in `.nova-held` without the page or the log saying where it came from.
13k. On the settings page, under Music, "Check library": the block says "Checking 1 of N: artist - title" and counts up every half minute or so, and nothing is said aloud or sent to the phone. "Stop" ends it after the song it's on and the block says when the last check was. Run it again: only songs which are new or whose file changed are heard. Clean songs go to "Clean and swapped", "Only comes down clean", "Unsure" or "Couldn't swap", each with its reason. Switch Navidrome off part way, for about ten minutes: the block says "Paused: ..." and Check library becomes "Carry on", which goes on from where it was.
13l. The holding folder: the block says "Holding folder: N files waiting for you to empty", N being the songs in `.nova-held` (not their `.lrc` and `.txt`). Empty `.nova-held` by hand, apart from `.ndignore`: it says nothing waits within ten seconds, and Navidrome doesn't list anything from it.
13m. The nightly check: on the settings page, under Music, "Check new songs every night" is on and "Nightly check at" says 05:30. The morning after the release, the log at 05:30 says "The night's library check: Checking the library.", and the Clean songs block shows "Checking 1 of N: artist - title" while it runs, N being only the songs which are new or whose file changed since the last check, or "Last check: today at" once it's done. (A day's check is done once its time has come, so moving the time later in the day starts none until tomorrow: don't test it with a time a few minutes ahead.) Nothing is said aloud or sent to the phone. Once that morning's check has been seen, on a later night before 05:30 (at 03:00, say), set "Nightly check at" to a time over an hour gone (01:00): no check starts, and the log says "The night's library check, at" that time, "is over an hour gone, so the next is tomorrow night's". This uses up that night's check, which isn't made up (once a day's check has run, nothing more is logged that day, so it can't be tried after it). Put it back to 05:30.
13n. The weekly note: with something put on the block's lists since the last note, on a day no note went, set "Weekly note on" to today and "Weekly note at" to two minutes ahead, outside quiet hours. At that minute one ntfy message titled "Clean songs" arrives on the phone, e.g. "This week Nova swapped 1 clean song for an explicit one. See the settings page.", and tapping it opens the settings page. Nothing more comes in the next few minutes, nor after setting "Weekly note at" ten minutes later still. (One due in quiet hours waits until they end, and comes once then.) Put the day and time back to Monday and 09:30.

With Navidrome, a song playing, and playlists of your own to try on (here Gym and Car, made up: use two of yours, or make two first), and SoulSync set up. Check Navidrome's own page after each, as what Nova says is only half of it. Nothing here plays or rings.

34a. While a song plays: "Hey Nova, add this to Gym" - "Added Endless to Gym." (the song's title as Navidrome has it), and it's the last song on Gym in Navidrome, still there after SoulSync's next run. With another song, "add this to Gym and Car" - "Added Endless to Gym and Car." naming both; say a name slightly wrong, such as "gym bangers", and the reply names the playlist it used. Pause the song first: it's still "this". "Add the last song to Car" adds the one before it. With nothing queued: "Nothing's playing."
34b. "Hey Nova, add this to Gym" again - "Endless is already on Gym.", and it's on Gym once. If it has another copy in the library (the single and the album one, a clean and an explicit file), adding that is "already on" too.
34c. "Hey Nova, add this to Jim", with nothing like Jim among your playlists - "You haven't got a playlist called Jim. Want me to make it?" Let the next song start, then say "yes": Jim is made with the song which was playing when you were asked, not the new one. Ask again and say "thanks" instead: nothing is made.
34d. "Hey Nova, add this to Daily Mix 1" (and "add this to my daily mix") - "SoulSync remakes Daily Mix 1, so that wouldn't stay.", and nothing is on it; "take this off Daily Mix 2" says the same, and "add this to New Music" says "I remake New Music every morning, so that wouldn't stay." "Add this to Gym and Daily Mix 1" does Gym and says which it refused. With SoulSync switched off for a minute: "add this to Gym" - "I can't reach SoulSync to check Gym, so I haven't changed it." and nothing is added. If SoulSync still mirrors a playlist (before the deploy's one-off, or one you mirror later): adding says "Added Endless to Gym, and set Gym to append only in SoulSync so it stays." the first time and nothing more after, and "take this off Gym" says "SoulSync copies Gym from Spotify, so it would put Endless back. Stop mirroring it in SoulSync and I can."
34e. "Hey Nova, take this off Gym" - "Took Endless off Gym.", and every copy of it is gone from Gym in Navidrome and stays gone after SoulSync's next run. Again - "Endless isn't on Gym."
34f. "Hey Nova, make a playlist called Chill" - "Made Chill.", empty in Navidrome. Say it again - "You've already got Chill." "Add this to a new playlist called Chill2" - "Made Chill2 with Endless on it." With Chill taken, "add this to a new playlist called Chill" adds the song to it and says so ("You've already got Chill. Added Endless to Chill."), and doesn't make a second one.
34g. "Hey Nova, like this" - "Liked Endless.", and it's starred in Navidrome; again - "You've already liked Endless." "Unlike this" - "Unliked Endless.", and again - "You haven't liked Endless." "Add this to my liked songs" and "take this off my liked songs" do the same.
34h. "Undo that" after each of 34a, 34e, 34f and 34g: after "add this to Gym and Car" the song is off both, said as one reply ("Took Endless back off Gym and Car."); after "take this off Gym" it's back on Gym, at the end ("Put Endless back on Gym."); after making Chill, Chill is deleted ("Deleted Chill."); after a like, it's unliked. Add this to Gym, then "set a timer for ten minutes", then "undo that" - "The last thing I did wasn't a playlist or calendar change, so I've left it." and the song stays on Gym. Add it, ask "what's the weather?", then "undo that" - it's taken off (a question doesn't count). A second "undo that" - "There's nothing to undo." Ten minutes after a change, "undo that" - "That was too long ago to undo." On a second satellite, "undo that" after a change made on the first finds nothing to undo.

## Timers, reminders and alarms

14. A one-minute timer; when it rings, "Hey Nova, stop". It stops straight away, not at the end of its sound - and the log says "Dismissed by voice without asking the model", even if it was heard as something like "bus stop" or "soap".
15. "Hey Nova, remind me in two minutes to drink water" - later, "This is a reminder: drink water". Between its rounds, ask something else: the answer ends "Shall I turn it off?" - "Yes, I don't need it any more" turns it off ("not yet" would leave it).
16. "Hey Nova, remind me at" a time a few minutes ahead - it goes off at that minute.
17. An alarm a couple of minutes ahead - it rings in bursts, getting louder. "Hey Nova, stop" in a gap stops it straight away.
18. "Hey Nova, set a reminder", then answer its question without saying "Hey Nova".

## Everything else

19. "Hey Nova, what's the weather?"
19a. "Hey Nova, turn off the kitchen lights" (whatever yours are called in Home Assistant) - chime, and they go off. "Set the living room lights to 30 percent." "Unlock the front door" - it refuses.
19b. "Hey Nova, what have I got on Thursday?" and "am I busy this weekend?" - read from your Google Calendar, with the times. "What have I got on this week?" - an overview without the times, in about 15 seconds. "When's my next shift?" and "when's my MOT?" (or anything else on it). Anything at midnight is said as "midnight", not "12 AM".
19c. "Hey Nova, remember that my favourite colour is green" - "I'll remember that". "What do you know about me?" includes it; "forget my favourite colour" - and it's gone.
19d. With something playing in the satellite's room - a video on the PC, with HASS.Agent's media player on, say - the brain's log says `Something's playing in the <name> satellite's room (...), so its wake word needs 0.75`, and `Nothing's playing in the <name> satellite's room now, so its wake word needs 0.5` once it stops (unless those settings were changed). While it plays, "Hey Nova, what time is it?" is still answered, and "Hey Nova, stop" still stops. If a line from the video wakes it, it's ignored without a word, and the log says `Taken as speech from what's playing, so ignored`.
20. Unplug the microphone for 5 seconds, plug it back in, wait 10, then "Hey Nova" - it works without a restart.
21. Set a one minute timer, then unplug the Pi's network cable and wait 10 seconds. "Hey Nova" - it says "Sorry, I can't reach my server right now", and nothing new can be set, as it understands nothing without the brain. When the timer is due it rings from the Pi's own copy, and "Hey Nova" stops it. Plug the cable back in, and once it has reconnected "Hey Nova, what time is it?" is answered again.
21a. The internet down, but not the network: take the router's connection to the internet out (its WAN cable), leaving the home network up, and wait 10 seconds. "Hey Nova, set a one minute timer" - it's set straight away. "Hey Nova, how far away is the moon?" - straight away, it says it can't reach the internet. The lights ("turn on the lamp") and Navidrome still work, as they're on the home network. Plug the connection back in.
22. `systemctl --user restart voice-assistant` - it stops within a second or two, rather than hanging for 20.
23. `kill -STOP $(systemctl --user show -p MainPID --value voice-assistant)` - within about a minute, systemd restarts it (`journalctl --user-unit voice-assistant` says "Watchdog timeout").
24a. "Hey Nova, when do Liverpool play next?", "where are Liverpool in the table?", "did Stevenage win?" - each is answered from Home Assistant, with the local day and time.
24b. "Hey Nova, who's left in the Carabao Cup?", "is the Champions League on next week?", "what stadium are Liverpool playing in next?", "what are the odds for Liverpool's next game?" - each is answered, or with a plain "No odds are listed for it." Asked for the odds of Liverpool's next three games, the first two have theirs or that none are listed, and for the third Nova says it doesn't have them, never "No odds are listed for it."
24c. "Hey Nova, follow Arsenal", then "unfollow Arsenal" - "You're now following Arsenal." and "You've stopped following Arsenal.", and the settings page's Football section shows each within a minute.
24d. On the settings page, add a team and remove it. Switch one team's Goals off and on.
24e. With a followed team's match about to start, "... play ... in 15 minutes." is said once, after the announcement sound. During the match, a goal, half time, the second half and full time are each said within about two minutes.
24f. "Hey Nova, is Liverpool playing?" - Nova answers it, and the brain's log shows nothing sent to Assist for it.
24g. With Home Assistant's football sensor made unavailable (or its script renamed - the file `football.py` in Home Assistant's scripts folder, not the shell command's entry) for six minutes: "Hey Nova, what's the score?", naming no team, gets "I can't get football right now."; with the script renamed, "Hey Nova, when do Liverpool play next?" gets it too. And nothing is announced. (With the shell command's entry renamed instead, that question gets "Football isn't set up in Home Assistant.", as Home Assistant no longer has the command.)
25a. "Hey Nova, what's the starting 11 for Liverpool?" within an hour of a Liverpool kick-off: their starting eleven and bench, and the other side's if asked. Earlier in the day: when the match is, and that line-ups usually come about an hour before.
25b. "Hey Nova, when do South Shields play next?" - their next match, whatever the competition, and a league match among their fixtures (from BBC Sport, which names the league itself, e.g. "National League N / S"), not only cup ties.
25c. During a South Shields league match: the card shows the score and the minute, and a goal is announced.
25d. On a day with no followed match today or tomorrow but one within 14 days, the card shows that match. England's next match shows only once it is within 14 days.
26a. "Hey Nova, what happened in the England match on Sunday?" - what happened, in order, each with its minute and, for a goal, the score after it; Anderson's red card; and the penalty conceded by Cubarsí (from one of ESPN's notes). For Sunday 15 November that is Kane's penalty, Konsa, White, Anderson's red card, Oyarzabal's penalty and Williams, ending Spain 2, England 3. Nothing is called a mistake unless a note says what happened. For a later match, its own goals, cards and notes.
26b. "Hey Nova, what happened in the Nations League on Sunday?" - every match of the day is a line with its score and then its scorers, with the minute of each, and its red cards.
26c. A score is said as "Spain 2, England 3" or in words, never with the word "dash", in answers and in the announcement of full time. A shoot-out - ask about a match which went to penalties, or hear one announced - is said "on penalties, 4 to 3", never "4 dash 3".
27a. "Hey Nova, when did Liverpool last play Newcastle?", and "Hey Nova, what happened last time Liverpool played Newcastle?" - each is Newcastle United 2, Liverpool 2, on Sunday 23 August 2026 at St James' Park, with its scorers and the minute of each, said as any match is.
27b. "Hey Nova, what happened in the Liverpool match on the 16th of March 2025?" - the League Cup final, which is further back than Liverpool's last few matches: its score, who was at home, the ground and its scorers, and the year in "on Sunday the 16th of March 2025".
27c. "Hey Nova, when did Liverpool last play ...", naming a team they have not met in five seasons (a lower-league club) - "No match between Liverpool and <the team> is listed in the last 6 seasons.", which is this season and the five before it, and never that they have never played. The other team is named as Home Assistant found it, so a name which finds another team (Rangers found as Stafford Rangers) shows. For a national side ("Hey Nova, when did England last play Brazil?", if they have not met in the results ESPN lists) it is "No match between England and Brazil is among ESPN's results for England.", as its one list has no seasons in it. And "Hey Nova, when did Liverpool last play Liverpool FC?" is "Liverpool and Liverpool FC are the same team.", with nothing of a match.
28a. "Hey Nova, who won the Euros?" - the last European Championship (ESPN's "EURO"), found and said from what Home Assistant has: its final and who won it, never "ESPN found nothing called Euros".
28b. "Hey Nova, who's left in the EFL Cup?" - the Carabao Cup, which Claude names as it is properly called and Home Assistant also knows as the EFL Cup: the teams still in it, as for any cup.
29a. "Hey Nova, what happened in the match?" the morning after a followed team's evening match, or just after midnight: that match, with its goals and cards, as if its team had been named - not "Name a team for its match." From just past fifteen hours after its kick-off (10:46 after one at 19:45) it is "Name a team for its match." again, and a match in play is asked about first.
30a. "Hey Nova, when did South Shields last play Corley?": Nova asks whether you meant Chorley ("Did you mean Chorley?"), and "yes" gives the August match. It does not guess another team, and where Home Assistant gives no nearest name it says how far back it looked, as before.
31a. On a day a followed team's match is postponed (or cancelled): "Hey Nova, is South Shields playing tonight?" (or "when do South Shields play?") says the match is postponed, in plain words, with its time ("South Shields v Harborough Town, National League North, tonight at 7:45 PM, is postponed."), never that the team has no fixtures or isn't playing; and Nova says by itself, once, at the time its 15-minute warning would have come, "South Shields' match against Harborough Town tonight is postponed." (under the Starting switch, not in quiet hours or while you are out), and not again after a restart, with no 15-minute warning, kick-off or "Full time" for it.
32a. With the Google Maps Travel Time integration set up (its sensor's polling off) and zones named Work and Uni: "Hey Nova, what time do I leave for work?" - the Work calendar's next event, the drive, and a time to leave by with the buffer taken off, ending "Want a reminder?"; "yes" sets "Leave for Work" a few minutes before it, and the settings page's timers show it. Then "Hey Nova, how long is the walk to Uni?" gives a time and a distance. "Hey Nova, how long is the drive to my nowhere's?" says to add it as a zone, and asks Google for nothing. On the settings page, the Journeys settings show and save, and the "Travels with me" switch beside each satellite shows and the satellites' row still fits a phone's width. Google's request count for the day matches Nova's use (a few, not the hundreds the integration's own sensor would make).
33a. The settings page as a list, on your phone: it opens on a list in four groups, with a line under every row ("Nothing playing", "2 of 2 connected") and what needs attention, if anything, above it. Scroll down, tap a row: that section opens alone, and the phone's back gesture returns to the list at the place you scrolled to. Change a setting under Football and one under Notifications: the save bar shows "2 changes not saved yet." wherever you are, the changes are still there after going back to the list and into the other section, and one Save keeps both. Search for "mute": Satellites is found with the microphone's switch under it, and a tap on that opens Satellites with the switch's row scrolled to and marked for a moment; clear the box and the list is back. Search for "zzzz": "Nothing on Nova's page matches that." Open the page at its address with `#music` on the end: Music opens; tap the page once (Chrome skips an entry no tap made), then Back goes to the list, not out of the page. On a laptop or any screen 1,180 pixels wide or more, the list sits down the left with the open section beside it, Status when none is chosen, and a tap on another row swaps the section, with one Back from there still returning to the list.
34i. With a throwaway "Dentist" on Thursday at 3 on your own calendar (use dates ahead; the events are throwaways, and nothing is emailed to anyone for any of these): "Hey Nova, cancel the dentist on Thursday" - at once, with no question, "Removed Dentist, Thursday at 3 PM." (the day as adding says it), and it's gone from Google Calendar on your phone and from "what have I got on Thursday?". Again - "I can't find the dentist on your calendar on Thursday." For an event on a calendar Google only lets Nova read: "I can't change that, Google only lets me change your own calendars." For an invite from someone else: "That's an invite from someone else. Change it in Google Calendar." and the event is left.
34j. With two Dentist events on Thursday, at 3 on one calendar and at 5 on another: "Hey Nova, cancel the dentist on Thursday" - nothing is removed, Nova lists both with their times and the calendars' names and asks which one. "The 3 o'clock one", and "cancel the dentist on Thursday at 5", and naming the calendar, each remove that one alone.
34k. "Hey Nova, move the dentist to Friday at 4" - "Moved Dentist to Friday at 4 PM.", the same length as before in Google. "Move the dentist to 5" keeps its day. An all-day event "moved to Friday" stays all day. "Move the dentist to yesterday" - "That day's already gone - which day did you mean?" and it doesn't move.
34l. "Hey Nova, rename the dentist to dentist check-up" - "Renamed Dentist to Dentist check-up, Thursday at 3 PM.", with its time and reminders as they were.
34m. With a throwaway daily "Gym" at 6 for five days, and the third day being Saturday: "Hey Nova, cancel the gym on Saturday" takes off that day only. On a fresh series, "cancel the gym on Saturday, all of them" - "Removed Gym from Saturday on.", the first two days still there, and Google's own editor shows the series ending on Friday. On another fresh one, "move the gym to 7 on Saturday, all of them" - "Moved Gym to 7 PM from Saturday on.", the first two days still at 6 and the rest at 7. "Move the gym to Sunday, all of them" - "I can only change the time for all of them. Move the days in Google Calendar." With a series which began before today, "cancel yesterday's gym, all of them" - "That day's already gone - which day did you mean?", and nothing changes. Delete the throwaway series after.
34n. "Undo that" after each of 34i, 34k, 34l and 34m, and after "put the dentist on Thursday at 3": the event comes back with the same reminders ("Put Dentist back, Thursday at 3 PM."; check in Google that it's the same event and not a copy, and for one of a repeating series that it keeps repeating), the old time returns ("Moved Dentist back to Thursday at 3 PM."), the old name returns ("Renamed it back to Dentist."), the old series is whole and the new one is gone ("Put Gym back from Saturday on."), and the added one goes ("Took Dentist back off your calendar."). Remove the dentist, then "add milk to my shopping list", then "undo that" - "The last thing I did wasn't a playlist or calendar change, so I've left it." and the dentist stays removed; remove it, ask "what have I got on Thursday?", then "undo that" - it comes back. Move the dentist by voice, delete it in Google Calendar on your phone, then "undo that" - "Dentist was already gone.", and nothing else is sent.

## The manual scripts

`scripts/manual/` has four checks which need real hardware or accounts, so nothing runs them for you. None is needed for every release; run one when what it checks has changed. Each is run from the clone with `venv/bin/python`, and the brain's two as the brain's user (with `runuser -u nova -- ` in front, as the README says).

On a satellite:

- `scripts/manual/check_audio.py` - lists the sound devices, records until you stop talking, saves it as `test_audio.wav` and plays it back, through the same microphone and speaker Nova uses. For a new microphone or speaker, or a new `/etc/asound.conf`.
- `scripts/manual/check_wake_word.py` - counts the wake word's detections until Ctrl+C, and `--sensitivity 0.6`, say, tries another confidence for the run without changing any setting. For tuning `thresholds.wake_word_confidence`.

On the brain:

- `scripts/manual/check_actions.py` - runs sample actions through the ActionExecutor without Claude, and says whether each came back as an ActionResult. Music plays for real if Spotify is signed in.
- `scripts/manual/check_integration.py` - runs a few sample queries through Claude, the actions they ask for and Piper, and says PASS or FAIL for each. It needs the Claude key in `~/.config/nova/.env`, Spotify signed in for the music query, and Piper's models for the speech, which it writes as `test_integration_N.wav` in the folder it's run from. For a change to Claude's tools or the actions.

## Leaving it running

For a soak test, leave it running as normal for a few days, with one monitor on each machine, and use Nova as you would anyway. Neither machine has `tmux`, so each monitor is started detached from the terminal, and goes on after you log out.

Do the brain's own checks first, before the monitors start, so they don't show as restarts or a changed process in the readings. On the brain, as your own account:

- Restart it, and check it comes back as active:

  ```bash
  sudo systemctl restart nova-backend
  sudo systemctl status nova-backend
  ```

- Check the satellite rings from its copy while the brain is stopped. Set a one-minute timer, and about 20 seconds before it's due, stop the brain's process. It runs as `nova`, so it's signalled as `nova`:

  ```bash
  pid=$(systemctl show -p MainPID --value nova-backend)
  sudo -u nova kill -STOP "$pid"
  ```

  The Pi rings it from its copy (its log says "Lost the brain" after 15 seconds of silence). 30 seconds after the stop, let it carry on, in the same shell:

  ```bash
  sudo -u nova kill -CONT "$pid"
  ```

  Never leave it stopped for longer: `nova-backend.service` has `WatchdogSec=60`, so a minute's stop is a restart. Once it carries on, the brain finds the timer due and rings it again, through the Pi - nobody had answered it. One "Hey Nova, stop" ends it everywhere.

Then start the monitors. Each one writes `soak.csv` afresh, so copy an older one away first if you want to keep it.

On the Pi, from the repo:

```bash
nohup venv/bin/python scripts/soak_monitor.py --hours 72 > ~/soak-pi.out 2>&1 &
```

On the brain, as `nova` with its own home and its venv's Python. `nova`'s home is closed to your account, so every path is a full one, and `setsid -f` detaches it from the terminal:

```bash
sudo -u nova -H setsid -f /home/nova/Home-Assistant/venv/bin/python /home/nova/Home-Assistant/scripts/soak_monitor.py --hours 72 --brain < /dev/null > ~/soak-brain.out 2>&1
```

The brain's monitor refuses to start under any other home, since it would find an empty logs folder and report no errors. Neither counts errors logged before it started.

Before leaving them, log out and back in, and check each is still going - `pgrep -af soak_monitor` on the Pi, and on the brain:

```bash
pgrep -u nova -af soak_monitor
sudo -u nova tail -2 /home/nova/.local/share/nova/logs/soak.csv
```

The last line of `soak.csv` is the latest reading, and a new one comes every 10 minutes.

Each monitor samples its service's memory, CPU, open files, restarts, errors and log size every 10 minutes into `soak.csv` in its logs folder (`~/.local/share/nova/logs` on the Pi, `/home/nova/.local/share/nova/logs` on the brain), and says at the end whether anything grew that shouldn't have - in `~/soak-pi.out` or `~/soak-brain.out`, in your own home. Afterwards, the speaker shouldn't have run dry, nor the microphone overflowed - this should find nothing:

```bash
journalctl --user-unit voice-assistant | grep -E "ran dry|overflowed"
```
