# 0022: Clean songs found by ear, and swapped

**Context.** Decision 0012 made "a clean version reported as downloaded" a must-not, and judged it by labels: Navidrome's `explicitStatus` and clean words in a title or album name. A library clean-up on 15 and 16 Nov 2026 showed the labels lie both ways:

- Gunna's `endless.mp3` is clean audio on an album tagged "The Last Wun (explicit)", and its ISRCs belong to the explicit recording.
- SICKO MODE and Money Trees sit on albums Navidrome shows as "(clean)", but the audio is explicit. The "(clean)" comes from SoulSync's tagging: it matches each download to a MusicBrainz release and writes that release's note ("clean") as the album comment, which Navidrome appends to the album name. A fresh download of Gangsta's Paradise got the same note.
- SoulSync downloads from Soulseek, then YouTube (music videos are often the clean edit), whatever the metadata it searched with said.

A throwaway test on the brain (16 Nov, his named job) listened instead. Nova's Nemotron model, given each song from Navidrome and its lyrics from Navidrome (SoulSync's saved lyrics files), heard 1 of Endless's 16 lyric swears, against 7 of 14 (fukumean), 4 of 5 (pushin P), 49 of 72 (Over Hoes & Bitches), 9 of 14 (SICKO MODE) and 45 (Money Trees, lyrics not found) for explicit songs. Parakeet heard about half as many. Nemotron took about 35 s for a 2-minute song on two of pve2's cores at low priority.

What the user wants, in his words: the explicit versions in his library, judged by the audio; built into Nova rather than a one-off ("make it part of nova actually"); and when a song is clean, Nova swaps it itself ("B", "1").

**Decision.**

