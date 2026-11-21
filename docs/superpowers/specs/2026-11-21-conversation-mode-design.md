# Conversation mode

21 November 2026. Piece 1 of 2. Piece 2 (plans written into the vault, and a Plans list) has a design of its own and is built after this.

## Why

Nova takes one request per "Hey Nova" (or per press at the PC). It listens again only when it has asked a question, at most three times, keeps the last five exchanges and forgets them after five minutes. That suits commands. It doesn't suit what he did once with another assistant by voice, hands busy: plan a whole thing in one long spoken session.

He wants an open conversation: he talks, Nova answers, he talks again, with no wake word in between. If Nova is speaking and he starts talking, it stops so he can talk over it. It ends when he says so. He expects to use it now and then, for brainstorming, a run of questions or a run of tasks, and wants it because it's a good thing for an assistant to have. He wants it at the Pi and at the PC alike, finished before the Mac satellite is built.

## What changes for him

- "Hey Nova, let's talk" (or a press and the same at the PC) starts a conversation. From then on he just talks. Nova answers each thing he says and listens again by itself.
- Talking while Nova speaks turns its voice down at once, and stops it as soon as it has heard real words from him. A cough or a bang turns it down for a moment and it carries on.
- Nova remembers the whole conversation until it ends.
- It stays on Haiku, so it's quick and cheap. When the talk turns to planning, or he says "think harder", it moves up to a bigger Claude model, and back when he says so or the conversation ends.
- Music, lights, timers and the rest work in the middle of it as they do now.
- "End conversation" ends it. So does going quiet for five minutes.

## Goals

1. A conversation can be started and ended by voice at any satellite, and from Home Assistant.
2. In a conversation every reply is followed by listening, with no wake word, no press and no limit on turns.
3. His voice alone cuts in on a reply: dip first, stop on real words (his choice, option 3). The words he cut in with are the start of his next turn; none are lost.
4. It works at the PC through its speakers. That needs Nova's own sound taken out of the PC's microphone, which the PC doesn't have today.
5. The conversation is patient: a thinking pause doesn't end his turn, and a long turn isn't cut off at 15 seconds.
6. The whole conversation is remembered until it ends.
7. Haiku by default; a bigger model for planning, by his word or Nova's own judgement.
8. Everything he might change is on the settings page.
9. When a conversation ends, what was said in it is still to hand for a moment, so piece 2 can turn it into a plan. Piece 1 writes none of it down.

## Non-goals

- Saving anything from a conversation (piece 2).
- Telling his voice from someone else's. Nova's voice check only knows how he says "Hey Nova"; it can't judge other speech. In a conversation, anyone talking in the room is heard as him. See "Later".
- Working out from the words whether he has finished a thought. A pause of a set length ends his turn.
- Moving a conversation from one satellite to another.
- More than one conversation at a time.
- The Mac satellite.
- A new look for the orb or the clock. They show listening, thinking and speaking as now.
- Faster answers. See "Shaped by hardware".

## Anti-goals

- A conversation that starts by itself, or carries on with nobody there. The microphone is open throughout, and what is said goes to Claude. It only starts when asked, it's plain that it's on, and it ends itself after a quiet spell.
- Nova stopping for its own voice. If what it hears is what it's saying, that's echo, not him.
- Nova talking over him, or cutting him off while he's thinking.
- Anything changing outside a conversation. An ordinary request at either satellite behaves exactly as today.
- A reply stopping for the television. Speech from something playing is already left alone (`not_for_me`); in a conversation that matters more, not less.
- A big bill by accident. The bigger model costs many times Haiku and a long conversation sends its history with every turn.

## Approaches

**The mode itself.** One sensible way: build on what's there. The brain already listens again after a question (`satellite.listen()`, `_handle_interaction(follow_up=True)`) and already answers a wake word said over a reply (`_heard_over`, `talked_over`). A conversation makes every reply listen again, with its own patience and no limit. A second, always-streaming pipeline beside the first was dropped: it would copy most of session.py and the two would drift.

**Knowing he has started talking over a reply.** Three ways, put to him on 21 Nov:

1. Any voice stops it, decided on the satellite. Instant, but a cough, the television or a trace of Nova's own voice also stops it.
2. Words stop it, confirmed by the brain from the live transcript. About half a second slower, far fewer false stops.
3. **Dip, then stop (his choice).** The volume dips the moment a voice is heard, and the reply stops once the brain hears real words. With no words, the volume comes back. Feels instant, is rarely wrong, and is the most work.

