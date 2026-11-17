# When to leave, and how long a journey takes

17 November 2026. Agreed in chat with Nathaniel; the calls marked "my call" are the designer's, his to change.

## Why

His words: "it would be cool to be able to ask nova "what time to leave to get to work" and it would tell me the current approx driving time, and say to get to work on time leave at .... and make sure it gives like a small buffer too. if theres heavy traffic or an accident id like to know. then it can ask if I would like a reminder for when to leave." And: "if something like significant happens before the timer, it could tell me". He wants saved places ("my mum's uni work"), driving assumed, walking and cycling on request, "maybe public transport", and the journey worked out from where he is when he asks from a satellite that isn't at home (a MacBook satellite is planned).

Success: he asks once in the morning, hears a leave-by time he can trust, says yes to the reminder, and is told before it rings if traffic has moved it.

## What exists (found 17 Nov)

- **The arrival time is already there.** Nova reads his Google calendars itself (`google_calendar.py`); each calendar but his own is an activity by its name, and he has Work and Uni. `GoogleCalendar.events(first, days, calendar_ids)` gives each event's start as a local datetime. His words: "all my schedule is in google calendar".
- **Home Assistant** (2026.10.2) has one zone, Home, no journey integration, and his person entity with his phone's position. Its Google Maps Travel Time integration has two actions which answer with data: `google_travel_time.get_travel_times` (driving, walking, bicycling; an optional `departure_time`, a time of day such as `08:00:00`) and `google_travel_time.get_transit_times` (public transport; `departure_time` or `arrival_time`). Each takes `config_entry_id`, `origin` and `destination`; an origin or destination is an address, coordinates, or the entity id of a zone, person or device tracker, which Home Assistant turns into a position itself. The answer is `routes`, each with `duration` (seconds), `duration_text`, `static_duration_text` (the time without traffic, as text only), `distance_meters` and `distance_text`. Nothing in it names an incident, a road or, for public transport, a line or a departure.
- **Google's side:** the integration uses the Routes API, needs a key from a Google Cloud project with billing switched on, and has 5,000 free requests a month. The integration's own sensor asks every 10 minutes, about 4,300 a month, unless its polling is switched off.
- **Nova's guard:** every Home Assistant service goes through `HomeAssistant._call_service`, which only calls what is in `SERVICES`, only asks those in `RETURNS` for an answer, and checks each domain's fields. `google_travel_time` is in neither. The voice path gives a Home Assistant call 5 seconds unless told otherwise (football's ask has 20).
- **Asking, then acting on a yes:** a tool's answer can carry `ask_first`, an `Action` which only a plain yes said next runs (`pc.py`, `downloads.py`, `Conversation.handle`). The reply has to end with that one question, and the action's tool is `for_model=False`, so Claude can't run it.
- **Reminders:** `Clock.set_timer(label=..., is_reminder=True, at="YYYY-MM-DD HH:MM", satellite=...)` sets one, kept across restarts in `timers.json` and rung on the satellite it was set from. It returns words, not the reminder's id, and nothing moves a reminder's time: only remove and set again.
- **Speaking unasked:** `football_watch.py` is the pattern: a daemon thread, its state in a `ChoiceStore`, settings read at each use, words said through the brain on the default satellite, dropped in quiet hours or when Home Assistant says he's out. The phone is reached through `Ntfy.send_soon(self.ntfy.phone, ...)`.
- **Where a request came from:** a tool knows its satellite's name (`ActionExecutor.satellite`). A satellite has no place and nothing says one is away from home.
- **Privacy rule already in the code:** what Claude reads goes to Anthropic, so people, zones and trackers are kept from it, and Home Assistant's positions are never rendered.

## Goals

1. **When to leave.** "What time do I leave for work?" (and "to get to uni", "when should I set off for work") says when the next Work event starts, how long the journey takes, and the time to leave by, with the buffer taken off. For example: "Work starts at 9. The drive is 24 minutes, so leave by 8:26." The event is that activity's next timed one starting within the next 24 hours (my call); with none, Nova says so and gives the journey time now.
2. **A time he says wins.** "What time do I leave to be at my mum's for 6?" uses 6, calendar or not. A place with no calendar and no time said gets the journey time and "When do you need to be there?".
3. **How long a journey takes.** "How long to walk to uni?", "how long's the drive to my mum's?", "how long to cycle to the Metrocentre?", "how long to get to work by bus?" say the time and the distance. Driving unless he names another way; public transport gives a time only.
4. **Traffic said.** When the drive is slower than Google's time without traffic by both 5 minutes and a fifth (my call, on the settings page), Nova says so: "Traffic's heavy: that's 12 minutes more than on a clear road."
5. **The reminder.** A leave-by answer ends "Want a reminder?". A plain yes sets a reminder called "Leave for work" for the leave-by time, less a heads-up (5 minutes, my call, on the page), on the satellite he asked from. No offer when the leave-by time is under 2 minutes away or past: Nova says "You need to leave now." instead.
6. **Watching it.** While a reminder for a drive from home waits, Nova asks again every 5 minutes through the 45 minutes before the leave-by time (both on the page). When the leave-by time has moved by 5 minutes or more (on the page) it moves the reminder and says so, on the satellite he asked from and to his phone: "Traffic to work is heavier now. Leave by 8:15, not 8:26." Later as well as earlier (my call), so the reminder is never left at a time Nova no longer believes. Not held back by quiet hours or by his being out (my call): he asked for this reminder, and the phone gets it either way. Cancelling the reminder ends the watch, and so does his leaving (see the builder check, 2).
7. **Places are Home Assistant's zones** (my call; he said "set different locations in Nova"). He adds Work, Uni, his mum's and any other on Home Assistant's map; Nova reads the zones at each ask, so a new one works at once and a removed one is gone. A place is matched by its zone's name as said, and to an activity by the same name through the calendar's own words ("shift" is Work). Anything else he says is passed to Google as he said it ("the Metrocentre").
8. **From where he is.** A satellite at home asks from the Home zone. A satellite marked on the settings page as one that travels with him (my call: a switch beside each satellite, off for all today) asks from his person entity, which Home Assistant turns into his phone's position. "How long to get home?" works from such a satellite. A reminder set from one is set but not watched (see the builder check, 2).
9. **Settings page**, a Journeys group: buffer minutes (10), reminder heads-up minutes (5), minutes between checks (5), minutes before leaving that the checks start (45), minutes of change worth saying (5), the traffic threshold, and the most Google requests in a month (3,000). All take effect at once.
10. **Without Claude.** The main phrases of goals 1 to 3 are local intents, so they answer with Claude out of reach; Claude is offered the same tool for other wordings.

## Non-goals

- Naming an accident, roadworks or a closure. Google through Home Assistant gives only times; this is the one thing he asked for that this design doesn't do. TomTom's routing names incidents on a free key and could be added later for the reason alone.
- Bus numbers, departure times, directions, alternative routes, more than one stop.
- A reminder for every shift without being asked.
- The event's own location as the destination (his events carry one; the zone of the activity's name is used).
- Home Assistant sensors or dashboard cards for journeys.

