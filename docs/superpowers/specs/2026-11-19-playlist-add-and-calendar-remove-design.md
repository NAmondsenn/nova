# Changing playlists and calendar events by voice

19 November 2026. Agreed in chat with Nathaniel; the calls marked "my call" are the designer's, his to change.

## Why

His words: "Can you make it so that Nova can add to playlists if I name what playlists to add to? This would be good for when listening to new songs, like the auto-added music. And one more thing is Nova can add calendar events but not remove them." Then: "can it make a new playlist too, make it so i can remove songs off playlists too, like remove this song from this playlist. editing events, i kinda want that too like being able to move one or rename one".

Success: while a new song plays, "add this to Gym and Car" puts it on both and it stays there; "take this off Gym" takes it off and it stays off; "make a playlist called Chill with this" starts a new one; "cancel the dentist on Thursday", "move it to Friday at 4" and "rename it to dentist check-up" change the calendar; "undo that" takes back a mistake.

## What exists (found 19 Nov, read-only)

### Music
- Music plays from Navidrome (`music.player` is navidrome). Nova signs in as the user who owns all six of his Navidrome playlists, so it can write to them. `NavidromePlayer` (`navidrome.py`) holds the queue (`_queue`, `_index`) and `_find_playlist`, which matches a spoken name loosely (`distinctive` and `best`) for "play my X playlist". `NavidromeLibrary` writes Nova's own New Music playlist by the id it recorded (`make_playlist`, `replace_playlist`, `playlist_with_id`; `new_music.py`).
- Three of the six are remade: New Music by Nova each night, Daily Mix 1 and 2 by SoulSync's "Nova: daily mixes" automation (sync mode replace). SoulSync's `GET /api/personalized/playlists` names every playlist it remakes (also Your Listening Mix, Discovery Shuffle, Hidden Gems; each has `name`, no Navidrome id).
- The other three are SoulSync mirrors of his old Spotify playlists, made 14-15 Oct (`GET /api/mirrored-playlists`: each has `server_playlist_id`, which is the Navidrome playlist's id, `sync_name`, and `sync_mode`, null for all three, so SoulSync's global mode). Whatever the mode, a sync makes Navidrome's playlist hold every song in SoulSync's copy: "reconcile" (the default) also removes anything else, "append" never removes but adds back anything missing. A sync of a mirror runs when one of its missing songs finishes downloading (`sync_downloaded_playlists`); five or so songs across the three are still missing.
- Deleting a mirror in SoulSync (`DELETE /api/mirrored-playlists/<id>`) deletes only SoulSync's own rows for it (`MusicDatabase.delete_mirrored_playlist`); the Navidrome playlist is left as it is and is never synced again.
- A mirror's mode is set with `PATCH /api/mirrored-playlists/<id>/preferences` `{"sync_mode": "append"}`; SoulSync's own tests say a mirror the user adds songs to on the server has to be append-only (issue 1704).
- Nova already talks to SoulSync (`soulsync.py`, `SoulSync(url, key)`, `soulsync.url` and `SOULSYNC_API_KEY`).

### Calendar
- Google's API directly (`google_calendar.py`), not Home Assistant. The sign-in already has `calendar.events.owned`, which covers changing and deleting events on his own calendars: no new sign-in. Of his three calendars Nova can write to two; the third is read-only to it.
- `events()` reads a window with `singleEvents=true`, so each time a repeating event is on comes back as its own event; `_read_event` keeps only summary, start, end and all-day, not the event's id, its series (`recurringEventId`) or its organiser.
- `find()` matches every word given against the summary over `SEARCH_DAYS` (90) from a day. `add()` checks the day hasn't gone, picks the calendar (`_calendar_to_add_to`), and `_insert` sends once and never twice when Google's answer is unclear (`_NotSure`); it doesn't return the new event's id.

## Goals

### Playlists

Which song: "this" is the song at the queue's place, playing or paused, whoever paused it; "Nothing's playing" only with nothing queued. "The last song" is the one before it in the queue (my call: covers a song that has just changed).