**Where the voice is noticed** (my call). On the brain, not the satellite. While a reply plays in a conversation the satellite sends what its microphone hears, as it does for a recording, and the brain's own detector (Silero) and speech-to-text (Nemotron) decide. The other way, a detector on each satellite, would dip perhaps a tenth of a second sooner, but means a second model on the Pi, a first one on the PC, and the same rule written twice. A dip a fifth of a second after he starts is soon enough, and the stop needs the brain's words either way. The satellite still stops a reply by itself for the wake word or a press, as today.

**The PC** (my call). Use the echo canceller the Pi already has (echo.py, speexdsp) on Windows, shipping speexdsp's library with the PC satellite. The alternatives: Windows' own voice processing isn't reachable through the audio library the satellite uses; and pressing to cut in is what the PC does today, not what he asked for. Echo removal on the PC is the riskiest part of this piece, so it's measured before anything is built around it, with the press as the fallback (see "Measured first").

## How it works

### Starting and ending (conversation.py, llm.py)

A conversation starts in three ways:

- A fixed phrase, handled without Claude as the other everyday commands are: "let's talk", "let's have a conversation", "conversation mode", "start a conversation".
- A tool Claude can call, `start_conversation`, for the same wish in other words ("can we talk something through").
- The Home Assistant switch (below).

It ends in six:

- A fixed phrase, when it is the whole of what he said: "end conversation", "end the conversation", "stop the conversation", "that's all", "that's everything", "goodbye". Not "bye": speech-to-text already treats a lone "bye" as a word imagined in noise and blanks it.
- A tool Claude can call, `end_conversation`, for the same in other words ("okay, I'm done, thanks").
- The Home Assistant switch.
- A quiet spell: `conversation.idle_minutes` (5) since the last turn Nova answered. A turn Claude judges `not_for_me`, one heard as nothing and one imagined in noise don't count, so a film playing to an empty room can't keep it going.
- Three turns in a row that Claude judges `not_for_me`: Nova says it will leave it there, and ends. Each such turn costs a request with the whole history.
- The satellite going away, or Nova restarting.

Nova marks both ends out loud with a few words and the sounds it already has: the listening sound as it starts, the falling sound as it ends. No new sounds. An idle end says so briefly ("I'll leave it there.") so the open microphone never closes silently, or stays open unannounced.

One conversation at a time. Asked for at a second satellite while one is on, Nova says where the first one is and doesn't start another.

`conversation.enabled` off removes the phrases, the tools and the switch.

### Turn after turn (session.py)

In a conversation, after each reply (and after a turn with nothing to say), the session asks the satellite to listen again, as it does for an answer today. What comes is handled as a **fresh request**, not as an answer: the everyday commands handled without Claude, routines and dismissals all work, which is what "tasks work as they do now" needs, and what the start, end and model phrases rely on. Only after a reply ending in "?" is the next turn handled as the answer to it (`follow_up=True`), as now, so questions with set answers still work. The differences from an ordinary listen:

- **No limit on turns.** `max_follow_ups` doesn't apply.
- **Patience.** His turn ends after `conversation.pause_seconds` (1.5) of quiet, not `silence_duration` (0.6). Nothing may end his turn sooner than that: if the PC's extra end rules (voice drop, no new words; branch press-ends) have landed, in a conversation they wait at least as long.
- **Long turns.** One turn may last `conversation.longest_turn` (90 s), not `recording_timeout` (15 s). The satellite's own cap on one recording (30 s) is lifted to match while a conversation is on.
- **Waiting for him to start.** Listening between turns doesn't give up after 6 s. It waits until he speaks or the quiet spell ends the conversation. No "I didn't catch that" for silence, a cough or noise heard as nothing: Nova just keeps listening.
- **No listening sound between turns.** It would play dozens of times. The orb and the clock show listening.
- **At the PC** the microphone and speaker stay open for the whole conversation (the microphone light stays on, which is right: it is listening). A press still does what it does today: over a reply it cuts in, during his turn it ends the turn.

Typed requests (Ask Nova in Home Assistant) during a conversation join it as turns.

### Talking over a reply (session.py, remote.py, satellite.py, speaker.py)

While a reply plays in a conversation:

1. The satellite sends the brain what its microphone hears, with its own sound already taken out.
2. The brain runs Silero on it. Speech for `DIP_AFTER` (about 0.15 s) sends the satellite "dip": the reply's volume drops to `conversation.dip_level` (30%) over a few hundredths of a second. A playback's volume can already be changed while it plays (`set_playback`), but it jumps; the dip needs a fade.
3. From a moment before that speech began, the audio also goes to Nemotron. When Nemotron has a real word (not one it imagines in noise), and the words aren't Nova's own (below), the brain stops the reply, exactly as a wake word over it does today, and carries straight on recording: that audio, from before the first word, is his next turn.
4. If no real word comes within `CUT_IN_WAIT` (about 1 s) of the voice ending, the brain sends "undip" and the reply carries on at full volume from where it is.

