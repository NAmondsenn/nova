# Listening stats and Wrapped

15 November 2026. Draft for his approval; asked for from the car with two SoulSync screenshots, so the calls marked "my call" are mine and his to change.

## Why

His words: "listening stats like soulsync too, i really like these stats ... and how you can make like a spotify wrapped". He sent SoulSync's Stats page (the 7-day totals, top artists, the activity chart and the when-you-listen grid) and its Library Health block (formats, unplayed tracks, track count, disk size).

My reading, his to correct: he wants those stats where he already lives (asking Nova, and Home Assistant), and a proper Wrapped he looks forward to, the way Spotify's was before Navidrome replaced it.

Success: he can ask Nova "who have I listened to most this month?" and get a short spoken answer; he opens a Music view in Home Assistant and sees the same kind of picture as SoulSync's page; and in early December he gets a "your 2026 Wrapped is ready" notification that opens a tap-through story on his phone.

## What exists today (found 15 Nov)

- SoulSync (CT 102, `boulderbadgedad/soulsync:latest`) already records every play: it polls Navidrome and keeps `listening_history` (from 11 Oct 2026). Navidrome 0.64.2 (CT 104) keeps its own `scrobbles` table over the same dates, with near enough the same count, so the two agree.
- SoulSync serves the stats behind its page: `/api/stats/cached`, `overview`, `top-artists`, `top-albums`, `top-tracks`, `timeline`, `genres`, `library-health`, `library-disk-usage`, and `/api/stats/year` ("Your Year", a fixed last-12-months story with top lists and discoveries; the button on his screenshot). Its worker rebuilds them every 30 minutes, so reads are instant.
- Nova already talks to SoulSync (`voice_assistant/soulsync.py`, bearer key `SOULSYNC_API_KEY`, address on the settings page) for fetching music by voice.
- SoulSync's stats come only in rolling windows: last 7 days, 30 days, 12 months, or all time. There is no endpoint for raw plays, so no calendar periods ("today", "October") from it.
- History only starts on 11 Oct 2026. Everything before that was on Spotify. Navidrome's `scrobbles` rows are deleted when a track's file is (ON DELETE CASCADE); SoulSync's history keeps title and artist as text, so it survives library clean-ups.

## Goals

1. **Ask Nova.** A listening-stats tool for Claude: plays, listening time, top artists, songs and albums, over the last 7 days, 30 days, 12 months or all time ("this week", "this month", "this year" map to those). Short spoken answers: "This week you listened for 12 hours. Artist One was top with 40 plays, then Artist Two and Artist Three."
2. **Music view in Home Assistant**, in the glass theme like the AI view: plays, listening time, artists and tracks with change against the period before; listening by day chart; when-you-listen grid; top artists with pictures; a small library block (tracks, hours, unplayed share, size, formats). One period switch: 7 days, 30 days, 12 months, all time.
3. **Wrapped.** A tap-through story page on his phone, one card at a time: minutes listened, top artist (with picture), top five artists, top song, top album, top genre, when you listen (night owl or morning), longest streak, busiest day, each month's top artist, artists discovered, the year in numbers. One for the year (ready 1 December, like Spotify's) and a smaller one for each month (ready on the 1st). An ntfy notification links to it. "Nova, what's my Wrapped?" reads a short version aloud.
4. **Settings page:** Wrapped on or off, monthly recaps on or off, when the yearly one is ready (default 1 December).

## Non-goals

- Recommendations or anything needing a local model (waits for the AI machine, memory ai-machine-music).
- A new service or container. SoulSync, Navidrome, Nova and HA are enough.
- Spoken announcements Nova starts by itself. Only the Wrapped notification, and only through ntfy.
- Replacing SoulSync's own Stats page; it stays as it is.

## Anti-goals

- Numbers that disagree with SoulSync's page. One source, so they can't.
- Making Nova's replies slower: stats come from SoulSync's ready-made cache, never a scan per question.
- His listening data in the public repo. Test data uses made-up artists and songs.
- A Wrapped that looks like a default HA dashboard. It should feel like an event.

## Approaches

1. **SoulSync as the one source (recommended).** Nova and HA read SoulSync's stats API. No new history to keep, numbers match his favourite page, history survives library clean-ups. Risk: `/api/stats/*` is SoulSync's internal API, not its versioned `/api/v1`, and the image is `:latest`, so an update could change it. Kept to one small reader in Nova with tests on recorded answers, and a polite spoken failure if it breaks.
2. Nova keeps its own history by watching Navidrome's now-playing. A second copy of what SoulSync already does, more code, and a second set of numbers.
3. Read Navidrome's database directly. Cross-container file access, no API, and plays vanish when files are deleted. Ruled out.

