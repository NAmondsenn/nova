# The PC as a push-to-talk satellite

Date: 18 November 2026. Phase 6, first piece. Status: design, for the user's approval.

## Why

The user wants to talk to Nova at his Windows PC by pressing a key, and have the PC answer like a second Nova in the room: replies and music on the PC's speakers, and everything the brain can already do (music, Home Assistant, lists, football, the clock) working from it with no extra work. In his words, "the Windows version of Nova". The Pi stays the always-on satellite in the same room (the bedroom).

This is the first of four pieces, in the order he agreed on 18 November:

1. **This one:** the PC as a push-to-talk satellite.
2. Opening apps, games and web pages on the PC, and PC commands said at the PC ("go to sleep"), with sleep never triggered by the word inside a sentence and shutting down still asking first.
3. Fingerprint approval for big actions (a phone passkey, or Windows Hello at the PC).
4. The Mac version.

Pieces 2 to 4 are not designed here, but this piece mustn't make them harder.

## Goals

- The PC joins the brain as a satellite, in the satellite list with the Pi, with its own volume, mute and conversation, and its own Nova device in Home Assistant.
- **Numpad 3** starts listening. Listening ends when the brain decides he has stopped speaking, when the time limit runs out, or when he presses Numpad 3 again.
- Replies, chimes and music play on the PC's default speakers. "Play some music" said at the PC plays on the PC.
- Turning the music up and down, pausing and stopping work as on the Pi.
- The PC's room is the bedroom, so "turn the light off" at the PC means the bedroom light (its Nova device is given the bedroom area in Home Assistant).
- **Alarms and timers set at the PC ring on the Pi**, because the PC is often asleep or off. The rule, so it holds if satellites are added elsewhere: a push-to-talk satellite hands its timers and alarms to an always-on satellite in the same room, and rings them itself only when its room has none.
- It starts at login, runs in the background with no window, comes back by itself after sleep or a lost connection, and is restarted if it crashes.
- When the PC can't reach the brain, a press of Numpad 3 answers with the recorded "I can't reach the server", as the Pi does.

## Non-goals

- No "Hey Nova" on the PC. The PC and the Pi share a room, so both would wake on every wake word. No wake word model and no voice check are loaded on the PC.
- Opening apps and pages, PC-only commands, fingerprint approval and the Mac: pieces 2 to 4.
- No Spotify Connect speaker on the PC (the Pi's raspotify isn't carried over; Navidrome is what plays music now).
- No change to how the Pi behaves on its own.

## Anti-goals (it fails if any of these happen, even if the rest works)

- The PC's microphone is open without a press of Numpad 3, or stays open past the limits the Pi already has (`LIMIT_SECONDS`, `MOST_SECONDS`). "Open" means the Windows microphone stream itself, not just sending audio: Windows' microphone light is on only while Nova is listening.
- The satellite stops the PC going to sleep on its own (it sleeps after 30 minutes idle on mains power).
- An alarm or timer set at the PC goes unheard because the PC was asleep.
- Anything on the Pi changes or breaks while it's the only satellite in its room. The only change to the Pi is the room-wide stopping, pausing and volume below, and only while the PC is connected.
- Anything other than the PC itself can make the PC start listening, or the brain accepts the PC without the HMAC handshake every satellite already passes.
- A second copy of the satellite's playback or protocol code that has to be kept in step with the Pi's.

## Constraints and what's there today

- **The brain was built for several satellites** (decision 0006, section 6): room, volume, mute, playback and conversation are each the satellite's own. A satellite is added with `add-satellite` in `backend.py`, which gives it a password (`satellites.json` on the brain; `NOVA_BACKEND_TOKEN` in the satellite's `.env`). Its name must be one hostname label; the PC will be `pc` (set by `backend.satellite`, since its machine name is `nathanspc`).
- **The connection:** the brain listens on 10400 on its tailnet address only (and 127.0.0.1). The PC is on the tailnet (`nathanspc`), so it connects as the Pi does, by `nova:10400`.
- **The satellite program is nearly portable.** `satellite.py` and the 18 modules it imports use only `numpy`, `sounddevice`, `soundfile`, `soxr`, `onnxruntime` (for the wake word), `pyyaml` and `python-dotenv`, all with Windows builds. The systemd watchdog already does nothing outside systemd. The echo canceller loads `libspeexdsp` by ctypes and already carries on without it, with a warning. Nothing else in the closure is POSIX-only.
- **The voice check runs on the satellite** and the wake word's confidence is sent to it in `state`; neither applies to a press.
- **Timers and alarms** ring on the satellite they were set from (clock.py), and clock_copies.py can move all of one satellite's timers and alarms to another at once (for removing a satellite), but not hand over a single one as it is set.
- **Room** is the Home Assistant area given to the satellite's Nova device (ha_device.py, decision 0005).
- **Numpad 3** is already held for this in `flow100.ahk` on the PC (homelab repo `desktop/pc/`), with a "not built yet" tip. AutoHotkey 1.1 runs it, under its UI Access build.
- **CT 109 limits** for building and testing: `nice -n 19 timeout 600`, one suite at a time, load below 3 first. Tests run on the Pi with `.superpowers/tools/pi_test.sh`, in London time and UTC.
- Python on the PC: the `py` launcher is installed. The `python` on the PATH is the Microsoft Store stub, so it isn't used.