**Nova's own words.** The brain knows the text it is speaking. If the words Nemotron hears are a run of words from the reply being said, they're echo that got past the canceller: no stop, undip. This is the guard against the anti-goal, on top of echo cancelling, and it's why the stop needs words and not just sound.

**Nemotron is only fed while a voice is heard**, not for the whole reply. See "Shaped by hardware".

**"Hey Nova" and a press while this is on.** One loop owns the Pi's microphone, so while it is sending the microphone to the brain the satellite isn't listening for the wake word itself. In a conversation that's fine: "Hey Nova" said over a reply is words, so the brain stops the reply for it, and the wake word is taken off the front of the turn as it is now. At the PC a press while the microphone is being sent during a reply cuts in (stops the reply and starts his turn), as a press over a reply does today; it doesn't end a recording.

**Listening noises.** "Yeah", "mm-hm", "okay", "right" and the like, said while Nova talks, are real words but not a wish to cut in. While everything heard so far is from a short list of those, the reply stays dipped and carries on; any other word stops it. "Stop" always stops it.

**While something else plays in the room.** When Home Assistant shows a media player playing in the satellite's room (the test the wake word's stricter threshold already uses), the voice cut-in is off for that reply and only the wake word or a press cuts in. `not_for_me` is decided by Claude after a turn, too late to save a reply a film's dialogue has already stopped.

**Until the PC's canceller is ready.** echo.py cancels nothing until it has lined the speaker up with the microphone, which takes some seconds of sound, and the PC opens its devices afresh for each conversation. Until it says it's ready, the PC's cut-in is a press only.

A reply stopped this way is noted in the history as cut short, as one stopped by the wake word is today, so Claude knows he didn't hear the rest.

`conversation.talk_over` set to "wake word or press" switches the voice cut-in off altogether: the satellite doesn't send its microphone during a reply, and the wake word and a press over a reply work exactly as they do now.

Outside a conversation nothing changes: a reply is only stopped by the wake word or a press.

### The PC hears itself out (echo.py, the PC satellite's install)

- speexdsp's library ships with the PC satellite and echo.py loads it on Windows.
- echo.py's two hard parts, measured for the Pi's devices, are measured again for the PC's: the speaker's and microphone's clocks drifting apart, and the gap between a sound being played and heard. Windows' audio may behave differently; the canceller's method stays, its numbers may not.
- Echo cancelling on the PC only runs while a conversation is on, when both devices are open anyway. An ordinary press is as it is today.
- Only what Nova plays itself is taken out. A game, a video or a call on the PC is not; nor is it at the Pi. That speech is left to the words rule and `not_for_me`.

### The other satellite in the room (quiet_others.py)

Today every other satellite ignores the wake word while Nova speaks on one. In a conversation that holds for the whole conversation, not only while Nova speaks, for satellites in the same room: "Hey Nova" said from habit must not start a second turn at the Pi while he's talking to the PC. Satellites in other rooms are unaffected.

### Memory (llm.py)

- While a conversation is on, exchanges aren't dropped at five or forgotten after five minutes. Up to `conversation.history_exchanges` (100) are kept; past that the oldest go.
- The history is sent with every turn, so the request is laid out for Claude's prompt caching to cover it (the system prompt already is), or a long conversation costs far more than it need.
- When it ends, the last five exchanges stay as ordinary history, so "Hey Nova, what did you say about X" a minute later still works, and the rest are let go, after the "ended" step below has had them.
- **For piece 2:** as a conversation ends, the session calls an `ended` step with the whole of it (each exchange, when, and which model answered). Piece 1's step does nothing. Nothing from a conversation is written to disk by piece 1, and what the console shows follows `logging.log_transcripts` as now.

### How Nova talks in a conversation (llm.py)

The system prompt gains a short part while a conversation is on: this is a spoken back-and-forth; answer in a few sentences unless asked for more; ask one thing at a time; don't end every reply with a question; speech from something playing is still `not_for_me`. A reply ending in "?" needs no special handling, as every reply listens again.

### The bigger model (llm.py, conversation.py)

