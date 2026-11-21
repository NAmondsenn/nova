# 0036: The PC knows when you stop speaking

20 November 2026 · His choice in conversation: "make the fix", which is to build it now rather than catch a slow one live first · Design: [the spec](../superpowers/specs/2026-11-20-pc-knows-when-you-stop-design.md) · Built; his named deploy and hand checks (71 to 75) to come

**Context.** The PC (a push-to-talk satellite, 0026) sometimes took far too long to answer. The brain ends a recording when Silero, its speech detector, hears 0.6 s of quiet, and at the PC that quiet sometimes never came. He wanted the best fix: "the PC should know when I stop speaking like the Pi".

**The evidence.** Since the PC went live, 8 of 57 PC requests ran 6 to 14 s long after short sentences, 3 of them to the 15 s cap ("Play some music" was one); none of 94 requests at the Pi did. Ruled out:

- *The microphone itself:* a fifine USB mic, which in a quiet room scores Silero 0 on every frame.
- *Nova's own music:* it was paused on the PC at once.
- *Windows media:* Home Assistant showed nothing playing at those times.

So it is something audible around the PC then - a game, a video, Discord, voices - which Silero takes for someone talking. The PC's mic is close to him, so that sound is far quieter at it than his voice is; at the Pi, across the room, it isn't, which is why the Pi never did this.

**What he decided.**

1. **Build the fix now** (approach 1 of the spec), rather than wait to catch one live (approach 2). The notes below answer the same question afterwards. Raising Silero's threshold for the PC (approach 3) was set aside as blunt: it misses his quieter words and does nothing when the background really sounds like speech.
2. **Two new ways to end a recording at the PC, either one enough**, with Silero's own rule still ending it as before:
   - *His voice has dropped away:* the sound reaching the mic stays well below how loud he was, for the usual 0.6 s. This is how push-to-talk headsets tell someone has stopped.
   - *No new words:* speech-to-text, which follows the recording as it is made, has found no new word for 1.5 s of audio. A backstop for loud sound which isn't speech.
3. **Notes of the long ones, never with audio.** The brain is in the weekly backup, which is kept up to 3 months, so no recording is kept for this. Any PC recording whose speech runs over 5 s is noted - numbers, and the words only while "Keep what's said in the logs" (`logging.log_transcripts`) is on, the same switch as Nova's other logs. The newest 50 are kept, with no end date.

**What was built.**

- **Voice drop** (`voice_assistant/recording.py`). `record_until_silence` takes an optional `voice_drop`, a ratio (None is off). Once he is speaking it keeps his loudness: the highest level of any 0.15 s stretch whose newest frame Silero calls speech. A short stretch means one click doesn't set it and the dips between syllables don't count. From then on a frame also counts towards the 0.6 s of quiet when its own 0.15 s level is below that loudness times `voice_drop`; a frame Silero calls quiet counts as before. The default is 18 dB below, a ratio of about 0.126, from `thresholds.press_voice_drop_db`. The comparison is against his own loudness in this recording, not a fixed level.
- **No new words** (`speech_to_text.py`, `nemotron.py`, `recording.py`). Nemotron already hears the recording as it is made. It now also reads its words-so-far after each piece and keeps how much audio it had been given when they last changed. `NemotronTranscription.quiet_words()` gives the seconds of recording heard since then - audio time, not clock time, so a Nemotron running behind doesn't count as quiet. `record_until_silence` takes `words_quiet` (that callable) and `no_words_after` (1.5 s by default, from `thresholds.press_no_words_seconds`). It returns None until a real word has been heard, and goes back to None if Nemotron later can't say its words, so a failure never ends a recording. Words imagined in noise (`speech_to_text.HALLUCINATIONS`) only keep a recording going, never end it early.
- **Only at the PC** (`session.py`). Both are passed only when the satellite is push-to-talk (`self.satellite.push_to_talk`), for a press and for a follow-up answer at the PC alike. The Pi's recordings are unchanged: it is told neither.
- **The notes** (`voice_assistant/long_recordings.py`). One JSON line per push-to-talk recording whose speech (speech_started to speech_ended) lasts over 5 s, in `long-recordings.jsonl` in the brain's logs folder (`/home/nova/.local/share/nova/logs`, the one place the service can write that survives a restart). A line has when it was noted, the seconds of speech, what ended the recording (`ended_by`), his loudness, each 0.15 s level and Silero's probability for each, how long the words had been still when it ended, and the words heard (only while `log_transcripts` is on). It is written once the words are known, so it is there even when none were heard, which is the case of loud sound which wasn't speech. The file is rewritten to keep the newest 50 lines. It is best-effort: a failure is logged and never stops a request being answered. Each request's line in `latency.jsonl` carries `ended_by` too, so it shows how often each way fires.
- **`ended_by`** is one of "silence", "voice drop", "no new words", "limit", "feed ended", "no speech" from the recorder, and the session says "pressed" in place of "feed ended" when the satellite ended it on a press of its key.
- **Settings**, under Sound and voice, Listening on the settings page, read as each recording starts, so a save is used straight away:

  | Setting | Meaning | Default | Limits |
  |---|---|---|---|
  | `thresholds.press_voice_drop_db` | How far below his own loudness counts as stopped, at a computer | 18 | 6 to 40 |
  | `thresholds.press_no_words_seconds` | Seconds without a new word that end it, at a computer | 1.5 | 0.8 to 5 |

  The limits are where an ordinary dip between words, or a pause, would start ending a recording (under 6 dB, under 0.8 s), and where the rule would hardly ever fire (over 40 dB, over 5 s).

