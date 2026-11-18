# 0025: When to leave, and how long a journey takes

17 November 2026 · Agreed with the user in chat · Built

**Context.** The user's words: "it would be cool to be able to ask nova 'what time to leave to get to work' and it would tell me the current approx driving time, and say to get to work on time leave at .... and make sure it gives like a small buffer too. if theres heavy traffic or an accident id like to know. then it can ask if I would like a reminder for when to leave." And: "if something like significant happens before the timer, it could tell me". He wants saved places, driving assumed, walking and cycling on request, "maybe public transport", and the journey worked out from where he is when he asks from a satellite that isn't at home (a MacBook satellite is planned). On the source: "i think we should use google maps".

What a look on 17 November showed, all read-only:

- **The arrival time was already there.** Nova reads his Google calendars itself; each calendar but his own is an activity by its name (Work, Uni), and `GoogleCalendar.events` gives each event's start. "all my schedule is in google calendar".
- **Home Assistant** (2026.10.2) had one zone, Home, no journey integration, and his person entity. Its Google Maps Travel Time integration has two actions which answer with data: `google_travel_time.get_travel_times` (driving, walking, bicycling, with an optional `departure_time`) and `google_travel_time.get_transit_times`. An origin or destination is an address, coordinates, or the entity id of a zone, person or device tracker, which Home Assistant turns into a position itself. The answer is `routes`, each with `duration` (seconds), `static_duration_text` (the time without traffic, as text only), `distance_meters` and texts. Nothing in it names an incident or a road or, for public transport, a line or a departure.
- **Google's side:** the integration uses the Routes API, needs a key from a Google Cloud project with billing switched on, and has 5,000 free requests a month. The integration's own sensor asks every 10 minutes, about 4,300 a month, unless its polling is switched off.
- **Nova's guard:** every Home Assistant service goes through `HomeAssistant._call_service`, which only calls what is in `SERVICES`, only asks those in `RETURNS` for an answer, and checks each domain's fields. `google_travel_time` was in neither.
- **Asking, then acting on a yes:** a tool's answer can carry `ask_first`, an `Action` which only a plain yes said next runs, and whose tool is `for_model=False`, so Claude can't run it.
- **Reminders:** the clock sets one, keeps it across restarts and rings it on the satellite it was set from, but nothing moved a reminder's time.
- **Speaking unasked:** `football_watch.py` is the pattern: a daemon thread, its state in a `ChoiceStore`, settings read at each use.
- **Privacy rule already in the code:** what Claude reads goes to Anthropic, so people, zones and trackers are kept from it, and Home Assistant's positions are never rendered.

**What the user wants, and why.** To ask once in the morning, hear a leave-by time he can trust, say yes to the reminder, and be told before it rings if the traffic has moved it. A time he can trust means one made from the right event, the journey as it is, and nothing said when something is missing.

**Approaches.**

1. **Google Maps through Home Assistant (chosen).** The best traffic data of the three, every way of travelling, and Home Assistant holds the key and turns zones and his person into positions, so Nova never sees one. Costs: a key with a billing card, and no reason for a delay.
2. **TomTom, called by Nova.** Names incidents, free key with no card, no public transport, thinner data on minor roads, and Nova would hold positions itself. The designer's first pick; he asked how its data compares and chose Google.
3. **Waze through Home Assistant.** No key, driving only, times only, unofficial and sometimes answers with nothing.

**Decision.**