## Anti-goals

- A leave-by time said with confidence from the wrong event, an event already begun, an all-day event, an old journey time, or a failed lookup. With anything missing Nova says what it couldn't find and gives no time.
- A Google bill. Nova counts its own requests by calendar month and, at the cap, stops asking and says so.
- An address, coordinates or a zone's position in the public repo, in a log line, or in anything Claude reads. Zone and place names in tests and docs are made up.
- A reminder that rings at a time Nova has since replaced, rings twice, or outlives its cancelling.
- Claude setting or moving a leave reminder by itself.

## Approaches

1. **Google Maps through Home Assistant (chosen: "i think we should use google maps").** The best traffic data of the three, every way of travelling, and Home Assistant holds the key and turns zones and his person into positions, so Nova never sees one. Costs: a key with a billing card, and no reason for a delay.
2. **TomTom, called by Nova.** Names incidents, free key with no card, no public transport, thinner data on minor roads, and Nova would hold positions itself. The designer's first pick; he asked how its data compares and chose Google.
3. **Waze through Home Assistant.** No key, driving only, times only, unofficial and sometimes answers with nothing.

## Decided

- **One module, `voice_assistant/journeys.py`:** finds the place, the origin, the arrival time and the journey, and makes the words; a watcher beside it in the football watcher's shape. One tool for Claude and the local intents, whose words are said directly (Claude replies "Okay."), taking the place as said, the way of travelling, and an arrival time if he gave one. A second, `for_model=False`, sets the reminder on the yes.
- **The guard:** `google_travel_time`'s two actions join `SERVICES` and `RETURNS`, untargeted, with their fields listed and checked: an origin or destination is a `zone.` or `person.` entity id Nova found in Home Assistant, or the place's words as he said them, bounded in length and characters as football's names are.
- **Which Google entry:** Nova finds the integration's config entry in Home Assistant at run time; with none it says journeys need Google Maps Travel Time set up in Home Assistant.
- **Live or forecast:** a leave-by time under 45 minutes away uses the journey as it is now. Further off, Nova asks Google for the journey at about the leaving time (`departure_time`), and says "about" before the minutes. The watch always uses now.
- **The clock** gains what the watch needs: the id of a reminder it sets, and moving one to a new time, kept across restarts as reminders are.
- **A lookup gets 10 seconds** on the voice path (my call); a slower or failed one is said as Home Assistant's other failures are.
- **Setup, his or on his named job:** a Google Cloud key with the Routes API and billing; the Google Maps Travel Time integration added in Home Assistant with its sensor's polling switched off; the zones added. The decision record says how, without his places.

