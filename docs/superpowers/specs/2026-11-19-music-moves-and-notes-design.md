# Moving music between satellites, and notes saved on the computer

19 November 2026. Asked for by the user ("make it so that you could switch where music is playing at between the different satellites... play it on my Pi or change this to the PC"; "the PC and Mac satellite, when it's made, can create notes and it saves it to that device's downloads folder"). He chose "just get going" over questions, so every call below not in his words is mine, listed at the end for him to overturn. Decision record: 0030 (0029 is another session's, unpushed).

## What it does

1. **Moving music.** "Play it on my Pi", "move this to the PC", "switch to the Mac", "change it to the computer": the music carries on from where it was, on the satellite named, with the same queue, repeat and shuffle, and stops where it was. "Play Drake on the PC" starts new music on another satellite. Navidrome only (Spotify isn't used; Nova says so if it's the player).
2. **Notes.** At a computer satellite (the PC today, the Mac once built), "make a note that the bins go out on Tuesday", "take this down: ...", "write me notes on the French Revolution" save a plain text file in that computer's own Downloads folder. "Add milk to my notes" still goes on the notes list in Home Assistant, as now. The Pi has no notes tool, so it behaves exactly as now.

## Why

He moves between the Pi and the PC in the bedroom (and later the Mac), and wants music to follow him without starting it again; and he wants something said at his computer to land as a file he can open there.

## How a satellite is named

- Each satellite says in its hello what machine it is: `machine` is `pi` (Linux whose device-tree model starts "Raspberry Pi"), `pc` (Windows), `mac` (macOS) or `linux` (anything else); and `saves_notes: true` when it has a Downloads folder (Windows and macOS only). Both are optional: the brain takes a missing or odd value as unknown and never refuses a satellite for it, and an old brain ignores them, so either side can be deployed first.
- The brain keeps both in `satellite_rooms.json` beside each satellite's kind and area, so they are known while it's away.
- Words that name a satellite, after dropping "the", "my" and a trailing "satellite": its own name (dashes and underscores as spaces, so "home assistant"); its machine's words (`pi`: "pi", "raspberry pi"; `pc`: "pc", "computer", "desktop"; `mac`: "mac", "macbook", "laptop", "computer"); and any names set for it on the settings page. A name set on the page wins over a machine word: if "computer" is set on one satellite, it means that one alone.
- Two satellites matching is said back ("Which one: the PC or the Mac?") and nothing moves. A word matching none falls through: a local rule leaves it to Claude, and Claude's tool says there's no satellite by that name and lists those there are.
- What a satellite is called in replies: its first page name if it has one, else "the Pi", "the PC" or "the Mac", else its name.
- Settings page, Satellites section: each satellite's row says what machine it is ("Raspberry Pi", "PC", "Mac") and has a "Also called" text box (names split on commas, at most 5 names of at most 30 characters each), saved through `/api/satellites/called`. New satellites are picked up from their hello, with no setup.

## Moving music

- From: the satellite spoken to if its player has a queue (playing or paused); else one in its room that has; else the one satellite anywhere playing; else the one with music paused. None: "There's no music to move." To: the satellite named. The same one: "It's already on the PC." Not connected: "The PC isn't connected, so the music's staying on the Pi." Nova doesn't wake a sleeping PC (he can say "turn on my PC" first).
- The player hands over its queue, the order before shuffling, which song, how far into it (from the satellite's own reports of what it has played), repeat and shuffle; the source stops and forgets its queue; the target plays from that point (ffmpeg told to start there, after the input, so it's exact). The target keeps its own music volume. Moved music always plays, even if it was paused, since moving it means wanting it.
- If Nova is talking in the target's room, the target starts once the reply ends, as any music asked for during a reply does. If the target's in another room, it starts straight away.
- Local rules, before "send it to my Mac" and the lists: verbs move, switch, change, swap, transfer, play, take; an optional "it", "this", "that", "the music", "the song" or "music"; then "to", "onto", "on", "over to"; then the satellite's words. Taken only if the words name a satellite (otherwise left to the rules below and Claude). "Send" and "put it on" stay with sending a reply to a device.
- Claude: a `move_music` tool (`to`), and an optional `on` on `play_music` and `play_playlist` for new music elsewhere; both use the same naming.
- A reply for a move: "Moved to the PC."

## Notes

- Brain to satellite: `save_note` with an id, a title and the text. Satellite to brain: `note_saved` with the id and either the file name it used or an error sentence. The brain waits up to 5 seconds.
- The satellite writes only into its Downloads folder, never a folder inside it, and never over an existing file: the title made safe (no path characters, control characters, leading dots, trailing dots or spaces, or Windows' reserved names; at most 80 characters; "Note" if nothing's left) plus `.txt`, then " (2)", " (3)" up to 99 if taken, each opened exclusively. UTF-8, with Windows line endings on Windows. Text over 50,000 characters is refused. The note's text is never logged on either side.
- The Downloads folder: on Windows the user's real Downloads folder from Windows itself (it may have been moved), falling back to `%USERPROFILE%\Downloads`; on macOS `~/Downloads`. A satellite whose folder is missing says it doesn't save notes.
- Claude's `save_note` tool (`title`, `text`) is offered only to a session whose satellite saves notes. Its description: the user's own words when they dictate; written notes, plain text and at most about 400 words, when they ask for notes on something (Claude's replies are capped at 1,024 tokens, thinking included); "add X to my notes" is the list, not this.
- Reply: "Saved to your Downloads as Bins." (no extension, which Piper would spell out). Failure: "I couldn't save it on the PC: <why>." or "The PC didn't answer, so the note wasn't saved."
- The Mac: the satellite code saves on macOS already; it is checked for real when the Mac satellite is built.

## Not done

- Waking a sleeping PC to move music to it; moving Spotify; playing on two satellites at once (they'd drift apart); saving notes anywhere but Downloads, or from the Pi; naming a satellite by its room.

## Calls made for him (his to overturn)

Notes are plain `.txt`, titled by Claude; Downloads only; the Pi gets no notes tool; moved music always plays; the target keeps its own volume; "computer" can mean the PC or the Mac until he names one on the page; no waking the PC; replies "Moved to the PC." and "Saved to your Downloads as ...".
