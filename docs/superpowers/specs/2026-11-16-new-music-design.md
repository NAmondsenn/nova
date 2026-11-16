# New music, found and fetched for him

16 November 2026. Agreed in chat with Nathaniel.

## Why

His words: "storage soon will be no issue whatsoever so i may as well be downloading lots of music to make the library bigger and make my music player more like spotify. i want alot alot so im not always listening to same music." He picked option 1 of two: tracks picked to his taste, Spotify-style, over whole albums from similar artists.

Success: every week a few hundred new songs he is likely to enjoy land in Navidrome by themselves, mixed into playlists he can open in Symfonium or ask Nova for, and new releases from the artists he plays most arrive without asking.

## What exists (found 16 Nov)

- SoulSync already keeps a discovery pool (about 82,600 tracks from about 10,000 artists similar to his) and makes personalised playlists from it and from his listening: `discovery_shuffle`, `hidden_gems`, `popular_picks`, `listening_mix`, `daily_mix` (variants 1 to 4), `time_machine`, `genre_playlist`, `seasonal_mix`. `fresh_tape` and `archives` mirror Spotify's Release Radar and Discover Weekly and need a linked Spotify account, which SoulSync hasn't got, so they are left out.
- Its automation `personalized_pipeline` (action config: `kinds`, `refresh_first`, `skip_wishlist`, `sync_mode`) refreshes chosen playlists, syncs them to Navidrome and puts missing tracks on the wishlist. Its existing automations then download the wishlist ("Auto-Process Wishlist"), rescan, and re-sync the playlists once the downloads are in ("Auto-Sync Playlists After Database Update"). None of the personalised playlists exist yet.
- Its artist watchlist downloads new releases of watched artists ("Auto-Scan Watchlist", daily). One artist is watched. `POST /api/watchlist/add` takes an `artist_id` and `artist_name`; a numeric id is read as SoulSync's own library artist id, which is what its listening stats give.
- Every route needed (`/api/personalized/...`, `/api/automations`, `/api/watchlist/...`) accepts Nova's existing SoulSync key; only `/api/settings` needs an admin login, and none of this needs it.
- `content_filter.allow_explicit` is on (the default). It stays on: he wants explicit versions, never clean ones.
- Nova's `play_playlist` already plays any Navidrome playlist by name.

## Goals

1. **Playlists, made and downloaded by SoulSync** (my numbers, his to change in SoulSync's own Discover page):

   | Playlist | Kind | Refreshed | Size |
   | --- | --- | --- | --- |
   | Discovery Shuffle | `discovery_shuffle` | weekly, Monday 03:00 | 50 |
   | Hidden Gems | `hidden_gems` | weekly, Monday 03:00 | 30 |
   | Your Listening Mix | `listening_mix` | weekly, Monday 03:00 | 50 |
   | Daily Mix 1 and 2 | `daily_mix` 1, 2 | nightly 04:00 | 25 each |

   Two `personalized_pipeline` automations (weekly and nightly), `refresh_first` on, `sync_mode` "replace" so each playlist is the fresh list. Rough yield: 150 to 300 new songs a week; an estimate, not a cap (the Daily Mixes are "mostly songs you own", so they add few).
2. **New releases from his top artists:** Nova follows his top 25 artists of the last 12 months (from the listening stats it already reads) on SoulSync's watchlist, on the 1st of each month, adding only, never removing. SoulSync's daily watchlist scan downloads their new releases.
3. **Nova, by voice:** "find me something new" plays the new-music playlist (Discovery Shuffle by default); "play my Daily Mix" already works through `play_playlist`; "what's new this week?" says how many albums and songs arrived in Navidrome in the last 7 days and names a few artists.
4. **Settings page:** the new-music playlist's name, and how many top artists to follow (0 switches the following off).

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
- Day-to-day tuning of playlists and schedules is in SoulSync's own pages; Nova's settings page holds only Nova's two settings.

## Answers from the builder check (all from SoulSync's code and data, 16 Nov)

1. **What following an artist downloads:** SoulSync's watchlist scan looks back `discovery_lookback_period` days for an artist it has never scanned; it is 30. So a newly followed artist brings their last month's releases, then each new one; never their back catalogue. Nova doesn't touch the lookback.
2. **Which artist id:** the 12-month stats' `top_artists` holds 25 entries; each `id` is SoulSync's own library artist id (all found in its `artists` table), but it isn't numeric, while `/api/watchlist/add` only treats numeric ids as library ids and may read others as a provider's id. The builder settles the exact request against the route's code (an explicit `source` with the artist's provider id, or `add-batch`), and checks after each add that the watchlist row's name is the artist's; a mismatch is undone and logged.
3. **Playlist sizes:** per playlist, `PUT /api/personalized/playlist/<kind>[/<variant>]/config` with `limit`; no admin login.
4. **"What's new this week?":** everything that arrived in Navidrome in the last 7 days (its newest albums), whatever brought it in. Simple, and the clean-up's replacements counting as new is fine. (My call.)
5. **Playlist names in Navidrome:** SoulSync's own: "Discovery Shuffle", "Hidden Gems", "Your Listening Mix", "Daily Mix 1", "Daily Mix 2". The setup run confirms them.
6. **The yield** is an estimate, not a cap.

## Constraints

- British spelling, no em dashes, no emojis; Nova's comment style.
- Lab writes (the setup script's run, deploy, restart, pushes) need his named job.
- CT 109 load limits for tests.
- Hardware: the music disk (500 GB, about 10 GB used) holds years of this; the 12 TB at Christmas removes the limit. Soulseek's speed is the real ceiling.
