# New music, found and fetched for him

16 November 2026. Agreed in chat with Nathaniel.

## Why

His words: "storage soon will be no issue whatsoever so i may as well be downloading lots of music to make the library bigger and make my music player more like spotify. i want alot alot so im not always listening to same music." He picked option 1 of two: tracks picked to his taste, Spotify-style, over whole albums from similar artists.

Success: every week a few hundred new songs he is likely to enjoy land in Navidrome by themselves, mixed into playlists he can open in Symfonium or ask Nova for, and new releases from the artists he plays most arrive without asking.

## What exists (found 16 Nov)

- SoulSync already keeps a discovery pool (about 82,600 tracks from about 10,000 artists similar to his) and makes personalised playlists from it and from his listening: `discovery_shuffle`, `hidden_gems`, `popular_picks`, `listening_mix`, `daily_mix` (variants 1 to 4), `time_machine`, `genre_playlist`, `seasonal_mix`. `fresh_tape` and `archives` mirror Spotify's Release Radar and Discover Weekly and need a linked Spotify account, which SoulSync hasn't got, so they are left out.
- Its automation `personalized_pipeline` (action config: `kinds`, `refresh_first`, `skip_wishlist`, `sync_mode`) refreshes chosen playlists, syncs them to Navidrome and puts missing tracks on the wishlist. Its existing automations then download the wishlist ("Auto-Process Wishlist") and rescan. None of the personalised playlists exist yet.
- Corrected 16 Nov after the final review, from SoulSync's source: `discovery_shuffle` and `hidden_gems` select only tracks he doesn't own, and a personalised playlist sync which matches nothing makes no Navidrome playlist (only mirrored playlists get an empty one). The re-sync after downloads ("Auto-Sync Playlists After Database Update") covers mirrored playlists only. So the discovery playlists download their songs but never reach Navidrome; a playlist of mostly owned songs, such as a Daily Mix, does.
- Its artist watchlist downloads new releases of watched artists ("Auto-Scan Watchlist", daily). One artist is watched. `POST /api/watchlist/add` takes an `artist_id` and `artist_name`; a numeric id is read as SoulSync's own library artist id, which is what its listening stats give.
- Every route needed (`/api/personalized/...`, `/api/automations`, `/api/watchlist/...`) accepts Nova's existing SoulSync key; only `/api/settings` needs an admin login, and none of this needs it.
- `content_filter.allow_explicit` is on (the default). It stays on: he wants explicit versions, never clean ones.
- Nova's `play_playlist` already plays any Navidrome playlist by name.

## Goals