## Decided (my calls, his to change)

- **Source:** approach 1.
- **Where the Music view gets its data:** Nova reads SoulSync and announces a "Nova music" device to HA over MQTT, the way `usage_device.py` feeds the AI view: sensors for the numbers and lists, and image entities for the top artists' pictures. Why not HA's own REST sensors: SoulSync's pictures are Navidrome `/rest/getCoverArt` paths that need Navidrome's sign-in, which a browser showing HA can't give, while Nova already fetches covers for the clock display; and the key stays in one place. Refreshed every 30 minutes, when SoulSync rebuilds.
- **Where Wrapped lives:** a page on Nova's existing web server (behind the settings page's sign-in, through NPM like the settings page), linked from the HA Music view and the ntfy notification. Not an HA view: a story needs full-screen cards and swipes HA cards can't do well.
- **Wrapped periods:** the yearly one is made on 1 December from SoulSync's last 12 months (its "Your Year" data plus the 12-month stats), as Spotify's also stops short of the calendar year. A monthly one is made at the start of the 1st from the last 30 days, with the real dates shown (so a 31-day month shows 2 to 31). Each is saved on the brain when made (a few KB of JSON), so it stays as it was even after files change.
- **Rolling windows, not calendar months:** what SoulSync gives. The cost: Nova can't answer "how was my October?" or "what did I play today?". Getting those means Nova keeping its own play log by watching Navidrome (approach 2), a bigger build I'd only add if he misses them.
- **Dashboard:** a Music view on the desktop dashboard (`dashboard-home`) and the same on the phone dashboard (`dashboard-mobile`), since he looks at this on his phone.
- **What SoulSync can't supply is left out, not built.** Its cache has, per window: overview, the period before (for the change figures), timeline, clock (by hour), rhythm (the week grid and streaks), genres, top artists, albums and tracks; its year data adds totals, active days, each month's top artist, peak day, top hour and discoveries; plus library health. Any card needing more is dropped rather than making Nova keep its own play log.
- **Pictures on the Wrapped page** come through Nova, as the clock display's covers do.
- **Away from home:** works, as his phone runs Tailscale always-on with AdGuard's DNS, so the lab's own DNS names resolve anywhere (vault network.md).

## Open for him

1. **Spotify history before 11 Oct.** Without it, the 2026 Wrapped covers 11 Oct to 30 Nov only. Option: request his "Extended streaming history" from Spotify's privacy page (takes up to 30 days, so it has to be asked for now to be in time), upload it to ListenBrainz (a free scrobble service, which accepts Spotify exports), and let SoulSync import it through its existing ListenBrainz import. That puts his listening on a third-party service; his call.
2. **SoulSync's own "Your Year".** It already exists (the button on his screenshot). If he's happy with it, part 3 shrinks to a link and a notification. I recommend building Nova's own on the same data: monthly recaps, saved copies, spoken, his look, and a notification when it's ready.

## Constraints

- British spelling, no em dashes, no emojis; Nova's comment style (memory working-style, design-principles).
- CT 109 load limits for all tests (memory ct109-load-limits).
- Deploys, pushes, `nova-backend` restarts, HA config saves and ntfy topic changes need his named OK (memory auto-mode-named-jobs).
- The brain's 8 GB disk: saved Wrappeds are small and capped.
- No GPU needed anywhere; nothing here is shaped by hardware.

## Order of work

0. Check that Nova's SoulSync key can read `/api/stats/*` (SoulSync limits read-only keys to `/api/v1`; Nova's key may be admin). If not, he makes a key in SoulSync's settings. Record sample answers with names swapped as test fixtures.
1. Nova: the SoulSync stats reader and the listening-stats tool for Claude.
2. Nova: the "Nova music" HA device (sensors and artist pictures); then the Music view on both dashboards (`ha-dashboard` repo).
3. Nova: Wrapped page, monthly and yearly making, ntfy, settings page controls, "what's my Wrapped".
4. If he says yes to open item 1: his Spotify request, then the ListenBrainz import check.

Each task Opus-reviewed, one commit per fix, a regression test that fails on the old code; branch `listening-stats` from `main` b9c969d.
