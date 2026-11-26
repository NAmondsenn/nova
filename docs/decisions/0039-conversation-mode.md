# 0039: Conversation mode

21 November 2026 · Asked for by the user in chat, the design approved ("yes I approve the design... I like it") · Design: [the spec](../superpowers/specs/2026-11-21-conversation-mode-design.md) · Plan: [the plan](../superpowers/plans/2026-11-21-conversation-mode.md) · Built; the measurements and his named deploys and hand checks (113 onwards) to come

**Context.** Nova takes one request per "Hey Nova" (or per press at the PC). It listens again only after a question, at most three times, keeps the last five exchanges and forgets them after five minutes. That suits commands. It doesn't suit what he once did with another assistant by voice, with his hands busy: plan a whole thing in one long spoken session. He wanted an open conversation: he talks, Nova answers, he talks again with no wake word in between, and if Nova is speaking and he starts to talk, it stops so he can. He expects it now and then, for brainstorming, a run of questions or a run of tasks, and wants it because it is a good thing for an assistant to have. At the Pi and the PC alike, finished before the Mac satellite.

**What he decided.**

1. **An open conversation, at the Pi and at the PC.** Started by voice or from Home Assistant, ended by voice, a quiet spell or Home Assistant. No wake word or press between turns, no limit on turns.
2. **Haiku by default, a bigger model for planning.** The conversation starts on `llm.model`; when the talk turns to planning, or he says "think harder", it moves up to a bigger Claude model, and back when he says so or it ends.
3. **Talking over a reply: dip at once, stop on real words** (the third of three ways put to him). The reply turns down the moment a voice is heard and stops once Nova has heard real words from him; with no words the volume comes back. The other two were any voice stops it (instant, but a cough, the television or a trace of Nova's own voice stops it too) and words stop it (about half a second slower, far fewer false stops).
4. **"Conversation mode" among the start phrases**, with "let's talk", "let's have a conversation", "start conversation mode" and "start a conversation". It ends on "end conversation", "end the conversation", "end conversation mode", "stop the conversation", "that's all", "that's everything" and "goodbye", only as the whole of what he said. Not "bye": speech-to-text already treats a lone "bye" as a word imagined in noise.

**What I decided for him** (the spec's "My calls", for him to overturn).

1. The voice is noticed on the brain (Silero and Nemotron over what the satellite sends during a reply), not on each satellite: a second model on the Pi, a first one on the PC and the same rule written twice, for a dip perhaps a tenth of a second sooner.
2. Echo cancelling on the PC reuses the Pi's method (echo.py, speexdsp); a press is the fallback if it can't be made reliable.
3. Once on the bigger model it stays there until he says, or the conversation ends: moving up turn by turn would ask Haiku first every time, doubling the wait on the turns already slowest.
4. Sonnet 5.5 as the bigger model's default, Opus 5.5 offered.
5. Five minutes of quiet ends a conversation, 1.5 s of quiet ends a turn, a turn may last 90 s.
6. The start and end phrases above, plus Claude's own judgement through the tools `start_conversation`, `end_conversation` and `think_harder`.
7. No listening sound between turns, and no new sounds or orb look.
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
18. "Think harder" alone answers his last turn again (narrowed during the build, below).
19. "Goodbye" ends it, "bye" doesn't.
20. At the PC, a press is the only cut-in until the echo canceller is ready.

**What was built.**

- **The state** is one brain-wide `OpenConversation` (`voice_assistant/open_conversation.py`): whether a conversation is on, where, what ends it, which model it is on. One at a time; asked for at a second satellite, Nova says "We're already talking at the {name}." Every new behaviour is behind `on_at(satellite)`, so outside a conversation an ordinary request at either satellite behaves exactly as before: no listening during a reply, three follow-ups at most, 0.6 s of quiet.
- **Turn after turn.** After each reply the session asks the satellite to listen again, with no wake word, press or limit, and a conversation's own patience: `pause_seconds` of quiet ends his turn, a turn may last `longest_turn`, and it waits as long as it takes him to start, saying nothing for silence or noise. Each turn is a fresh request. At the PC the microphone and speaker stay open for the whole conversation.
- **Talking over a reply** (`cut_in.py`, the session's frame loop). The satellite sends what its microphone hears during a reply; the brain's Silero dips the reply after `DIP_AFTER` of voice and Nemotron, fed only once Silero has heard a voice (the hardware rule), decides whether the words are real. Words which are the reply's own are echo and don't stop it. The words he cut in with, from before the first one, are the start of his next turn.
- **The PC hears itself out.** The Pi's echo canceller now runs on Windows with speexdsp's library, only while a conversation holds the devices open; until it is ready the PC's cut-in is a press.
- **Memory** (`llm.py`). The whole conversation is kept, up to `history_exchanges`, laid out for Claude's prompt caching; at the end the last five stay as ordinary history and an `ended` step gets the whole of it, for piece 2. Nothing is written to disk. The system prompt gains a short part for a spoken back-and-forth.
- **The bigger model.** By his words ("think harder", "use the bigger model"; back with "quick answers", "back to normal") or, with `switch_itself` on, by Claude's `think_harder` tool.
- **Things during one.** Timers, alarms and announcements wait for a gap; music, the room's other satellite and mute are covered below.
- **Home Assistant.** A "Conversation" switch on each satellite's device: on while one is running there, on starts one, off ends it. It is withdrawn while conversations are switched off.
- **Settings**, a section "Conversation" on the settings page (in the Nova group, after Claude), all used straight away, even in the conversation in progress: `conversation.enabled` (on), `pause_seconds` (1.5, 0.6 to 5), `longest_turn` (90, 15 to 300), `idle_minutes` (5, 1 to 30), `talk_over` (the voice at a computer and the wake word elsewhere, the voice anywhere, or the wake word or a press), `dip_level` (0.3, 0.1 to 0.8), `bigger_model` (Sonnet 5.5, or Opus 5.5), `switch_itself` (on) and `history_exchanges` (100, 20 to 300); and, under Claude with the other usage prices, the bigger model's two.

**Decisions made during the build.**

*The PC's echo library.*
- The library is conda-forge's `speexdsp` package for Windows, pinned by hash (the package's and the DLL's) in `install.ps1`, and unpacked by a small helper (`scripts/windows/unpack_library.py`) with the PC's own Python. The package is a zstd archive, which PowerShell 5.1 can't open and Python 3.13 has no module for, so **`zstandard` is added to the PC's Python environment at install** (`requirements-pc.txt`, pinned). The satellite never imports it. This is a new package on his PC, put to him for his say.
- A NuGet package (`SpeexDSPSharp.Natives`) was the first source and was refused: a single-publisher package is a weaker source for a binary run on his PC than conda-forge.

*Matching the reply's own words.*
- A run of words is the reply's own (so not his) when it matches the reply, with the edges tolerated: the first heard word may be the end of a word in the reply, the last the start of one, if the fragment is 3 or more letters (2 would have made "no", "so" and "hey" the reply's own in most ordinary replies). Curly apostrophes are straightened. Digits are not normalised against spoken numbers; the measurements will show whether that matters.
- "Stop" always stops a reply, even if "stop" is in the reply: his word must never be missed.
- The words are judged again at two moments, with the same rule: when the voice goes quiet (settle: a short drain of speech-to-text, never its second model, which would run on every cough against the hardware rule), so a short real interruption such as "no" isn't lost to the undip; and at the end of a reply (the own-echo guard), so his next turn starts from where the reply ended when what was heard was Nova's own voice.

*The bigger model.*
- "Think harder" said alone re-asks the last exchange only when Claude answered it with a plain or read-only answer. After a command it only moves up and says "Okay.", because re-asking would run it twice (a second timer, a duplicate calendar event). "Let me think about that." is said before the re-ask, by his words as well as by Claude's tool, so the wait isn't silence.
- The bigger model's requests use medium effort and up to 4096 tokens, where Haiku's use low and 1024: thinking counts inside the maximum, so 1024 would cut planning answers off, and "think harder" at low effort contradicts itself. My call, for him to overturn; slower and dearer planning turns are the cost, and the two constants are in `llm.py`.
- Prices are as `config.yaml` has them, `usage.bigger_input_price` and `bigger_output_price`: Sonnet 5.5 at $2 a million input tokens and $10 output (Opus 5.5 $4 and $20), taken from Anthropic's published list on 21 November 2026. For him to check against Anthropic's pricing page; they don't follow `bigger_model` by themselves.

*Starting, ending and the sounds.*
- A start at a wake-word satellite whose microphone is muted is refused, saying so; at the PC Windows is unmuted, as the spec says.
- The falling sound at an end follows the listening-sound setting: off means no sound, though the line is still said for an end nobody asked for. The reply "Go ahead." and "Okay." come through the action's result, once; the session adds only the sounds and "I'll leave it there.".
- Any error in an open turn ends the conversation with "I'll leave it there." Claude being unreachable is a spoken fallback, not an error, so this is for real bugs only and can't loop.
- `enabled` turned off while one is on doesn't cut it off: it carries on until it ends in the ordinary ways, so the microphone is never stuck open. Only starting goes.
- Anything said while a timer rings silences it, as at any request: "that's all" ends the conversation as well. What rings is read as his turn begins, once the wait for it has had a gap, as a timer which came due in that wait, which can last minutes, rang in its gap; without a gap it is read as the wait began, so one which came due a moment before he spoke, and hasn't rung yet, isn't silenced unheard.
- A turn begun by the wake word or a press over a reply, whenever the voice cut-in isn't on (before the PC's canceller is ready, while something plays in the room, or with `talk_over` on wake word or press), has the conversation's patience too: `pause_seconds`, `longest_turn`, and the PC's press end rules no sooner than the pause. It keeps an ordinary request's wait to start, its moment from before the wake word, and the wake word taken off.
- So that `longest_turn` really is such a turn's limit, a satellite told a conversation is on there makes a recording begun by the wake word or a press uncapped, with no cap, as a recording the brain asks for open has none; and the brain's feed for that wake keeps up to the longest turn which can be set (`MAX_TURN_FEED_SECONDS`, 310 s with the pre-roll), all of it. Before this the satellite ended it at `longest_request` (the ordinary request's 15 seconds) and said "limit". Outside a conversation both are capped as before.
- Uncapped is not open. Such a recording is otherwise an ordinary wake's or press's: at a computer a second press ends the turn rather than cutting in, and a line Nova says without listening ("Let me think about that.", "Sorry, I didn't catch that.") that starts while a press's recording is going is stopped by it, as any reply is, rather than said over him; a conversation's end crossing it leaves it for the brain to end, rather than ending it silently. Its feed keeps its start however long it waits to be read (an open feed keeps only the last 30 to 60 seconds), so a long think with web search no longer loses it. For a while these were made open, and both went wrong.
- A routine never starts a conversation, run by itself or asked for by voice: the step says "A routine can't start a conversation." Run by itself, one would have been left on with nobody listening, the switch on, the room's other satellite kept quiet and every other satellite refused.
- A satellite not updated for conversations (the PC asleep through a deploy, say) can't start one: it says "This satellite needs updating for conversations.", Claude isn't offered the tool there, and its device has no Conversation switch. Its turns would have been ordinary listens, and an older PC mutes Windows' microphone again at the end of each.
- A session the brain has let go of (its satellite dropped and connected again, or was removed, while its turn was still being answered) can't start a conversation: the executor is told it can't converse there, and refuses as at any satellite which can't, though nothing will be heard. Its turn's own start, if it was past that check as the session was let go of, is ended as the turn finishes, as nothing else would end it: OpenConversation notes which executor started each conversation, and the old session ends only one its own started, and not one a newer session at the satellite has begun as its own. Before this one could stay on with nobody listening, the switch on, the room's other satellite kept quiet and every other satellite refused.
- A request begun over a reply, by his voice, the wake word or a press, in a conversation or not, has Claude told so: "The user began speaking before your last reply had finished, so they may not have heard the end of it." Before this nothing in its history said a reply was stopped part way (the spec's "as today" was mistaken), so it could build on words he never heard. Such a note (`LLMHandler.note`) is held until his next message goes into the history, then written at its head in round brackets on a line of its own and left as written, so the cached history before it never changes; it is Claude's alone, never in the console, the timeline or anything said, and it goes when everything said before it has been forgotten. "Think harder", which asks the last exchange again, takes it out and puts it back through the handler, so its notes go with whichever is kept.
- How a big action waiting for his OK ended (approved and done, approved but it didn't work, refused, or not answered in time) is noted for Claude at the satellite which asked, in the same way, naming it as he was shown it: `The user approved "Remove 'Dentist', Tue 25 Nov 10:00" and it has been done.` The outcome is said in a gap, so it wasn't in Claude's history, and "okay, done" in a conversation could have Claude ask for the same again. Noted before it's said, so his answer goes with it. One the trust window does at once needs none, as its reply is the tool's own answer; nor does an album download's approval, which is asked by the downloads, not the satellite's tools, and is left as it was.
- In a conversation at the satellite, shutting down or restarting a PC, by his words or by Claude's tool, asks first ("Shut down the PC?", "Restart the PC?") and is done only on a yes said next; anything else leaves it, and is a fresh request. Main had them act at once because a deliberate "Hey Nova" or press came first (0037); in a conversation none need, so a remark not meant for Nova could. It is the way they asked before that change: the question's `ask_first` runs `press_pc_button`, which Claude isn't offered, and at the PC the button is pressed once the chime has played. Outside a conversation they act at once, as before. Sleep, screen off and lock don't ask, as before: each is undone by touching the PC, and they don't share the path.
- The reply that starts a conversation ("Go ahead.") isn't listened over, as the satellite hasn't been told yet.
- By default (`talk_over` "voice_at_computers") the voice cuts in only at a satellite pressed to talk, a computer's; at the Pi only the wake word does, as with "wake_word_or_press", and nothing is listened to or dipped over a reply there. Measured at the Pi on 22 November, its echo canceller took only about 3 dB off Nova's own voice, which the detector read as speech about 97% of the time and which drowned his words over it, so a reply there would dip at once, stay dipped and seldom stop; the PC plays into headphones. "voice" keeps the voice cutting in anywhere, for a satellite whose microphone can take it.
- The Conversation switch's start is judged when it is taken, not when it was asked for, with at most one pending: a second tap while the session is busy does nothing, off cancels a start waiting, and one for conversations switched off meanwhile is refused aloud.

*Music, timers and routines.*
- Navidrome's music asked for mid-conversation plays at `MUSIC_SHARE` (a third) of its volume until the end, then at full volume. Spotify's can't be turned down by Nova and its sound isn't taken out of the microphone, so music asked for on Spotify is kept paused until the conversation ends and Nova says "I'll play it when we're done."
- A timer or announcement mid-conversation rings in the next gap; a timer dismissed by voice gives up its gap. A routine due mid-conversation runs in the next gap too, keeping any question Nova had asked.

**Not done, and known.**

- **The measurements ("Gate A" in the plan) have not been run.** They make sound in his room, use his PC and load a second copy of the speech models beside the live brain, so they wait for him. Until they are, these are the plan's defaults and no more: `DIP_AFTER` 0.15 s, `CUT_IN_WAIT` 1.0 s, the detector's threshold while a reply plays, and `MUSIC_SHARE` 0.3. The branch is built on the plan's main branch of every choice (voice cut-in on, music quiet). See "Measurements" below; what each finding would change is in the plan's Gate A table.
- Anyone's voice counts as his, as in the spec: someone else talking in the room is heard as him. Three turns in a row not meant for Nova end it, and a television playing turns the voice cut-in off.
- A mute of Windows' microphone done by hand (its touch button, Windows' settings, the tray menu) never reaches the brain, so it doesn't end a conversation as Nova's own mute does.
- A phrase of his that also occurs in the reply doesn't stop it ("no" in "there is no rain"), and "right", "okay" and "yes" never stop it even as real answers. "Stop" always does.
- A timer ringing at the room's other satellite can't be dismissed by that satellite's wake word during a conversation, as it is kept quiet; a press works.
- On Spotify, music asked for at another satellite in the room, or by Home Assistant's own play button, during a conversation still plays at once.
- `install.ps1` has not run on Windows: the PC's library has been loaded there once by hand, but the install step itself is untested; that is the first of the morning's checks.

**Anti-goals** (a failure even if it works). A conversation that starts by itself or carries on with nobody there; Nova stopping for its own voice; Nova talking over him, or cutting him off while he thinks; anything changing outside a conversation; a reply stopping for the television; a big bill by accident.

**Shaped by hardware.** pve2 has no GPU, so speech-to-text, the detector and the voice all share its processor.
- The gap between him finishing and Nova starting stays what it is today, plus the longer pause he is allowed: quick turn-taking, not an instant voice chat.
- Nemotron listens during a reply only once Silero has heard a voice, since it shares the processor with the voice being made at that moment. Whether the reply stutters while it does is one of the measurements.
- The bigger model is slower than Haiku; that wait is Anthropic's, not pve2's.
- The AI machine he plans, with a GPU, would lift the first two: faster words, and room to listen all the time.

**Measurements** (to run before the hand checks, with him there; each makes sound or uses his PC, so warn first). The scripts are in the release checklist's "The manual scripts"; none of this is written down apart from the numbers.

1. **The Pi.** `measure_cut_in.py` plays about 30 s of Nova's voice, then the same with music at `MUSIC_SHARE` under it, then once with him talking over it on cue; `score_cut_in.py` scores each. Sets `DIP_AFTER`, the detector's threshold while a reply plays, and whether the own-words guard is enough.
2. **The PC.** The same three from a temporary copy of the satellite, plus how long the canceller takes to be ready from freshly opened devices (`ready.json`).
3. **The brain.** `measure_voice_load.py`: does the voice stutter, or its words come late, while Nemotron works.

| Measurement | Result | Changes |
|---|---|---|
| Pi: leftover voice as speech, with and without music | | |
| Pi: his voice over a reply (how soon a dip, how soon words) | | |
| PC: leftover voice, and how long the canceller takes to be ready | | |
| PC: his voice over a reply | | |
| Brain: the voice's real-time factor with and without Nemotron, and its longest gap | | |

**Setting it up (his named jobs, in order).** Push to GitHub main; pull as nova and restart `nova-backend`; the Pi's `update.sh`; then the PC's `scripts/deploy_pc.sh` (which unpacks the library, adding `zstandard`). Then the measurements, then the checks in the release checklist (113 onwards), warning before sound.
