# "Mute" means the microphone

Small change to 0034, agreed with him on 20 Nov 2026 ("that is exactly how I want it").

## Why

He took "mute" to mean the microphone. His desk mic has its own touch mute, so what he wants by voice is Nova's listening off, and sound muting only when he says sound or audio.

## What he decided

- Plain "mute", "mute the mic", "mute the microphone" and "stop listening" turn Nova's listening off. Nova says "Microphone off." and stops hearing the wake word until it's turned back on.
- At the Pi it mutes the Pi. At the PC it mutes the room's always-listening satellite (the bedroom Pi), since the PC only listens while the key is held. A device can be named from anywhere: "mute the Pi", "stop listening on the Pi".
- While something rings or Nova is speaking, "mute" still stops it (unchanged).
- Back on: the settings page's microphone switch (exists), a microphone button on the smart clock dashboard and on the Nova view (the Nova device's "Microphone muted" switch in Home Assistant), or by voice from another satellite ("unmute the Pi" at the PC). Dashboard buttons are previewed before saving live.
- Sound muting needs the word: "mute the sound", "mute the audio", "unmute the sound", "turn the sound off/on" do what plain "mute" did in 0034 (Windows at the PC, the music at the Pi, or the device named). Plain "unmute" is the microphone.

## Approaches

1. Reuse Session.set_muted (the settings page's and Home Assistant's mute) from a new action (chosen): one mute state, already shown on the page, the clock display and in Home Assistant.
2. A separate voice-only mute: two states for one thing. Rejected.

## Design

- New action `mute_microphone(muted: bool, where?)` on ActionExecutor (local rules; also offered to Claude for other wordings). Target: `where` via Brain.find_satellite(here=spoken-at); else the spoken-at satellite if it listens for a wake word; else (push-to-talk, the PC) the room's always-listening satellite (Brain.ring_on gives it). It calls that satellite's Session.set_muted. Replies: "Microphone off." / "Microphone on." for the spoken-at satellite; for another, "<Spoken name>'s microphone is off." / "... is on." ("The Pi's microphone is off.", or a name he gave it, "Bedroom Pi's microphone is off."); a satellite not connected says so as other device actions do.
- A named push-to-talk satellite ("mute the PC"): 0026 says a press works whatever the mute, so muting it would do nothing. Reply "The PC only listens while you hold the key, so there's nothing to mute." (my call, his to overturn).
- Intents: plain `mute|mute (the )?(mic|microphone)|stop listening|turn (off|on) the (mic|microphone)|unmute|unmute (the )?(mic|microphone)|start listening` with optional `on <device>` / `the <device>` → mute_microphone. Sound wording `(mute|unmute) (the )?(sound|audio)|turn the (sound|audio) (off|on)` with optional device → set_volume(mute=...) as 0034. "Mute" over ringing or speech still stops first (Conversation's existing order; keep the test).
- Claude's set_volume `mute` description says it's for sound only when the user says sound or audio; the new tool is for the microphone.
- Dashboards (ha-dashboard): a microphone button using the Nova device's "Microphone muted" switch on the smart clock dashboard and on the Nova view, discovered by entity at run time where the dashboard allows; screenshots shown to him before saving live.
- 0034 gains a note pointing here; decision record update in the same change.

## Not changed

The microphone's existing switches; 0026's "a press works whatever the mute"; the stop words.
