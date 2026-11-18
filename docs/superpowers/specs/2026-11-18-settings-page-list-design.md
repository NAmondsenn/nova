# Nova's page as a list, one section at a time

18 November 2026. Agreed in chat with Nathaniel; the calls marked "my call" are the designer's, his to change.

## Why

His words about Nova's own settings page: "I no longer like this page, and I kind of hate it to be honest. It's impossible to navigate. It looks nice, but it's unorganized, cluttered, and yeah, like I said, absolutely impossible to navigate, especially on a phone. It's unusable." He couldn't find the satellites on it. Shown three tappable mock-ups (tabs, a list, a jump bar), he chose the list: "yes b".

Success: on his phone he reaches anything in one tap from a short first screen, can tell how each thing is without opening it, finds a setting by typing a word of it, and comes back with the phone's own back gesture. Nothing the page does today is lost.

## What exists (found 18 Nov)

- One page, `voice_assistant/web/settings.html` (441 lines), drawn by `settings.js` (3,077 lines, no framework, no build step) and styled by `settings.css` (1,880 lines): a night sky, frosted glass panels, the Outfit face, Nova's star. He likes the look.
- Thirteen sections stacked in one scroll, in a grid on a wide screen: Status (its tiles, Satellites, the microphone's mute, Recent problems), Music (the player, playlists, Spotify's sign-in, Clean songs), Devices, Batteries, Football, Calendars and lists, Routines, Sound, Timers and alarms, Clock displays, Memory, Keys (with the page's password) and Settings. No menu, no search, no way to jump.
- Settings is one form, `#settings`, with one save bar: every setting Nova lets him change (`EDITABLE` in `settings_page.py`), in groups by the start of its key (`GROUPS` in `settings.js`: Music, Wrapped, Football, Journeys, Voice, Sounds, Claude, Home, Notifications, Listening, Privacy, Clock displays, and Other for the rest), far from the feature each belongs to.
- `load()` asks `/api/state` every 20 seconds and each part is drawn again only when its data changed (`drawIfChanged`), never while a control in it is pressed or a field in it is being changed. Music, the calendars, the home's choices, the batteries, the clean check and football have their own loads.
- With more than one satellite, a row of chips chooses which one the page looks after; Devices, Sound and Timers and alarms are that satellite's.
- The page's JavaScript has tests run with Node (`tests/web/*.test.mjs`): the script runs in a `vm` over a small stand-in for the DOM. Node is at `~/tools/node/bin/node` on CT 109; there is no browser there.
- `/api/state` does not carry how many journeys Nova has asked Google for this month.

## Goals

1. **A first screen that is a list.** Under Nova's masthead (unchanged): what needs attention, a search box, then every section as a row in four groups, each row with its icon, its name and one line on how it is now.

   | Group | Sections |
   | --- | --- |
   | Now | Status, Music, Timers and alarms |
   | Home | Devices, Batteries, Clock displays |
   | Daily | Journeys, Football, Calendars and lists, Routines |
   | Nova | Satellites, Sound and voice, Claude, Notifications, Memory, Keys, Everything else |

2. **One section at a time.** A tap on a row opens that section alone, with "All of Nova" at its top to go back. The phone's back gesture and the browser's Back go back to the list, at the place it was scrolled to; a second Back leaves the page as it would have. A section has its own address (`#music`), so a link or a reload opens it.
3. **A feature's settings sit with the feature.** Each group of settings moves into its section, under what the section already shows:

   | Section | Settings whose keys start |
   | --- | --- |
   | Music | `music.`, `navidrome.`, `soulsync.`, `spotify.`, `clean_check.`, `new_music.`, `wrapped.` |
   | Football | `football.` |
   | Journeys (new section) | `journeys.` |
   | Sound and voice (was Sound) | `voice.`, `sounds.`, `thresholds.` |
   | Claude (new) | `web_search.`, `conversation.`, `usage.` |
   | Notifications (new) | `ntfy.`, with the quiet hours |
   | Clock displays | `display.` |
   | Everything else (new) | `home_assistant.`, `mqtt.`, `logging.`, and any key no section claims |

   The one save bar stays: it shows wherever he is while anything is changed and unsaved, says how many, and saves them all. Changes are kept when he goes to another section or back to the list.
4. **Satellites is its own section:** the list of satellites with their buttons and the "Travels with me" switch, adding one, the new-password panels, the choice of which the page looks after, and the microphone's mute. Status keeps its tiles, Recent problems and the restart notice.
5. **The line on each row** says how the thing is, from what the page has already loaded, and is drawn again as that changes:

   | Section | Its line, for example |
   | --- | --- |
   | Status | "All well", or the first thing that isn't (see the builder check, 4) |
   | Music | the song and artist playing, or "Nothing playing" |
   | Timers and alarms | "1 reminder, 1 alarm" or "Nothing set" |
   | Devices | "2 lights on" or "All off" |
   | Batteries | the lowest one, "Tablet at 14%", in the warning colour when it is under its mark |
   | Clock displays | "1 connected" |
   | Journeys | "21 of 3,000 asks used this month" |
   | Football | "2 teams followed" |
   | Calendars and lists | "Signed in to Google" or "Sign in to Google" |
   | Routines | "2 routines" |
   | Satellites | "2 of 2 connected" |
   | Sound and voice | "Voice at 60%" |
   | Claude | "Web search on" or "Web search off" |
   | Notifications | "Quiet from 23:00 to 07:00" or "No quiet hours" |
   | Memory | "12 things remembered" |
   | Keys | "6 of 11 set" |
   | Everything else | none |

   A line whose data hasn't loaded, or couldn't, is left empty, never guessed.
6. **What needs attention** sits above the list, one row each, tapping through to its section: a restart is needed, Nova's settings file can't be read, the default satellite is not connected, a battery is under its mark, Google or Spotify needs signing in to, a problem in the last hour. With none, nothing is shown there.
7. **Search.** Typing in the box narrows the list to sections which have every word typed, in their name, their line, their settings' labels or their own words on the page (headings, labels, buttons: "mute", "password", "travels"), and under each lists the settings and controls which matched; a tap on one opens its section with it scrolled to and marked for a moment. With nothing matching: "Nothing on Nova's page matches that."
8. **A wide screen** (1,180 px and over, the page's existing wide breakpoint) shows the list down the left and the open section beside it, Status when none is chosen (my call). Between a phone and that, as a phone.
9. **Journeys' count:** `/api/state` gains `journeys`: the asks made this month and the cap, or nothing without journeys, for the row's line and the section.
10. **The look is kept:** the sky, the glass, the type, the star, the existing controls. New pieces (rows, the back button, the search box, the attention rows) are made of the same parts.

## Non-goals

- Changing what any control does, what Nova's API takes or gives (but for goal 9), or adding a setting.
- The Nova view in Home Assistant (another piece of work).
- Tabs, or more than one page: one page, one script, no framework, no build step, as now.
- Rewriting how each section draws itself.

## Anti-goals

- A setting or control that was on the page and now can't be reached. A key no section claims lands in Everything else, as it lands in Other today.
- Unsaved changes lost by opening another section, going back, or the 20-second refresh.
- The refresh closing the open section, scrolling the page, taking the focus, or redrawing a control under his finger.
- The back gesture leaving the page from inside a section.
- A row's line saying something untrue: an old value shown as now, or "All well" while the state couldn't be read.
- The log-in panel, the page's password and the locked state behaving differently: locked, nothing but the log-in shows.

## Approaches

1. **Keep every section and its drawing code; add the list and the showing of one section on top (chosen).** Sections stay in the page and are hidden, not removed, so each keeps its state, its unsaved fields and its refresh. A few blocks move (Satellites, the settings' groups). Least to get wrong in 3,000 lines that mostly have no tests.
2. Draw each section afresh when it is opened. Cleaner in the end, but it rewrites the page and loses the half-typed and the unsaved unless each section learns to keep them.
3. A page per section from the server. Every tap is a load, the star and the music lose their place, and the server grows routes for what is only layout.

## Decided

- The sections stay `<section>` elements in `settings.html`; which is open is a class or attribute on the page and CSS shows it. The new sections (Journeys, Satellites, Claude, Notifications, Everything else) are added there.
- The list, its lines, the attention rows and the search are a part of `settings.js` of their own (a clearly marked block, or a second file `settings_list.js` loaded beside it, the builder's choice by what keeps `settings.js` readable), with their rules as plain functions over data so Node can test them without a DOM.
- Which section a setting belongs to is one table in the script, replacing `GROUPS`; inside a section the old group names stay as sub-headings where a section holds more than one (Music: Music, Wrapped; Sound and voice: Voice, Sounds, Listening; Everything else: Home, Privacy, Other).
- Going to a section pushes one history entry; going back pops it. Opened from a link or reload with `#name`, Back goes to the list.
- The save bar is fixed to the bottom of the screen while there are unsaved changes, above the phone's home bar, as its toasts already allow for.
- Before it goes live he sees it: the real page's files with Nova's answers stood in for are published as a private Claude artifact for him to try on his phone (my call), since no browser on CT 109 can draw it.

## Answers from the builder check (18 Nov, all my calls)

1. **Keys.** Several keys are optional, so an unset one is not a fault: the row says how many are set of how many there are, in the ordinary colour, and no attention row is made for a key. A missing key that matters already shows in Status (Claude can't be reached, Home Assistant not connected).
2. **Satellites.** Only the default satellite being away needs attention, as that is where warnings and alerts are said; a push-to-talk one is off as a matter of course. The row's line counts them all plainly.
3. **Search** covers every control, not only the settings: it reads each section's own words from the page as it stands (headings, labels, button text, placeholders) beside the settings' labels, so nothing is listed by hand and a control added later is found.
4. **Status's line.** "All well" only when the state was read, every tile is good and no problem (not a warning) is listed in the last hour. Otherwise the first tile that isn't good, in the tile's own words, else "A problem at 14:08" with the latest one's time. No counts, as the page is given only the latest eight. The attention row for a problem is the same rule.
5. **The settings form's other messages.** That Nova's settings file can't be read is an attention row on the list and is said at the top of every section which has settings. The save bar stays up after a save which needs a restart, offering "Restart now" as it does today, beside the restart's attention row and the notice in Status.

## Left to the builder

How the hidden sections' inputs stay part of the one settings form; the icons for the new sections (in the style of the existing ones); the exact wording of an attention row; how long a found setting stays marked; the transitions, if any, kept short and off under `prefers-reduced-motion`.

## Constraints

- British spelling, no em dashes, no emojis; Nova's comment style in all three files.
- The repo is public: example names in tests and docs are made up.
- No new dependency, in Python or in the page.
- Tests: Python on the Pi through `pi_test.sh` (London and UTC); the page's with `~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/`, never comparing DOM nodes or large objects; `node --check` on each script. CT 109 load limits.
- Other sessions push Nova's main (the PC satellite, the Nova view): fetch and merge main before the last test run, and tell them before pushing.
- Deploys, pushes and restarts need his named job.
- Hardware: none shapes this.