- A conversation starts on `llm.model` (Haiku).
- It moves up when he says so ("think harder", "use the bigger model"; fixed phrases). Said alone, the phrase has the bigger model answer his last turn again; with more after it ("think harder about the route"), the bigger model answers the rest. It also moves up when Claude calls a tool, `think_harder`, offered only in a conversation and only while `conversation.switch_itself` is on, for when the talk has turned to planning or needs real working-out. The turn that calls it is answered again by the bigger model, with a word first ("Let me think about that.") so the wait isn't silence.
- Once up, it **stays up** until he says to go back ("quick answers", "back to normal") or the conversation ends. My call, and a change from "drops back by itself" as I first put it to him: moving up turn by turn would ask Haiku first every time, doubling the wait on exactly the turns that are already slowest.
- `conversation.bigger_model` chooses it: Sonnet 5.5 by default, Opus 5.5 offered.
- Usage is recorded by model already; prices become per model, so the dashboard's pounds are right, and the daily spending alert works as it does now.

### Things that happen during a conversation

- **Timers, alarms and announcements** don't wait for the conversation to end, as they wait for a request today: it could be half an hour. They wait for the next gap, when Nova isn't speaking and he isn't mid-turn, then ring or speak. Dismissing by voice needs no wake word, as he's already being listened to.
- **Music.** Music playing when a conversation starts pauses, as for any request, and comes back when it ends. Music asked for during one plays at the level Nova already uses under its own voice until the conversation ends. The voice cut-in still applies to replies; whether music's leftover echo sets off dips is one of the things measured first.
- **Mute.** Muting the microphone ends the conversation: Nova's own mute at the Pi, Windows' microphone mute at the PC. Starting a conversation at the PC, by any route, unmutes Windows' microphone as a press does: it is as deliberate.

### Home Assistant (ha_device.py)

Each satellite's device gains a switch, "Conversation": on while one is running there, turning it on starts one (as the phrase does, with the sound), turning it off ends it. So it can be seen and ended from the phone, and started from a key or an automation.

### Settings (a new section, "Conversation")

| Setting | Meaning | Default | Limits |
|---|---|---|---|
| `conversation.enabled` | Whether conversations can be started at all | on | |
| `conversation.pause_seconds` | Quiet that ends his turn | 1.5 | 0.6 to 5 |
| `conversation.longest_turn` | The longest one turn of his may last, in seconds | 90 | 15 to 300 |
| `conversation.idle_minutes` | Quiet that ends the conversation | 5 | 1 to 30 |
| `conversation.talk_over` | What cuts in on a reply: his voice, or only the wake word or a press | voice | |
| `conversation.dip_level` | How far a reply turns down when a voice is heard, as a share of its volume | 0.3 | 0.1 to 0.8 |
| `conversation.bigger_model` | The model for planning | Sonnet 5.5 | the page's list |
| `conversation.switch_itself` | Whether Nova may move up by itself | on | |
| `conversation.history_exchanges` | The most exchanges remembered in one conversation | 100 | 20 to 300 |

All read as they're used, so a save applies to the conversation in progress. The existing `conversation` section (history_length, history_max_age) keeps its settings.

## Measured first

Three things decide whether the voice cut-in can work, and none can be known from the code. The plan's first tasks measure them, before the cut-in is built. They make sound in his room, so each waits for his go.

1. **The Pi.** While Nova speaks (and while its music plays), record the echo-cancelled microphone and run the brain's Silero and Nemotron over it. How often does Nova's own leftover voice read as speech, and as words? This sets `DIP_AFTER`, the detector's threshold while a reply plays, and whether the own-words guard is enough.
2. **The PC.** Load speexdsp on Windows, run echo.py against his microphone and speakers, and measure the same. Before that: how much of Nova's voice is left at all, and how long the canceller takes to be ready from freshly opened devices (and whether what it learnt can be kept between conversations).
3. **The brain's processor.** Nemotron listening during a reply shares pve2's processor with the voice still making the reply's later sentences. Does the reply stutter, or the words come late?

What the answers change:

- Clean enough at both: build as written.
- Leftover voice sets off dips but not words: dips are held back for longer or the threshold is raised while a reply plays; the stop still works.
- The reply stutters while Nemotron listens: Nemotron waits for a gap between the voice's sentences, or the stop falls back to the detector alone with a longer wait.
- The PC's echo can't be made reliable: at the PC, `talk_over` is "wake word or press" by default (a press cuts in, as today), voice can be switched on for headphones, and he is told before the rest is built.

## Shaped by hardware

pve2 has no GPU, so speech-to-text, the detector and the voice all run on its processor.

