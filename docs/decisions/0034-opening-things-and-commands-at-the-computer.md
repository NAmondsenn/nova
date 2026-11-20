# 0034: Opening things and commands at the computer

20 November 2026 · Asked for by the user in chat, widened in his second message · Design: [the spec](../superpowers/specs/2026-11-19-pc-open-close-and-commands-design.md) · Built; his named deploys and hand checks (47 to 62) to come

**Context.** The PC is a push-to-talk satellite (0026), but there was nothing to say at a desk that the PC itself did: opening a game, a page, closing a program, putting the PC to sleep, volume. He wanted to drive it by voice the way he drives the house, and asked for it from the Pi too ("open Fortnite on my PC").

**What he decided.**

1. **The PC satellite opens things itself** (approach 1). HASS.Agent stays for power.
2. **Opening** apps, games and web pages by voice, at the PC and, with the PC named, from the Pi. **Closing** by "close X" and "close this". **Searching:** "search Google for X" opens a new tab in the default browser (his is Firefox) with the window brought to the front.
3. **Power words at the PC** ("go to sleep", "lock", "turn the screen off", "shut down", "restart") act on the PC spoken to, only as the whole request, never from a word inside a sentence. Shut down and restart still ask first.
4. **Volume and mute** work for any device he names and, with none named, for the device spoken to only. At the PC that is Windows' own volume, at the Pi the Pi's own Nova volume. Stop and pause stay room-wide. This replaces 0026's "volume acts on every satellite in the room".

**What was built.**

- **The list.** The PC builds a list of what it can open (Start menu, Epic Games manifests, Steam libraries; Uninstall entries dropped, duplicates keep one) at start and every 10 minutes, and sends it up when it changes, names and ids only. The launch target never leaves the PC, and the id is looked up again there. A second list, of programs with a visible window, is sent when it changes and dropped on disconnect.
- **`open_url`** takes only `https://`, no user:password part, at most 2,000 characters. Searches are one the brain builds, on a search site set on the settings page (Google by default; DuckDuckGo and Bing).
- **Close** posts `WM_CLOSE` and never terminates a process, never Nova itself, Explorer, the desktop or the taskbar. Whatever Nova opens is brought to the front.
- **Volume on the PC** is Windows' master volume through Core Audio on the satellite (the step comes from the brain; changing the level unmutes; the level is clamped 0 to 100).
- **Settings page**, per satellite that opens things: an "Opens apps and pages" switch (on by default, live), "Found N apps and games.", "Also called" names for apps, the search site, and the PC's HASS.Agent entity when there is more than one PC.
- **Claude's tools** `open_on_computer`, `open_web_page`, `search_on_computer`, `close_on_computer`, and `set_volume` with `where` and `mute`, offered once a satellite that opens things has been seen since the brain started. Claude never sees a launch target.

**My calls (his to overturn).** From the spec:

- "Start X" is an opening word only when X is an item on the list, by its whole name (see the final rulings). "Play X" stays music.
- At the Pi, "open X" with no device named uses the only satellite that opens things, and says where.
- Close acts on every window of that program; Steam and Discord minimise to the tray, as their X does.
- The list refreshes every 10 minutes and at start; there is no refresh button.
- "Close X" for something not open falls through unless the PC was named.
- The last list is kept after the PC disconnects (not saved to disk), so "open Fortnite" at the Pi with the PC asleep says "Your PC's off." rather than going to Claude.
- Volume follows his rule for Claude's wordings and "turn the music up" as well as the short ones; the alarm volume stays Nova's; "volume up" at the Pi now changes only the Pi's music, not the PC's Nova music in the same room.
- "Mute" at the Pi mutes its music only, never alarms, timers or Nova's voice; while something rings, "mute" still stops it.
- "Close this" from the Pi works only with the PC named.
- The search site is a setting; the browser is Windows' default.
- The settings switch covers opening, closing and searching, not power or volume.
- Windows volume goes through the satellite, not HASS.Agent (see the reading below).

From the build (rulings while it was made):

