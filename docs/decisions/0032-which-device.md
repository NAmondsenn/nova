# 0032: Claude is told which device the user is speaking through

19 November 2026 · Agreed with the user in chat ("nathaniels pc, bedroom pi, stuff like that") · Built

**Context.** Asked "what device are you?" at the PC, Nova said it wasn't tied to one device: Claude was told the room (0005) but not which satellite the request came through, and the brain knows each only by its name, which for the Pi is `home-assistant`, easily taken for Home Assistant itself.

**Decisions.**

1. **The device in the situation line.** Every request Claude answers starts with the line it already gets (the time, timers, the room and lights, what plays). It now also says which satellite the user is speaking through, how it listens, and the others in its room, which may hear too: "You're speaking through Nathaniel's PC (push-to-talk). Also in this room: the bedroom Pi." Something typed in Home Assistant's Ask Nova box says it was typed and where the reply is said; a routine's steps, which nobody spoke, say nothing of a device. Built by Session._here (which never fails the request: without it, only the sentence is lost) and passed through Conversation to LLMHandler._situation.
2. **Spoken names.** `backend.names` in the brain's settings gives each satellite's name the words Nova uses for it, e.g. `pc: "Nathaniel's PC"`, `home-assistant: "the bedroom Pi"`. One without is called by its name. A value which isn't words is refused as the settings are read.
3. **Nothing else changes.** What a request does, and where music plays, is as it was: only what Claude is told.
