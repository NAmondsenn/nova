# The PC knows when you stop speaking

20 November 2026. His choice in conversation: "make the fix" (option 1: build it now, rather than catch one live first).

## Why

PC Nova sometimes takes far too long to answer. The brain ends a recording when Silero, its speech detector, hears 0.6 s of quiet. Since the PC went live, 8 of 57 PC requests kept "speech" going for 6 to 14 s after short sentences ("Play some music" ran to the 15 s cap); none of 94 Pi requests did. Ruled out: Nova's own music (paused on the PC at once), Windows media (Home Assistant shows nothing playing then), and the microphone itself (a fifine USB mic; in a quiet room Silero scores 0 on every frame). So it is something audible around the PC at those times - a game, a video, Discord, voices - which Silero takes for someone talking. He wants the best fix: "the PC should know when I stop speaking like the Pi".

## What changes for him

At the PC, Nova stops listening about as soon as he stops talking, whatever else is making sound. Nothing changes at the Pi.

## Goals

- On a push-to-talk satellite (the PC), a recording also ends when:
  1. **His voice has dropped away**: the sound reaching the mic stays well below how loud he was while speaking, for the usual 0.6 s. He is close to the mic; a game, a video or a voice across the room is far quieter there. This is how push-to-talk headsets tell someone has stopped.
  2. **No new words**: speech-to-text, which follows the recording as it's made, has found no new word in the last 1.5 s of audio (a backstop for loud sound that isn't speech).
- Either one is enough; Silero's own rule still ends it as before.
- For a week from deploy, any PC recording whose speech runs on for more than 5 s is noted on the brain - numbers only, no audio (his call, 20 Nov: the brain is in the weekly backup, kept up to 3 months) - so we can see it's fixed, or what's left.
- The two new limits are on the settings page, under Sound and voice, Listening.

## Non-goals

- The Pi: it hears the room from across it (far-field), where "much quieter than his voice" doesn't hold. It keeps today's rule.
- Cancelling the PC's own sound out of the mic (echo cancelling on Windows).
- Telling his voice from another person's.

## Anti-goals

- Cutting him off mid-sentence: a short pause or a quiet word must not end a request. Both new rules wait at least as long as today's (0.6 s of quiet; 1.5 s without a word), and the voice-drop rule compares against his own loudness in this recording, not a fixed level.
- Changing anything at the Pi.
- Keeping any audio or words for this: the notes are numbers only.

## Approaches

1. **Voice drop plus "no new words" on the brain (chosen).** The brain already has every frame and the live transcript; nothing changes on the PC, and the rules only switch on for a push-to-talk satellite.
2. Catch one live first, then fix exactly that. Surer of the cause, but waits on him being at the PC when it happens. He chose not to wait; the kept recordings answer the same question afterwards.
3. Raise Silero's threshold for the PC. Blunt: a louder threshold misses his quieter words, and doesn't help when the background really sounds like speech.

## How it works

### Voice drop (recording.py)

`record_until_silence` gains an optional `voice_drop` (a ratio, None meaning off). Once speech has started it keeps his loudness so far: the highest level of any 0.15 s stretch Silero calls speech (so the PC's own press chime, which its mic hears, doesn't set it), a short window so a single loud click doesn't set it and the dips between syllables don't count. From then on a frame also counts towards the 0.6 s of quiet when its own 0.15 s level is below that loudness times `voice_drop`. A frame Silero calls quiet still counts as before. The default is 18 dB below (a ratio of about 0.126). A background close to his own loudness won't be caught this way - that's what the second rule is for.

**Both new rules wait for his first word** (my call, from the builder check): neither can end a recording until Nemotron has heard a real word in it (not one it imagines in noise - see speech_to_text's HALLUCINATIONS). In the noise this fix is for, Silero can call the background speech before he says anything, and Nemotron's words come 0.6-1.2 s behind the audio; without the wait, a follow-up answer he's still thinking about, or a slow start, would be cut off. The cost: a lone word Nemotron hears as nothing, followed by noise, still runs to the limit as today.

### No new words (speech_to_text.py, nemotron.py, recording.py)

Nemotron already hears the recording as it's made. It now also reads its words-so-far after each piece, and keeps how much audio it had been given when they last changed. `NemotronTranscription` offers `quiet_words()`: seconds of the recording heard since the words last changed (audio time, not clock time, so a Nemotron running behind doesn't count as quiet). `record_until_silence` gains optional `words_quiet` (that callable) and `no_words_after` (1.5 s by default): once a real word has been heard, a recording whose words have been still for that much audio ends. Words imagined in noise only keep it going, never end it early.

### Only at the PC (session.py)

The session passes both only when its satellite is push-to-talk (`self.satellite.push_to_talk`), for a press and for a follow-up answer at the PC alike. The Pi's recordings are unchanged.

### Noting the long ones for a week (a small new module)

When a push-to-talk recording's speech (speech_started to speech_ended) lasts over 5 s, the brain appends one JSON line to `~/.local/share/nova/logs/long-recordings.jsonl` (the only place the service may write that survives a restart): when, what ended it ("silence", "voice drop", "no new words", "limit", "pressed"), his loudness, each 0.15 s level and Silero's probability for each, and when the words last changed - numbers only, never audio or words. At most 50 lines; lines older than 7 days are dropped at each write; it switches itself off 7 days after the deploy date written in the code (a constant). Each request's timeline line notes `ended_by` too, so latency.jsonl shows how often each rule fires.

### Settings (Sound and voice, Listening)

| Setting | Meaning | Default | Limits |
|---|---|---|---|
| `thresholds.press_voice_drop_db` | How far below his own loudness counts as stopped, at a computer | 18 | 6 to 40 |
| `thresholds.press_no_words_seconds` | Seconds without a new word that end it, at a computer | 1.5 | 0.8 to 5 |

Read as each recording starts, so a save is used straight away.

## Testing

- recording.py: neither rule ends a recording before the first real word (background called speech from the press, then he speaks late); speech then a steady quieter background (above Silero's line in the fake detector) ends after 0.6 s with `voice_drop` on, runs to the limit with it off; a quiet word mid-sentence (short dip) doesn't end it; a pause under 0.6 s doesn't; loudness that builds slowly isn't cut off by an early quiet start; words still for 1.5 s end it, words still changing keep it going, a Nemotron behind real time doesn't end it early.
- speech_to_text.py: `quiet_words()` from a fake Nemotron whose partial text changes at given points.
- session.py: a push-to-talk satellite's recording gets both rules with the settings' values; a wake-word satellite's gets neither.
- The notes: only push-to-talk recordings over 5 s, numbers only (no audio, no words), at most 50 lines, none over 7 days, none after its end date.
- Settings: config limits, the page's rows.
- Live: after deploy, his normal use; the kept recordings show whether any still run long and which rule ended them.

## Left to the builder

- Names, the exact smoothing (a 0.15 s window is the intent), and where `ended_by` is noted.
- How the partial words are read from sherpa-onnx (`get_result` on the stream between pieces).
