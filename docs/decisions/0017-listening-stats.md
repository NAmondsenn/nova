# 0017: Listening stats and the Wrapped, from SoulSync

15 November 2026 · Agreed with the user in chat · Built

**Context.** The user likes SoulSync's Stats page (plays, listening time, top artists, when you listen) and wanted those where he already is: asking Nova, and Home Assistant, and a Wrapped like Spotify's, which Navidrome doesn't have. The design is in `docs/superpowers/specs/2026-11-15-listening-stats-design.md`, the plan in `docs/superpowers/plans/2026-11-15-listening-stats.md`.

**One source: SoulSync.** SoulSync already records every play, since it polls Navidrome, and serves the stats behind its page, rebuilt every 30 minutes. `listening.py` reads them into one snapshot, with the top artists' pictures, every 30 minutes, and the voice tool, the Home Assistant device and the Wrapped all read that snapshot and nothing else, so what Nova says, shows and recaps can't disagree with each other or with SoulSync's page. A question is answered from the snapshot at once, never by a scan. A refresh which fails leaves the last good snapshot in place, with the reason beside it. Two other approaches were rejected:

- *Nova keeps its own history by watching Navidrome's now-playing.* A second copy of what SoulSync already does, more code, and a second set of numbers which could disagree with the page he likes.
- *Reading Navidrome's database directly.* Cross-container file access, no API, and a play's row goes when its file is deleted. SoulSync's history keeps the title and artist as text, so it survives a clean-up of the library.

The risk accepted is that `/api/stats/*` is SoulSync's internal API, not its versioned `/api/v1`, and its image is `:latest`. The reader checks the shape it gets, tests run on recorded answers (with made-up artists and songs, as the repository is public), and a change is said politely, never guessed at.

**Rolling windows, so no calendar periods.** SoulSync's stats come only as the last 7 days, 30 days, 12 months or all time. Nova can't answer "how was my October?" or "what did I play today?", and doesn't pretend to: "this week", "this month" and "this year" are taken as 7 days, 30 days and 12 months, and the tool tells Claude the periods are rolling. What SoulSync's cache lacks is left out, not built. Answering those would mean Nova keeping its own play log, which is the rejected approach above, to be built only if he misses them.

**The Home Assistant device.** Nova announces a "Nova music" device over MQTT, as `usage_device.py` feeds the AI view: a sensor for the plays in each window with the top lists and the rest as attributes, a library sensor, and twenty image entities for each window's five top artists. Home Assistant's own REST sensors would have been less code, but the pictures are Navidrome's cover-art paths, which need Navidrome's sign-in, which a browser showing Home Assistant can't give; Nova already fetches pictures with that sign-in. And SoulSync's key stays in one place. The device reads the snapshot, so it is refreshed when SoulSync rebuilds. The Music view that shows it is in the Home Assistant configuration's repository, not this one.

**The Wrapped.** `wrapped.py` makes a monthly one on the 1st, from the last 30 days, with the real dates shown, and a yearly one on the 1st of the month named by `wrapped.yearly_month` (December), from SoulSync's last 12 months and its "Your Year", as Spotify's also stops short of the calendar year.

- *Made only from a fresh snapshot.* It is made only from a snapshot taken at or after 00:35 on the 1st. SoulSync's last rebuild with the month's final plays is by 00:30 and Nova reads as often, so a read after 00:35 has the whole month. A snapshot from before would miss the end of the month, or, with SoulSync down, be days old; then it waits for the next read rather than making a wrong one.
- *Quiet hours.* The ntfy notification goes to the phone topic and is held through quiet hours (ntfy's `quiet_from` and `quiet_until`), sent at the first look outside them. Whether it has been told is kept in the Wrapped's own JSON, so a restart neither repeats nor loses it.
- *Kept as made.* Each is saved on the brain, in the `wrapped` folder of Nova's data folder, as a JSON file of a few KB with its pictures beside it, so it stays as it was after SoulSync's stats move on. The newest 24 monthly ones are kept, with their pictures, and every yearly one, as there is one a year. The brain's disk is small, so this is capped.
- *Served behind the settings page's login.* The story page, `/wrapped`, and its `/api/wrapped` routes are on the settings page's server, so they need its sign-in, over the tailnet or through NPM, and the pictures come through Nova. It is a page of Nova's own, not a Home Assistant view, because a story needs full-screen cards and swipes which Home Assistant's cards can't do well.
- *By voice.* "What's my Wrapped?" reads a short version of the newest, the year's or the month's, or one by its year or month.

**What's left to the user.**

- The settings page's `wrapped.link`, the settings page's address through NPM, for the link in the notification. It is empty in `config.yaml`, which is public; with none, the notification has no link.
- His Last.fm import. SoulSync's history starts on 11 October 2026, and what came before was scrobbled to Last.fm. SoulSync can import it, matching against what it has by title, artist and time, so the overlap shouldn't double. He enters his Last.fm username and key on SoulSync's settings page himself and runs the import; the plays of the last 7 days must be the same before and after it, and all time should grow by the older plays.

**Consequences.** Nova depends on SoulSync's internal stats API keeping its shape. The Wrapped depends on SoulSync having been up and read at 00:35 on the 1st, else it is made late, from the next read, rather than wrong. A settings page which is down can't show a Wrapped, though the notification and Nova's voice still can.