- `safe_url` and `search_url` live in `voice_assistant/computers.py` with only standard-library imports at module level, so the satellite, which has no brain dependencies, can use them. The satellite re-checks every address itself.
- The executor knows whether the request came from a PC (`at_pc`), so a power word at a PC waits until the reply has been heard before the button is pressed.
- Tests which asserted room-wide volume were changed to the new rule; the pause and stop ones were not.
- A device he names is checked before the one spoken at: "open Steam on the Mac", said at the PC, goes to the Mac.
- A named PC or Mac which is not connected gets "Your PC's off." or "Nova isn't running on ...", not "can't open things", which would mislead after a brain restart with the PC asleep.
- Refusals say "I can't find X on <its spoken name>." using 0032's names, so nothing is hard-coded to a person.
- With no device named, the local open and close rules count only an exact or "Also called" match, plus a single every-word fit when spoken at a computer. A partial or ambiguous request falls through to Assist or Claude, whose tools still ask "Which one". Why: "open the windows", "start the music" and "open the garage" must keep their meaning. The cost is that some partial names go through Claude, which is slower.
- Search words are in the transcript line "Handled locally" as he said them (the existing transcript design, off with transcripts); the spec's host-only rule covers addresses Nova builds.
- Ruling D, bringing things to the front. The throwaway foreground test on the PC (the plan's Task 0) was not run, as there was no named job for it. The satellite was built on the likely answer: `AllowSetForegroundWindow(ASFW_ANY)` before the launch, then the new (or the browser's) window restored with `SW_RESTORE` and raised with `SetForegroundWindow`, joining the input of the thread in front with `AttachThreadInput` for a moment if Windows refuses. Check 48 is the first real test of it. The cost if it's wrong: things open but only flash in the taskbar until it's changed.

From the final review (rulings for the last fixes):

- **Close by the name in the list.** A game's window is often named nothing like the game ("FortniteClient-Win64-Shipping"), so "close Fortnite" found nothing. The satellite now lists a window under its name in the list of things to open when its exe lies in that Epic or Steam game's install folder; the folders stay on the PC and are never sent. Its own name still closes it too. The cost: a few path checks per window, every 3 seconds.
- **Search at the Pi.** A plain "search for X" at a satellite which doesn't open things (the Pi) goes to Claude's own web search and is answered aloud, as before. It opens the browser on the PC only with the PC named ("search for X on the PC") or with Google said ("search Google for X", "google X", "look up X on Google"). At the PC, every form opens the browser. "Search for it" (or "that", "this") always goes to Claude, which knows what it was. The cost: "search for X" at the Pi no longer opens the PC.
- **A named device's volume.** "Volume down on the Pi", said elsewhere, changes the Pi's music if its music is playing now, else its Nova voice. Before, it went by whether the music had been paused for him to speak, which is only true of the satellite he spoke to. The cost: none expected.
- **"Start" needs the whole name.** With no device named, the single app with every word said counts only after "open", "launch" or "load up", never "start", so "start the music" at the PC keeps its meaning (check 58). "Start" opens an app by its whole name or his own name for it. The cost: "start rocket" goes to Claude rather than opening Rocket League.

Smaller fixes from the same review: matching a request against a computer's list never asks Home Assistant (only the reply "Your PC's off." does), so an asleep PC's apps still match from the Pi; a named computer that has just connected says "I'm still finding the apps on ..."; a bare power word at one of several PCs acts on the HASS.Agent PC chosen for it; "quit it" and "exit that" never close the window in front (only "close this" does), and "restart it" isn't a power word; a removed satellite's apps are forgotten; a Start menu shortcut whose AppID is a `steam://` or Epic link is launched as that link; the PC keeps its switch and "Also called" box on the settings page after a brain restart.

**Things to know.**

- A satellite given a room-like name ("Bedroom") would take "set the bedroom to 30" as a volume for it.
- Windows mute at the PC silences Nova's own PC sounds too; alarms ring on the Pi (0026), so they are still heard.
- Several smaller points were left, listed in the ledger (for example a stale "Also called" name still claiming "go to <name>.com").

**The reason for Windows volume going through the satellite, corrected by check 62.** On 20 November 2026 check 62 read HASS.Agent's `media_player.windows_pc` with the PC on: `volume_level` 0.46, not muted, while Windows was at 47 percent. So HASS.Agent's reading is right now; the 0.0 of 19 November was most likely taken while the PC was asleep or waking. (An earlier read at 01:53 that day, with the PC off, showed `unavailable` and settled nothing.) The satellite path is kept because it works, reads and sets in one step, and does not depend on HASS.Agent or MQTT being up.

**His ruling of 20 November: a press unmutes Windows.** When Windows is muted and he presses Numpad 3, Nova unmutes it first (the level stays as it is), before the press sound, so he hears the sound and the reply. A press which ends a recording or talks over a reply does not touch the volume. Saying "unmute" to Nova works too, as it already did.

**Anti-goals** (a failure even if it works). A spoken word becoming a shell command or a path; opening anything named Uninstall; "mute" silencing an alarm or timer, or the microphone (that stays the Pi's switch); force-killing, losing unsaved work, or closing Nova, Explorer or the desktop; "go to sleep", "restart" or "shut down" acting from inside a longer sentence ("I'm going to sleep after this match", "restart the song") or at the Pi without naming the PC; shutting down or restarting without the question and a yes. Not built: running commands, opening documents or folders, typing and clicking, waking a sleeping PC, the Mac.

**Setting it up (his named jobs, in order).** Push to GitHub main and restart `nova-backend`; the Pi's `bash update.sh`; the PC's `scripts/deploy_pc.sh`. Then the checks below, warning before sound.
