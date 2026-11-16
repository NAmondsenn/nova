# 0020: New music, found and fetched

16 November 2026 · Agreed with the user in chat · Built

**Context.** The user wants a much bigger music library, so that his player is more like Spotify and he isn't always hearing the same songs. Storage is about to stop being a limit. Of two options he chose tracks picked to his taste, Spotify-style, over whole albums from similar artists. The design is in `docs/superpowers/specs/2026-11-16-new-music-design.md`, the plan in `docs/superpowers/plans/2026-11-16-new-music.md`.

**SoulSync picks and downloads; Nova adds the voice and the following.** SoulSync already keeps a pool of tracks by artists similar to his, makes personalised playlists from it and from his listening, wishlists what the library lacks and downloads it, and downloads new releases of the artists on its watchlist. None of the playlists were made and the watchlist held one artist. So the work is to switch SoulSync's own parts on, and to add only what it lacks. Two other approaches were rejected:

- *Nova schedules everything, from its settings page.* A second scheduler and a second history beside SoulSync's, more code, and more to break on a SoulSync update.
- *Lidarr with import lists.* It adds whole albums and artists, which is the option he turned down.

**The playlists, by a one-off script.** `services/soulsync/discovery-setup.py` in the private homelab repository sets the sizes of SoulSync's playlists (Discovery Shuffle, Hidden Gems, Your Listening Mix, and two Daily Mixes) and creates two of SoulSync's `personalized_pipeline` automations, one weekly and one nightly, which refresh the playlists, sync them to Navidrome and wishlist what is missing; SoulSync's existing automations then download the wishlist and re-sync once the downloads are in. It is idempotent, a dry run unless given `--apply`, and run once on his named job. Day-to-day tuning stays in SoulSync's own pages: Nova holds none of it. The sizes are mine to start from and his to change.

**Following his top artists, in Nova.** `following.py` has a watcher which, once a month from 3 in the morning, puts his top artists of the last 12 months, as the listening stats already give them, on SoulSync's watchlist; SoulSync's daily watchlist scan then downloads their new releases. The stats' artist ids are SoulSync's library ids, which the watchlist doesn't take as they are, so each artist is added by the provider ids the library holds for it, with an explicit source and the other ids linked. Each new row is then checked to be the artist listened to, by name, and a wrong one is taken off again, kept in a record until it is, so a restart never leaves it there. It only ever adds: an artist he stops playing stays followed, and one already watched is left alone. A new follow brings the last month of releases, then each new one, never the back catalogue. His listening is never logged, only counts. How many to follow is `new_music.follow_top_artists`, 25 by default and 0 to switch it off.

**By voice.** "Find me something new" plays `new_music.playlist`, "Discovery Shuffle" by default, which `play_playlist` can only do by an exact name here, so a near miss is never taken for it. It is Navidrome only, and if the playlist isn't there yet Nova says so. "What's new this week?" reads from Navidrome's newest albums, whatever brought them in, how many albums and songs and the artists with the most, for up to 31 days back. The clean-up's replacements count as new, which is fine. "Play my Daily Mix" already worked.

**Explicit, never clean.** SoulSync downloads explicit versions, as he wants. Nothing here changes that setting.

**Consequences.**

- Nova leans on SoulSync's own API for the watchlist and the library artist, which is not its versioned one, and its image is `:latest`. A change makes the following say so politely, and leave the month to the next look, rather than guess.
- Soulseek's speed, and the 500 GB music disk, are the limits. A rough 150 to 300 new songs a week is an estimate, not a cap, and the Daily Mixes, being mostly songs he owns, add few. The 12 TB disk at Christmas removes the disk limit.
- Not built: removing what he skips, whole discographies of similar artists, recommendations from a language model (waits for the AI machine), and Fresh Tape and Archives, which need a linked Spotify account.