## Approaches

- **A. The Pi's satellite program on Windows, with a push-to-talk mode.** Chosen by the user on 18 November. One codebase, so replies, music, volume, stop, reconnecting and the fallback come with it, and a fix to the satellite fixes both. Costs Python and a few packages on the PC, and the echo canceller is off unless its Windows library is supplied.
- **B. A new, smaller Windows client** speaking the protocol, push-to-talk, replies and music only. Rejected: a second copy of playback and protocol code to keep in step, with stop, volume and reconnecting rebuilt and liable to drift.
- **Home Assistant's own Assist on the PC.** Ruled out: it wouldn't be Nova.

## The design

### On the PC

- The same code as the Pi, installed with only the satellite's requirements, in a Python virtual environment of its own. `openwakeword` is imported at the top of `wake_word.py`, and `satellite.py` imports that at the top, so the import is made lazy (only when the wake word is used). Then the PC needs neither it nor what it pulls in (`scipy`, `scikit-learn`, `tqdm`). The Pi's behaviour is unchanged.
- A setting, `satellite.trigger: wake_word | push_to_talk` (default `wake_word`, so the Pi is unchanged). With `push_to_talk`, the satellite loads no wake word model, no voice check and no wake clips, and starts a recording only on a press.
- **Sound streams open only when needed** (push-to-talk only; the Pi keeps decisions 0001 and 0002's always-open streams). The microphone stream opens on a press and closes when the recording ends. The speaker stream opens when something is to play and closes after a short idle spell (a few seconds), so Windows can sleep and its microphone light is off. To cover the moment the microphone takes to open, the press chime plays first (below), and recording starts as it opens. Nothing said before the chime is expected to be heard.
- **The press:** the satellite listens on a local-only channel (127.0.0.1, or a named pipe) for "toggle". `flow100.ahk`'s Numpad 3 sends it. A press when idle starts a recording, exactly as a wake word would (pausing music first, as the Pi does), but sent with `pre_roll: 0` and a new field saying it was a press. A press while recording ends the recording now and sends what was said; ended with nothing said, the brain does nothing. A press while Nova is replying does what the wake word does over a reply on the Pi: stops it and starts listening (`talked_over`). So two quick presses just silence a reply.
- **What he hears:** a press plays `sounds/listening.wav` on the PC at once, from the satellite itself, whatever `sounds.listening_enabled` says, since a press needs an answer straight away. The brain doesn't play it again for a pressed recording. (Today the brain plays it, after the round trip, and only when that setting is on.)
- **Mute:** a press is deliberate, so mute doesn't block it, as the clock display's hold-to-talk already ignores mute (`Session.listen_now`). The brain's existing press path for the clock display (`listen_now`, `stop_held`) is looked at first and reused where it fits, rather than adding a second one.
- **Running it:** a Task Scheduler task at his login starts a small launcher with no window (`pythonw`). The launcher runs the satellite and starts it again whenever it exits, waiting a little longer each time it fails quickly, since Task Scheduler's own restart doesn't reliably restart a process that exits. It is his user's process, so it uses his default microphone and speakers. Windows never sends `SIGTERM`, so a logoff or shutdown counts as an unclean stop (`RUNNING_MARK`). That's accepted, or the launcher clears the mark on a clean Windows shutdown if that is easy.
- **Data and keys:** `NOVA_CONFIG` and `NOVA_DATA` under his `%LOCALAPPDATA%\nova`, which is his user's only. `.env` holds `NOVA_BACKEND_TOKEN`.

### On the brain

- `wake` gains an optional field for a press. The brain treats a pressed recording like a woken one, with no pre-roll.
- **Timers and alarms:** a satellite says, as it connects, whether it is push-to-talk. The brain keeps, for each satellite, whether it is push-to-talk and the last Home Assistant area it was seen in, so both are known while it's disconnected or Home Assistant can't be reached. A timer or alarm set from a push-to-talk satellite is given, as it is set, to an always-on satellite with that same area, connected or not, since the Pi rings from its copy. If there's none, it goes to the default satellite. It stays on the PC only if the PC is the only satellite. The reply says where it will ring only if that isn't the same room. This is new work in `clock_requests` (setting a timer or alarm), not `clock_copies.move_satellite`, which moves everything at once. A handed-over timer then shows under the Pi on the settings page and in Home Assistant, not the PC. Asking for or cancelling timers by voice already covers every satellite.
- **The settings page:** the PC appears in the satellite list like the Pi, marked as push-to-talk. Its sound settings work as they do for the Pi's.

### The room acts as one (his call, 18 November)

Stopping, pausing and volume act on the whole room, whichever satellite in it was spoken to:

- "Stop" (or dismissing) silences whatever is ringing on any satellite in the room, so a timer set at the PC and ringing on the Pi is stopped from the PC.
- A press or a wake pauses Nova's music on every satellite in the room while he's heard, and each carries on after, as one satellite does today.
- "Pause", "resume", "turn it up" and "turn it down" act on whatever Nova is playing in the room. If both are playing, both.
- Everything else stays with the satellite spoken to: where new music starts, where replies play, and what's asked.

"The room" is the Home Assistant area kept for each satellite (above). A satellite with no known area is a room of its own, so the Pi alone behaves exactly as today. `media_watch.playing_in` (the wake word's stricter mode while something plays) is left as it is: it already leaves out Nova's own music.

### Adding it

1. `add-satellite pc` on the brain gives its password. This is his named job, as is anything live.
2. Install on the PC: Python venv, packages, config (`backend.address: nova:10400`, `backend.satellite: pc`, `satellite.trigger: push_to_talk`), the `.env`, and the login task. The script is run over SSH from CT 109 and he names the job.
3. In Home Assistant, the new Nova device for `pc` is given the Bedroom area.
4. `flow100.ahk`'s Numpad 3 is changed to send the press.
5. He checks: a question, music, volume, a light, a timer (which should ring on the Pi), pressing 3 to stop listening and to stop a reply, the PC going to sleep and coming back, and the brain being restarted.

### Updating it

CT 109 pushes to the PC over the existing `ssh pc`; the PC gets no key to anything. What it pushes is the commit the brain runs (CT 107's checkout), never unreleased work on CT 109, so the protocol always matches. A small script on CT 109 copies that commit's satellite files to the PC and reinstalls packages only when the requirements changed. Each update is his named job, like the Pi's.

## Left to the builder

- Whether the local channel is a 127.0.0.1 port or a named pipe, and what the AutoHotkey side sends. Either way it must refuse anything that isn't local.
- The exact field name for a press in `wake`, and how the satellite says it is push-to-talk when it connects (the handshake's `hello`, or its first message).
- How "always-on satellite in the same area" is decided: for now a satellite that isn't push-to-talk.
- Whether the echo canceller's Windows library is worth supplying. My call: no, since a press pauses the music and stops a reply first, so there is little to cancel.
- Tests: the press, toggling off, the hand-over of timers, and push-to-talk loading no wake word, with the existing fakes, on the Pi in London time and UTC. A live check on the PC by him after deploy.
- A decision record for this piece in `docs/decisions/` (the next number), and the roadmap's Phase 6 line.

## Calls I made (for him to check)

- No "Hey Nova" on the PC.
- A press while listening ends the listening and sends what he said, rather than throwing it away.
- A press over a reply stops it and listens, as the wake word does; two presses just silence it.
- The chime on a press is `listening.wav`, played by the PC at once and always, even with the brain's start-of-listening sound turned off.
- Mute doesn't block a press.
- On the PC the microphone and speaker streams open only when needed, so the PC still sleeps after 30 minutes and the microphone light is only on while listening.
- A timer set at the PC goes to an always-on satellite in the same room even while it's disconnected, else the default satellite.
- Room-wide stopping, pausing and volume: his call, not mine.
- The echo canceller stays off on the PC.
- The PC's satellite name is `pc`.
- Updates go from CT 109 over SSH, not from GitHub.
