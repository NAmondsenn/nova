# Nova's music in Windows' media controls

Date: 19 November 2026. Approved by the user in chat ("yes"), after a throwaway test on his PC proved it works.

## Why

Music Nova plays on the PC satellite isn't visible or controllable on the PC: no entry in the volume pop-up, and the media keys and Numpad Enter (which sends the play/pause media key) do nothing to it. Windows' own media system (System Media Transport Controls, SMTC) fixes that, and HASS.Agent's Windows media player, which reads it, shows Nova's music in Home Assistant for free.

## The test (19 November, 11:14-11:25)

A windowless `pythonw` started by Task Scheduler in his session registered as a media session through `winrt` (`MediaPlayer` with its command manager off, then `system_media_transport_controls`), showed in Windows' session list as the current session, and received play, pause, next and previous within a millisecond, playing or paused. **Setting `is_play_enabled = is_pause_enabled = True` in one chained line left next and previous off without an error; set each flag on a line of its own.** Windows named it after the program: "pythonw.exe".

## What it does

- **Brain to PC.** For a satellite which says in its hello that it has Windows media controls (`media_controls: true`), its session sends `now_playing {"state": "playing"|"paused"|"stopped", "track", "artist", "album"}` whenever any of those change, read from the satellite's own player's `now_playing()` every `MUSIC_EVERY` (5) seconds and straight after acting on a button. Music "elsewhere" (Spotify's `here: False`) counts as stopped. When the cover changes, the message carries the cover image as its payload (with `"cover": true`); otherwise no payload.
- **PC to Windows.** `voice_assistant/windows_media.py`, used only by a push-to-talk satellite on Windows with `winrt` importable (else it does nothing, and the hello says false): shows the track, artist, album, cover and playing/paused as a music session, and clears it (disabled) when stopped. Speech is never shown.
- **Buttons.** Play, pause, next and previous from Windows go to the brain as `media {"action": "play"|"pause"|"next"|"previous"}`. The brain acts on **this satellite's own music only**: pause pauses it (and cancels any resume after speech), play resumes it, or plays as Home Assistant's play button does when nothing is loaded, next and previous skip. Then it sends `now_playing` at once.
- **Name.** The satellite sets its AppUserModelID to `Nova.Satellite` before registering, and `install.ps1` makes a Start-menu shortcut "Nova" carrying that AppUserModelID (and the launcher as its target), so Windows shows "Nova" rather than "pythonw.exe".
- **Packages.** The winrt packages (winrt-runtime, winrt-Windows.Foundation, winrt-Windows.Foundation.Collections, winrt-Windows.Media, winrt-Windows.Media.Playback, winrt-Windows.Storage.Streams) are pinned in `requirements-pc.txt` at the versions installed on the PC on 19 November (4.2.0 and 4!10.0.28000.2957).

## Not done

No seek bar or position, no volume (Windows' slider stays the system volume), nothing on the Pi, no change to the room-wide voice rules (0026).