The same song (my call, agreed): a playlist entry is the song if it has the same Navidrome id, or the same title and artist as the library helpers compare them, so the album copy and the single, or a clean and an explicit file, count as one; a remix or a live version has another title and is another song. This is what "already on", "isn't on", removing every copy and undoing go by.

1. **Add.** "Add this to Gym", "add this to Gym and Car": on each playlist named. Names are matched loosely, as playing one is, and the reply names the playlists used, so a mishearing shows: "Added Endless to Gym Bangers and Car." A song already on a playlist isn't added again: "Endless is already on Gym."
2. **New playlist.** "Make a playlist called Chill" makes an empty one; "add this to a new playlist called Chill" makes it with the song. A name already taken (matched exactly, not loosely) isn't made twice: "You've already got Chill", and the song is added to it if he asked to add. "Add this to Jim" with no playlist near Jim: "You haven't got a playlist called Jim. Want me to make it?", and a yes makes it with the song.
3. **Remove.** "Take this off Gym", "remove this song from Gym": every copy of it on that playlist is removed (my call: he wants it off). Not on it: "Endless isn't on Gym."
4. **Remade playlists** are left alone, for adding and removing, with the reason: New Music (by its recorded id) and any whose name matches one SoulSync remakes, asked at the time so a new mix is covered: "SoulSync remakes Daily Mix 1, so that wouldn't stay."
5. **A playlist SoulSync still mirrors** (one he mirrors later; the three today stop being mirrors, see "One-off change"), matched by `server_playlist_id`, else `sync_name`: adding first switches it to append, then adds, and the first time says so: "Added Endless to Gym, and set Gym to append only in SoulSync so it stays." Removing is refused: "SoulSync copies Gym from Spotify, so it would put Endless back. Stop mirroring it in SoulSync and I can."
6. **SoulSync set up but unreachable:** nothing is added or removed, as Nova can't tell whether it would stay: "I can't reach SoulSync to check Gym, so I haven't changed it." SoulSync not set up: plain Navidrome. Making a new playlist needs no SoulSync check.
7. Two playlists named and one refused: the other is still done, and the reply says which.
8. **Liked songs** (my call, agreed): "like this", "add this to my liked songs" stars the song in Navidrome, and "unlike this", "take this off my liked songs" unstars it, as playing already treats "liked songs" as the starred songs (`means_liked_songs`). Never offered as a playlist to make. "Liked Endless." Already starred: "You've already liked Endless."
9. Nothing queued: "Nothing's playing." No song before this one: "There isn't a last song." The Spotify player: "Changing playlists only works with Navidrome."

### Calendar

Finding the event, for every change: by its words, as `find` does, on the day given or, with no day, from today over `SEARCH_DAYS`; a time narrows it ("the 3 o'clock one"). Several matches: nothing changes; Nova lists them (day, time, calendar) and asks which. None: "I can't find the dentist on your calendar on Thursday." A repeating event: only that day's, unless he says "all of them" or "every week", which changes it from that one on (his choice, 19 Nov), as Google Calendar's "this and following events" does; earlier ones are never changed, so the record of past shifts and lectures stays. Removing from this one on ends the series just before it (its rule's `UNTIL`; the whole series if it's the first). Moving or renaming from this one on ends the old series just before it and starts a new one from it with the same repeat rule and the change. An event on a calendar Nova can only read: "I can't change that, Google only lets me change your own calendars." An invite from someone else is left alone: "That's an invite from someone else. Change it in Google Calendar." (My call: changing it would tell the organiser.)

