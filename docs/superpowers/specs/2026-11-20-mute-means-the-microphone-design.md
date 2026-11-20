# "Mute" means the microphone

Small change to 0034, agreed with him on 20 Nov 2026 ("that is exactly how I want it").

## Why

He took "mute" to mean the microphone. His desk mic has its own touch mute, so what he wants by voice is Nova's listening off, and sound muting only when he says sound or audio.

## What he decided

- Plain "mute", "mute the mic", "mute the microphone" and "stop listening" turn Nova's listening off. Nova plays a short sound (mic_off.wav) instead of speaking, and stops hearing the wake word until it's turned back on.
- At the Pi it mutes the Pi. At the PC it mutes the room's always-listening satellite (the bedroom Pi), since the PC only listens while the key is held. A device can be named from anywhere: "mute the Pi", "stop listening on the Pi".
- While something rings or Nova is speaking, "mute" still stops it (unchanged).
- Back on: the settings page's microphone switch (exists), a microphone button on the smart clock dashboard and on the Nova view (the Nova device's "Microphone muted" switch in Home Assistant), or by voice from another satellite ("unmute the Pi" at the PC). Dashboard buttons are previewed before saving live.
- Sound muting needs the word: "mute the sound", "mute the audio", "unmute the sound", "turn the sound off/on" do what plain "mute" did in 0034 (Windows at the PC, the music at the Pi, or the device named). Plain "unmute" is the microphone.

## Approaches

1. Reuse Session.set_muted (the settings page's and Home Assistant's mute) from a new action (chosen): one mute state, already shown on the page, the clock display and in Home Assistant.
2. A separate voice-only mute: two states for one thing. Rejected.

## Design

- New action `mute_microphone(muted: bool, where?)` on ActionExecutor (local rules; also offered to Claude for other wordings). Target: `where` via Brain.find_satellite(here=spoken-at); else the spoken-at satellite if it listens for a wake word; else (push-to-talk, the PC) the room's always-listening satellite (Brain.ring_on gives it). It calls that satellite's Session.set_muted. Success is confirmed by a sound, not words (his ruling, 20 Nov): mic_off.wav for off, mic_on.wav for on, played on the satellite spoken at, also for another satellite and after "Stopped, and"; the reply text is only logged, as "Microphone off." / "Microphone on." for the spoken-at satellite; for another, "<Spoken name>'s microphone is off." / "... is on." ("The Pi's microphone is off.", or a name he gave it, "Bedroom Pi's microphone is off."); refusals and failures (push-to-talk, not connected, no such satellite) still speak.
- A named push-to-talk satellite ("mute the PC"): 0026 says a press works whatever the mute, so muting it would do nothing. Reply "The PC only listens while you hold the key, so there's nothing to mute." (my call, his to overturn).
- Intents: plain `mute|mute (the )?(mic|microphone)|stop listening|turn (off|on) the (mic|microphone)|unmute|unmute (the )?(mic|microphone)|start listening` with optional `on <device>` / `the <device>` → mute_microphone. Sound wording `(mute|unmute) (the )?(sound|audio)|turn the (sound|audio) (off|on)` with optional device → set_volume(mute=...) as 0034. "Mute" over ringing or speech still stops first (Conversation's existing order; keep the test).
- Claude's set_volume `mute` description says it's for sound only when the user says sound or audio; the new tool is for the microphone.
- Dashboards (ha-dashboard): a microphone button using the Nova device's "Microphone muted" switch on the smart clock dashboard and on the Nova view, discovered by entity at run time where the dashboard allows; screenshots shown to him before saving live.
- 0034 gains a note pointing here; decision record update in the same change.

## The PC's own microphone (his rulings, 20 Nov, later the same day)

Said at the PC (a satellite whose hello has system_volume, machine pc). These replace "At the PC it mutes the room's always-listening satellite" for the mic words and plain "mute"; "stop listening" is unchanged.

1. **"Mute mic", "mute the mic", "mute the microphone", "unmute mic", "turn the mic off/on"** mute or unmute the PC's own microphone in Windows, for everything: the default capture endpoint's mute (Core Audio, eCapture and eConsole, the same IAudioEndpointVolume and COM thread as Windows' volume in pc_volume.py). Confirmed with mic_off.wav or mic_on.wav on the PC, no words.
2. **"Mute the audio", "mute the sound"** stay Windows' sound mute.
3. **Plain "mute" and plain "unmute"** at the PC ask "The mic or the audio?" and listen for the answer without the wake word. An answer naming the mic or microphone does 1, audio or sound does 2; anything else is a fresh request, including the other verb ("mute the audio" after "Unmute."). It uses the existing question asked first (Conversation.asked_first, as "Shut down the PC?" and the download offer), which now also takes a Choice of set answers.
4. **"Stop listening"** at the PC still mutes the room's Pi, and a device named ("mute the Pi", "unmute the Pi", "mute the Pi's mic") goes to that device. At the Pi, plain "mute" still mutes the Pi. Said over something ringing, "mute" still stops it first.
5. **A mute lasts an hour.** After `computer.mic_mute_minutes` (60 by default, a whole number from 1 to 1440, on the settings page as "Minutes the PC's microphone stays muted", used straight away) the satellite turns the microphone back on by itself and plays mic_on.wav. The brain sends the minutes with each mute. The time is kept on the satellite, in its data folder (mic_muted_until.json), so it outlives a restart of the brain or the satellite. If the microphone was turned back on by hand meanwhile (his mic's touch button, or Windows), nothing is done and nothing plays. Unmuting by voice ends the time; muting again starts it again.
6. **A press while the microphone is muted** (by Nova or by hand) unmutes it for that turn only, before the press sound and the recording, and mutes it again when the turn is over: at the brain's end-of-turn `allow`, so the answer to a question Nova asks ("The mic or the audio?", "Shut down the PC?") is heard, however the recording ended (the key, the limit, the brain stopping it). Losing the brain, or the satellite stopping, mutes it again too. An `allow` from the turn before, arriving while a press is still recording, is left for this turn's own. The hour carries on as it was. If the hour runs out during the turn, it's left on, with the hour's own sound. A press when it isn't muted touches nothing, and it's never left muted when it wasn't before the press. A failure is logged and the press goes on.

Hand check: mute the PC's mic by voice, put the PC to sleep for a while, wake it after the time is up: the mic should come back on with mic_on.wav within about 30 seconds (the wait looks at the clock in 30-second slices, as Windows doesn't count a wait's time while the PC sleeps).

Protocol: `computer` gains the action `"mic"` with `mute` (a bool) and, for a mute, `minutes`, answered by `computer_done` with `muted`. The brain sends it only to a satellite which said system_volume; the satellite checks both fields. An older satellite answers "I can't do that on this computer.", which is said. Claude has `mute_computer_mic`, offered only at a computer which said system_volume.

## Not changed

The microphone's existing switches; 0026's "a press works whatever the mute"; the stop words.
