# 0029: The other satellites stop listening while Nova speaks on one

19 November 2026 · Agreed with the user in chat ("yes", "make it so in general all other satellites stop listening but the one playing") · Built

**Context.** On 18 November at 15:08 the PC said "I'm Nova, a voice assistant running locally in your home...", and the Pi, in the same room, heard "Nova" from the PC's speakers: its wake word fired at 0.93 and its voice check let Nova's voice through at 0.45. It sent the rest of the reply to the brain as a request. Each satellite cancels only its own echo, and the wake word's stricter mode while something plays in the room (0010) leaves out Nova's own playback on purpose.

While looking into it, a second way for the PC to hear itself was found: a press while the brain is still working on the last request leaves the PC's microphone open as the reply starts, and the PC has no echo cancelling (0026).

**Decisions.**

1. **Quiet while another speaks.** While Nova speaks on one satellite, every other connected satellite, in any room, ignores the wake word; the one speaking keeps listening, to be talked over as ever. Speech is anything streamed to a satellite that isn't music - replies, and announcements said aloud - not its own sound files (chimes, rings). Music never quiets anything, so "Hey Nova, pause" works over it.
2. **How.** Each satellite's speaker says when it starts and stops speaking (RemoteSpeaker.on_speech). That only wakes one thread on the brain, which looks at who is speaking now and works out who to tell (quiet_others.py), so two changes heard in the wrong order, or a satellite connecting again under the same name, can't leave one deaf or listening: it sends `quiet {"on": ...}` on a change, to a new connection, and again every 20 seconds while a satellite is to stay quiet. A satellite stays quiet for at most 60 seconds without being told again, comes back 0.5 seconds after it's told the other has stopped, for the last words leaving that satellite's speakers, and at once if it loses the brain. It isn't the microphone mute, and Home Assistant's switch doesn't show it.
3. **A press still works.** It's deliberate, and Nova's voice can't make one.
4. **No reply into a press's open microphone.** On a push-to-talk satellite, a reply which starts while a recording a press started is open is stopped at once, as a press over it would have been, and the brain is told so. The brain's own listen, for the answer to a question, keeps its chime. The Pi is unchanged. Presses are logged.

**What it costs.** While the PC speaks, "Hey Nova" to the Pi does nothing; Numpad 3 on the PC still interrupts it. After an accidental press with nothing said, the answer to the request before it is dropped and has to be asked again.