1. **Remove.** "Cancel the dentist on Thursday": removed straight away (his choice, option A), said back: "Removed Dentist, Thursday at 3."
2. **Move.** "Move the dentist to Friday at 4": the same length unless an end is given; an all-day event stays all day unless a time is given. A day already gone is refused, as adding does. "Moved Dentist to Friday at 4." For "all of them", only the time of day can change; moving them to another day: "I can only change the time for all of them. Move the days in Google Calendar."
3. **Rename.** "Rename the dentist to dentist check-up": "Renamed Dentist to Dentist check-up, Thursday at 3."
4. Each change is sent once: an unclear answer from Google is said as "I can't tell whether it changed", never sent again, as adding does.
5. Nothing is emailed to anyone (Google's `sendUpdates=none`).

### Undo

"Undo that" reverses the last playlist or calendar change Nova made, of any kind, within ten minutes: an add (the event or the song taken back off), a removal (the event set back to confirmed, so reminders, repeats and guests come back, else a copy added; the song added back at the end, as Nova shuffles playlists anyway), a move or rename (the old times or name put back), a change from this one on (the old series' rule put back and the new series deleted), a new playlist (deleted), a like (unliked, and the other way). One change is kept, the latest; a change of several playlists at once is undone whole. Kept in memory only, so a restart forgets it (my call, as the page's Undo does).

It only reaches a change that was the last thing Nova did (my call, agreed): any other action since - milk on the shopping list, a timer - leaves it alone, so "undo that" never deletes the dentist when he meant the milk: "The last thing I did wasn't a playlist or calendar change, so I've left it." An undo can't itself be undone. Nothing to undo: "There's nothing to undo." Too late: "That was too long ago to undo." Undoing is sent once too, and said back: "Put Dentist back, Thursday at 3."

## One-off change (his named job, at deploy)

Stop SoulSync mirroring his three Spotify playlists (his choice, option A): `DELETE /api/mirrored-playlists/<id>` for each, after reading their ids and confirming each `server_playlist_id` is a Navidrome playlist that stays. Their Navidrome playlists stay as they are and become ordinary playlists. The few songs still missing still download into the library, but aren't put on their playlist. SoulSync's database is backed up nightly with CT 102.

## Non-goals

- Spotify playlists; deleting a whole playlist (other than undoing one just made); renaming a playlist; adding a song by name that isn't playing.
- Adding guests, changing an event's calendar, moving repeating events to other days, or changing past times of a repeating event.
- Any new setting on the settings page: nothing here is something he'd tune (my call).

## Approaches

Songs staying on his mirrored playlists:
1. **Chosen (his pick, "A"):** stop SoulSync mirroring the three; future mirrors get the append switch for adding and a refusal for removing. Adding and removing then always stay. Cost: the few missing songs don't land on their playlists.
2. Keep the mirrors; on removing, also tell SoulSync to stop pairing the song with the playlist ("unmatch"). The missing songs still arrive, but SoulSync then shows the song as missing, a manual sync might download it again, and it leans on more of SoulSync's insides.
3. Rejected: change Navidrome only (SoulSync undoes it); change on Spotify (he's left Spotify, nothing refreshes the mirrors).

Calendar: one sensible way, Google's `events.delete` and `events.patch` beside the existing `events.insert`. Home Assistant isn't used, as Nova's calendar has never gone through it; Home Assistant has no way to write Navidrome playlists.

## Left to the builder

- Which satellite's player "this" comes from with two satellites playing, whether undo is per satellite, how a yes to "Want me to make it?" keeps the song, and how many matches to list: follow how the code already does the nearest thing.
- A repeat rule with `COUNT` rather than `UNTIL`, when ending a series early or splitting it.
- Tool names and shapes, following `add_to_calendar` and `play_playlist` in `actions.py`; where the playlist writes live in `navidrome.py`.
- Restoring a removed event: check on a throwaway event on his primary calendar (his OK first: it writes to his calendar) that setting a deleted event, and a deleted occurrence of a repeating one, back to `confirmed` brings it back; keep the copy as the fallback.
- That Nova's SoulSync key may change a mirror's mode; if it can't, the reply says to set append in SoulSync and the song isn't added.
- Live checks on a throwaway playlist (make, add, remove, undo, delete) and a throwaway event, both silent writes, with his OK.
- A decision record with the next free number, and lines in the release checklist for his hand checks.