1. **The source is Google Maps, through Home Assistant, never directly.** `google_travel_time`'s two actions join `SERVICES` and `RETURNS`, untargeted, with their fields listed and checked: an origin or destination is a `zone.` or `person.` entity id, or a place's words cut down by `journey_place` (letters, digits, spaces and `, . ' & / -`, at most 80 characters). Any other entity's id sent as a place is refused, whatever its case, so a light's name never goes to Google as a place. Nova finds the integration's config entry in Home Assistant at run time (kept five minutes); with none it says "Journeys need Google Maps Travel Time set up in Home Assistant." A lookup gets 10 seconds on the voice path.
2. **Places are Home Assistant's zones.** He adds Work, Uni, his gran's and any other on Home Assistant's map; Nova reads the zones at each ask, so a new one works at once and a removed one is gone. A place is matched by its zone's name as said, or by the calendar's own word for the activity a zone is named as ("shift" finds the zone named Work, "lectures" the one named Uni), a zone's own name winning over a word; and the place's activity is then found by the same name through the calendar's words. Anything else he says, a place's name, an address or a postcode, goes to Google as he said it ("the Metrocentre"), and Nova says the place back as it heard it ("The drive to the Metrocentre is 15 minutes, 6 miles."), so a mishearing is caught by ear.
3. **The arrival time is his calendar's, or the one he says.** "What time do I leave for work?" finds the place's activity and its next timed event which begins within the next 24 hours; an event in progress or all day is not it. "Work starts at 9:00 AM. The drive is 24 minutes, so leave by 8:26 AM." The buffer (10 minutes) is taken off. A time he says ("to be at my gran's for 6") wins, calendar or not. With no event Nova says so and gives the journey time now; a place with no calendar and no time said gets the journey time and "When do you need to be there?". With anything else missing (the calendar unreadable, a time not understood, a time already gone) Nova says what it couldn't find and gives no leave-by time.
4. **The words.** Driving is assumed; walking, cycling and public transport on request. "How long to walk to uni?" says the time and the distance (in miles; kilometres on the settings page). When a drive is over Google's time without traffic by both 5 minutes and a fifth, Nova says "Traffic's heavy: that's 12 minutes more than on a clear road." A drive whose leave-by time is under 45 minutes away uses the journey as it is now; one 45 minutes or more away asks Google for the journey at about the time of leaving (`departure_time`) and says "about" before the minutes, and says nothing of the traffic as it is now. A walk's or a cycle's leave-by time is always from the journey as it is now. A leave-by time under 2 minutes away or past is "you need to leave now" with no offer. Public transport gets a journey time only: Google's answer has no departure, so the arrival less the duration can be later than the bus leaves, which is the confident wrong time this must not give. "What time do I leave for work by bus?" gives the time and says it can't say which one to catch.
5. **The reminder and the watch.** A leave-by answer ends "Want a reminder?", and only a plain yes said next runs the tool (`set_leave_reminder`, `for_model=False`) which sets "Leave for Work" for the leave-by time less a heads-up (5 minutes), on the satellite he asked from. A drive from home is then watched by `JourneyWatcher`: from 45 minutes before the leave-by time it asks again every 5 minutes (a pass of the thread every minute), and when the leave-by time has moved by 5 minutes or more it moves the reminder and says so, on the satellite and to his phone: "Traffic to Work is heavier now. Leave by 8:15 AM, not 8:26 AM." Later as well ("has eased"), so the reminder is never left at a time Nova no longer believes; a move that leaves under 2 minutes drops the reminder and says "You need to leave now." Not held back by quiet hours or by his being out: he asked for this reminder. Cancelling or ringing the reminder ends the watch. Once Home Assistant has shown him at home since the reminder was set, and then shows him away, he has left: the reminder is dropped without a word and logged by its id. While Home Assistant can't say where he is, both stay. Only a drive from home is watched: a walk or a cycle doesn't change, and a reminder set from a satellite that travels with him is not watched, as his own moving would look like the traffic clearing.
6. **Where from.** A satellite at home asks from the Home zone. A satellite marked on the settings page as one that travels with him (a switch beside each satellite, off for all) asks from his person entity, which Home Assistant turns into his phone's position: "how long to get home?" works from it. Nova never reads, holds or logs the position.
7. **The cap.** Nova counts its own asks of Google by calendar month (in `journeys.json`, counted before each ask, so one that fails still counts) and at `journeys.monthly_requests` (3,000 by default, against Google's 5,000 free) stops asking and says "I've asked Google for all the journeys I'm allowed this month." Home Assistant's own sensor, if its polling is left on, would use most of the free requests alone, so setup switches it off.
8. **Settings page, a Journeys group,** all used at the next ask: buffer minutes, reminder heads-up, minutes between checks, minutes before leaving that the checks start, minutes of change worth saying, the traffic threshold (minutes and percent), the most requests a month, and the unit distances are said in. And the travels-with-me switch beside each satellite.
9. **Privacy.** The journey tool's description names no zone, and nothing journey-related is `reads=True`. A journey's answer holds a place's name as he said it, a time and a distance, never an address or a position, so it goes into Claude's history like any answer, and Claude carries his reply to "When do you need to be there?". No log line holds a place: a failure logs its kind, a refused journey's log line names the fields given and not the places, and the clock's `move_timer` and `drop_timer` log a timer's id (and `move_timer` its new time) and no label, as a reminder's label is a place.
10. **Without Claude.** The questions heard locally are the "leave for" ones ("what time do I leave for work?", "when should I set off to get to uni?") and the "how long" ones which name a way of travelling ("how long is the drive to work?", "how long does it take to walk to uni?", "how long to get to the station by bus?"). "How long to get to X" with no way named, a said time, a day or any qualifier after the place ("for work tomorrow", "from here", "this afternoon") goes to Claude, who has the same tool, can take the arrival time and the day, and can tell work from "how long to get to sleep?".
11. **What isn't done.**
    - Naming an accident or roadworks: Google through Home Assistant gives only times. This is the one thing he asked for that this doesn't do. TomTom's routing names incidents on a free key and could be added later for that reason alone.
    - A leave-by time for public transport (above).
    - Watching a reminder set from a satellite that travels with him, or one for a walk or a cycle (above). The first is looked at again when such a satellite exists.
    - The event's own location as the destination: the zone of the activity's name is used.
    - A forecast for a day beyond tomorrow uses that time of day's next occurrence.
    - Bus numbers, departure times, directions, alternative routes and more than one stop; a reminder for every shift without being asked; sensors or cards for journeys in Home Assistant.

**Goals.**
- He asks "what time do I leave for work?" and hears the start, the drive and a time he can act on.
- He says yes and the reminder rings at the time, or is moved, with a word, if the traffic moves it.
- A place he adds in Home Assistant works at once.

**Must not happen.**
- A leave-by time said with confidence from the wrong event, an event already begun, an all-day event, an old journey time or a failed lookup.
- A Google bill: Nova stops itself at the cap.
- An address, coordinates or a zone's position in the repository, a log line, or anything Claude reads.
- A reminder which rings at a time Nova has since replaced, rings twice, or outlives its cancelling.
- Claude setting or moving a leave reminder by itself.

**Constraints.** Nova's design rules: Home Assistant first, places found at run time, everything he'd change on the settings page, deterministic actions without the model. The repository is public, so every place in tests and documents is made up (Work, Uni, Gran's, the Metrocentre).

**Setup.** Done by the user, or on his named job, never by Nova:

1. A Google Cloud project with the Routes API switched on and billing enabled (Google gives 5,000 free requests a month), and a key.
2. In Home Assistant, the Google Maps Travel Time integration added with that key, and its sensor's polling switched off (the integration's System options): left on, it asks every 10 minutes, about 4,300 a month. Its setup also asks for an origin and a destination; any will do, such as `zone.home` for both, as Nova gives its own each time it asks.
3. Zones in Home Assistant for the places, named as the calendars are for work and uni, so that "what time do I leave for work?" finds both the zone and the calendar.

README's "Journeys" says the same.

**My calls, not the user's** (for him to check). The design's own:
- Home Assistant's zones are the places, and so are not set in Nova itself ("set different locations in Nova").
- The event is the activity's next timed one within 24 hours.
- The buffer is 10 minutes, the reminder's heads-up 5, the traffic threshold 5 minutes and a fifth, the watch 45 minutes before the leave-by time every 5 minutes, a change worth saying 5 minutes, and the cap 3,000 a month; all on the settings page.
- "About" for a leave-by time 45 minutes or more away.
- A move later as well as earlier, said even in quiet hours and when he is out.
- A lookup gets 10 seconds.
- Public transport gets no leave-by time; only a drive from home is watched; the watch ends when he leaves.
- The travels-with-me switch, off for every satellite.

Made during the build, which the design didn't have:
- A place beginning "my " which matches no zone is refused ("I don't know where my gran's is. Add it as a zone in Home Assistant.") instead of being searched for, so a personal place is never guessed at: a mishearing sent to Google would be said back as a confident drive to the wrong place.
- A place said by the calendar's word for an activity ("my shift", "lectures") finds the zone named as that activity, after a zone's own name has been tried and found none; with no such zone, or no readable calendar, it is as any other place.
- Zones are matched whatever the apostrophe did: "my grans" finds "Gran's", and a curly apostrophe in a zone's name, which a phone's keyboard puts there, is the same as a straight one.
- The questions heard without Claude are the ones in 10 above, and no more.
- Home Assistant's guard refuses any other entity id as a place, in any case.
- A refused journey's log line names the fields given, never the places.
- The clock gained `move_timer` and `drop_timer`, which log a timer's id (and `move_timer` its new time) and never its label.
- A reminder whose time has come is never moved: it is about to ring, and the next look forgets it.
- The watcher's leave-by arithmetic holds across a clock change (an aware time, as the ask's is).
- A distance is said in miles, with a setting for kilometres; the design had no unit.
- A reminder's ring time is rounded up to the minute and is at least a minute ahead, so it never rings before the minute it says.

**Tests** fake Home Assistant and the calendar, with a clock the tests set: each answer and each reason for refusing one, the apostrophe, the place begun "my ", the cap across a month's end, the forecast, the reminder and a yes, each of the watcher's moves and each reason for ending it, a clock change, a restart mid-watch, and the guard's refusals. Nothing in the repository names a real place or holds a key.

**Hardware.** None shapes this: a lookup is one call to Home Assistant and a pass of the watcher is a read of a file. A misheard place name is the speech model's, which pve2's lack of a GPU limits; zone names are few and a mishearing is said back, so it is caught by ear.