1. **Judged by ear, never by label.** Whether a song is clean is decided by listening to it against its lyrics.
   - The lyrics come from Navidrome (`getLyricsBySongId`, which reads the `.lrc` or `.txt` SoulSync saves beside a song), else from LRCLIB's public search (`/api/search` by title and artist, taking the result nearest the song's length). With neither, the song isn't judged.
   - Nova counts the swears in the lyrics with one fixed list of words (the fuck, shit, bitch, nigga, pussy, dick, hoe and ass families, as in the test), and how many of them Nemotron hears in the audio.
   - Fewer than 3 in the lyrics: not judged (nothing to censor, as with Gangsta's Paradise). Heard at least 35% of them: **explicit**. Heard 15% or fewer: **clean**. In between: **unsure**, left alone and listed. The three numbers are defaults in config.yaml (builder's call: unlikely to change, and a page full of thresholds helps nobody).
   - The counting is the test's, exactly: the lyrics and the transcript are each searched, case-insensitively, with `\b(fuck\w*|motherfuck\w*|shit\w*|bitch\w*|nigga\w*|nigger\w*|pussy|pussies|dick\w*|hoes?|ass)\b`; "heard" is the number of matches in the transcript, not capped at the lyric count; the share is heard divided by expected. The audio is the song as Navidrome streams it, down-mixed to mono 16 kHz float by ffmpeg, given to Nemotron's streaming recogniser in one-second pieces from start to end. (Builder: copy this method, since the thresholds were set from it.)
   - Labels still steer what Nova *picks* to download (pick_track avoids a candidate named clean); they no longer decide whether what *landed* is clean. Navidrome's `explicitStatus` and album names are not used for that any more.
   - **What's already had.** 0012's checks of what the library already has (`_as_had`, `_in_navidrome`'s clean offer, `_none_queued`, the look before a song goes on the wishlist) use the stored verdicts (5) instead of labels: a song judged clean counts as "only a clean version" (the offer to download the explicit one stays, and a yes runs a swap, 4); everything else, judged or not, counts as had. So "download SICKO MODE" says "You've already got it." even on a "(clean)" album.

2. **Listening runs apart from the conversation.** A worker process of its own, started when there is something to judge and closed when idle, at the lowest priority, with its own copy of Nemotron and two threads, one song at a time. Nova's speech to text keeps its model and its speed. (Builder's call: a thread inside the brain can't be given a lower priority than the rest of it, and a second model costs memory, see Hardware.) Audio comes from Navidrome's stream, decoded with ffmpeg in memory; nothing is written.

3. **After every Nova download.** When a song lands (0012 section 7), Nova judges the new copy before saying anything.
   - Explicit, or fewer than 3 swears in its lyrics: "X has been downloaded.", as now. Listening that failed is said differently (5).
   - Unsure: "X has been downloaded, but I couldn't tell if it's the clean version." It goes on the page's Unsure list.
   - Clean: Nova swaps it (4). If the swap brings an explicit copy: "X has been downloaded." If every try comes down clean: "X only came down clean, so I've kept it." and it goes on the page's list.
   - For an album, each new track is judged; clean tracks are swapped one at a time as songs, under the album, preferring a SoulSync candidate on the same album. The album's message names how many were swapped and how many only came down clean.
   - The "came down as a clean version" sentences and the album-name checks behind them go.
   - Messages keep 0012's rules: to the asking satellite at any hour, else the phone.

4. **The swap.**
   1. Move the clean file, and its `.lrc` and `.txt` beside it, from the music folder into a hidden holding folder inside it: `.nova-held/`, same relative path. A blank `.ndignore` in `.nova-held/` keeps Navidrome out of it (Navidrome's documented rule). A move is a hard link then an unlink, never overwriting; a name already taken in the holding folder gets " (2)", " (3)" and so on. Nothing outside the music folder is ever touched, and nothing is ever deleted.
   2. Ask Navidrome to scan, and wait until it no longer lists the moved copy.
   3. Make SoulSync forget it: sign in to SoulSync as Nova's admin profile (`POST /api/auth/login`), find SoulSync's own row for that file (its library search, matched by file path), and remove only the row (`DELETE /api/library/track/<id>`, never with `delete_file`). Without this SoulSync's wishlist drops the request as "already owned" (seen live on 16 Nov).
   4. Fetch again through the wishlist (0012 section 3), watch, and judge the new copy.
   5. Up to "Tries to swap a clean song" more fetches (settings page, Music; default 2). Each try is a fresh request, so SoulSync may pick another source.
   - If anything in steps 1 to 3 fails, the file goes back where it was (if moved) and the song goes on the list with the reason; nothing is fetched.
   - If a re-fetch doesn't land (SoulSync finds nothing, or the song limit passes), Nova takes that request off SoulSync's wishlist (`DELETE /api/v1/wishlist/<id>`, so it can't land later beside a restored copy), moves the copy it last set aside back to where it was (unless something now sits at that path, when both stay and the list says so), asks Navidrome to scan, and lists the song under "Couldn't swap" with the reason. The user never ends up with no copy of a song he had.
   - A swap's progress (what was moved where, which try, what was asked of SoulSync, when) is saved in Nova's data folder as it goes. After a restart Nova carries each unfinished swap on: still within its limit, it keeps watching; past it, it restores as above.
   - A swap is never started for an unsure song.
   - Finding the file: Nova asks Navidrome's native API for the song's real path (`/api/song/<id>`, relative to its one library at `/music`, which is the music folder; checked live 15 Nov), signed in as Nova's Navidrome user, and joins it to the music folder on the page. It refuses a path that is absolute, climbs out with `..`, or isn't a file there. The Subsonic `path` is never used, as Navidrome builds it from tags.

5. **Checking the library.** A "Check library" button on the settings page, under Music, starts a background check of every song in Navidrome whose lyrics have at least 3 swears. It judges each and swaps the clean ones as in 4, one song at a time.
   - Progress ("Checking 112 of 398: artist - title"), the last finished check's date, and the lists (Clean and swapped, Only comes down clean, Unsure, Couldn't swap and why) show on the page. They are kept in Nova's data folder, so a restart doesn't lose them; a check stopped by a restart carries on from where it was.
   - A "Stop" button ends it after the current song.
   - Verdicts are kept per song: Navidrome's song id with the file's size and modified time from the native API. A later check only listens to songs that are new or whose file changed, and never tries again a song that only came down clean; a download's verdict is kept the same way. A download's judging goes ahead of a running check.
   - When listening fails for a song (no lyrics, a worker or ffmpeg error), it is "not judged" with the reason, and for a download Nova says "X has been downloaded, but I couldn't check it for a clean version." rather than a plain "downloaded".
   - It says nothing aloud and sends nothing to the phone. Results are on the page.
   - The page also says how many files are in `.nova-held/`. Emptying it is the user's job, outside Nova.

6. **Set-up.**
   - pve2 shares only the music folder with the brain's container: `mp1: /mnt/library/media/music,mp=/mnt/music` (read-write). Not the parent folder, which also holds films, shows and VOD. Inside the container the music files belong to `nova` already (uid 1000 = pve2's 101000), so no user changes are needed. The container restarts for it.
   - The music folder's path goes on the settings page (Music, "Music folder", default `/mnt/music`). With no folder, or one Nova can't write to, songs are still judged, and clean ones are listed instead of swapped.
   - Nova gets its own SoulSync profile with admin rights, if SoulSync allows a second admin (else the user's own). Its profile name goes on the page (Music, "SoulSync admin profile"); its password goes under Keys as `SOULSYNC_ADMIN_PASSWORD`, never shown back.

7. **The music leftovers** from 0012's build ledger, done in the same round as they touch the same code:
   - "demo" joins navidrome.VERSIONS, so matching what's had treats a demo as a version (play's matching changes with it; the builder checks play).
   - The watch's dead "done" and "failed" branches and their tests go (SoulSync never says done for songs or albums since the wishlist route).
   - A dashed part drops its own brackets before it is read ("Song - Extended Mix (feat. X)" is a mix).
   - A wider title no longer counts as had for a download ("Endless Love" isn't "Endless"); play's partial titles are unchanged.
   - `process_wishlist` isn't called when SoulSync queued nothing.
   - "clean lyrics" and "clean advance copy" read as clean versions when picking.

8. **Every night, a weekly note, and only explicit swapped in** (added 17 Nov 2026, from his answers passed on by the new-music session: "do both: a one-off sweep, then nightly checks of new arrivals"; "swap it in only if the new copy is really explicit; keep the clean original, never delete it; a short weekly note"). SoulSync's own automations now bring hundreds of songs a week (decision 0020), which aren't Nova downloads, so only a check hears them.
   - **Nightly check.** Nova starts the library check itself once a night at "Nightly check at" (settings page, Music; default 05:30, after SoulSync's 04:00 daily mixes and Monday's 03:00 discovery), with a switch "Check new songs every night" (default on). As a check only hears songs that are new or whose file changed (5), a night's check is short. A check already running or paused is carried on rather than started again; one missed while Nova was off isn't made up until the next night.
   - **Only explicit is swapped in.** A swap keeps the new copy only if it is heard explicit (or has too few swears to judge, as a song with nothing to censor). If the last try's new copy is unsure, or couldn't be heard, it is set aside into `.nova-held` too and the original clean copy is put back; the song goes on the Unsure or Couldn't swap list. Never deleted, either copy. (Supersedes 3's "unsure: kept" for the swap's new copy; a download heard unsure on landing, before any swap, is still kept as 3 says.)
   - **Weekly note.** Once a week, on "Weekly note on" and "at" (settings page, Music; default Monday 09:30, outside quiet hours) with a switch "Send a weekly note" (default on), Nova sends one ntfy message to the phone topic, only when something happened since the last note: how many songs were swapped, only came down clean, are unsure or couldn't be swapped, and how many files wait in `.nova-held`, with the page's address to look. A note due in quiet hours waits for them to end. The lists' entries carry when they were added, so the note counts only the week's.

**Not part of this.**
- Deleting anything, including the holding folder.
- Downloading from Deezer or another paid source (SoulSync has no Deezer login here).
- Listening to songs whose lyrics have fewer than 3 swears.
- Changing SoulSync's tagging. The "(clean)" album notes stay; Nova just stops believing them.

**Must not happen.**
- A file deleted, overwritten, or moved anywhere but `.nova-held/` inside the music folder.
- A swap of a song judged explicit or unsure.
- A clean copy reported as "downloaded".
- Nova's replies slowed by the listening (it runs apart, at the lowest priority).
- A key or password in a log, a message or the page.

**Left to the builder.**
- How the worker process is started, fed and closed; how Navidrome's stream is decoded.
- The LRCLIB search details and its timeout.
- How SoulSync's row is matched to the moved file, checked against the running SoulSync (read-only) before writing the client.
- The exact words beyond those fixed above.
- Where in the page's Music section the controls sit.

**Tests** fake Navidrome, SoulSync, LRCLIB, the worker and the file system (temporary folders), as 0012's tests do. Nothing in the repository names the user's hosts or holds a key. The model itself isn't loaded in tests.

**Hardware.** pve2 has no GPU, so listening runs on its processor: about 35 s for a 2-minute song on two cores, so a first library check of about 400 songs takes three to four hours, and each download is about half a minute later to be called downloaded. A GPU in the planned AI machine would make both a matter of seconds. The worker's own Nemotron needs about 1 GB of memory while it runs; the brain's container has 4 GB, about 2.8 GB free.
