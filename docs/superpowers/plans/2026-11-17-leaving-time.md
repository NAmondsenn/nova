# Leaving Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nova answers "what time do I leave for work?" and "how long to drive to X?" from Home Assistant's Google Maps actions and the user's calendar, offers a reminder, and moves it if the traffic changes.

**Architecture:** One new module, `voice_assistant/journeys.py`, owned by the brain and shared by every session as `PCPower` is: it finds the place (a Home Assistant zone, else the words as said), the origin, the arrival time (the calendar activity of the place's name, or a time said) and the journey, and makes the words. Home Assistant is reached only through two new methods on `HomeAssistant`, behind the existing service guard. A `JourneyWatcher` thread in the football watcher's shape rechecks watched reminders through the clock, which gains an id and a move.

**Tech Stack:** Python 3 as the repo has it, `requests`, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-17-leaving-time-design.md`. Read it first; this plan argues from it.

## Contract

What Home Assistant answers, found live on 17 Nov 2026 (places made up here):

- `POST /api/services/google_travel_time/get_travel_times?return_response` with `{"config_entry_id": "...", "origin": "zone.home", "destination": "zone.work"}` answers in about 0.3 s: `{"changed_states": [], "service_response": {"routes": [{"duration": 1053, "duration_text": "18 mins", "static_duration_text": "20 mins", "distance_meters": 14027, "distance_text": "14.0 km"}]}}`.
- `"mode"` is `driving` (default), `walking` or `bicycling`; any other is HTTP 400. `"departure_time": "08:00:00"` gives the forecast for that time of day (`duration` 1562 against 1053 now, the same `static_duration_text`).
- `static_duration_text` can be more than the live time ("20 mins" against 18 live), and for longer journeys reads like "1 hour 5 mins".
- An origin of `person.<id>` works and is the person's position. A destination in plain words ("Metrocentre, Gateshead") works. A place Google can't find answers `{"routes": []}`.
- `get_transit_times` takes `config_entry_id`, `origin`, `destination` and answers in the same shape.
- The entry id comes from the websocket: `{"type": "config_entries/get", "domain": "google_travel_time"}` gives a list of entries, each with `entry_id` and `state` (`"loaded"` when usable).

## Global Constraints

- British spelling, no em dashes, no emojis. Comments and docstrings in the surrounding files' style (full sentences, Args/Returns/Raises).
- The repo is public. No real place, address, postcode, coordinate, person's name or calendar detail in code, tests, docs, commit messages or log lines. Test places are "Work", "Uni", "Gran's" and "the Metrocentre".
- Nova never reads, holds or logs a position. Zones are known by entity id and name only.
- Nothing journey-related is `reads=True` or told to Claude beyond the tool's own description; no zone names in any description.
- Every setting the spec names is on the settings page and read at each use.
- One commit per task, each with tests that fail on the old code. Commit messages in the repo's style (a plain sentence, then a short body), ending `Co-Authored-By: Claude <noreply@anthropic.com>`.
- No `git stash`, no pushes, no deploys, nothing written to Home Assistant or the brain.
- CT 109 load limits: check `cat /proc/loadavg` first and wait if the first number is above 3; one test run at a time; no repeat runs; never run pytest on CT 109 itself.

## Review Focus

1. The calendar event is tomorrow, or began ten minutes ago, or is all day: the leave-by time must come from the next timed event not yet begun, with "tomorrow" said, never from the one in progress (Task 3).
2. Home Assistant answers `{"routes": []}`, or a route with `duration` missing or not a number: Nova says it couldn't find the place or get a journey time, and never gives a time (Tasks 1 and 3).
3. The month turns over, or the request count file is hand-edited into nonsense: the count starts again at 0 for the new month and a bad file reads as 0, not as a crash or an unlimited month (Task 3).
4. The reminder is cancelled by voice, rings, or is removed on the settings page while watched: the watch forgets it at its next look and says nothing more (Task 6).
5. The brain restarts with a watched reminder waiting: the watch carries on from `journeys.json`, and a reminder that came due while the brain was off is not moved or announced as changed (Task 6).

## Test commands

- One file: `NOVA_TREE=$PWD NOVA_CHECK=journeys nice -n 19 timeout 600 bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh tests/test_journeys.py` from the worktree `/home/nathaniel/work/Home-Assistant-journeys`.
- Whole suite, London then UTC (only where a task says so): the same without a file, then again with `TZ_OVERRIDE=UTC` in front.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/journeys && /tmp/nova-check-pytest/bin/ruff check .'` after a test run has copied the tree.
- JavaScript: `node --max-old-space-size=768 --test --test-concurrency=1 tests/web/` only if a task adds a web test.

---

### Task 1: Home Assistant: zones and the journey ask, behind the guard

**Files:**
- Modify: `voice_assistant/home_assistant.py` (the `SERVICES` block near line 109, `_call_service` near line 1658, new methods beside `football_ask`)
- Test: `tests/test_home_assistant.py`

**Interfaces:**
- Produces:
  - `JOURNEY_MODES = ("driving", "walking", "bicycling", "transit")`
  - `JOURNEY_TIMEOUT = 10`
  - `class NoJourneys(ValueError)`: Home Assistant has no loaded Google Maps Travel Time entry.
  - `journey_place(words: str) -> str`: the words cut down to letters of any alphabet, digits, spaces and `, . ' & / -`, one space at a time, at most 80 characters; `""` if nothing is left. Modelled on `football_name`.
  - `HomeAssistant.zones(timeout: Optional[float] = None) -> List[Dict]`: `[{"id": "zone.work", "name": "Work"}, ...]` from `/api/states`, the name being `friendly_name` else the id's last part; nothing else of a zone is kept.
  - `HomeAssistant.journey(origin: str, destination: str, mode: str = "driving", departure: Optional[str] = None) -> Optional[Dict]`: `{"seconds": int, "clear_seconds": Optional[int], "metres": int}` for the first route, or `None` when `routes` is empty. `departure` is `"HH:MM"` and only goes with `driving`. `transit` calls `get_transit_times`; the rest call `get_travel_times` with `mode`. Raises `NoJourneys`, `NotAllowed`, `ValueError` (an answer of the wrong shape, or a `duration` or `distance_meters` which isn't a number of 0 or more), `requests.RequestException`.

- [ ] **Step 1: Write the failing tests** in `tests/test_home_assistant.py`, using the file's `make_home` fixture and the Contract's shapes:

  - `test_a_journey_is_asked_through_the_allowlist`: with the websocket's entries faked as one loaded entry `"entry1"` and the Contract's driving answer, `home.journey("zone.home", "zone.work")` is `{"seconds": 1053, "clear_seconds": 1200, "metres": 14027}` and the one request is `POST /api/services/google_travel_time/get_travel_times?return_response` with json `{"config_entry_id": "entry1", "origin": "zone.home", "destination": "zone.work", "mode": "driving"}`.
  - `test_a_journey_at_a_time_of_day_asks_for_it`: `departure="08:05"` sends `"departure_time": "08:05:00"`; with `mode="walking"` no `departure_time` is sent.
  - `test_public_transport_asks_the_transit_action`: `mode="transit"` posts to `get_transit_times` with only `config_entry_id`, `origin`, `destination`.
  - `test_no_route_is_none_and_a_bad_answer_raises`: `{"routes": []}` gives `None`; a route with `"duration": "soon"`, one with no `distance_meters`, and a `service_response` which isn't a dict each raise `ValueError`.
  - `test_the_time_on_a_clear_road_is_read_from_its_words`: parametrised `"20 mins" -> 1200`, `"1 min" -> 60`, `"1 hour 5 mins" -> 3900`, `"2 hours" -> 7200`, `"soon" -> None`, missing `-> None`.
  - `test_without_a_loaded_entry_there_are_no_journeys`: no entries, and one whose `state` is `"setup_error"`, each raise `NoJourneys`, with no service request made.
  - Add to the parametrised `test_only_the_services_nova_calls_are_ever_called`: `("google_travel_time", "get_travel_times", {"config_entry_id": "e", "origin": "zone.home", "destination": "zone.work", "entity_id": "zone.work"})`, the same with `"destination": "light.lamp"`, with `"destination": "x" * 81`, with `"mode": "flying"`, with `"departure_time": "now"`, with an extra `"units": "imperial; rm"` key spelt `"command"`, and `("google_travel_time", "reload", {})`.
  - Add to `test_an_answer_is_only_asked_of_what_both_lists_allow`: `get_travel_times` with good data and `returns=False`.
  - `test_zones_are_names_and_ids_only`: states holding two zones with `latitude`, `longitude` and `radius` attributes give exactly `[{"id": "zone.home", "name": "Home"}, {"id": "zone.work", "name": "Work"}]`.
  - `test_a_place_is_cut_down_to_what_may_be_asked`: `journey_place("  the  Metrocentre; rm -rf ")` is `"the Metrocentre rm -rf"`; a string of 200 letters comes back 80 long; `journey_place("{}")` is `""`.

- [ ] **Step 2: Run them and see them fail** (`tests/test_home_assistant.py`); expected: the new tests fail on names that don't exist.

- [ ] **Step 3: Implement.** `SERVICES["google_travel_time"] = ("get_travel_times", "get_transit_times")`, both added to `RETURNS`, the domain added to `UNTARGETED`. In `_call_service`'s refusal, one more clause for the domain: the keys are within `{"config_entry_id", "origin", "destination", "mode", "departure_time"}` (transit: the first three only); `config_entry_id` fully matches `[0-9A-Za-z_-]{1,64}`; `origin` and `destination` are each a `zone.` or `person.` entity id (`(zone|person)\.[a-z0-9_]+`) or equal to their own `journey_place()` and not empty; `mode` is one of the three driving, walking, bicycling; `departure_time` fully matches `[0-2][0-9]:[0-5][0-9]:00`. The entry id is read through `self.socket.call(...)` (see the Contract) and kept for 300 seconds. `journey` passes `timeout=JOURNEY_TIMEOUT`. Extend the comment above `SERVICES` and `_call_service`'s `Raises` to say what the new domain may be given, and the module docstring with a short paragraph on journeys. The one `"/api/services/"` in the module stays the only one.

- [ ] **Step 4: Run `tests/test_home_assistant.py`**; expected: all pass.

- [ ] **Step 5: Commit** `voice_assistant/home_assistant.py` and `tests/test_home_assistant.py`: "Ask Home Assistant for a journey, and for its zones by name".

### Task 2: The clock gives a reminder's id and moves one

**Files:**
- Modify: `voice_assistant/clock_requests.py` (`set_timer`, and two new methods beside `remove`)
- Test: `tests/test_clock.py`

**Interfaces:**
- Produces:
  - `set_timer(...)`'s success reply gains `"id": int`, the timer's id. Nothing else about it changes.
  - `Clock.move_timer(timer_id: int, due_at: float) -> bool`: sets a waiting timer's `due_at` (epoch seconds) and its `duration` to the seconds from now, saves, and tells `on_change` as every change does (use `self._changing()`). `False`, changing nothing, if no waiting timer has that id or `due_at` is not after now.
  - `Clock.timer_due(timer_id: int) -> Optional[float]`: a waiting timer's `due_at`, or `None` once it has rung, been cancelled or removed.

- [ ] **Step 1: Write the failing tests** in `tests/test_clock.py`, with the file's own clock fixture:
  - `test_setting_a_reminder_gives_its_id`: two reminders set give replies whose `"id"` values differ and match `overview()`'s.
  - `test_a_reminder_is_moved_and_stays_moved_after_a_restart`: set for 600 s, `move_timer(id, now + 1200)` is `True`, `timer_due(id)` is about `now + 1200`, a second clock loaded from the same files has it at the same time, and `on_change` was told.
  - `test_a_reminder_that_is_gone_or_a_time_gone_by_is_not_moved`: `move_timer(999, now + 60)` and `move_timer(id, now - 1)` are `False`; after `cancel_timer`, `timer_due(id)` is `None` and `move_timer` is `False`.
- [ ] **Step 2: Run `tests/test_clock.py`**; expected: the three fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run `tests/test_clock.py`**; expected: all pass.
- [ ] **Step 5: Commit**: "Give a reminder's id when it's set, and let one be moved".

### Task 3: journeys.py: the place, the arrival time, the journey and the words

**Files:**
- Create: `voice_assistant/journeys.py`
- Modify: `config.yaml` (a `journeys:` section after `football:`), `voice_assistant/config.py` (`LIMITS`, `POSITIVE`, `CHOICES`)
- Test: `tests/test_journeys.py`, `tests/test_config.py` if the limits there are tested by name

**Interfaces:**
- Consumes: Task 1's `HomeAssistant.zones`, `.journey`, `NoJourneys`, `journey_place`, `JOURNEY_MODES`; `HomeAssistant.people()`; `GoogleCalendar.calendars()`, `.activity_calendars()`, `.activity_words()`, `.events(first, days, calendar_ids=[id])`; `clock_words.format_duration`, `spoken_clock_time`; `choices.ChoiceStore`.
- Produces:
  - `JOURNEY_DEFAULTS = {"month": "", "requests": 0, "watches": {}, "travelling": []}` (the `ChoiceStore` defaults for `journeys.json`).
  - `class Journeys.__init__(self, home, calendar, clock, store: ChoiceStore, settings: Callable[[], Dict])`. `calendar` and `clock` may be `None`.
  - `Journeys.ask(self, place: str, mode: str = "driving", leave: bool = False, arrive_at: Optional[str] = None, satellite: Optional[str] = None, now: Optional[datetime] = None) -> Dict`: `{"success", "message"}`, with `"ask_first": Action("set_leave_reminder", {...})` when a reminder is offered. `mode` is one of `"driving"`, `"walking"`, `"cycling"`, `"transit"` (cycling is Home Assistant's `bicycling`). `arrive_at` is `"HH:MM"` (its next occurrence) or `"YYYY-MM-DD HH:MM"`, local.
  - `Journeys.look(self, origin: str, destination: str, mode: str, departure: Optional[str] = None) -> Optional[Dict]`: Task 1's `journey` answer through the monthly count; raises `OverCap` when the month's cap is reached. The watcher uses it too.
  - `class OverCap(Exception)`.
  - `Journeys.travels(self, satellite: Optional[str]) -> bool` and `Journeys.set_travels(self, satellite: str, on: bool) -> None`, kept under `"travelling"`.
  - The reminder Action's params, all plain JSON: `{"place": str (as said back), "destination": str, "origin": str, "mode": str, "arrive": float (epoch), "leave_by": float (epoch), "watch": bool}`.

**Settings** (`config.yaml`, each with a comment in the file's style; read through `settings()` at each use, a missing or unusable one falling back to the default):

| Name | Default | Range | Meaning |
| --- | --- | --- | --- |
| `buffer_minutes` | 10 | 0 to 60 | taken off the leave-by time |
| `reminder_minutes` | 5 | 0 to 30 | how long before the leave-by time the reminder rings |
| `check_every_minutes` | 5 | 2 to 30 | between the watch's looks |
| `watch_minutes` | 45 | 10 to 120 | how long before the leave-by time the watch starts, and under which the journey is asked as it is now |
| `change_minutes` | 5 | 2 to 30 | a move of the leave-by time worth saying |
| `heavy_minutes` | 5 | 1 to 60 | traffic is heavy at this many minutes over a clear road... |
| `heavy_percent` | 20 | 0 to 100 | ...and this much over it |
| `monthly_requests` | 3000 | 0 to 5000 | the most asks of Google in a calendar month; 0 asks none |
| `distance_unit` | `miles` | `miles`, `km` | how a distance is said |

**Behaviour the tests pin (exact words):**

- **The place.** The words as said, lower-cased, with a leading "my " or "the " dropped for matching, against each zone's name the same way (so "my mum's" finds a zone named "Mum's", and "home" finds `zone.home`). A match gives the zone's id as the destination and its name as the place said back. Otherwise the destination is `journey_place(words)` and the place said back is those words; if that is empty: `"Where to?"` (not a success).
- **The origin.** `zone.home`, unless `travels(satellite)`: then the first person's id from `home.people()`; if that can't be read, `"I can't tell where you are right now."`.
- **Minutes** are the seconds divided by 60 and rounded up, at least 1, said with `format_duration`. **Distance:** miles are metres / 1609.344, km metres / 1000; under 10 to one decimal place ("6.4 miles", but "1 mile" and "1 km" for exactly 1.0), 10 and over whole ("14 miles").
- **A journey time** (`leave=False`, or public transport whatever was asked):
  - driving: `"The drive to {place} is {duration}, {distance}."`
  - walking: `"The walk to {place} is {duration}, {distance}."`
  - cycling: `"The cycle to {place} is {duration}, {distance}."`
  - transit: `"Public transport to {place} takes {duration}."` and, when `leave` was asked, `" I can't say which one to catch, so I can't give a time to leave."` after it.
- **Heavy traffic** (driving, asked as it is now, `clear_seconds` known): when the live time is over the clear time by at least `heavy_minutes` and at least `heavy_percent` of the clear time, add `" Traffic's heavy: that's {n} minutes more than on a clear road."` (`"1 minute more"` for one), n rounded to the nearest minute.
- **The arrival time** for `leave=True`: `arrive_at` if given; else the place's activity: the zone's name (or the words) looked up in `calendar.activity_words()`, then that calendar's next event which is timed and starts after now and within 24 hours, read over today and tomorrow. Events in progress and all-day events are passed over.
  - no calendar set up, or no activity of that name, and no `arrive_at`: the journey time, then `" When do you need to be there?"` (a success; no `ask_first`).
  - an activity with no such event: `"You've no {activity} in the next day."`, then the journey time.
  - the calendar can't be read (`google_calendar.READ_FAILURES`): `"I can't read your calendar right now."`, then the journey time.
- **When to leave.** The leave-by time is the arrival time less the journey's seconds (rounded up to a minute) less `buffer_minutes`. If the leave-by time is `watch_minutes` or more away and the mode is driving, the journey is asked again with `departure` set to the leave-by time's `"HH:MM"` (one more ask, once), and the duration is said with "about". Times are said with `spoken_clock_time`; a time not today has `" tomorrow"` after it.
  - from the calendar: `"{Activity} starts at {start}. The drive is {duration}, so leave by {leave}."` (activity capitalised as the calendar names it; "The walk is", "The cycle is" for the others).
  - from a time said: `"The drive to {place} is {duration}, so leave by {leave} to be there for {arrive}."`
  - then the heavy sentence if it applies, then `" Want a reminder?"` with `ask_first`, `watch` being true only for driving from `zone.home`.
  - leave-by under 2 minutes away or past: the same first sentence with `"so you need to leave now."` in place of `"so leave by {leave}."`, and no offer.
  - no clock: no offer.
- **Failures**, none giving a time: `None` from `look`: `"I couldn't find a way to {place}."`; `NoJourneys`: `"Journeys need Google Maps Travel Time set up in Home Assistant."`; `OverCap`: `"I've asked Google for all the journeys I'm allowed this month."`; `NotAllowed`, `ValueError` or `requests.RequestException`: `HomeAssistant.failure_message(error)` for the last, and `"I couldn't get a journey time."` for the others, each logged once with the error's type and never with the place.
- **The count.** Each call of `home.journey` adds one to `"requests"` under the store's lock, before it is made; `"month"` is the local `"YYYY-MM"`, and a different month sets the count to 0 first. At `monthly_requests` or more, `look` raises `OverCap` without asking.

- [ ] **Step 1: Write the failing tests** in `tests/test_journeys.py`, with a fake home (`zones()`, `people()`, and `journey()` recording its calls and giving canned answers), a fake calendar (`calendars`, `activity_calendars`, `activity_words`, `events`), a real `ChoiceStore` in `tmp_path` and a settings dict. One test per bullet above, named for what it shows, each asserting the whole message. They must include: every Review Focus case 1 to 3; the forecast ask (two calls, the second with `departure`, "about" said); heavy traffic said and not said either side of both thresholds; a zone found by "my gran's" and said back as "Gran's"; words not a zone passed on and said back; the travelling satellite's origin being the person's id; `km` and `miles`; an `arrive_at` of `"18:00"` and of a full date; the reminder Action's params in full; transit with `leave=True`; and each failure's words with no ask made after `OverCap`.
- [ ] **Step 2: Run `tests/test_journeys.py`**; expected: fails on the import.
- [ ] **Step 3: Implement `voice_assistant/journeys.py`** with a module docstring saying what it is, where the data comes from and the privacy rule, and the settings in `config.yaml`, `LIMITS`, `POSITIVE` and `CHOICES`.
- [ ] **Step 4: Run `tests/test_journeys.py` and `tests/test_config.py`**; expected: all pass.
- [ ] **Step 5: Commit**: "Work out a journey and when to leave for it".

### Task 4: The tools, the reminder on a yes, and the wiring

**Files:**
- Modify: `voice_assistant/journeys.py` (`remind`), `voice_assistant/actions.py` (`UNAVAILABLE`, `__init__`, two tools), `voice_assistant/session.py` (the `ActionExecutor(...)` call near line 226), `voice_assistant/brain.py` (build `self.journeys` after the clock and calendar exist), `voice_assistant/settings_page.py` (`EDITABLE`), `voice_assistant/web/settings.js` (`GROUPS`)
- Test: `tests/test_journeys.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_settings_page.py`

**Interfaces:**
- Consumes: Task 2's `set_timer` id; Task 3's `Journeys`.
- Produces:
  - `Journeys.remind(self, place, destination, origin, mode, arrive, leave_by, watch, satellite: Optional[str] = None, now: Optional[float] = None) -> Dict`: sets the reminder `"Leave for {place}"` at `leave_by` less `reminder_minutes` through `clock.set_timer(label=..., is_reminder=True, duration_seconds=..., satellite=satellite)`; if that moment is under a minute away it is set for a minute from now. On success says `"I'll remind you at {time}."` and, when `watch`, keeps `{"destination", "origin", "place", "arrive", "leave_by", "satellite", "seen_home": False}` under `"watches"` by the timer's id as a string. The clock's own refusal (a clash, a satellite gone) is passed on as it is.
  - `ActionExecutor(..., journeys=None)`; `UNAVAILABLE["journeys"] = "Journeys need Home Assistant."`
  - Tool `journey(place, mode=None, leave=None, arrive_at=None)`, `needs="journeys"`, `required=["place"]`, calling `self.journeys.ask(place or "", mode or "driving", bool(leave), arrive_at or None, satellite=self.satellite)`. Its description, in the file's voice: how long a journey takes or when to leave for one, for "how long's the drive to work?", "how long to walk to the station?", "what time do I leave for uni?", "when should I set off to be at my gran's for 6?"; `place` as the user said it, never a guess at an address; `mode` an enum of the four, left out for driving; `leave` true when they ask when to leave or set off; `arrive_at` only when they say a time, as local `HH:MM` or `YYYY-MM-DD HH:MM`; "What's found is said for you, so just reply 'Okay.'"; after "When do you need to be there?", call it again with `leave` and `arrive_at`.
  - Tool `set_leave_reminder(...)` taking the Action's params, `needs="journeys"`, `for_model=False`, with the comment `press_pc_button` has on why.
  - `brain.journeys`: `Journeys(self.home, self.calendar, self.clock, ChoiceStore(os.path.join(paths.DATA_DIR, "journeys.json"), JOURNEY_DEFAULTS), lambda: self.config.section("journeys"))` when `self.home` is set, else `None`.
  - `EDITABLE` rows for all nine settings, each `"now"`, labelled in plain words (e.g. "Minutes to spare when working out when to leave"); `GROUPS` gains `["Journeys", ["journeys."]]` after Football.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_journeys.py`: `test_a_yes_sets_the_reminder_and_keeps_the_watch` (a real `Clock` as `tests/test_clock.py` makes one: the reminder is due at `leave_by - 300`, labelled "Leave for Work", on the asking satellite, and `"watches"` holds it by id); `test_a_walk_is_reminded_but_not_watched`; `test_a_reminder_almost_due_is_set_a_minute_on`; `test_the_clocks_refusal_is_passed_on`.
  - `tests/test_actions.py`: `test_the_journey_tool_asks_for_the_satellite_it_was_said_on`; `test_claude_is_never_offered_the_leave_reminder` (`set_leave_reminder` is not in `tool_schemas()`, and `execute` of it with `from_model=True` is refused); `test_without_home_assistant_journeys_say_so`; and, in the file's own test that every tool has a description and parameters, the two new tools pass.
  - `tests/test_conversation.py`: `test_a_yes_to_want_a_reminder_sets_it`: a turn whose result carries the journey's `ask_first` ends listening; "yes" next runs `set_leave_reminder` with those params; "no" says "Okay, I'll leave it." (follow the PC's tests there).
  - `tests/test_settings_page.py`: every `journeys` setting in `config.yaml` is in `EDITABLE` and has a limit (follow the football test of the same kind, if there is one; else one new test).
- [ ] **Step 2: Run those four files**; expected: the new tests fail.
- [ ] **Step 3: Implement** as the Interfaces say.
- [ ] **Step 4: Run the four files**; expected: all pass.
- [ ] **Step 5: Commit**: "Offer journeys as a tool, and set the reminder on a yes".

### Task 5: The main phrases without Claude

**Files:**
- Modify: `voice_assistant/intents.py` (new rules in `_rules`, placed before the activity rules near line 373, and their handler)
- Test: `tests/test_intents.py`

**Interfaces:**
- Consumes: the `journey` tool's params.
- Produces: local rules giving `Action("journey", {"place": ..., "mode": ..., "leave": ...})` with `"Okay."`.

- [ ] **Step 1: Write the failing tests** in `tests/test_intents.py`, parametrised:
  - leave, driving: "what time do I leave for work", "what time should I leave to get to work", "when do I need to leave for uni", "when should I set off for work", "what time do I have to leave to get to my gran's" give `{"place": "work" | "uni" | "my gran's", "mode": "driving", "leave": True}`.
  - how long: "how long is the drive to work" and "how long to drive to the metrocentre" (driving); "how long is the walk to uni", "how long to walk to uni", "how long does it take to walk to uni" (walking); "how long to cycle to work", "how long is the cycle to work" (cycling); "how long to get to work by bus", "how long to work on public transport" (transit); "how long to get to work", "how long does it take to get to my gran's" (driving). Each with `"leave": False`.
  - not matched (each must still give what it gives today, checked by comparing with the rules before this task: assert `match()` returns `None` or a non-journey action): "what time do I have work tomorrow", "when do I finish uni today", "how long is left on my timer", "how long until my alarm", "what time is it", "when should I leave" (no place), "how long to boil an egg".
- [ ] **Step 2: Run `tests/test_intents.py`**; expected: the journey cases fail.
- [ ] **Step 3: Implement** the rules and one handler. A said time ("to be at work for 9") is left to Claude: no rule takes it.
- [ ] **Step 4: Run `tests/test_intents.py` and `tests/test_conversation.py`**; expected: all pass.
- [ ] **Step 5: Commit**: "Hear the main journey questions without Claude".

### Task 6: Watching a leave reminder

**Files:**
- Modify: `voice_assistant/journeys.py` (`JourneyWatcher`), `voice_assistant/brain.py` (`_say_journey`, building, starting and stopping the watcher beside football's)
- Test: `tests/test_journeys.py`, `tests/test_brain.py` if the brain's watchers are tested there

**Interfaces:**
- Consumes: `Journeys.look`, the `"watches"` store, `Clock.timer_due`, `Clock.move_timer`, `Clock.remove("timer", id)`, `football_watch.user_home`.
- Produces:
  - `JourneyWatcher(journeys: Journeys, say: Callable[[Optional[str], str], None], home_now: Callable[[], Optional[bool]], settings: Callable[[], Dict])` with `check(now: float)`, `start()`, `stop()`, in `FootballWatcher`'s shape: a daemon thread named `"journeys"`, one pass never ending it, the wait between passes 60 seconds.
  - `Brain._say_journey(satellite: Optional[str], message: str) -> None`: says it on that satellite as `_say_download` does (at any hour) and also sends it to `self.ntfy.phone` with the title "Leaving time" when there is a phone topic. Never raises.

**What one look does, for each watch:**
1. `timer_due(id)` is `None` (rung, cancelled, removed): forget the watch. Nothing said.
2. `home_now()`: `True` sets `seen_home`; `False` with `seen_home` set means he has left: remove the reminder (`remove("timer", id)`), forget the watch, log "Dropped the reminder to leave, as Home Assistant says the user has left", say nothing. `None` changes nothing.
3. Not yet within `watch_minutes` of `leave_by`, or looked at less than `check_every_minutes` ago (the time of the last look is kept on the watch): nothing.
4. Otherwise `look(origin, destination, "driving")`. `None`, `OverCap` or a failure: log once per watch, leave the reminder as it is.
5. The new leave-by time is `arrive` less the journey (rounded up to a minute) less `buffer_minutes`. If it differs from the watch's `leave_by` by `change_minutes` or more:
   - move the reminder to the new time less `reminder_minutes` (not before a minute from now), keep the new `leave_by`, and say, earlier: `"Traffic to {place} is heavier now. Leave by {new}, not {old}."`; later: `"Traffic to {place} has eased. Leave by {new}, not {old}."`
   - if the new leave-by time is under 2 minutes away or past: `"Traffic to {place} is heavier now. You need to leave now."`, remove the reminder and forget the watch.
   - if `move_timer` is `False` the watch is forgotten and nothing is said.
   The watch is saved before anything is said, so a restart doesn't say it twice.

- [ ] **Step 1: Write the failing tests** in `tests/test_journeys.py`, with a real `Clock` and the fakes of Task 3, one per numbered case above and per Review Focus 4 and 5, asserting the words said, where (`satellite`), the reminder's new time, and the store. Include: no ask of Google outside the watch window or within `check_every_minutes` of the last; a change of 4 minutes says nothing and moves nothing; the look's failure leaves the reminder; a restart (a new `Journeys` and watcher on the same files) carries on and doesn't repeat a change already said.
- [ ] **Step 2: Run `tests/test_journeys.py`**; expected: the watcher tests fail.
- [ ] **Step 3: Implement**, with the watcher built in the brain only when `self.journeys` is set, started and stopped where football's is.
- [ ] **Step 4: Run `tests/test_journeys.py` and `tests/test_brain.py`**; expected: all pass.
- [ ] **Step 5: Commit**: "Watch a reminder to leave, and move it when the traffic does".

### Task 7: The travels-with-me switch

**Files:**
- Modify: `voice_assistant/settings_page.py` (the satellites' state near `_satellites`, a `set_travels` handler, its route beside `/api/satellites/default`), `voice_assistant/web/settings.js` (`showSatellites`)
- Test: `tests/test_settings_page.py`

**Interfaces:**
- Consumes: `Journeys.travels`, `Journeys.set_travels`.
- Produces: each satellite in the page's state gains `"travels": bool` (always `False` without journeys); `POST /api/satellites/travels` with `{"name": str, "on": bool}` answers `{"success", "message"}` ("{name} travels with you now." / "{name} stays at home now."), refusing a name that isn't a satellite, an `on` that isn't a boolean, and saying "Journeys need Home Assistant." without journeys. In the row, a labelled checkbox "Travels with me" whose title explains it: journeys are worked out from where your phone is.

- [ ] **Step 1: Write the failing tests** in `tests/test_settings_page.py`: the state's flag before and after; each refusal; that the change is logged with who made it, as `make_default`'s is.
- [ ] **Step 2: Run `tests/test_settings_page.py`**; expected: the new tests fail.
- [ ] **Step 3: Implement.** The checkbox follows the football switches' way of drawing and changing (`changeFootball` and its row), using the page's `change()` helper.
- [ ] **Step 4: Run `tests/test_settings_page.py`**; expected: all pass.
- [ ] **Step 5: Commit**: "Mark a satellite as travelling with the user, on the settings page".

### Task 8: The record, and the whole suite

**Files:**
- Create: `docs/decisions/0024-leaving-time.md`
- Modify: `README.md` (a short "Journeys" part where Football's is, and the setup: the Google Cloud key with the Routes API and billing, the Google Maps Travel Time integration with its polling switched off, zones named as the calendars are), `docs/roadmap.md` if it lists what is built

- [ ] **Step 1: Write decision 0024** in the format of `docs/decisions/0023-football-did-you-mean.md` and 0014: `# 0024: When to leave, and how long a journey takes`, the status line `17 November 2026 · Agreed with the user in chat · Built`, then **Context.**, **Decision.** (numbered: the source and why, places as zones, the arrival time, the words, the reminder and the watch, the cap, privacy, what isn't done: incidents by name, public transport's leave-by time, a travelling satellite's watch), **My calls, not the user's**, **Hardware.** (none shapes this; a misheard place is the speech model's). No real place in it.
- [ ] **Step 2: Run the whole suite, London, then UTC, then ruff** (Test commands). Expected: everything passes but the one test known to fail now and then in London, `test_home_assistant`'s 3000-deep script walk; if that alone fails, say so and don't rerun.
- [ ] **Step 3: Commit**: "Record decision 0024 and how journeys are set up".