**My calls (his to overturn).**

- **Both rules wait for his first real word.** Neither can end a recording until Nemotron has heard a real word in it. In the noise this fix is for, Silero can call the background speech from the moment of the press, and Nemotron's words come 0.6 to 1.2 s behind the audio; without the wait, a follow-up answer he is still thinking about, or a slow start, would be cut off. The cost: a lone word Nemotron hears as nothing, followed by noise, still runs to the limit as it does today. A listener which can't say its words (a stand-in in a test) leaves "no new words" off and the voice drop working from the start of speech.
- **His loudness comes only from what Silero calls speech.** The PC's own press chime reaches its mic, is loud, and isn't speech, so a window counts only if it is made wholly of frames from the run of speech that started the recording, and only when its newest frame is called speech. Without that the chime would set his loudness far too high and every real word would look like a drop.
- **A recording the PC's key ended is noted "pressed".** The recorder can only tell its feed ended; the session knows a press did it, and that is worth saying.

**Not done.**

- **The Pi.** It hears the room from across it (far field), where "much quieter than his voice" doesn't hold, and it never showed the problem. It keeps today's rule.
- **Echo cancelling on Windows**, to take the PC's own sound out of the mic.
- **Telling his voice from another person's.**

**Known gaps.**

- **The 18 dB default is unproven against his real background.** It has been tested only with made-up levels, never against a recording of the PC with a game going. The notes are there to tune it: if long recordings remain, the lines show his loudness and each level, and so what the setting should be. The cost if it is wrong: some long recordings remain until the setting is changed, or he is cut off in a quiet sentence (check 72).
- **A background close to his own loudness which also sounds like speech** (a person beside him) isn't caught by the voice drop. "No new words" is the backstop for it, once a word has been heard and only if the background isn't being read as words.
- **A note is not written** when speech-to-text itself fails (it raises), when a typed answer replaces the recording, or when the satellite drops mid-request. A speech-to-text error which comes back as an error looks, in the note, like nothing heard.
- **The Pi is unchanged,** so its long recordings, if it ever has any, are neither ended earlier nor noted.

**Anti-goals** (a failure even if it works). Cutting him off mid-sentence: a quiet word or a pause under 0.6 s must not end a request, which is why the voice drop compares against his own loudness in this recording and both rules wait at least as long as today's. Changing anything at the Pi. Keeping any audio for this, or the words while `log_transcripts` is off.

**Setting it up (his named job).** The brain only: push to GitHub main, pull as `nova`, restart `nova-backend`. The PC and the Pi need nothing. Then the checks in the release checklist (71 to 75), warning before sound, and read `long-recordings.jsonl` after a few days of normal use.
