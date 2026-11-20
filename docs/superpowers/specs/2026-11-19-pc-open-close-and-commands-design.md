# Opening and closing things on the PC, and PC commands said at the PC

Phase 6 piece 2 of 4 (after the PC satellite, 0026; before fingerprint approval, then the Mac).

## Why

The PC is a full push-to-talk satellite (0026), but Nova can't yet do the PC things you'd most naturally say at a desk: "open Fortnite", "open the BBC website", "close Discord", "go to sleep", "volume up". He wants to drive the PC by voice the way he drives the house.

## What he decided

- **Approach 1:** the PC satellite opens things itself; HASS.Agent stays for power.
- **Opening** apps, games ("open Fortnite") and web pages by voice, at the PC.
- **From the Pi too:** "open Fortnite on my PC" said at the Pi works.
- **Closing:** "close X".
- **PC commands said at the PC:** "go to sleep", "lock", "turn the screen off", "shut down", "restart" act on the PC being spoken to, only when they are the whole request, never from a word inside a sentence. Shut down and restart still ask first, as now.
- **Volume, option A, then widened (his second message):** volume works for any device he names ("volume up on the PC", "set the Pi to 30"), and with no device named it is the device spoken to, only. At the PC that's Windows' own volume; at the Pi it's the Pi's own Nova volume. This overturns 0026's room-wide volume. Stop and pause stay room-wide.
- **Also added in his second message:** "close this", "mute" (and unmute), "search Google for ..." in the default browser (his is Firefox) as a new tab with the window brought to the front, and the PC's volume from the Pi.

## Goals