- The gap between him finishing and Nova starting stays what it is today, plus the longer pause he's allowed. It will feel like quick turn-taking, not an instant voice chat.
- Nemotron listens during a reply only once Silero has heard a voice, since it shares the processor with the voice being made at that moment.
- The bigger model is slower to answer than Haiku; that wait is Anthropic's, not pve2's.

The AI machine he plans (with a GPU) would lift the first two: faster words, and room to listen all the time.

## Testing

Every rule gets a test that fails without it. In outline:

- conversation.py: each start and end phrase; an end phrase only as the whole utterance; the tools; `enabled` off; a second satellite is refused.
- session.py: a reply in a conversation is followed by listening; more than three turns; the conversation's pause, longest turn and wait-to-start are used, the ordinary ones outside it; silence and noise between turns say nothing; the quiet spell ends it and says so; a timer due mid-conversation rings at the next gap; mute ends it; the `ended` step gets the whole conversation.
- The cut-in, with fake detector and speech-to-text: voice then words stops the reply and the same audio becomes the next turn; voice with no words dips then undips and the reply finishes; words which are the reply's own don't stop it; `talk_over` off leaves only the wake word and press; none of it happens outside a conversation.
- speaker.py: a playback's volume dips and returns without a click.
- satellite.py and remote.py: the new messages (send the microphone during a reply, dip, undip, conversation on and off); the PC keeps its devices open through a conversation and closes them after; the recording cap lifts.
- echo.py: loads on Windows with the shipped library, and still does nothing, with the warning, without it.
- quiet_others.py: same-room satellites stay quiet for the whole conversation, others don't.
- llm.py: history kept past five and past five minutes in a conversation, capped at the setting, trimmed to five on ending; the conversation part of the prompt; `think_harder` moves up and stays, the phrases move both ways, `switch_itself` off hides the tool; prices by model.
- ha_device.py and the settings page: the switch; each setting's limits and row.
- By hand, on his go (they make sound): start, a few turns, cut in mid-reply, cough mid-reply, a long thinking pause, a task, think harder, end; at the Pi and at the PC through speakers; with the other satellite in the room.

## Order of building, and other work in flight

Three branches not yet on main touch the same files: pc-app (the PC satellite as an app), press-ends (the PC's end rules) and approvals. This is built on main as it stands and merged with whatever has landed at deploy. The PC's shipped library has to fit however pc-app packages the satellite.

## Later

- Telling his voice from others': a speaker model on the brain, taught his voice, checking each turn. Worth it if conversations get interrupted by other people or the television in practice.
- Ending his turn by what he said, not only by a pause.
- The tray menu and the PC app starting a conversation (once pc-app is live).

## My calls, for him to overturn

1. The voice is noticed on the brain, not on each satellite.
2. Echo cancelling on the PC reuses the Pi's method; a press is the fallback if it can't be made reliable.
3. Once on the bigger model it stays there until he says, or the conversation ends.
4. Sonnet 5.5 as the bigger model's default.
5. Five minutes of quiet ends a conversation; 1.5 s of quiet ends a turn; a turn may last 90 s.
6. The start and end phrases above, plus Claude's own judgement through the tools.
7. No listening sound between turns; no new sounds or orb look.
8. Timers and announcements come at the next gap.
9. The other satellite in the room stays quiet throughout.
10. Music asked for mid-conversation plays quietly until it ends.
11. A "Conversation" switch in Home Assistant on each satellite.
12. Anyone's voice counts as his, for now.
13. Mute ends the conversation; starting one at the PC unmutes Windows' microphone.
14. Each turn is a fresh request, so commands and routines work; only a turn after a question is its answer.
15. Only turns Nova answers keep a conversation going; three in a row not meant for it end it.
16. While something else plays in the room, only the wake word or a press cuts in.
17. Listening noises ("yeah", "mm-hm") don't stop a reply; "stop" does.
18. "Think harder" alone answers his last turn again.
19. "Goodbye" ends it, "bye" doesn't.
20. At the PC, a press is the only cut-in until the echo canceller is ready.

## Left to the builder

- Names, message shapes on the link, and where each rule lives within the files named.
- `DIP_AFTER`, `CUT_IN_WAIT`, the dip's fade time and the detector's threshold while a reply plays, from the measurements.
- How "a run of words from the reply" is matched (how many words, how loosely).
- The exact wording Nova says at the start, the end and before thinking harder.
- How speexdsp's library is built or fetched for Windows and shipped.
- Model ids, prices and request settings for the bigger model, checked against Anthropic's current documentation.
- The decision record's number, taken at merge.
