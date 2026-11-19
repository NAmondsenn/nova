# Adding songs to playlists, and removing calendar events

19 November 2026. Agreed in chat with Nathaniel; the calls marked "my call" are the designer's, his to change.

## Why

His words: "Can you make it so that Nova can add to playlists if I name what playlists to add to? This would be good for when listening to new songs, like the auto-added music. And one more thing is Nova can add calendar events but not remove them."

Success: while a new song plays, "add this to Gym and Car" puts it on both and it stays there; "cancel the dentist on Thursday" takes it off the calendar, and "put it back" undoes a mistake.

## What exists (found 19 Nov, read-only)

- Music plays from Navidrome (`music.player` is navidrome). Nova signs in as the user who owns all six of his Navidrome playlists. `NavidromePlayer._find_playlist` already matches a spoken name loosely (`distinctive` and `best` in `navidrome.py`) for "play my X playlist", and `NavidromeLibrary` writes Nova's own New Music playlist by its recorded id.
- Three of the six are remade: New Music by Nova (recorded id in `new_music.py`), Daily Mix 1 and 2 by SoulSync's "Nova: daily mixes" automation (sync mode replace). SoulSync's `GET /api/personalized/playlists` lists every playlist it remakes by name (also Your Listening Mix, Discovery Shuffle, Hidden Gems).
- The other three are SoulSync mirrors of his old Spotify playlists (`GET /api/mirrored-playlists`; each has `server_playlist_id`, which is the Navidrome playlist's id, `sync_name`, and `sync_mode`, null for all three, so SoulSync's global mode, "reconcile" by default). Reconcile removes anything on the Navidrome playlist that isn't in SoulSync's copy, so a song Nova added straight into Navidrome would be taken off at the next sync of that playlist. A sync runs when one of the playlist's missing songs finishes downloading; one has two waiting now.
- SoulSync has a per-playlist "append" mode for exactly this ("a mirror the user adds songs to ... has to be append-only", its test for issue 1704), set with `PATCH /api/mirrored-playlists/<id>/preferences` `{"sync_mode": "append"}`.
- The calendar is Google's API directly (`google_calendar.py`), not Home Assistant. Nova's sign-in already has `calendar.events.owned`, which covers deleting on his own calendars, so no new sign-in. Of his three calendars Nova can write to two; the third is read-only to it. `find` already looks events up by a word or two over `SEARCH_DAYS`, and `_insert` sends an add once and never twice when Google's answer is unclear.

## Goals

### Adding to playlists

1. "Add this to Gym", "add this to Gym and Car": the song playing (or paused while Nova talks) goes on each playlist named. "Add the last song to Gym": the one before it in the queue (my call, cheap, covers a song that has just changed).
2. Each name is matched loosely, as playing a playlist is, and the reply names the playlist actually used, so a mishearing shows: "Added Endless to Gym Bangers and Car."
3. A song already on a playlist isn't added twice: "Endless is already on Gym."
4. A name with no playlist: "You haven't got a playlist called Jim." Nova never makes a playlist.
5. A remade playlist is refused with the reason: New Music (by its recorded id), and any whose name matches one SoulSync remakes, asked at the time so a new mix is covered: "SoulSync remakes Daily Mix 1, so it wouldn't stay."
6. A SoulSync mirror (matched by `server_playlist_id`, else by `sync_name`) whose mode isn't "append" is switched to "append" first, then the song is added. The first time, the reply says so: "Added Endless to Gym, and set Gym to append only in SoulSync so it stays." Any mirror he makes later is covered the same way.
7. SoulSync set up but unreachable: nothing is added, as Nova can't tell whether it would stay: "I can't reach SoulSync to make sure it stays on Gym, so I haven't added it." SoulSync not set up: the song is just added in Navidrome.
8. With two playlists named and one refused, the other is still added, and the reply says which went where.
9. Nothing playing: "Nothing's playing." The Spotify player: "Adding to playlists only works with Navidrome."

### Removing calendar events

1. "Cancel the dentist on Thursday", "remove my MOT": found by its words, as `find` does, on the day given or, with no day, from today over `SEARCH_DAYS`. A time narrows it ("the 3 o'clock one").
2. One match: removed straight away (his choice, option A) and said back: "Removed Dentist, Thursday at 3."
3. Several matches: nothing is removed; Nova lists them with day, time and calendar and asks which.
4. None: "I can't find the dentist on your calendar on Thursday."
5. A repeating event: only that day's, unless he says "all of them" or "every week", which removes the whole series.
6. An event on a calendar Nova can only read: "I can't remove that, Google only lets me change your own calendars." An invite from someone else is left alone: "That's an invite from someone else. Decline it in Google Calendar." (My call: removing it would tell the organiser.)
7. Sent once: an unclear answer from Google is said as "I can't tell whether it was removed", never retried, as adding does.
8. "Put it back" within ten minutes restores the last event removed: the same event set back to confirmed, so its reminders, repeats and guests come back; if Google won't, a copy is added. "Put Dentist back, Thursday at 3." Kept in memory only, so a restart forgets it (my call, as the page's Undo does).

## Non-goals

- Taking a song off a playlist, making playlists, or adding a song by name that isn't playing (he can ask later).
- Editing or moving an event.
- Spotify playlists.
- Any new setting: nothing here is something he'd tune (my call).

## Approaches

For songs staying on his mirrored playlists:
1. **Chosen (his pick, "1"):** Nova switches a mirror to append the first time it adds to it, then adds in Navidrome. Covers future mirrors with no work. Cost: a song deleted on Spotify is no longer deleted here (nothing refreshes them from Spotify now anyway).
2. Stop SoulSync mirroring them. Simpler for Nova, but the few songs still waiting wouldn't be added when they download, and a later mirror has the same problem.
3. Rejected: add in Navidrome only (SoulSync takes it back off); add on Spotify (he's left Spotify, and nothing refreshes the mirrors).

For removing events: one sensible way, Google's `events.delete` beside the existing `events.insert`. Home Assistant isn't used because Nova's calendar has never gone through it.

## Left to the builder

- Tool names and shapes, following `add_to_calendar` and `play_playlist` in `actions.py`.
- Restoring a removed event: check on a throwaway event on his primary calendar (his OK first: it writes to his calendar) that setting a deleted event, and a deleted occurrence of a repeating one, back to `confirmed` brings it back; keep the copy as the fallback.
- That Nova's SoulSync key may make the preferences change; if it can't, the reply says to set append in SoulSync and the song isn't added.
- A decision record with the next free number, and a line in the release checklist for his hand checks.
