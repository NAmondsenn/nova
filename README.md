# Nova

A self-hosted voice assistant, with a Raspberry Pi in each room to talk to. Say "Hey Nova", ask for something, and it answers out loud - timers, reminders, alarms, music from your own Navidrome server or Spotify, the lights through Home Assistant, your calendar from Google, the weather, and general questions through Claude. Everything stays on your own network except what goes to Claude - your words, what Nova remembers about you, your timers and lights, and what it reads from Home Assistant for you - and Spotify, your calendar, which is read from Google, and the notifications and rings sent to the Mac, tablet and phone through the Home Assistant app, which go by its push service.

It's built in two halves - see [docs/decisions/0006](docs/decisions/0006-the-pi-as-a-satellite.md) - and in stages, in [docs/roadmap.md](docs/roadmap.md). The **brain** runs on a home server (a container on PVE2) and does everything which thinks: speech-to-text, Claude, the voice, music, Home Assistant, timers, memory and the settings page. A **satellite** is a Raspberry Pi in a room, which only hears and speaks: its microphone, speaker and wake word. One brain looks after any number of satellites.

## What it can do

- **Timers and reminders** - "set a timer for ten minutes", "remind me to take the pizza out in 20 minutes", "remind me at 4pm to call mum". Timers ring until they're answered, for up to ten minutes; reminders come back every half a minute for five. Both survive a restart.
- **Alarms** - "wake me up at 7 on weekdays". They ring in bursts, building up from quiet, with gaps for "Hey Nova, stop" to be heard in.
- **Music** - from your own [Navidrome](https://www.navidrome.org/) server, or from Spotify: play a song, artist, album, genre or playlist (or your liked songs), pause, skip, go back, shuffle, repeat, "what's this song?". The music pauses while Nova is talking, and comes back when it's done. Navidrome's plays through Nova itself, so it pauses instantly and keeps working with the internet down. With SoulSync set up, "download Gooba by 6ix9ine" or "download the album Rumours by Fleetwood Mac" fetches what Navidrome hasn't got, and "play" something missing offers to - see "Fetching music by voice" below.
- **Moving music** - "play it on my Pi", "move this to the PC" or "switch the music to the Pi" carries Navidrome's music on from where it was, with the same queue, repeat and shuffle, on the satellite you name, and stops it where it was; "play Drake on the PC" starts new music there instead. A satellite is called by what it is - "the Pi", "the PC" (and "the Mac", once it's a satellite) - or by names you give it in the "Also called" box under it on the settings page, in Satellites. "The computer" is the PC or the Mac, so with both Nova asks which. A satellite which is off isn't woken for it: "The PC isn't connected, so the music's staying on the Pi." See [docs/decisions/0032](docs/decisions/0032-music-that-moves-and-notes-on-the-computer.md).
- **Your playlists** - with Navidrome, while a song plays: "add this to Gym and Car", "take this off Gym", "make a playlist called Chill" or "like this". A playlist which is remade (New Music, a daily mix) is refused, and one SoulSync mirrors is set to append only first, or refused for taking a song off, so a change stays. "Undo that" takes the last change back. See [docs/decisions/0030](docs/decisions/0030-changing-playlists-and-the-calendar.md).
- **Volume** - for Nova's voice, for timers and alarms, and for the music, each kept separately.
- **The lights** - "turn off the kitchen lights", "set the living room lights to 30 percent", "make the lamp blue", "make the lights warmer", through Home Assistant. Short commands like "lights on" mean the lights in the room Nova is in.
- **In Home Assistant** - Nova is a device there, with its volumes, next alarm, timers, stop, restart, a box to make it say something, a switch to mute its microphone, what it's playing with controls for it, and an Ask Nova box to type a request in. See "Nova in Home Assistant" below.
- **Your calendar** - "what have I got on Thursday?", "am I busy this weekend?", from every calendar in your Google account - see "Google Calendar" below. "What have I got on this week?" is an overview - which days there's work or uni, and anything else by day - rather than every time of every day. "When's my MOT?" looks for one thing, up to three months ahead. And "what time do I have work tomorrow?", "when's my next shift?" or "when's gym tomorrow?" - each of your calendars but your own is an activity, by its name, with every event on it. "Put the dentist on Thursday at 3" adds it to your calendar, and says back what it added.
- **Changing your calendar** - "cancel the dentist on Thursday", "move it to Friday at 4" or "rename it to dentist check-up", for one day of a repeating event, or from that one on with "all of them". Only your own calendars, never an invite, and nobody is emailed. "Undo that" takes the last change back. See [docs/decisions/0030](docs/decisions/0030-changing-playlists-and-the-calendar.md).
- **Batteries** - "how's my Mac's battery?", "what's my phone on?" or "battery levels", for every device Home Assistant has a battery level for. Nova also warns you as one runs low. See "Batteries" below.
- **Your phone** - "where's my phone?" says where Home Assistant's app last put it, and "ring my phone" or "find my tablet" rings it, wherever it is. Where it is never goes to Claude. See "Finding your phone" below.
- **Lists** - "add milk to my shopping list", "what's on my shopping list?", "tick off milk" or "I've got the eggs", for Home Assistant's own to-do lists, found by the words you use for them, so "notes" is a list called notepad. With no list named, adding and reading use your main one - todo, unless you choose another on the settings page, under Calendars and lists - and ticking off looks through them all. Assist is asked first, but never to remove anything: a thing is only ever ticked off, so it can be put back in Home Assistant. Several things at once - "add eggs, milk and bread" - go to Claude, which adds each one, and with Claude out of reach they're added as one, rather than lost.
- **Notes at your computer** - at the PC (and the Mac, once it's a satellite), "make a note that the bins go out on Tuesday", "write this down: ..." or "write me notes on the French Revolution" saves a plain text file in that computer's own Downloads folder, and Nova says "Saved to your Downloads as Bins." A note is never saved over another: a second "Bins" is "Bins (2)". The Pi doesn't save notes itself, but "make a note on my PC ..." said at the Pi saves it in the PC's Downloads ("Saved to the Downloads on the PC as Bins."), and with no computer named it goes to the only one that saves notes. "Add milk to my notes" is still the list above, on any satellite. A note's text is kept out of Nova's log, apart from the words you said when transcripts are on. See [docs/decisions/0032](docs/decisions/0032-music-that-moves-and-notes-on-the-computer.md).
- **Your PC** - "turn on my PC" wakes it with Home Assistant's Wake-on-LAN button, and "shut down my PC", "restart my PC", "put my PC to sleep", "turn off my PC's screen" and "lock my PC" press its HASS.Agent buttons. "Is my PC on?" says whether it is. Each confirms with the success chime rather than words (or says what it did, with the chime off), and shutting down and restarting don't ask first. At the PC, the button is pressed once the chime has played. There's nothing to set up if HASS.Agent's device and the Wake-on-LAN button are in Home Assistant: Nova finds the PC by its device, whatever its buttons are called, and never presses its other buttons, nor any of Proxmox's. With more than one PC or more than one Wake-on-LAN button, choose which button wakes each PC on the settings page, under Calendars and lists - one button wakes one PC, so choosing it for one takes it off another.
- **The weather** - now, today or tomorrow, from Home Assistant's own weather: Met's forecast, for Home Assistant's home location, so there's nothing to set in Nova. A forecast over three hours old is said with its age. A home with more than one weather entity uses the first by entity id, unless you choose another on the settings page, under Calendars and lists ("Weather from").
- **Football** - "when do Liverpool play next?", "did Stevenage win?", "where are Liverpool in the table?", "who's left in the Carabao Cup?", "what's the starting 11 for Liverpool?", "what happened in the England match?" or "when did Liverpool last play Newcastle?", of any team or competition, read from Home Assistant. Nova also says, by itself, when a team you follow is about to play (or its match is called off), scores, reaches half time, starts the second half and finishes. See "Football" below.
- **When to leave** - "what time do I leave for work?" says when your next Work event starts, how long the drive is and when to leave by, with a few minutes to spare, and offers a reminder; "how long's the drive to my gran's?", "how long to walk to uni?" or "how long to cycle to the Metrocentre?" say the time and the distance, with a word if the traffic is heavy. A drive from home which has a reminder is watched, and the reminder moved, and said, if the traffic changes the time to leave. From Home Assistant's Google Maps Travel Time and its zones. See "Journeys" below.
- **Your listening** - "who have I listened to most this month?", "how long did I listen this week?" or "what are my top songs this year?", from SoulSync's stats, which count the last 7 days, 30 days, 12 months or all time, never a calendar month or today. The same numbers, with the top artists' pictures, are a "Nova music" device in Home Assistant, and on the 1st Nova makes a Wrapped of them - one for each month, and one for the year in December - kept as made and told to your phone through ntfy as a story page to tap through on the settings page's address, which is set on its page under Wrapped. "What's my Wrapped?" reads the newest out. See [docs/decisions/0017](docs/decisions/0017-listening-stats.md).
- **New music** - SoulSync downloads songs picked to your taste by itself, and Nova follows your top artists on its watchlist each month so their new releases arrive too. Each morning Nova makes a "New Music" playlist in Navidrome of what came in over the last 14 days (its name and days on the settings page), which "find me something new" plays, and "what's new this week?" says how many albums and songs have come into your library, and from whom. See [docs/decisions/0020](docs/decisions/0020-new-music.md).
- **Anything else** - answered by Claude, which can search the web when you ask it to ("search for...", "look up...").
- **Follow-ups** - when Nova asks you something, just answer; there's no need to say "Hey Nova" again.
- **Talking over it** - "Hey Nova" is heard over the music Nova plays and over its own replies, which stop the moment it's said. "Hey Nova, stop" stops whatever it's saying or playing.
- **Not answering a film** - while a media player which Home Assistant puts in the satellite's room is playing (HASS.Agent's, for a PC, say), the wake word has to be surer, and Claude is told, so that what sounds like speech from what's playing, rather than a request for Nova, is ignored without a word. Stop, pause, volume, timers and the other everyday commands are answered as ever, and Nova's own music doesn't count. See `thresholds.media_wake_word_confidence` under Configuring.
- **Remembering you** - mention something about yourself ("I've started a new job at Aldi", "my sister's called Amy") and Nova keeps it, says "I'll remember that", and uses it from then on. "What do you know about me?" reads it back; "forget that..." or "forget everything you know about me" takes it away. It's kept in `~/.local/share/nova/memory.json`, and goes to Claude with each request so it can use it.
- **Notifications** - with your own [ntfy](https://ntfy.sh/) server, alerts posted to the topics you choose - your homelab's, say - are read out as they arrive, and "tell me more" reads all of one. At night only the most urgent are; "any notifications?" has the rest. "Send that to my phone" sends Nova's last answer to your phone's topic, and a reminder nobody answered and Nova restarting after a problem go to topics of their own; "send that to my Mac" (or the tablet) sends it through the Home Assistant app's notifications instead.
- **Routines** - several things at once from a few words of your choosing ("Hey Nova, good morning"), or by themselves at a set time: each step is something you'd say to Nova - "what's the weather", "turn on the kitchen lights", "play my morning playlist" - done just as if you had, so a routine can do anything Nova can. Made and run on the settings page, and kept in `~/.local/share/nova/routines.json`.
- **A settings page** - how Nova's doing, what's playing, your lights and Home Assistant's scripts and scenes, your batteries and when each warns, your calendars and lists, the sign-ins to Spotify and Google, its volumes, timers and alarms, what it remembers, its keys and its main settings, from your phone or laptop - over your tailnet, and only for you, or, if you choose, through Nginx Proxy Manager at an address of your own, behind a password of its own.

The everyday commands - timers, pause, skip, volume, the time, the weather, the lights, the calendar and the lists - are recognised on the brain without asking Claude, so they're quicker. With the internet down, the brain still handles timers, alarms, the time, the lights and Navidrome; anything which needs Spotify or Claude says straight away that it can't reach the internet, rather than going quiet. With the brain unreachable, a satellite can't understand anything, and says so - but its timers and alarms, which it keeps a copy of, still ring, and "stop" still stops them.

## How it works

```
 on the satellite                                           over the tailnet                on the brain
microphone ─► wake word ─► sends what you say ────────────────────────────────► Nemotron ─► local command, or Claude ─► action ─► Piper ─► speaker
               (openWakeWord)                              <───── the voice ──────  (hears as you speak)                (timers, music, ...)
                                                                                    (Silero says when you've stopped)
```

- `satellite.py` runs the satellite: it listens, sends the recording to the brain as it's made, and plays what comes back.
- `backend.py` runs the brain: it hears each satellite's recording, works out the answer, and sends the speech back. Each satellite connects with a password of its own.
- `voice_assistant/` holds everything else, one module per job. On the brain, `brain.py` holds what every satellite shares, and gives each one connected a session of its own (`session.py`), which answers it through `remote.py`, the satellite as the brain sees it. `conversation.py` works out each reply: `intents.py` the commands handled locally, `llm.py` Claude, and `actions.py` everything Nova can do - each tool Claude is offered, declared beside the code which carries it out. `vad.py`, `speech_to_text.py` and `text_to_speech.py` handle speech, and `clock.py`, `music.py` with `navidrome.py` and `spotify_controller.py`, `home_assistant.py`, `volume.py` and `weather.py` are the features themselves. On a satellite, `microphone.py` and `speaker.py` own the audio devices, `wake_word.py` hears "Hey Nova", and `fallback.py` rings the timers and alarms from its copy while the brain can't be reached. Between the two, `satellite_link.py` keeps the connection, and `protocol.py` says what goes over it and how each end proves the password.
- The microphone is opened once and never closed, keeping the last few seconds of audio, so nothing said straight after "Hey Nova" is lost. Nemotron on the brain hears the recording as it's made, so it's mostly worked out the words by the time you stop talking. The speaker is opened once too, and everything that plays goes through it, so a timer or alarm can be stopped the moment you speak.

The reasons behind the bigger choices are in [docs/decisions/](docs/decisions/).

## Installing

Nova is installed in two roles, from the same clone of the repository. Set the brain up first, then each satellite. Both are done with `setup.sh`, which asks a few questions as it goes and is safe to run again - anything already set up is left alone, and each step says whether it was done (✓), skipped (–) or needs you (✗). The role is kept in `~/.config/nova/role`, so `update.sh` and a later `setup.sh` know which it is.

### The brain

The brain is a container (or any machine) running Debian, with Python 3.13 and about 4GB of memory, on your tailnet. It needs an [Anthropic API key](https://console.anthropic.com/) and, for Spotify, Spotify Premium and a [Spotify app](https://developer.spotify.com/dashboard) for its client ID and secret, with `http://127.0.0.1:8888/callback` as its redirect URI. It has no microphone or speaker, and nothing outside your tailnet can reach it - unless you serve the settings page through Nginx Proxy Manager, which opens a port on the brain to your whole network: anything there but NPM gets a bare 403, and through NPM the page needs its password (see [At an address of your own, through Nginx Proxy Manager](#at-an-address-of-your-own-through-nginx-proxy-manager)).

The brain never runs as root. Make it a user of its own (here `nova`, with no sudo), clone the repository as that user, and run the setup as root, from outside or inside the container - root does only the system steps (packages, the service), and everything else, including the packages in `venv/`, the models, and the settings and keys in that user's `~/.config/nova`, is done as the user who owns the clone. It refuses a clone owned by root.

```bash
runuser -u nova -- git clone https://github.com/NAmondsenn/Home-Assistant.git /home/nova/Home-Assistant
cd /home/nova/Home-Assistant
bash setup.sh --brain          # as root
```

In order, it:

1. Checks this is Debian with Python 3.11 or newer - though the exact versions in step 2 are for Python 3.13, which Debian 13 has.
2. Installs the system packages (ffmpeg among them), makes a Python environment in `venv/`, and installs the Python packages - the exact versions in `requirements-backend.txt`.
3. Asks for the assistant's name, wake word and Spotify speaker name, saving any you change to `~/.config/nova/config.yaml`.
4. Asks for your API keys and writes them to `~/.config/nova/.env`, readable only by you. An existing one is left as it is.
5. Downloads Piper, the voice (`en_US-hfc_female-medium`), the Nemotron speech-to-text model and Parakeet (a second, small one, asked when Nemotron hears nothing in a lone word such as "Pause.") into `models/`. They're checked against pinned SHA-256 hashes, so a tampered download stops setup rather than being run. Nemotron needs about 1.5GB free, and Parakeet about 400MB more - without room for Parakeet, or the internet to fetch it, setup carries on without it. `models/` can be a link to a folder somewhere with more room, and everything follows it. openWakeWord's own models (the brain uses one for its voice activity check) are fetched by its package, into `venv/`, with no hash check of Nova's.
6. Installs the brain as the `nova-backend` system service, which runs as that user and starts at boot, and starts it.

Then let each satellite in, by its machine's name: on the settings page (see [The settings page](#the-settings-page)), under Satellites, type it in and Add a satellite. The page shows a password for it once, so keep it for the satellite's setup. Without the page, `add-satellite` does the same, printing the password - run it as the brain's user, not root, as the passwords are kept in that user's `~/.config/nova/satellites.json`, which is where the service reads them.

```bash
cd /home/nova/Home-Assistant
runuser -u nova -- venv/bin/python backend.py add-satellite kitchen
```

`remove-satellite NAME` stops letting one in, run the same way, and lets it go straight away if it's connected - as Remove does, under Satellites on the settings page. Its timers, alarms and routines move to the default satellite (the first added, unless another is made the default on the page), and its copy of the alarms is emptied if it's connected. One that isn't keeps its copy for a week, so wipe it or connect it before switching it on.

From here on, anything this README has you run on the brain which reads or writes its settings, keys or data - a `venv/bin/...` command, or a file in `~/.config/nova` or `~/.local/share/nova` - is for the brain's user, not root, whose own folders the service never looks in. Run commands with `runuser -u nova -- `, as above, and edit its files as that user, e.g. `runuser -u nova -- nano /home/nova/.config/nova/.env`. Only `systemctl`, `journalctl`, `tailscale` and the setup and update scripts are for root. Put the brain on your tailnet, where it's reached by its name and port 10400, as `nova:10400` - as root:

```bash
tailscale serve --bg --tcp 10400 tcp://localhost:10400
```

The brain itself only listens on its own machine, so that's the one way in. The brain's own settings and keys, and everything it saves, are in `~/.config/nova` and `~/.local/share/nova` on its machine, and are where the rest of this README says "the brain".

The brain's log is `journalctl -u nova-backend -f`, and `systemctl restart nova-backend` (as root) restarts it. To update it, run `bash update.sh` there, as root, from its clone - or `bash update.sh --no-pull` to use the code as it is, after switching branch, say, instead of fetching first.

### A satellite

You'll need a Raspberry Pi with Raspberry Pi OS (64-bit), a USB microphone and a speaker, on the same tailnet as the brain, and the password the settings page (or `add-satellite`) gave for it. There's no standalone mode: without its brain a satellite can only ring its timers and alarms, and say it can't reach it. The speaker and the microphone have to be two separate sound cards, and not the Pi's own HDMI or headphone output, which setup's shared audio step leaves out. For "Hey Nova" itself you'll need a wake word model of your own - see [The wake word model](#the-wake-word-model) - or the satellite answers to "Hey Jarvis" instead.

```bash
git clone https://github.com/NAmondsenn/Home-Assistant.git
cd Home-Assistant
bash setup.sh
```

In order, it:

1. Checks this is Raspberry Pi OS or Debian, with Python 3.11 or newer, a speaker and a microphone.
2. Installs the system packages, makes a Python environment in `venv/`, and installs the Python packages - on Python 3.13 the exact versions in `requirements.lock`, on 3.11 the newest ones `requirements.txt` allows. Either is only what the microphone, speaker and wake word need.
3. Asks for the assistant's name, wake word, Spotify speaker name and the brain's address (`nova:10400`), saving them to `~/.config/nova/config.yaml`.
4. Says the name the brain knows this satellite by - its machine's name - and asks for the password the settings page gave for it (not shown as it's typed), writing it to `~/.config/nova/.env` as `NOVA_BACKEND_TOKEN`, readable only by you. It's the only secret a satellite holds. Left blank, the satellite won't start until it has one, which setup says - give it one later with `bash setup.sh --new-password`.
5. Looks for your wake word model (see [The wake word model](#the-wake-word-model)) and copies it into `models/` - without one, the satellite listens for "Hey Jarvis" - and has openWakeWord download its own models, which its package keeps in `venv/`. A satellite downloads no speech or voice models - the brain does the hearing and the speaking.
6. Offers to write `/etc/asound.conf`, so the assistant and Spotify can share the speaker and microphone - in short bursts, so a sound stops quickly and the wake word reacts sooner - backing up the old one. It looks for a speaker and a microphone on two different sound cards, leaving out the Pi's own HDMI and headphone outputs, and writes nothing if it can't find both.
7. Offers to install raspotify, so the Pi shows up as a Spotify Connect speaker (see "Spotify on a satellite", under Music).
8. Sets the satellite to start at boot, as the `voice-assistant` user service, and starts it.

Each satellite is named by its own machine's name, which the brain knows its password by - the same name given to `add-satellite`. Set `backend.satellite` in `config.yaml` to use another. Either way, it must fit the rule `add-satellite` and the settings page keep to: lower-case letters, digits and hyphens, not starting or ending with a hyphen.

`bash setup.sh --yes` asks nothing, taking what it finds and the defaults; `--no-spotify` leaves Spotify out, and `--no-service` doesn't start Nova at boot. `--brain` and `--satellite` choose the role; without one, an existing install keeps its own.

To give a satellite a new password - one turned away, or a password that may have got out - press New password beside it on the settings page, under Satellites. The brain lets it go straight away, and turns it away until it has the new one, which the page shows once. Then, on the Pi, as its user:

```bash
cd Home-Assistant
bash setup.sh --new-password
```

It asks for the new password (not shown as it's typed), puts it in `~/.config/nova/.env` in place of the old one, leaving the rest of the file as it was, and restarts the satellite. A Pi with a new machine, or a new SD card, is set up as above with the same name, after New password - adding it again isn't needed, and removing it would move its timers, alarms and routines to the default satellite.

To update to the latest code later - it never touches your settings, keys or saved data - run this on the brain (as root) and on each satellite (as its user), adding `--no-pull` to use the code as it is rather than fetching first - after switching branch by hand, say, or to go back to a tag, which can't be pulled:

```bash
bash update.sh
```

and to take a satellite off the Pi, asking before removing anything which can't be got back:

```bash
bash uninstall.sh
```

### The PC

A Windows PC can be a satellite too, in the same room as a Pi: it listens only when a key is pressed (Numpad 3, from the user's AutoHotkey script), plays replies and music on its own speakers, and hands the timers and alarms set at it to the Pi, which is always on. It has no wake word, so it needs no model, and its microphone and speaker are open only while it's in use. See [docs/decisions/0026](docs/decisions/0026-the-pc-as-a-push-to-talk-satellite.md).

It's installed from a machine which can `ssh pc` to the PC and `ssh nova` to the brain (CT 109, here), with this repository checked out:

1. On the brain, `add-satellite pc` gives its password. Put it on the PC in `%LOCALAPPDATA%\nova\config\.env` as `NOVA_BACKEND_TOKEN=<the password>`; the install never writes that file.
2. `bash scripts/deploy_pc.sh` sends the commit the brain is running (never anything newer) and installs it: the code, a Python environment from the `py` launcher, the nine packages in `requirements-pc.txt`, a settings file if there is none (the brain at `nova:10400`, the name `pc`, `satellite.trigger: push_to_talk`, `audio.echo_cancellation: false`) and a Task Scheduler task, `Nova satellite`, which starts the launcher when the user logs on. The launcher runs the satellite with no window, and starts it again if it stops. Run it again to update: it stops the task, the launcher and the satellite (the running satellite holds the packages' files open), installs packages only when `requirements-pc.txt` changed, and starts the task again.
3. In Home Assistant, give the PC's Nova device (`nova_` and the machine's name) the area of the room it's in, which is how Nova knows which Pi it shares a room with.
4. The key's script posts to the satellite, which listens on this machine only: `POST http://127.0.0.1:10401/press` (`satellite.press_port`) with the header `X-Nova-Press: 1`. It must be that address, not `localhost`, and with that header, or the satellite refuses it. In AutoHotkey 1.1, as for any other webhook, with a short timeout and the error caught, so a stopped satellite neither holds up the key nor pops up an error box:

```ahk
try {
    request := ComObjCreate("WinHttp.WinHttpRequest.5.1")
    request.SetTimeouts(1000, 1000, 1000, 1000)
    request.Open("POST", "http://127.0.0.1:10401/press", false)
    request.SetRequestHeader("X-Nova-Press", "1")
    request.Send()
}
catch {
}
```

A press starts listening; a press while listening stops it and sends what was said; a press over a reply stops it and listens. Mute doesn't block a press.

Everything is under `%LOCALAPPDATA%\nova`: `app` (the code and its `venv`), `config` (`config.yaml` and `.env`) and `data`, whose `logs` folder has `smart_assistant.log`, the launcher's `launcher.log` and `satellite-stderr.log`, what the satellite writes to stderr (an error before its own log starts, say) along with a copy of its log, kept to about 2 MB (the launcher moves it to `satellite-stderr.log.1` each time it reaches 1 MB, as the satellite runs). To restart the satellite, end its `pythonw.exe` (the one running `satellite.py`) and the launcher starts it again; to stop it for good, end the task with `Stop-ScheduledTask 'Nova satellite'`. Windows never tells a program it's shutting down, so the launcher takes away the satellite's running mark as it starts, after a logon or a deploy: only a crash while it runs reaches the brain as an unclean stop. To remove it: `Unregister-ScheduledTask 'Nova satellite' -Confirm:$false`, then delete the folder.

### Music

Navidrome or Spotify - `music.player` in `config.yaml` says which, and it's Spotify unless you change it. Both are set up on the brain, which plays the music through each satellite's speaker.

**Navidrome.** Make a Navidrome user for Nova (or use your own), then put its password under Keys on the settings page (at `https://` and the brain's name on your tailnet - see [The settings page](#the-settings-page)), and its address and user in the page's Music section. Or, on the brain, add the password to `~/.config/nova/.env` yourself:

```
NAVIDROME_PASSWORD=the-password
```

and point Nova at it in `~/.config/nova/config.yaml` (the page's Music section has these too, used once Nova restarts):

```yaml
music:
  player: "navidrome"
navidrome:
  url: "http://navidrome.local:4533"
  username: "nova"
```

Songs are fetched as MP3 - Navidrome converts anything else as it sends it - and decoded with ffmpeg on the brain, which `setup.sh --brain` installs. Your liked songs are the ones starred in Navidrome. Nova never sends the password itself, only a salted hash of it, as Navidrome's API has it.

**Fetching music by voice.** With SoulSync, which finds music on Soulseek and files it straight into the folder Navidrome scans, Nova can get a song or album Navidrome hasn't got. Say "download Gooba by 6ix9ine", "get me Gooba" or "download the album Rumours by Fleetwood Mac". Or ask to play something Navidrome hasn't got: Nova says "I haven't got Gooba by 6ix9ine. Do you want me to download it?", and a plain yes said straight back ("yes", "go ahead", "do it") requests it - anything else leaves it. Switch the offer off with "Offer to download what Navidrome hasn't got" on the settings page, under Music (`music.offer_downloads`); "download" works either way. A download needs "download" or "get" in your own words, or that yes: if Claude takes some other answer for a request, such as "okay", it's only offered again for a plain yes. The settings page's Play never offers one, as nothing there can hear a yes.

- **Songs and albums, never a whole artist** - "download everything by Fleetwood Mac" is refused, as a whole discography could mean dozens of albums. A download isn't played when it lands, and Nova never deletes a file. A song Navidrome already has isn't requested ("You've already got Gooba by 6ix9ine."), nor one it has by the title asked for and SoulSync's artist - said a few seconds later, once SoulSync answers. An album it has is, as it may hold only some of the tracks: SoulSync gets the rest, and if it has them all, Nova says "You've already got Rumours by Fleetwood Mac." once SoulSync answers, a few seconds later. The same thing asked for twice while it waits is requested once ("I'm already getting X."), and asked to be played meanwhile, Nova says the same.
- **The original release, not a clean one** - Nova itself picks from what SoulSync finds, never a live, remix or other version unless you named it. For a song, it takes the first SoulSync lists which has every word of the title, isn't a clean, live, remix, demo, unplugged or mix version (by its own name or its album's), and is by the artist you named - or, for exactly that title, by one whose name is spelt closely like it, so "Endless by Gunner", as speech to text can hear Gunna, still finds Gunna's. The artist's own song with exactly the title comes first, wherever SoulSync lists it. With only a title, SoulSync's order decides: the first it lists which will do. A credit such as "(with Little Mix)" is never read as a version. For an album it passes over the clean, edited and censored editions, judged by their titles and by MusicBrainz's notes ("clean", "edited", "censored", "radio edit"). Names only steer what it picks, and never say whether what landed is clean: a candidate called "(Clean)", "(Clean Version)", "(Radio Edit)", "(Censored)" or the like, in a bracket or after a spaced dash (a hyphen, an en dash or an em dash), is passed over. A clean word only counts when nothing but "album", "version", "edit", "edition", "lyrics" or "advance copy" follows it there, so "(Clean Lyrics)" is one, but "(feat. Clean Bandit)" isn't, nor is a song called "Clean". What landed is judged by ear (see "Clean songs, found by ear", below). If you ask to download something Navidrome only has a clean version of - one Nova heard clean - Nova says "You've only got a clean version of X. Do you want me to download the explicit one?", and a yes swaps it for the explicit one.
- **What's said** - "X requested." once SoulSync has taken it, and "X has been downloaded." once it's in Navidrome. A song is named from "requested" on as SoulSync found it: "endless by Gunna requested." for "download Endless by Gunner". If it can't be got: "I couldn't download X.", then why when SoulSync said. A song which lands clean is swapped, or kept and said so, before anything is said of it (see "Clean songs, found by ear", below). An album is downloaded once the tracks SoulSync queued have landed - it skips those the library already has or is already getting, so with two of Rumours' eleven in the library, nine is all of it - counted by title, so a clean and an explicit copy of one song are one track and a live or remix take is its own. If SoulSync queues none of it, Nova says at once "You've already got X." if Navidrome has it (not only a clean version), or else "SoulSync says it already has every track, or is already getting them." An album that only partly came down is "Only 7 of the 9 tracks of X came down.", 9 being how many SoulSync queued, and one with clean tracks, "X has been downloaded. 2 of its tracks came down clean, so I swapped them for the explicit ones."
- **Where it's said** - on the satellite that asked, after its announcement sound as for an alert, at any hour: the quiet hours don't apply, as it's only ever the outcome of your own request. With that satellite away or not able to say it, it goes to your phone through ntfy (`ntfy.phone`, see below), and with no phone topic either it's only logged. Waiting never holds up a conversation, and several downloads can wait at once. One still waiting when the brain stops is forgotten, and logged.
- **How long it waits** - 10 minutes for a song and 30 for an album, from the request (`music.song_download_minutes` and `music.album_download_minutes`, on the settings page under Music and used at the next check), looking in Navidrome every 15 seconds. Navidrome's own file watcher picks new files up. Nova asks it to scan, once, shortly before time runs out, as SoulSync never says when a song or album is done. At the limit it says what it found. SoulSync may carry on, and anything which lands later is simply in Navidrome.
- **What's downloading** - ask "what's downloading?" or "has my song downloaded yet?" and Nova names what it's still waiting for, with how long ago each was asked for, or says nothing is - only what it's watching, not SoulSync's own queue.

To set it up:

1. In SoulSync's own settings, make an API key with admin scope - one which can only read can't request anything - and switch its API on. Albums go through SoulSync's MCP tools, which only answer with "Answer MCP requests at /mcp" switched on there too; with it off, Nova says "SoulSync's MCP is off, so it can't download albums. Turn it on in SoulSync's settings."
2. Put the key under Keys on the settings page (SoulSync's), or on the brain add it to `~/.config/nova/.env` yourself. It's used once Nova restarts, and never logged:

   ```
   SOULSYNC_API_KEY=the-key
   ```
3. Put SoulSync's address in the settings page's Music section (used straight away), or in `~/.config/nova/config.yaml`:

   ```yaml
   soulsync:
     url: "http://soulsync.local:8008"
   ```
4. Make Nova's Navidrome user an administrator, so it's allowed to ask Navidrome to scan. Without that, Nova's log says Navidrome refused, and it waits for Navidrome's own scan instead.

Without the address or the key, "download X" says "Downloading music isn't set up. SoulSync's address goes in Settings, under Music, and its key under Keys.", and "play" something missing says what it always did.

**Clean songs, found by ear.** A song's name and tags can't say whether it's clean: SoulSync tags some explicit downloads "(clean)", and a clean file can sit on an album tagged explicit. So Nova listens to it ([decisions/0022](docs/decisions/0022-clean-songs-by-ear.md)), and swaps a clean song for an explicit one itself.

- **How a song is judged** - against its lyrics, which come from Navidrome (the `.lrc` or `.txt` SoulSync saves beside a song), else from LRCLIB's public search. With neither, it isn't judged. Nova counts the swears in the lyrics (one fixed list of words) and how many Nemotron, its speech to text, hears in the song. Fewer than 3 in the lyrics: not judged, as there is nothing to censor. At least 35% heard: explicit. 15% or fewer: clean. In between: unsure, left alone and listed. The three numbers are `clean_check.lyric_minimum`, `explicit_share` and `clean_share` in `config.yaml`, not on the page, as they were set from a test on real songs.
- **After a download** - the new copy is judged before anything is said. Explicit, or too few swears to judge: "X has been downloaded." Unsure: "X has been downloaded, but I couldn't tell if it's the clean version." Not judged, as the listening failed or there were no lyrics: "X has been downloaded, but I couldn't check it for a clean version." Clean: Nova swaps it, then "X has been downloaded." for an explicit copy, or "X only came down clean, so I've kept it." if every try came down clean. A swap which can't be made says "X has been downloaded, but it's the clean version, and I couldn't swap it: " and why. A swap whose new copy is heard unsure says "X came down again, but not plainly explicit, so you've still got the clean version. The new copy is set aside.", and one whose new copy couldn't be heard, "X came down again, but I couldn't check the new copy, so you've still got the clean version. The new copy is set aside." - in both the original clean copy is back and nothing is "downloaded". In an album, "2 of its tracks came down again but not plainly explicit, so they're still the clean versions.", or "2 of its tracks came down again, but I couldn't check the new copies, so they're still the clean versions." For an album each new track is judged and the clean ones swapped one at a time, and the message counts them: "X has been downloaded. 2 of its tracks came down clean, so I swapped them for the explicit ones."
- **The swap** - the clean file, and the `.lrc` and `.txt` beside it, move into `.nova-held` inside the music folder, at the same path in it. Navidrome scans and drops it, Nova signs in to SoulSync as its admin profile and has SoulSync forget its row for the file (never the file itself, or SoulSync's wishlist drops the request as "already owned"), then asks for the song again through the wishlist. Up to "Tries to swap a clean song" more times (default 2), each a fresh request which may pick another source. If a fetch brings nothing, the request comes off SoulSync's wishlist, your original copy is put back where it was, and the song goes under "Couldn't swap" with the reason (or "Only comes down clean", if the tries before it all came down clean): you never end up with no copy of a song you had. Whenever a swap ends without an explicit copy, it's your original that comes back, and every copy the tries brought stays in `.nova-held` - except one which couldn't be moved, which stays in the library beside your original (or, if it's at your original's path, in its place, with your original kept in `.nova-held`). Only a copy heard explicit (or with too few swears to judge) is swapped in: if the new copy is heard unsure, or can't be heard, it's set aside in `.nova-held` too and the original clean copy is put back, under Unsure or "Couldn't swap", so an unsure new copy isn't kept. Nothing is ever deleted or overwritten, and nothing outside the music folder is touched. A swap's progress is saved, and a restart carries it on.
- **Check library** - the Clean songs block on the settings page, under Music, hears every song in Navidrome whose lyrics have at least 3 swears, and swaps the clean ones as above, one song at a time and apart from Nova's replies, at the lowest priority. It shows "Checking 112 of 398: artist - title", the date of the last check, and four lists: Clean and swapped, Only comes down clean, Unsure, and Couldn't swap, each with its reason. Stop ends it after the current song. A check stopped by a restart carries on from where it was, and one paused ("Paused: ..." and why) carries on with Check library, which then says Carry on, while Stop drops it. A later check hears only songs which are new or whose file changed, and swaps again, without hearing them, songs heard clean before whose swap couldn't be made - except a song whose new copy was set aside as unsure or unheard, which is left alone for 30 days, or one whose swap couldn't be made and whose original was put back, left alone for 7 days (each whatever Navidrome says of its file), and may then be tried again, coming off Unsure if that swap ends any other way, or one which only came down clean, which the check never tries again. It says nothing aloud and sends nothing to your phone. A download's judging goes ahead of a running check.
- **Every night** - Nova starts the library check itself at "Nightly check at" (default 05:30, after SoulSync's 04:00 daily mixes and Monday's 03:00 discovery), while "Check new songs every night" is on (the default). Both are under Music on the settings page, and used straight away. As a check hears only songs which are new or whose file changed, a night's check is short. A check already running is left to run, and one paused is carried on. The check starts at its time, or at a look within the hour after it; a night isn't made up once that hour has gone. So a night whose time passed while Nova was off, or came with the switch off, is skipped, and so is one whose time is moved to one over an hour gone (23:00 set to 13:00 at 14:00): the next is the following night's, at the new time. A day's check is done once its time has come, so moving "Nightly check at" later the same day starts none until the next day. Nova's log says "The night's library check: Checking the library." (or "Carrying on the library check.").
- **A weekly note** - once a week, on "Weekly note on" at "Weekly note at" (default Monday 09:30, outside quiet hours), while "Send a weekly note" is on (the default), Nova sends one message titled "Clean songs" to your phone topic ("Topic for sending things to your phone"), which opens the settings page: "This week Nova swapped 3 clean songs for explicit ones; 1 only comes down clean, 2 are unsure, 1 couldn't be swapped. 4 files wait in .nova-held. See the settings page." A list nothing went on is left out, and so is the holding folder when nothing waits in it; with nothing swapped it starts with what there is, "This week 2 songs only come down clean". It counts only what went on the lists since the last note, and starts "Since the last note," in place of "This week" when that was over 8 days ago. If nothing went on any list, no note is sent. One due in quiet hours (ntfy's `quiet_from` and `quiet_until`) waits for them to end, and one due while Nova was off goes once it's on again, whatever day that is. Each week's note goes once: moving its day or time after one has gone sends no second, and the next goes at the new day and time, or at once if that has already come since the last note was due. The first note, on a new install or the first start after this release, is due as soon as Nova starts (outside quiet hours), counting the last 7 days; as entries listed before this release carry no date, it usually finds nothing and sends no note, and the first real one is on the next "Weekly note on" day. With no phone topic, none is sent.
- **The holding folder** - the page says how many files wait in `.nova-held` ("Holding folder: 3 files waiting for you to empty"). Nova never deletes, so emptying it is your job, by hand.
- **Hardware** - listening runs on the brain's processor, as it has no GPU: about 35 seconds for a 2-minute song, with about 1 GB more memory while it runs, so a first check of a library of about 400 songs takes three to four hours, and each download is about half a minute later to be called downloaded. A GPU would make both a matter of seconds.

To set it up:

1. Give the brain's container the music folder, and only that folder: in the container's config on the Proxmox host, `mp1: /mnt/library/media/music,mp=/mnt/music` (read-write), then restart the container.
2. Install the brain's service again, as root on the brain, with `bash setup.sh --brain` (or `bash update.sh`), then `systemctl restart nova-backend`. Its sandbox lets it write only to its own folders and `/mnt/music`, and a service installed before this release has no `/mnt/music` in it. For a music folder anywhere else, add it with `systemctl edit nova-backend`, which keeps it through later installs:

   ```
   [Service]
   ReadWritePaths=-/your/music/folder
   ```
3. Put where it is as Nova sees it under Music on the settings page, "Music folder" (`clean_check.music_folder`, default `/mnt/music`). With no folder, or one Nova can't write to, songs are still judged, and clean ones are listed rather than swapped.
4. In SoulSync's own Settings, make a second profile with admin rights for Nova - SoulSync's API key can't remove a song, only a signed-in admin profile can. Put its name under Music, "SoulSync admin profile" (`clean_check.soulsync_admin`), and its password under Keys, "SoulSync admin", or on the brain in `~/.config/nova/.env` yourself. It's used once Nova restarts, and never shown back:

   ```
   SOULSYNC_ADMIN_PASSWORD=the-password
   ```
5. Without these, songs are still heard and listed, but none is swapped.

**Spotify.** Spotify Premium is needed. Its client ID and secret (`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, from Spotify's developer dashboard) go under Keys on the settings page, or in the brain's `~/.config/nova/.env` yourself, and your Spotify app has to list the brain's `SPOTIFY_REDIRECT_URI` as a redirect URI - setup.sh sets it to `http://127.0.0.1:8888/callback`. Once Nova has restarted with them, sign in on the settings page, under Music. Spotify's page opens in a new tab, and once you've agreed it sends you to an address that doesn't load: paste that address back on the settings page. Every satellite's Spotify uses the sign-in from its next command, with no restart. Without the page, sign in on the brain instead, as its user - it prints a link to open in a browser:

```bash
runuser -u nova -- venv/bin/python -m voice_assistant.spotify_controller
```

**Spotify on a satellite.** Spotify plays through raspotify on the satellite, which `setup.sh` offers to install, so the Pi shows up as a Spotify speaker called `spotify.device_name` (`Voice-Assistant` unless you changed it). raspotify has to be signed in to your Premium account too, once, apart from Nova's own sign-in above. Its own guide says that, with Spotify Connect discovery (zeroconf, over mDNS) left on, as `setup.sh` leaves it, it needs no username or password: open the Spotify app on a phone or computer on the same local network as the Pi and choose the Pi from its list of devices. raspotify then keeps the login, as `setup.sh` leaves its credential cache on and gives it the folder to keep it in. These are raspotify's own steps, not yet tried from a fresh Nova install. If Nova says it can't find the speaker, see [docs/troubleshooting.md](docs/troubleshooting.md).

### Home Assistant, for the lights

This is set up on the brain, which is what talks to Home Assistant - a satellite holds no token. Nova asks Home Assistant for things in plain words, through its own Assist, which can only reach the devices you've exposed to it. Besides the lights, that's switches, media players, scripts and scenes - "turn off AdGuard protection", "pause the TV", "is the fan on?", "run goodnight", "activate movie night" - after Nova's own commands, so "pause" is still Nova's music and "turn off the timer" Nova's timer. Dimming is Nova's own: it steps each light's brightness itself. What Assist doesn't understand - "make the lamp warmer" - goes to Claude, which can switch and set any light, exposed or not, run a script or scene exposed to Assist once Nova has checked it as below, and read how things are - "is Navidrome up?", "is the garage door open?" - but nothing else. What Claude reads goes to Anthropic, so Nova leaves out where people and things are, network and hardware addresses, the apps and windows in use, notifications and the batteries (which have their own question), and gives a phone's, a Mac's or the PC's sensors only as numbers and times.

Whatever's exposed, Nova never locks, unlocks or opens anything by voice: it checks what Home Assistant says each thing is - a lock, a valve, an alarm panel, or a door, garage door, gate or window - not only the words. Nor does it give Assist anything of the Proxmox integration, nor the PC's HASS.Agent commands, whether each is a button, a switch or anything else, bar the sleep, monitor sleep and lock buttons - through Assist, a shutdown or restart would be done with no yes. Something which acts and isn't in Home Assistant's registry isn't given Assist either, as it could be a new one of Proxmox's: a new one is, once the registry's next read has it - it's read every five minutes - but one with no unique id never is, so it's refused until it has one: `unique_id` in its YAML, or made in the UI. A YAML template or command_line switch and a scene with no id are like that. An old-style `group.*` can't take a unique id at all, so make it a group helper in the UI instead (Settings > Devices & services > Helpers > Group). A script, scene or group is only run once Nova has checked what it does, and a script Home Assistant can't show the contents of - one written in YAML outside `scripts.yaml` - isn't run by voice or offered on the settings page; nor is one that stops or restarts Home Assistant or its host.

1. In Home Assistant, make a user for Nova - Settings > People > Add person, allowed to log in. It needs to be an administrator for most of this, as Home Assistant only lets administrators ask what's in which room, read its registry (what each thing is and what's exposed) and show a script's contents. As an ordinary user, the wider rule is off - only light sentences go to Assist, without rooms, and only while Assist's sentence debugging doesn't answer either, as without the registry nothing it says they'd act on can be vouched for - there are no batteries, and no script is run at all, by voice, by Claude or from the settings page. An administrator's token can do anything in Home Assistant, so keep it as private as a password: what Nova does with it is kept to the lights, finding and ringing your phone, the PC's own buttons, reading how things are for Claude, and the scripts and scenes you run from the settings page or Claude runs for you, by Nova's own code, not by the token.
2. Log in as that user, and under its profile > Security, make a long-lived access token.
3. Put it under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself - it's a password, so don't paste it anywhere else:

   ```
   HA_TOKEN=the-token
   ```
4. Put Home Assistant's address under Everything else on the settings page, or in `~/.config/nova/config.yaml`:

   ```yaml
   home_assistant:
     url: "http://homeassistant.local:8123"
   ```
5. In Settings > Voice assistants > Expose, choose what Assist may control. Exposing something now means Nova works it by voice, so check what's exposed already - a switch such as AdGuard's protection may well be, from before. Names and areas are what you'll call things. Scripts aren't exposed by default, so expose any you'll ask for, such as goodnight - a routine's "run goodnight" step goes through Assist too. "Run goodnight" runs Home Assistant's script called goodnight; "goodnight routine" (or a routine's own words) is Nova's own routine. Claude isn't limited to these for the lights - it can work any light.

Then restart the brain. Its log says `Home Assistant at ...` once it's on. The calendar isn't Home Assistant's - Nova reads it from Google itself, below.

### Google Calendar

Nova reads your calendars straight from Google, not through Home Assistant, with a sign-in of its own kept on the brain - readable by Nova's user only, and never on a satellite. It asks Google for only what it uses: the list of your calendars, reading their events, and adding, moving, renaming and removing events on your own.

1. In [Google Cloud's console](https://console.cloud.google.com/), make a project for Nova, and under APIs & Services > Library switch on the Google Calendar API.
2. Under the OAuth consent screen, make it External, and add the three scopes Nova asks for: `calendar.calendarlist.readonly`, `calendar.events.readonly` and `calendar.events.owned`. Then publish it, so it's In production - in Testing, Google ends the sign-in after a week. Google may warn that the app isn't verified; it's your own, so carry on.
3. Under Credentials, make an OAuth client ID of type Desktop app. Its client ID and secret go under Keys on the settings page, or in the brain's `~/.config/nova/.env` yourself - the secret is a password, so don't paste it anywhere else:

   ```
   GOOGLE_CLIENT_ID=the-client-id
   GOOGLE_CLIENT_SECRET=the-client-secret
   ```

   Then restart the brain, which makes the calendar as it starts - the settings page can.
4. Sign in on the settings page, under Calendars and lists: "Sign in to Google" opens Google's page in a new tab. Google then sends that tab to an address which won't load; that's expected, and you paste that address back on the settings page. It's used from the next question, with no restart. "Sign in again" there signs in as a different account, or after the sign-in was removed in your Google account. Every box on Google's page needs ticking - a sign-in without one of them isn't kept, and says which.

Every calendar in your Google account's own list is read - one added there later turns up by itself, within five minutes - with each repeating event on each of its days. On the settings page, under Calendars and lists, each is shown by its name in Google, with a switch for whether it's in what's on: one switched off isn't read for "what's on" or "when's my MOT?", but is still read when it's asked about as an activity. Nothing of it is kept on the brain: with Google out of reach, Nova says "I can't reach Google's calendar right now", and once the sign-in needs doing again, it says so. `home_assistant.calendars` isn't used any more, and can be deleted from the brain's `config.yaml`.

Each calendar but your own - Google's primary one - Google's own (holidays, your contacts' birthdays) and one named by an email address, as a person's own shared with you is, is an activity, by its name: with a Work calendar, "what time do I have work tomorrow?" reads that calendar alone, and every event on it is work, whatever it's called - bar an all-day one, which "what's on" says by its own name. A Gym calendar is "when's gym tomorrow?" once Nova has read Google's list with it in - at start, and when the calendar's asked about once the list is five minutes old. "Working" and "shifts" mean Work, and "lectures" and "classes" Uni; a name of two words is heard by its first too - "uni" for "Uni timetable" - unless another starts with it, and in "when's uni?" or "am I at uni?" only with a day after it. A calendar named like one of Nova's own things, such as Timer or Music, is left to Claude, so it never takes over Nova's own command. Which are activities can be switched on the settings page - each calendar's "An activity" - and it's used straight away: heard from the next question, and told to Claude from its next request. Each choice is kept by the calendar, in the brain's `calendars.json` in Nova's data folder, so one Google stops listing for a while is as it was when it's back. A calendar named by an email address is never said by it, or told to Claude: your own, which Google names by yours, is "your calendar", and any other "a shared calendar" - switched on as an activity, it's said that way too, but can't be asked about by name. `home_assistant.activities` isn't used any more either, and can be deleted.

Adding by voice - "put the dentist on Thursday at 3" - goes to your own, primary calendar unless you name another, and lasts an hour unless you say when it ends; a day without a time is all day. Nova says back what it added - "Added Dentist, Thursday the 29th at 3 PM." - only once Google has taken it, and names the calendar when it's not the one new events go to. Google only lets Nova add to calendars you own, so a shared one such as Uni says so. Nova never changes or deletes an event, and never sends one twice: if Google doesn't answer in time, or has trouble of its own, it says it can't tell whether it was added, so you can check before asking again. "New events go to", on the settings page, sends them to another of your own calendars instead, from the next one added - back to the primary if that one's deleted or no longer yours.

### Batteries

With Home Assistant set up, the brain finds every device it has a battery level for - phones, tablets, laptops, remotes - by itself, so a new one is there without telling Nova. Ask "how's my Mac's battery?", "what's my phone on?" or "battery levels", and Nova says each level, with how old the reading is if it's stale.

It also warns you as a battery falls: once at 5% for a phone or tablet, and at 15% and 5% for anything else, each only once until the device has charged. The warning goes to your phone through ntfy (`ntfy.phone`), and is said out loud as well on the default satellite while you're home and it isn't quiet hours (ntfy's `quiet_from` and `quiet_until`). With no ntfy, or if sending fails, it's said instead, and held until the morning if it comes at night. Each device's marks, and its switches for warning you and for sending to your phone, are on the settings page's Batteries section, and take effect straight away. A Mac with Optimised Battery Charging holds at 80% and never says it's charging, so a mark set above 70 never starts over for it, and warns only once. `config.yaml`'s `batteries` section has only the marks each kind of device starts with - one for a phone or tablet, two for anything else - how often the batteries are read and when a reading counts as old. See `docs/decisions/0007-battery-warnings.md`.

### Football

With Home Assistant set up, ask about any team or competition - "when do Liverpool play next?", "what's the Stevenage score?", "where are Liverpool in the table?", "who's left in the Carabao Cup?", "what's the starting 11 for Liverpool?", "what happened in the England match on Sunday?", "when did Liverpool last play Newcastle?" - and Claude answers from what Home Assistant says: fixtures with their local day and time, live scores, results, tables, line-ups, what happened in a match, and where ESPN lists them, the ground and bookmakers' odds as percentages. Nova never calls ESPN and keeps no list of teams, so it only says what Home Assistant returned: with football unreadable or old it says "I can't get football right now.", and for yellow cards, that Home Assistant's football doesn't have them. Asked about the teams you follow in general, Nova names any team it can't get ("I can't get South Shields right now.") and says the rest; it's "I can't get football right now." when none can be got. A request with "football" in it, or naming a team you follow, which would otherwise have gone to Assist goes to Claude instead, as Assist would take "is Liverpool playing?" for a media player.

Line-ups come out about an hour before kick-off. Nova gives the formation, starting eleven and bench of the side you asked about once they are out, and the other side's if you ask for it; before that it says they aren't out yet and usually come about an hour before, and where only one side's is out, that the other's isn't yet. For a team it's the match in play, else its next if that's within a day, else its last if that was within the last day, else its next. With no team named, it's the followed team playing now or today. The matches ESPN doesn't list - non-league clubs' league games, such as South Shields' - Home Assistant fills in from BBC Sport for the teams you follow, with the score and minute while they're played, and they're announced like any other; but they have no ground, odds or line-ups, and a league only BBC lists has no table. A team you don't follow is asked about from ESPN alone, so follow it to have those matches. If BBC Sport can't be read, nothing is said of it: the team keeps BBC's last fixtures and results for up to two days, never a score in play from them, and after that has ESPN's matches alone.

What happened in a match is its goals, penalties and red cards, which Nova gives Claude in the order they happened, each with its minute: a goal or a penalty scored with the score after it, an own goal under the side it counts for, a penalty missed or saved, and a red card (a second yellow card counts as one). For a match ESPN carries it also has ESPN's own notes on the match, said as ESPN wrote them - a penalty conceded, a VAR decision, a penalty saved, a shot against the woodwork - and Claude says a mistake cost a game only where one of them says what happened, though it may say a goal was a late winner or equaliser from the minutes and the score. The notes come from ESPN's summary of the match, which is an extra: where Home Assistant can't read it, the match is said without notes, and an old copy of it gives its notes. Yellow cards, substitutions and the kicks of a shoot-out aren't listed, nor are those kicks among the notes. Where a match has goals on its score and Home Assistant lists none of them, Nova says its scorers aren't listed, and where the goals listed don't add up to the score, Nova says them without a score after each rather than a wrong one, and says that they don't add up. For a team it's the match in play, else its last; or, if you name a day, its match on that day, even while another is in play; a match still to come, or called off, has nothing to say. With no team named - "what happened in the match?" - it's the followed team whose match is in play, else the one whose match is over on the day (today unless you name one), the latest to kick off first, where the line-ups question, which keeps its own order, prefers a match about to start. With no day named, a match which is over and was played in the last 15 hours counts too - twelve hours after it ended, which Nova takes to be three hours after kick-off, as it has only the kick-off - so just after midnight, and the next morning (until 10:45 after a 19:45 kick-off), "what happened in the match?" is last night's match, and not "Name a team for its match."; a match in play still comes first, and a day named is only that day's.

Asked about a competition on a day - "what happened in the Nations League on Sunday?" - Nova gives every match of it as a line with its score, and, once it has started, its scorers after it with the minute of each ("(pen)" for a penalty, "(og)" for an own goal, under the side it counts for) and its red cards, or, where it has goals and none is listed, that its scorers aren't listed; this costs Home Assistant nothing more than the scores. A player's name is as the source gives it, which Claude shortens as people do. Nova never says a score with a dash between the numbers, which the speech reads out as "dash": a score is "Spain 2, England 3", a shoot-out is "Liverpool won on penalties, 4 to 3", a formation, in a line-up, is "4 2 3 1", and a note of ESPN's which has a score in it, "France 1-0 Türkiye", is said "France 1 to 0 Türkiye". Nova's announcements say a shoot-out the same way: "Liverpool won on penalties, 4 to 3".

The last time two teams played - "when did Liverpool last play Newcastle?" - is the latest match between them: this season's results first, then each season before it, back five, stopping at the first season with one. It is said as any match is, with its score, who was at home, the ground and, where Home Assistant has them, its scorers and red cards. Where there is none, Nova says how far back Home Assistant looked and no more: "No match between Liverpool and Newcastle United is listed in the last 6 seasons." (this season and five before it), never that they haven't played, as ESPN may not list a match that old. The other team is named as Home Assistant found it, not as it was asked for, so that a wrong pick (a "Rangers" which was found as Stafford Rangers) shows. A national side's list has no seasons in it, so for one Nova says "No match between England and Brazil is among ESPN's results for England." Where Home Assistant also read BBC Sport's month pages for the team's league matches (South Shields'), it says back to which month in the answer ("bbc_since", as YYYY-MM), and Nova names BBC Sport where its results were read: "No match between South Shields and Spennymoor Town is listed in the last 6 seasons, nor in BBC Sport's results since August 2021." (for a side whose one list has no seasons, "... is among ESPN's results for South Shields, nor in BBC Sport's results since August 2021."), and for a side only BBC Sport knows (its id begins "bbc:"), "No match between South Shields and Spennymoor Town is among BBC Sport's results for South Shields since August 2021." A side which only BBC Sport could know, and which South Shields never met, comes back from Home Assistant as one ESPN found nothing called, with no seasons; where it read BBC Sport's results for the team ("bbc_since"), Nova says so: "No match between South Shields and Nowhere Rovers is among BBC Sport's results for South Shields since August 2021, and ESPN found nothing called Nowhere Rovers." (with the other team as it was asked for, and as before, "ESPN, where Home Assistant's football comes from, found nothing called Nowhere Rovers.", where none was read). A month is said in words, never as "2021-08", which the speech would read out as "dash"; a match found says nothing of it, and one which isn't a year and month is refused as any answer of the wrong shape is. Where no meeting is found, Home Assistant may also give "nearest", one to three names of the team's opponents in the matches it looked through which are closest to the other team as it was asked, closest first ("Chorley" for "Corley", which the speech model hears for it), and Nova says them after the not-found line, including the one for a side ESPN found nothing called: "No match between South Shields and Corley is among BBC Sport's results for South Shields since August 2021, and ESPN found nothing called Corley. The nearest name among South Shields' opponents is Chorley." (for two or three, "The nearest names among South Shields' opponents are Chorley and Crawley Town."). Claude is told to ask "Did you mean Chorley?" rather than guess another team, and on a "yes" asks again with that name, which finds the match. Nothing of it is said with a match found, a day or the same team, and one which isn't one to three names (or has an empty or a non-text name) is refused as any answer of the wrong shape is. Nova asks for the other team only for a team's match; a team asked about against itself, under the same name, is its match alone, and under another ("Liverpool FC" against Liverpool) Home Assistant finds one team for both and Nova says "Liverpool and Liverpool FC are the same team." with nothing of a match. A day wins over the other team. So "what happened in the Liverpool match on the 16th of March 2025?" is that day's match, which Home Assistant finds in that season's results where it is further back than the team's last few, and a match of an earlier year is said with its year ("on Sunday the 16th of March 2025"). South Shields' league matches come from BBC Sport, as far back as the pages Home Assistant keeps, and for another team or an earlier day Home Assistant also reads BBC Sport's month pages for them, back five seasons; their cup matches come from ESPN as any other team's do.

Odds are only said where Home Assistant looked them up, which it does for a team's first two fixtures and for a match to come which it takes from a day's scoreboard. It gives such a match its odds, or marks it as one ESPN lists none for, and Nova then says "No odds are listed for it." A later fixture, which was never looked up, has nothing said of its odds either way, and nor has one from a Home Assistant which doesn't mark them. A competition is found by the name ESPN has for it, and ESPN calls the EFL Cup the Carabao Cup and the European Championship "EURO", so Claude is told to name a competition as it is properly called where it knows it; Home Assistant also tries a name without a plural "s" ("Euros") and keeps a few names ESPN doesn't use.

Home Assistant has to have three things, by these names: `sensor.football_teams`, the followed teams with their matches; `input_text.football_teams`, a text box of their names, separated by commas; and `shell_command.football_ask`, which answers a question about any team or competition. They live in Home Assistant's own configuration, not in this repository, and [docs/decisions/0014](docs/decisions/0014-football.md) says what each holds, and [0015](docs/decisions/0015-football-round-two.md) what round two adds to them: the line-ups question and BBC Sport's matches; [0016](docs/decisions/0016-football-what-happened.md) adds the question of what happened in a match, and the goals, penalties and red cards each match in an answer carries; [0018](docs/decisions/0018-football-older-games.md) adds the other team to the match question, with the seasons its answer looked through and the team it found, and a day further back than a team's last few matches; [0019](docs/decisions/0019-football-gaps.md) closes the gaps parked along the way (odds said only where looked for, a national side not found, the same team twice, a goal ruled out and the names of competitions); [0021](docs/decisions/0021-football-last-gaps.md) closes the last ones, and adds to the match answer the month BBC Sport's pages were read back to (`bbc_since`); [0023](docs/decisions/0023-football-did-you-mean.md) adds, where no meeting is found, the nearest names among the team's opponents (`nearest`). Without the sensor, Nova says football isn't set up in Home Assistant.

Teams are followed on the settings page, in the Football section (in the Daily group), or by voice - "follow Arsenal", "unfollow Arsenal" - which is usually a few seconds and can take up to about a minute, as Home Assistant looks the team up. For each team you follow, Nova says on the default satellite, after the announcement sound, when its match is about to start (15 minutes before, unless you change it on the page, in the Football section), each goal, half time, the second half starting and full time, within about two minutes. A score which goes down is said as a goal ruled out only once a reading about two minutes later (110 seconds or more after the first one which showed it) still shows it, as two of Home Assistant's readings a minute apart can both carry one old copy of the match; or, if the reading which ends the match, written after the first which showed it, shows it too, as no reading follows full time and one at full time is never an old copy of the match in play - so a goal ruled out just before full time is said once, before "Full time.", and where full time shows the higher score again, not at all. Each of the five is a switch for each team, used straight away. Nothing is said in quiet hours (ntfy's `quiet_from` and `quiet_until`), while Home Assistant says you're out, or for what happened while Nova wasn't watching for more than five minutes; what can't be said is dropped, not held. `config.yaml`'s `football` section has how often Nova looks, how old Home Assistant's football may be, and the minutes before kick-off.

A match which is called off (postponed, cancelled or abandoned) is said as that, in plain words, wherever a followed team's match is: Home Assistant keeps a called-off match of today or tomorrow in the sensor, with its phase `off` and its clock the source's word, and Nova says "South Shields v Harborough Town, National League North, tonight at 7:45 PM, is postponed." ("is cancelled", "was abandoned", or "is off" for any other word; "tonight" from five in the evening, "today" earlier, "tomorrow" for one at the head of the team's next fixtures) in a team's answer and the overview, in place of the team having no fixtures, and never with odds, a ground or a score. It also says it by itself, once, when the match's 15-minute warning would have come ("South Shields' match against Harborough Town tonight is postponed."), under the Starting switch, with the same quiet hours and the same rule about you being out, and instead of that warning, which a called-off match never gets. A match found called off only after its kick-off is said once when first seen, if its kick-off was today; one abandoned while Nova is watching it in play is said once as abandoned, and never gets "Full time." or a goal ruled out, but only once a reading about two minutes after the first one which showed it off (110 seconds or more) still shows it, as a score which goes down is held, so that a single reading which is an old copy or a glitch says nothing. A match which was said to be off isn't given the 15-minute warning if a later look shows it still to be played, and one which is moved to another day and called off again is said again. Asked for line-ups with no team named, when the only match today is called off, Nova says so from what Home Assistant gave ("... is postponed. No line-ups are listed for it.") instead of asking for another day's, and a called-off match never outranks another followed team's match which is on or over; in the overview a called-off match of tomorrow is its own part ("Tomorrow: ...") and doesn't take the place of the team's next match which is on. What has been said is kept with the rest of what Nova has seen of the match, so a restart doesn't say it again, and a match which comes back and is called off again the same day isn't said twice. [docs/decisions/0024](docs/decisions/0024-football-called-off.md) has the design.

### Journeys

With Home Assistant set up, ask "what time do I leave for work?" (or "when should I set off for uni?") and Nova finds that activity's next timed event within the next day on your Google calendar, works out the journey with Google Maps through Home Assistant, and says when to leave: "Work starts at 9:00 AM. The drive is 24 minutes, so leave by 8:26 AM." The leave-by time has a buffer taken off (10 minutes), and is, for a drive, the journey as it is now, or, for a leave-by time 45 minutes or more away, as Google expects it at the time of leaving, said as "about" (a walk or a cycle is always from the journey as it is now). With no such event Nova says so and gives the journey time. Name a day - "what time do I leave for work tomorrow?", which Claude takes - and it is that day's first event not yet begun, however far off ("Work starts at 9:00 AM tomorrow."), or "You've no work tomorrow." with the journey time. Say a time to be there - "what time do I leave to be at my gran's for 6?" - and that is used instead of the calendar; for a place with no calendar and no time said, Nova gives the journey time and asks "When do you need to be there?". "How long's the drive to Work?", "how long to walk to Uni?", "how long to cycle to the Metrocentre?" and "how long to get to Work by bus?" say the time and the distance, driving unless you name another way, with "Traffic's heavy: that's 12 minutes more than on a clear road." when a drive is over its time on a clear road by 5 minutes and a fifth. Public transport gets a journey time only, as Google doesn't say which one to catch.

A leave-by answer ends "Want a reminder?", and a plain yes sets "Leave for Work" a few minutes before the time to leave (5, on the settings page) on the satellite you asked from. For a drive from home Nova then looks at the traffic every 5 minutes through the 45 minutes before the time to leave, and if the time has moved by 5 minutes or more, moves the reminder and says so on the satellite and on your phone (through ntfy, whatever the hour): "Traffic to Work is heavier now. Leave by 8:15 AM, not 8:26 AM." Once Home Assistant has shown you at home in those 45 minutes, and then shows you away, you've left and the reminder is dropped; before them, where you are isn't asked, so popping out earlier in the day keeps it. Naming an accident or roadworks isn't possible, as Google through Home Assistant gives only times; a walk or a cycle has a reminder but no watch, and neither has one set from a satellite which travels with you.

Places are Home Assistant's zones, found at each ask: add Work, Uni and Gran's on Home Assistant's map, named as your calendars are for work and uni, and "what time do I leave for work?" finds both. The calendar's own words work too: "my shift" is the zone named Work, and "lectures" the one named Uni. "My gran's" which matches no zone isn't guessed at: Nova says to add it as a zone, and so it does for "work" or "my shift" with no zone for Work, as the calendar would still give a start for a place Google could only guess at. A place said as an entity's id ("person.sam") is never looked up or sent. Anything else you say - a shop, an address, a postcode - goes to Google as said, and Nova says it back as it heard it, so a misheard one is caught by ear. Journeys start from the Home zone; on the settings page, "Travels with me" beside a satellite makes that satellite start them from your phone's position, which Home Assistant works out and Nova never sees; such an answer says how far you are from somewhere, so Claude only hears that it was said. "How long to get home?" from a satellite at home is "You're already there." The settings page's Journeys group has the buffer, the reminder's heads-up, the checks, the traffic threshold, the units and the monthly limit, and each is used straight away.

To set it up (the Google and Home Assistant steps are yours; Nova does none of them):

1. In [Google Cloud's console](https://console.cloud.google.com/), make a project, switch on the Routes API under APIs & Services > Library, enable billing on the project, and make an API key. Google gives 5,000 free requests a month; Nova counts its own and stops at `journeys.monthly_requests` (3,000 by default), saying so.
2. In Home Assistant, add the Google Maps Travel Time integration (Settings > Devices & services) with that key, and then switch its sensor's polling off in the integration's System options. The sensor alone would ask every 10 minutes, about 4,300 requests a month, most of the free ones; Nova asks through the integration's actions and doesn't need it. The integration's setup also asks for an origin and a destination: any will do, such as `zone.home` for both, as Nova gives its own each time it asks.
3. Add the places as zones in Home Assistant (Settings > Areas, labels & zones > Zones), named as the calendars are: Work, Uni, Gran's.

Without the integration, Nova says journeys need Google Maps Travel Time set up in Home Assistant. [docs/decisions/0025](docs/decisions/0025-leaving-time.md) has the design and what it doesn't do.

### Finding your phone

With Home Assistant set up, the brain finds every phone, tablet or Mac with the Home Assistant app on it. Ask "where's my phone?" and Nova says whether it's at home, in one of Home Assistant's zones, or near a street and town, with how long ago that was. "Ring my phone", "find my tablet" or "ring the Mac" rings it too, through the app's own notifications: an Android phone says "Nova's looking for your phone" out loud on its alarm volume, an iPhone gets a critical alert, and a Mac a notification with its sound. Where it is goes no further than Nova, however it was asked - even of Claude: Claude's history only has "I said where your phone is.", and "where's my phone?" is still Nova's own while it's listening for an answer.

The one step: on the phone, let the Home Assistant app ring through Do Not Disturb - on Android, add Home Assistant to the apps allowed through Do Not Disturb, in the phone's Do Not Disturb settings; on an iPhone, allow its critical alerts in Settings > Notifications > Home Assistant. Without it, a phone on silent or Do Not Disturb doesn't ring.

### Nova in Home Assistant

Nova can also be a device of its own in Home Assistant, set up on the brain, through its MQTT broker (the Mosquitto add-on), so you can see and change things from Home Assistant's dashboards and automations. Its controls are Status, Volume, Alarm volume, Music volume, Next alarm, Timers, Stop, Restart, Speak, Backend, Microphone muted and Ask Nova, and with music set up, Playing, Cover, Play or pause, Next track, Previous track and Playlist. Nothing said to Nova, and none of its answers, is sent there - timers show only when they end. Each satellite is a device of its own. Without this, Nova works as it always did, and it carries on if the broker goes down.

1. In Home Assistant, make another user for Nova - Settings > People > Users > Add user, called `nova`, **not** an administrator, and with "Can only log in from the local network" on. The Users tab is only there with Advanced mode on in your profile. The broker accepts Home Assistant's users.
2. Put its password under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself:

   ```
   MQTT_PASSWORD=the-password
   ```
3. Say where the broker is under Everything else on the settings page (its host, port and user, used once Nova restarts), or in the brain's `~/.config/nova/config.yaml`. The host is Home Assistant's own address on your home network, not its web address:

   ```yaml
   mqtt:
     host: "192.168.1.20"
     username: "nova"
   ```
4. Restart the brain, and the device appears in Home Assistant. Put it in an area (its page > the pencil > Area): with an administrator's `HA_TOKEN`, that's how Nova knows what room it's in, so "lights on" means that room's lights. It looks every half a minute until Home Assistant has the device, then every five minutes, so giving it an area or moving it takes a few minutes to be noticed.

The microphone switch works from Home Assistant or the settings page, and stays as it was through a restart. While it's muted, Nova doesn't hear "Hey Nova", but what you send it with Speak is still said.

Ask Nova is a box to type a request in, answered out loud on that satellite as if you'd said it, muted or not, once any conversation going on has finished; the reply is never sent back to Home Assistant, and what you type is logged only as what you say is. If Nova asks something back, type the answer within a minute - or, after a spoken request, type it while Nova listens. See [docs/decisions/0027](docs/decisions/0027-ask-nova-by-typing.md).

Say on Nova is a second box beside it: whatever is typed is said out loud on that satellite, word for word, with no sound first, once any conversation going on has finished. Speak, for automations, keeps its sound.

### ntfy, for alerts and your phone

1. On your ntfy server, make Nova a user with only the access it needs, and a token for it - here with a `homelab` topic for alerts, and `clipboard` and `reminders` for your phone:

   ```bash
   ntfy user add nova
   ntfy access nova homelab rw
   ntfy access nova clipboard wo
   ntfy access nova reminders wo
   ntfy token add nova
   ```
2. Put the token under Keys on the settings page, or on the brain add it to `~/.config/nova/.env` yourself:

   ```
   NTFY_TOKEN=the-token
   ```
3. Say which topic is which in the settings page's Notifications section, or in the brain's `~/.config/nova/config.yaml`:

   ```yaml
   ntfy:
     url: "https://ntfy.example.com"
     alerts: ["homelab"]
     phone: "clipboard"
     reminders: "reminders"
     restarts: "homelab"
   ```

Then restart the brain. What it sends is tagged `nova`, so it never reads its own messages back out.

### The settings page

Nova serves a page for looking after it from a phone or a laptop. It opens on a list of Nova's sections in four groups - Now (Status, Music, Timers and alarms), Home (Devices, Batteries, Clock displays), Daily (Journeys, Football, Calendars and lists, Routines) and Nova (Satellites, Sound and voice, Claude, Notifications, Memory, Keys, Everything else) - each row with a line on how it is now ("Nothing playing", "Tablet at 14%"), left empty when Nova can't say. A tap opens one section alone; the phone's back gesture or All of Nova returns to the list where you were, and an address ending `#music` opens Music. What needs attention - a restart waiting, Nova's settings file unreadable, the default satellite away, a battery under its mark, Google or Spotify to sign in to, a problem in the last hour - is listed above the list, each row opening its section. The search box narrows the list to the sections which have every word you type, in a setting or a button, and lists what matched; a tap opens it, marked. On a wide screen the list stays down the left with the open section beside it.

Each feature's settings are in its own section, under what it shows (Home Assistant's, MQTT's and privacy's, and any Nova doesn't place, under Everything else), all under one save bar, which shows wherever you are while anything's changed and unsaved and keeps the changes as you move between sections. The star by Nova's name shows whether it's waiting, listening, thinking or speaking, and as a volume's dragged, Nova beeps on its speaker at each new level. Music shows what's playing, and plays something else; Devices switches and dims your lights, sets their colour and warmth, groups them by room, and runs your scripts and scenes - never one whose name says it locks or opens anything, and a script asks first where a scene is one tap; Batteries sets when each warns you and whether it's sent to your phone; Football follows and removes teams; Journeys sets how it works out when to leave; and Satellites mutes the microphone and has a "Travels with me" switch for each, which makes its journeys start from your phone instead of from home. Calendars and lists chooses which of your calendars are in what's on, which are activities and which one new events go to, your main to-do list, the weather entity and which Wake-on-LAN button wakes each PC; Spotify is signed in to under Music, and Google under Calendars and lists. Stop silences whatever rings and what Nova's saying, on every satellite, and a removed alarm, timer or reminder, or a forgotten memory, can be undone for ten seconds. Keys are only ever saved, never shown back. It's on the brain, not the satellites - one page for the whole house - and listens there alone, and only your own Tailscale login gets in:

1. In Tailscale's admin page, turn on HTTPS certificates - under DNS.
2. On the brain, have Tailscale pass the tailnet's requests on to the page, with a certificate for the brain's name (as root, which the brain's setup is run as; anyone else puts `sudo` in front):

   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```
3. Say who's let in, in the brain's `~/.config/nova/config.yaml`:

   ```yaml
   settings_page:
     allow: ["you@example.com"]
   ```

Then restart the brain, and open `https://` and its name on your tailnet - `tailscale status` shows it - on any of your devices. A change has to come from the page itself, so another website can't make one in your name; settings are saved to the brain's `~/.config/nova/config.yaml`, the one before kept as `config.yaml.bak`. Most are used straight away; under each, the page says whether it's used straight away or once Nova restarts, and when a save waits for a restart, it offers one. A satellite's own settings are changed on the satellite: the brain's address by hand, and its password with `bash setup.sh --new-password`, once New password, under Satellites, has made one.

#### At an address of your own, through Nginx Proxy Manager

The page can also be served through Nginx Proxy Manager (NPM), at `https://nova.<your domain>`, so it opens at home on any of your devices without Tailscale on - behind a password of its own, as NPM's requests carry no Tailscale login. The tailnet address stays as it is, with no password.

1. Set the page password first, on the page at its tailnet address: under Keys, Page password (for NPM) - at least 12 characters. It's kept only as a salted hash, never shown again, and a new one logs out every device using the old.
2. In the brain's `~/.config/nova/config.yaml`, give the page a second port and say that only NPM's address may use it, then restart Nova:

   ```yaml
   settings_page:
     proxy_port: 8766
     proxy_from: ["NPM's address"]
   ```

   That port listens on every address of the brain's, but anything but NPM gets a bare 403. With `proxy_from` empty, the port isn't opened at all.
3. In NPM, add a proxy host: domain `nova.<your domain>`, scheme `http`, forward to the brain's address and port 8766, with your wildcard certificate, Force SSL on, and the "LAN + Tailscale" access list. No websockets are needed.

Then open `https://nova.<your domain>`. Until you log in, it shows only a login form, and none of Nova's own is served - only the page's files, which are all in this repository. A login lasts 30 days on that device and can do everything the tailnet address can - keys and settings included. Keys are never shown, but a login can change where Nova finds Home Assistant, Navidrome, ntfy or MQTT, and Nova would send that service's token or password to the new address once it restarts, so a login is worth as much as the keys. On a shared or lost device, use Log out (under Keys, on that address) or Log out everywhere, which ends every login made with the page password, whichever device - from the tailnet address too. Replacing the page password at the NPM address asks for the current one as well. After 5 wrong passwords in a row, a device's tries are refused for a minute; NPM says which device each try is from, in the `X-Real-IP` header it sends by default. A forgotten page password is set again at the tailnet address.

### A clock display

A small touchscreen clock can sit beside a satellite as Nova's face: the time and weather, your next events, and what's playing, with a glowing orb while Nova listens and a sound wave while it speaks. Hold a finger on it to talk, swipe or tap for the next page, and its volume buttons change Nova's volume. The brain serves the page, on a port of its own (`display.port`, off until you set one), to a kiosk app on the clock with a key made on the settings page. See [docs/clock-display.md](docs/clock-display.md) to set one up.

### The wake word model

"Hey Nova" is a custom [openWakeWord](https://github.com/dscripka/openWakeWord) model, `models/hey_nova.onnx`. It isn't in this repository, and none comes with Nova, so you train one or supply your own. Only satellites listen for it.

**Without it**, Nova still starts, but the satellite listens for openWakeWord's own stock "Hey Jarvis" instead - saying "Hey Nova" does nothing - until `models/hey_nova.onnx` is there. Its log says `No wake word model at ...` as it starts, and setup says the same.

**To train one**, openWakeWord has an automated training utility: a Google Colab notebook which makes a model in under an hour, though its README warns that the model may do poorly in some settings, and a fuller notebook, `notebooks/automatic_model_training.ipynb` in openWakeWord's repository, which can make a better one but needs more development experience. Its README, under "Training new models", has both. Nova needs the `.onnx` file.

**To supply your own**, copy the file into `models/` on each satellite, before running `setup.sh` or after (then restart the satellite). Name it after the wake word, in lower case with each space an underscore, so "Hey Nova" is `hey_nova.onnx`. For another phrase, give `assistant.wake_word` that phrase on the satellite - setup asks for it - and name the model to match, as `hey_computer.onnx` for "Hey Computer". The brain takes the wake word off the front of what it hears using its own `assistant.wake_word`, so give it the same phrase.

Keep a copy of the file somewhere safe, as nothing else has it.

### Exact package versions

`requirements.txt` says what a satellite needs; `requirements.lock` says what's known to work on the Pi; and `requirements-backend.txt` is the brain's, all of them exact. To install exactly those (Python 3.13 only):

```bash
venv/bin/pip install --no-deps -r requirements.lock           # a satellite
runuser -u nova -- venv/bin/pip install --no-deps -r requirements-backend.txt    # the brain, from root
```

## Configuring

The repository's `config.yaml` has every setting, with its default and a comment saying what it's for - the wake word, how long to wait for you to talk, timer and alarm behaviour, the music player, and more. Don't change it there: put just the settings you want different in **`~/.config/nova/config.yaml`**, in the same sections, and they're used on top:

```yaml
music:
  player: "navidrome"
thresholds:
  silence_duration: 0.8
```

That file is this install's own, outside the repository, so an update never touches it and your settings never end up on GitHub. It's checked as Nova starts: a misspelt setting, one of the wrong kind, or one out of its range (a 5-second silence, say) stops it with a message naming it - `journalctl -u nova-backend` shows it on the brain, `journalctl --user-unit voice-assistant` on a satellite - rather than being quietly ignored. `update.sh` checks them the same way before it stops Nova, and if the new code would refuse them, it says why, takes the code it pulled back out and leaves Nova running as it was. A setting Nova no longer has, such as `weather.location`, is only warned about, with where it's set now, and can be deleted. Almost everything is set on the brain; a satellite's file has little beyond its brain's address, its name and the wake word.

Keys and passwords go in **`~/.config/nova/.env`**, readable only by you, never in a settings file. Everything Nova saves - timers, alarms, volume levels, what it remembers about you, the Spotify sign-in and its logs - is in **`~/.local/share/nova/`**.

The settings most likely to need changing:

| Setting | What it's for |
| --- | --- |
| `music.player` | `navidrome` or `spotify`. |
| `music.offer_downloads`, `music.song_download_minutes`, `music.album_download_minutes`, `soulsync.url` | Whether Nova offers to download a song or album Navidrome hasn't got when you ask to play it, how many minutes it waits for one to appear in Navidrome (10 for a song, 30 for an album), and SoulSync's address - see "Fetching music by voice". On the settings page, under Music, and used straight away. SoulSync's key goes under Keys. |
| `clean_check.nightly`, `clean_check.nightly_time`, `clean_check.weekly_note`, `clean_check.weekly_day`, `clean_check.weekly_time` | Whether Nova checks the library itself every night (default on) and at what time (default 05:30), and whether it sends a weekly note of what happened to clean songs (default on), on which day (default Monday) and at what time (default 09:30) - see "Clean songs, found by ear". On the settings page, under Music, as "Check new songs every night", "Nightly check at", "Send a weekly note", "Weekly note on" and "Weekly note at", and used straight away. |
| `clean_check.swap_tries`, `clean_check.music_folder`, `clean_check.soulsync_admin` | How many more times Nova asks SoulSync for a song it heard clean (default 2), where the music folder is on the brain (default `/mnt/music`), and the SoulSync profile Nova signs in as - see "Clean songs, found by ear". On the settings page, under Music, and used straight away. The profile's password goes under Keys. The listening thresholds stay in `config.yaml`. |
| `backend.address` | On a satellite, its brain, as `name:port`. |
| `thresholds.vad_threshold` | How sure Nova must be that it's hearing speech. Raise it if noise keeps it listening; lower it if quiet speech is missed. |
| `thresholds.silence_duration` | How long a pause ends what you're saying. |
| `thresholds.wake_word_confidence` | How sure the wake word model must be. Raise it if Nova wakes by itself. The brain's setting, not the satellite's own: on the settings page, under Listening, as "How sure Nova must be it heard its wake word", and the brain sends it to each satellite. Used straight away. |
| `thresholds.media_wake_word_confidence` | The same while a media player which Home Assistant puts in the satellite's room is playing, as a film's speech can sound like the wake word. Nova's own music doesn't count, and the higher of the two is used then. On the settings page, under Listening, and used straight away. |
| `wake_clips.keep` | Whether the satellite keeps 3 seconds of audio around each wake (2 before the wake word was heard, 1 after), so that a voice check can be trained on real false wakes. Off unless you switch it on in the satellite's own settings, for as long as it's needed. The clips stay on the Pi, readable by you only, and are never sent anywhere. Used once the satellite restarts. |
| `logging.log_transcripts` | Whether what's said is kept in the logs. |
| `sounds.listening_enabled`, `sounds.chime_enabled`, `sounds.reminder_enabled`, `web_search.enabled`, `conversation.history_max_age`, `spotify.pause_while_speaking` | The blip, the chime for done, and the sound before alerts and warnings (alarms, timers and reminders always sound), searching the web, how long a conversation's thread is kept, and pausing Spotify while Nova talks. On the settings page, and used straight away. |
| `ntfy.quiet_from`, `ntfy.quiet_until` | The night, when only the most urgent alerts are read out. |
| `journeys.buffer_minutes`, `journeys.reminder_minutes`, `journeys.check_every_minutes`, `journeys.watch_minutes`, `journeys.change_minutes`, `journeys.heavy_minutes`, `journeys.heavy_percent`, `journeys.monthly_requests`, `journeys.distance_unit` | The minutes to spare (10), how long before the time to leave the reminder rings (5), how often traffic is looked at while a reminder waits (5), how long before leaving that looking starts (45), the move of the time to leave worth saying (5), what makes traffic heavy (5 minutes and 20%), the most journeys asked of Google in a month (3,000) and whether distances are in `miles` or `km`. All on the settings page, under Journeys. See "Journeys". |
| `voice.speed` | How fast Nova speaks, from 0.75 to 1.5 times its usual speed. On the settings page, with a button to hear it first, and used from the next reply. |

## Running it

As a service (the usual way, once `setup.sh` has installed it). On a satellite:

```bash
systemctl --user restart voice-assistant
```
```bash
journalctl --user-unit voice-assistant -f
```

and on the brain, as root:

```bash
systemctl restart nova-backend
```
```bash
journalctl -u nova-backend -f
```

Or by hand, to watch what it's doing - `satellite.py` on a satellite, and `backend.py` on the brain (as its user, from root, with the service stopped):

```bash
venv/bin/python satellite.py                         # a satellite
runuser -u nova -- venv/bin/python backend.py        # the brain
```

Logs are in `~/.local/share/nova/logs/` - `smart_assistant.log` on a satellite and `backend.log` on the brain - trimmed at 5 MB, each with a `latency.jsonl` beside it. The brain's has how long each stage of every interaction takes, summarised by (on the brain, as its user, with `runuser -u nova -- ` in front):

```bash
venv/bin/python scripts/latency_report.py
```

A satellite's has, for each wake word, how long until the first of the reply reached it - the whole wait, as the user hears it - a line of JSON each, which the report doesn't summarise.

If something isn't working, [docs/troubleshooting.md](docs/troubleshooting.md) goes through it by symptom.

## Developing

The tests run anywhere - on a PC without a microphone, speaker or any of the heavy packages - since `tests/conftest.py` stands in for whatever isn't installed. The few tests which start a fresh Python are the exception, so `python-dotenv` has to be really installed:

```bash
pip install pytest ruff numpy soxr soundfile pyyaml requests python-dotenv
```
```bash
python -m pytest
```
```bash
ruff check .
```

Both also run on GitHub for every push (`.github/workflows/checks.yml`), along with a check of both locks - `requirements.lock`, the satellite's, and `requirements-backend.txt`, the brain's - for packages with known security problems, which is also run once a week, as those turn up without the code changing. The scripts in `scripts/manual/` need real hardware or accounts, so they're run by hand, and `scripts/soak_monitor.py` keeps an eye on a long-running assistant - [docs/release-checklist.md](docs/release-checklist.md) names each and says when to use it. Before changing the speech-to-text model, record yourself with `scripts/record_samples.py`, to measure the models on. `scripts/record_wake_samples.py` records "Hey Nova", a film and you talking, to train a voice check on - a check that it's your voice saying "Hey Nova", to tell it from a film's - and `scripts/train_voice_check.py` trains it on those and on the wake clips you've labelled, saving it in the satellite's data folder, never in the repository.

When picking work back up after a break, start with [docs/roadmap.md](docs/roadmap.md).