1. At a PC satellite: "open / launch / start X" opens an installed app or game, found at run time from the Start menu, Epic Games and Steam. No list kept by hand.
2. "Open <site>" / "go to <site>" opens a web page in the default browser.
3. "Close / quit X" closes a program that has a window, as clicking its X would.
4. Bare power words at the PC act on that PC through HASS.Agent (existing `PCPower`), whole request only.
5. Volume and mute act on the named device, else the device spoken to: Windows' master volume at the PC, the Pi's own Nova volume at the Pi.
6. From any satellite, the same open/close/search with "on my PC" / "on the PC" / its spoken name (0032's names).
7. "Close this" closes the window in front at the PC.
8. "Search Google for X" opens a Google search for X in a new tab of the default browser, with the window brought to the front.
9. Whatever Nova opens (app, game, page, search) comes to the front, not just a flashing taskbar button.

## Non-goals

- Running arbitrary commands, scripts or files; opening documents or folders by name.
- Typing, clicking, window arranging, "close everything".
- The Mac (piece 4). The design keeps the capability per satellite so the Mac can join later with `open -a` and its own volume.
- Waking a sleeping or off PC to open something (same rule as moving music in 0032).

## Anti-goals (a failure even if it works)

- A spoken word ever becomes a shell command or a path. Nova opens only an item from the satellite's own list, by that item's id, re-checked on the satellite, or an `https://` address.
- Anything named Uninstall... is never opened.
- "Mute" silencing an alarm or timer that should ring, or muting the microphone (that stays the Pi's switch).
- Force-killing a program, losing unsaved work, or closing Nova itself, Explorer or the desktop.
- "Go to sleep", "restart" or "shut down" acting from inside a longer sentence ("I'm going to sleep after this match", "restart the song"), or at the Pi without naming the PC.
- Shut down or restart without the existing question and a yes.

## Constraints

- HA first (his rule): power stays on HASS.Agent's buttons through `PCPower`. Opening, closing and volume can't go through HA: HASS.Agent has no list of installed apps, its `launchurl` stays never-pressed (0008), and its media player reads `volume_level: 0.0` live while the PC is clearly not muted, so a step can't be worked out from it.
- Discover at run time: the app list is built by the satellite and refreshed by itself.
- Anything he might change goes on the settings page.
- The repo is public: no app names from his PC in tests or docs beyond common ones (Fortnite, Discord, Steam).
- Push-to-talk only on the PC; no new hardware needed.

## Approaches considered

1. **The PC satellite opens, closes and sets volume itself (chosen).** It already runs as his account and talks to the brain. It builds the list and sends it up; the brain matches what was said and sends back an id. It keeps the list fresh by itself and carries over to the Mac.
2. **Everything through HASS.Agent.** `launchurl` can open `steam://` and `com.epicgames.launcher://` links, but there is no installed-app list (a hand-kept list breaks "discover at run time") and it means pressing the one button kept off. Rejected.
3. **HASS.Agent custom commands, one per app.** Most control, most upkeep, nothing for the Mac. Rejected.

## The design

### Satellite (Windows)

- **Hello** gains `opens: true` and `system_volume: true` (optional both ways, as `saves_notes` is). Only a Windows satellite sends them for now.
- **The list ("catalogue")**, built in the background at start and every 10 minutes, and sent with `apps` only when it changed:
  - Start menu: `Get-StartApps` (name, AppID), launched as `shell:AppsFolder\<AppID>` through Explorer.
  - Epic: `C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item`, applications only (`bIsApplication`, skipping content/DLC entries such as "LEGO Fortnite Content"), launched as `com.epicgames.launcher://apps/<namespace>%3A<item>%3A<app>?action=launch&silent=true`. The manifests can hold unescaped backslashes, so parse leniently. This is how Fortnite is found: it isn't in the Start menu.
  - Steam: every library in `libraryfolders.vdf`, each `appmanifest_*.acf`, launched as `steam://rungameid/<appid>`. Steamworks redistributables and tools are skipped.
  - Dropped: names starting "Uninstall"; exact duplicate names keep one (Start menu first, as it launches directly).
  - Each item: `{id, name, kind: app|epic|steam}`. The id is the satellite's own (a short hash); the launch target never leaves the PC.
- **`open {id}`:** looks the id up in its current list; unknown is refused. Launch with `os.startfile` on the target.
- **`open_url {url}`:** only `https://`, no user:password part, at most 2,000 characters; opened in the default browser (Firefox opens it as a new tab in its open window), then brought to the front.
- **Searches** are an `open_url` the brain builds: `https://www.google.com/search?q=<words, URL-encoded>`. The search site is a setting (Google by default), the browser is whatever Windows' default is, never named in code.
- **`close {name}`:** finds visible top-level windows whose program matches (catalogue name, the exe's FileDescription, or the exe name), and posts `WM_CLOSE` to each. It never terminates a process. It never touches Nova's own process, `explorer.exe`, or Windows' shell windows. It answers with the name closed, or "not open".
- **`close_front`:** posts `WM_CLOSE` to the foreground window, under the same never-close rules (not Nova, Explorer, the desktop or the taskbar); answers with the program's name or "nothing to close".
- **`volume {direction|level|mute}`:** Windows' default output endpoint master volume and mute (Core Audio `IAudioEndpointVolume`). The step is the brain's standard step, sent with the request. Changing the level unmutes, as Windows' own keys do. It answers with the new level and mute state.
- **Coming to the front:** the satellite runs in the background, and Windows' foreground lock usually stops a background process giving focus to what it starts (it only flashes in the taskbar). The satellite must bring the opened app's or browser's window to the front. Likely `AllowSetForegroundWindow(ASFW_ANY)` before the launch, then finding the new or reused window and raising it. This needs a throwaway test on the PC first, as for Windows media (his named job). Firefox reuses its running window for a new tab, so that window is the one raised.
- **Open windows:** the satellite also keeps a list of the programs that have a visible window (name as matched for close), checked every 3 s and sent with `windows` only when it changed. Close matches against this list.
- Each request is answered like `save_note`: an id and a short wait, then a sentence to say. Nova's own volume for the PC stays as it is: the Windows master scales it.

### Brain

- Keeps each satellite's last list (names and ids only) in memory, kept after it disconnects (not saved to disk, so a brain restart forgets it until the PC reconnects). The open-windows list is dropped on disconnect.
- **Local intents, whole request only:**
  - `(open|launch|start|load up) X [on <satellite>]` matched only when X names an item on the target satellite's last list, and `(close|quit|exit) X [on <satellite>]` only when X names a program in its open-windows list. Otherwise it falls through as today, so "start a timer", "open the garage" and "close the garage door" are untouched. With a PC named ("on my PC"), the request is the PC's whatever X is, so an unknown X there gets "I can't find X on your PC." or "X isn't open."  Matching: lower-case with ™, ®, © and punctuation dropped; an exact name wins ("Overwatch" over "Overwatch 2"); otherwise every word said is in exactly one name; two or more say "Which one: A or B?"; plus his "Also called" names (below).
  - `open|go to <site>` with a domain shape (`bbc.co.uk`, `youtube.com`) opens `https://<that>` directly. An app of the same name wins over a site ("open YouTube" opens his YouTube app).
  - **Power words** at a satellite whose `machine` is `pc`: `go to sleep|sleep`, `lock|lock (it|the pc|the computer)`, `turn (off )?the screen off|screen off|turn off the screen`, `shut down|shut it down`, `restart|reboot`, mapped to `PCPower` `sleep / lock / screen_off / off / restart`. Off and restart go through the existing ask-first flow unchanged. At the Pi these bare words keep their present behaviour.
  - **Volume** is routed in `ActionExecutor.set_volume`, so the local shapes (`volume up`, `louder`, `set the volume to 30`, "turn the music up") and Claude's own `set_volume` calls ("turn it up a bit") act alike. They gain an optional device: "volume up on the PC", "turn the Pi down", "set the PC to 30", "PC volume 40", named through 0032's satellite names.
    - No device named: the device spoken to, only (no longer room-wide).
    - Target has `system_volume` (the PC): general and music go to Windows' volume.
    - Target is the Pi (or any satellite without it): its own Nova volumes as today, music included, but only that satellite's players, not the room's.
    - The alarm category is always Nova's alarm volume on the target.
    - The volume beep plays on the target at the new level. A named device that's off or not connected says so ("Your PC's off.").
    - Anything naming a Home Assistant THING ("set the TV volume to 20") stays Assist's, as now: a satellite name is checked first, then the THING path.
  - **Mute / unmute**, whole request, optional device ("mute the PC", "unmute"):
    - While something rings or Nova is speaking, "mute" keeps its stop meaning (clock_words' STOP_WORDS) and is not a volume mute.
    - At the PC (or the PC named): Windows mute on or off.
    - At the Pi (or the Pi named): mutes the music playing on that satellite. Nova's voice, alarms and timers still sound, and the microphone switch is untouched. Unmute puts the music back where it was.
  - **Close this:** `close (this|that|this window|the window|it)`, whole request, at a satellite that opens things (or with the PC named from the Pi, which closes the PC's front window), sends `close_front`.
  - **Search:** `(search|google) (google )?for X|google X|look up X on google [on <satellite>]`, at or naming a satellite that opens things, says "Searching for X." and opens the search. At the Pi with no device named, it goes to the only known satellite that opens things, as "open" does. With none known, it falls through to Claude as today.
- **Claude tools** (offered once any satellite that `opens` has been seen since the brain started, connected or not): `open_on_computer(name, where?)`, `open_web_page(url, where?)`, `search_on_computer(words, where?)`, `close_on_computer(name|"this", where?)`, and `set_volume` gains `where?` and `mute?`, for wordings the patterns miss ("open the BBC website", "fire up Rocket League"). The brain does the matching. On no match it returns the three closest names for Claude to pick from or ask about. URLs are checked as above. Claude never sees launch targets.
- **Where:** the satellite spoken at if it `opens`. Otherwise the one named ("on my PC", via 0032's `names_satellite`/spoken names). Otherwise, if exactly one known satellite opens things, that one, said back as "Opening Fortnite on Nathaniel's PC." The PC asleep or off says "Your PC's off." (HASS.Agent's status). On but with Nova not connected, it says "Nova isn't running on your PC."
- **Which HASS.Agent PC a satellite is:** with one HASS.Agent PC, that one. With more, a per-satellite choice on the settings page (Satellites section).
- **Order for sleep:** the reply is said, and has finished on the PC, before the sleep, lock or screen-off button is pressed, so "Putting your PC to sleep." is heard.

### Settings page (Satellites section, per satellite that opens)

- **"Opens apps and pages"** switch, on by default. It covers opening, closing and searching. Off, the brain treats that satellite as not opening things (no matching, no Claude tools for it); power words and Windows volume at the PC are unaffected. Changed live, no restart.
- **Count line:** "Found 283 apps and games."
- **Search site** (one setting for every satellite): Google by default; DuckDuckGo and Bing as the other choices.
- **"Also called" for apps:** pairs such as `GTA` → Grand Theft Auto V Enhanced, `FC` → EA SPORTS FC 25, picked from the satellite's list.
- **Its HASS.Agent PC,** shown only with more than one.

### Logging and privacy

- App names are logged. For URLs only the host is logged. Nothing goes to the repo from his PC's list.

### Decision record

0034 (next free), replacing 0026's room-wide volume with "the named device, else the one spoken to".

## My calls (his to overturn)

- **"Start X"** is an opening word only when X is an item on the list.
- **"Play X"** stays music; opening a game needs open/launch/start.
- **At the Pi,** "open X" with no device named uses the only connected satellite that opens things, and says where.
- **Close** acts on every window of that program; Steam and Discord minimise to the tray when closed, as their X does.
- **The list refreshes** every 10 minutes and at start. No refresh button.
- **Close knows what's open** from a window list the PC keeps up to date; "close X" for something not open falls through unless the PC was named.
- **The last app list is kept** after the PC disconnects, so "open Fortnite" at the Pi with the PC asleep says "Your PC's off." rather than going to Claude.
- **Volume** follows his rule for Claude's wordings as well as the short ones, and for "turn the music up"; the alarm volume stays Nova's; "volume up" at the Pi now changes only the Pi's music, not the PC's Nova music in the same room.
- **"Mute" at the Pi** mutes its music only, never alarms, timers or Nova's voice; while something rings, "mute" still stops it.
- **"Close this" from the Pi** works only with the PC named ("close this on the PC").
- **The search site** is a setting, Google by default; the browser is Windows' default.
- **The settings switch** covers opening, closing and searching, not power or volume.
- **Windows volume goes through the satellite,** not HASS.Agent, because HASS.Agent reads 0.0. The builder confirms once live that HASS.Agent's reading is really wrong before relying on this reasoning in 0034.

## Left to the builder

- How the satellite calls Core Audio (pycaw or comtypes; pin it like winrt) and enumerates windows (ctypes `EnumWindows`).
- The exact regexes, within the shapes above, and where they sit in `LocalIntents`' order (power words before lists, as the PC ones are now).
- How the `apps` payload is framed (payload JSON, like `save_note`'s text, since MAX_LINE is short).
- Tests: protocol both ways; matching (exact over partial, duplicates, ™, Uninstall dropped, "start a timer" untouched); power words only as whole requests and only at a PC; volume routing by capability; URL checks; close never killing. Windows-only parts are unit-tested with fakes, then checked live on the PC (his named job; warn before anything that makes sound).