## Answers from the builder check (17 Nov, all my calls)

1. **Public transport gets a journey time only** (goal 3): no leave-by time, reminder or watch. Google's answer has no departure, so the start less the duration can be later than the bus leaves, which is the confident wrong time the anti-goals forbid. Asked "what time do I leave for work by bus?", Nova gives the journey time and says it can't say which bus to catch. Walking and cycling get the leave-by time and the reminder but no watch, as their time doesn't change.
2. **What ends the watch.** Only a drive from home is watched. Once Home Assistant has shown him at home since the reminder was set and then shows him away, he has left: the watch ends and the reminder is dropped without a word, logged. While Home Assistant can't say where he is, both stay. A reminder set from a satellite that travels with him is not watched, as his own moving would look like the traffic clearing; this is looked at again when such a satellite exists.
3. **Journey answers are not private.** They hold a place's name as he said it, a time and a distance, never an address or a position, so they go into Claude's history like any other answer, and Claude carries his reply to "When do you need to be there?". The tool's description tells Claude no zone names; a nearest zone's name is only ever said to him.

## Left to the builder

The local intents' exact phrases; how the watch's state is kept (on the reminder or in its own store); how `static_duration_text` is read into minutes; the words for every failure; where the request count is kept; how the travels-with-me switch is stored and drawn.

## Constraints

- Nova's design rules: Home Assistant first, places and devices found at run time, everything he'd change on the settings page, deterministic actions without the model, small commits each with a test that fails on the old code.
- The repo is public: nothing of his places, times or positions in it.
- Lab writes (Home Assistant's integration and zones, deploys, restarts, pushes) need his named job. CT 109 load limits for every test run.
- Hardware: none shapes this. The speech model mishearing a place name is the usual limit; zone names are few, so the nearest zone can be offered.
- British spelling, no em dashes, no emojis; Nova's comment style.