1. **Playlists, made and downloaded by SoulSync** (my numbers, his to change in SoulSync's own Discover page). They are where the new songs come from; Discovery Shuffle and Hidden Gems never appear in Navidrome themselves (see "What exists"), so what he opens is Nova's New Music playlist (goal 3):

   | Playlist | Kind | Refreshed | Size |
   | --- | --- | --- | --- |
   | Discovery Shuffle | `discovery_shuffle` | weekly, Monday 03:00 | 50 |
   | Hidden Gems | `hidden_gems` | weekly, Monday 03:00 | 30 |
   | Your Listening Mix | `listening_mix` | weekly, Monday 03:00 | 50 |
   | Daily Mix 1 and 2 | `daily_mix` 1, 2 | nightly 04:00 | 25 each |

   Two `personalized_pipeline` automations (weekly and nightly), `refresh_first` on, `sync_mode` "replace" so each playlist is the fresh list. Rough yield: 150 to 300 new songs a week; an estimate, not a cap (the Daily Mixes are "mostly songs you own", so they add few).
2. **New releases from his top artists:** Nova follows his top 25 artists of the last 12 months (from the listening stats it already reads) on SoulSync's watchlist, on the 1st of each month, adding only, never removing. SoulSync's daily watchlist scan downloads their new releases.
3. **Nova's New Music playlist, and by voice:** Nova keeps its own playlist in Navidrome, "New Music" by default, of the songs that arrived in the last 14 days, newest first, remade each morning; he opens it in Symfonium, and "find me something new" plays it; "play my Daily Mix" already works through `play_playlist`; "what's new this week?" says how many albums and songs arrived in Navidrome in the last 7 days and names a few artists.
4. **Settings page:** the new-music playlist's name, how many days it reaches back, and how many top artists to follow (0 switches the following off).

## Non-goals

- Removing songs he skips or never plays.
- Whole discographies of similar artists (option 2, which he turned down).
- Recommendations from a language model (waits for the AI machine).
- Fresh Tape and Archives (need Spotify linked).
- Clean versions, ever.

## Anti-goals

- Hammering Soulseek or the music disk: the yield above is the ceiling until he asks for more.
- Nova keeping its own copy of what SoulSync already does (scheduling, picking, history of what was served).
- Anything in the public repo naming his artists or listening.

## Approaches

1. **SoulSync's own automations, Nova for voice and following (chosen).** Everything that picks and downloads is SoulSync's, tuned once; Nova adds the parts SoulSync lacks: voice and following his top artists.
2. Nova schedules everything (refreshes, wishlisting) from its settings page. Duplicates SoulSync's scheduler and history; more code, more to break on a SoulSync update.
3. Lidarr with import lists. Lidarr adds whole albums and artists, which is option 2.

## Decided

- **One-off SoulSync setup** by a small, idempotent script kept in the `homelab` repo (`services/soulsync/discovery-setup.py`), run once on his named job: it sets the playlists' sizes, creates them, and creates the two automations if they aren't there. Run again, it changes nothing.
- **Following** lives in Nova (monthly, after 03:00 on the 1st, beside the Wrapped watcher's look): a top artist already watched is left alone; the follow is logged by count only.
- Day-to-day tuning of playlists and schedules is in SoulSync's own pages; Nova's settings page holds only Nova's three settings.
- **Nova's New Music playlist** (the controller's ruling of 16 Nov, after the final review found the discovery playlists never reach Navidrome): `voice_assistant/new_music.py`, once a day at its first look after 06:00 (SoulSync's nightly run is 04:00), gathers the songs that arrived in Navidrome in the last `new_music.days` days (default 14, 1 to 60), newest first, at most 500, and makes or replaces the playlist `new_music.playlist` names (default "New Music", was "Discovery Shuffle") through Subsonic: found by exact name among the playlists of the user Nova signs in as, its songs replaced with `createPlaylist`, set public with `updatePlaylist`. Nothing new leaves it as it is; Navidrome down is tried again at the next hourly look; no other playlist is touched; counts only in the log. It isn't a copy of anything SoulSync does: SoulSync has no playlist of what arrived.

## Answers from the builder check (all from SoulSync's code and data, 16 Nov)

1. **What following an artist downloads:** SoulSync's watchlist scan looks back `discovery_lookback_period` days for an artist it has never scanned; it is 30. So a newly followed artist brings their last month's releases, then each new one; never their back catalogue. Nova doesn't touch the lookback.
2. **Which artist id:** the 12-month stats' `top_artists` holds 25 entries; each `id` is SoulSync's own library artist id (all found in its `artists` table), but it isn't numeric, while `/api/watchlist/add` only treats numeric ids as library ids and may read others as a provider's id. The builder settles the exact request against the route's code (an explicit `source` with the artist's provider id, or `add-batch`), and checks after each add that the watchlist row's name is the artist's; a mismatch is undone and logged. Corrected after the final review: SoulSync stores the name Nova sends, so that check can't catch a wrong id. The real protection is that the ids are SoulSync's own enrichment ids for that library artist, the same trust as its own watch button; the check stays as harmless defence.
3. **Playlist sizes:** per playlist, `PUT /api/personalized/playlist/<kind>[/<variant>]/config` with `limit`; no admin login.
4. **"What's new this week?":** everything that arrived in Navidrome in the last 7 days (its newest albums), whatever brought it in. Simple, and the clean-up's replacements counting as new is fine. (My call.)
5. **Playlist names:** SoulSync's own: "Discovery Shuffle", "Hidden Gems", "Your Listening Mix", "Daily Mix 1", "Daily Mix 2". The setup run confirms them. Only those matching songs he owns reach Navidrome; Nova's "New Music" is the one he opens.
6. **The yield** is an estimate, not a cap.

## Constraints

- British spelling, no em dashes, no emojis; Nova's comment style.
- Lab writes (the setup script's run, deploy, restart, pushes) need his named job.
- CT 109 load limits for tests.
- Hardware: the music disk (500 GB, about 10 GB used) holds years of this; the 12 TB at Christmas removes the limit. Soulseek's speed is the real ceiling.
