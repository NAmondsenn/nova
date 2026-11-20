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

- "Start X" is an opening word only when X is an item on the list. "Play X" stays music.
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

**Things to know.**

- A satellite given a room-like name ("Bedroom") would take "set the bedroom to 30" as a volume for it.
- Windows mute at the PC silences Nova's own PC sounds too; alarms ring on the Pi (0026), so they are still heard.
- Several smaller points were left, listed in the ledger (for example: partial fits for "start" at the PC, "search for it" searching the word "it", a stale "Also called" name still claiming "go to <name>.com").

**The reason for Windows volume going through the satellite, checked live.** At 01:53 on 20 November 2026 (brain's clock), a read-only GET of `media_player.windows_pc` on the brain showed `state: unavailable`, `volume_level: None`, `is_volume_muted: None`. The PC was off, so this neither confirms nor refutes the earlier reading. The reason stands on HASS.Agent's reading of `volume_level: 0.0` on 19 November while the PC was on and not muted, which was the evidence when this was designed; to be re-read with the PC on (check 62).

**Anti-goals** (a failure even if it works). A spoken word becoming a shell command or a path; opening anything named Uninstall; "mute" silencing an alarm or timer, or the microphone (that stays the Pi's switch); force-killing, losing unsaved work, or closing Nova, Explorer or the desktop; "go to sleep", "restart" or "shut down" acting from inside a longer sentence ("I'm going to sleep after this match", "restart the song") or at the Pi without naming the PC; shutting down or restarting without the question and a yes. Not built: running commands, opening documents or folders, typing and clicking, waking a sleeping PC, the Mac.

**Setting it up (his named jobs, in order).** Push to GitHub main and restart `nova-backend`; the Pi's `bash update.sh`; the PC's `scripts/deploy_pc.sh`. Then the checks below, warning before sound.
