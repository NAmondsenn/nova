# Nova's orb on the PC's screen

20 November 2026. Approved in conversation ("that plan is exactly as I'd have imagined it").

## Why

The PC is a push-to-talk satellite: Numpad 3 starts a recording, and the only sign of
what Nova is doing is sound - the press chime, then the reply. There's nothing to see
whether it's still listening, has stopped and is working on it, or is about to speak.
The smart clock beside the Pi answers that with its orb (decision 0013). He wants the
same on the PC: a small orb on the screen while Nova listens, thinks and speaks there,
and nothing at all otherwise.

## What he sees

```
 ┌──────────────────────────────────────────────┐
 │                                              │
 │          (whatever's on the screen)          │
 │                                              │
 │                     ◉   <- the orb, ~64 px   │
 ├──────────────────────────────────────────────┤
 │ Start   taskbar                        12:04 │
 └──────────────────────────────────────────────┘
```

- The clock's orb, small: the five coloured lobes (electric blue, violet, cyan, indigo,
  pink-violet) turning round a white core, with the soft aura behind - no veil, no
  card, no text. It sits on whatever's underneath with soft edges.
- Bottom centre of the main screen, just above the taskbar, about twice the size of a
  taskbar icon.
- It grows and fades in on a press, and fades out when Nova has finished.
- Each state moves as it does on the clock: listening a slow drift; thinking smaller
  and quicker; speaking quick ripples with the core pulsing.
- Clicks go straight through it, it never takes the keyboard from a game or a document,
  and it has no taskbar button and isn't in Alt-Tab.

## Goals

- Shows listening, thinking and speaking for everything Nova does on the PC: a press
  and its reply, the answer to a question Nova asked (a follow-up listen), and anything
  Nova says there unasked (a reminder or alert spoken on the PC).
- On top of normal windows and borderless (windowed full-screen) games.
- On the settings page: on or off, where on the screen, and its size.
- Costs nothing while hidden: no timer, no drawing.

## Non-goals

- The Mac (piece 4 builds its own, on the same settings).
- Showing what Nova heard, as the clock does under its orb. Easy to add later.
- Showing the Pi's conversations on the PC's screen.
- Exclusive full-screen games, which Windows lets nothing draw over.
- Pressing the orb to talk: it lets clicks through on purpose.

## Anti-goals (a failure even if it works)

- Stealing focus or a keystroke from a game or from typing.
- A press, the chime or the reply ever waiting on the orb, or failing because of it. If
  the orb can't start, that's logged once and the satellite carries on without it.
- Visible stutter, or a noticeable load on the PC.
- A flicker of the orb between listening and speaking: thinking keeps it up.

## Approaches

1. **A see-through Windows window drawn by Nova itself (chosen).** A layered, topmost,
   click-through window made through Windows' own calls (ctypes), with each frame of
   the orb drawn in numpy - which the satellite already has - and handed to Windows
   with its own transparency (UpdateLayeredWindow). Nothing new to install, true soft
   edges over any background, and a 100 px orb is a millisecond's numpy a frame.
   The cost is Win32 plumbing, which only runs on the PC.
2. **The clock's own page in a small browser window** (pywebview over Edge WebView2).
   The exact CSS orb, but two new packages, and WebView2's transparent windows and
   click-through are unreliable; the page would also need a server or a file of its own.
3. **Qt (PySide6).** Good transparency and smooth animation, but well over 100 MB to
   install for one small orb, and a second GUI toolkit in the satellite.

Approach 1 fits the satellite (no new installs, as decision 0026 kept it) and is the
only one that's reliably see-through and click-through.

## How it works

### Where the states come from

The PC satellite already knows each moment itself, so the brain doesn't change how it
works - it only passes the settings on (below). In satellite.py:

- **Listening** from the moment a press starts a recording (`_begin`) or the satellite
  starts sending the answer the brain asked to hear (a follow-up), until that recording
  ends - the brain's `stop_listening`, a second press, or the limit.
- **Thinking** from the end of the recording until Nova's words start playing, or the
  brain's turn ends (`allow`) with nothing said - then it fades out.
- **Speaking** while Nova's words play on the PC: a `play_stream` of kind `reply` or
  `announcement`. Not music, not alarms, and not the satellite's own press chime or an
  announcement's chime (both `play_sound` files) - the clock shows "speaking" only for
  the words, too.
- **Hidden** otherwise. When speaking ends, it waits a moment (about 0.6 s) before
  fading, so a reply which comes in two pieces, or a follow-up listen straight after a
  question, doesn't make it blink.

These are told to the orb as they happen ("listening", "thinking", "speaking",
"hidden"); the orb works out its own fades from them.

### The window

- A new module, `voice_assistant/screen_orb.py` (the builder may name it), that only
  does anything on Windows, like `pc_volume.py` and `windows_media.py`: everywhere else
  it's a stand-in which ignores everything, so the Pi and the tests run unchanged.
- One thread of its own owns the window and its message loop, as Windows requires; the
  satellite only ever posts it a state, which never waits.
- Extended styles: layered, transparent (click-through), topmost, tool window (no
  taskbar button, not in Alt-Tab), no-activate. Shown without activating, and put back
  on top each time it appears, as games and other topmost windows can cover it.
- Placed from the main screen's work area (the screen minus the taskbar), worked out
  afresh each time it appears, so a resolution change, a moved taskbar or a different
  main screen is picked up. Sized for the screen's scaling (per-monitor DPI), so it's
  the same size on a 4K screen at 150% as on 1080p at 100%.
- While visible: about 30 frames a second from a timer on that thread. While hidden: the
  timer is stopped.
- Shown in screenshots, recordings and screen shares like any other window - his call
  (20 Nov): no `SetWindowDisplayAffinity`.
- Shown over games too, anti-cheat ones included (Fortnite), with no list of programs
  it hides for. My call: it's an ordinary window of its own that never touches the
  game, as Windows' own volume pop-up and Discord's newer overlay are. The on/off
  setting turns it off if a game ever minds.

### Drawing the orb

- Each frame is made in numpy as premultiplied BGRA: the two auras, the five lobes and
  the core, each a soft elliptical radial gradient, combined the way the clock's CSS
  does (lobes and core in screen blend over the auras), then the whole faded and scaled
  for the grow-in and fade-out.
- The motion follows display.css: each lobe on its own orbit and wobble, with the same
  colours, relative sizes and the per-state speeds (listening, thinking, speaking). It
  should read as the same orb as the clock's, not a pixel copy.
- The drawing is plain numpy with no Windows in it, so it's tested anywhere: frame
  size, alpha only inside the orb, premultiplication, each state differing, time
  moving it.

### Settings (settings page, section Satellites, beside the search site)

| Setting | Choices | Default |
|---|---|---|
| `computer.screen_orb` | on / off | on |
| `computer.screen_orb_place` | bottom centre, bottom left, bottom right, top centre, top left, top right | bottom centre |
| `computer.screen_orb_size` | small, medium, large (about 48, 64, 96 px at 100% scaling) | medium |

The size is the bright body - the lobes and core - which is what reads as "the orb"; the
aura's glow fades out around it to about 1.6 times that width, as on the clock, so the
window is that much bigger and its glow, not its body, sits just above the taskbar. My
call, matching "about twice the size of a taskbar icon".

The brain sends them to push-to-talk satellites with the state it already sends (as
`longest_request` is), and a change on the page reaches the PC at once. An old brain
which doesn't send them leaves the defaults; an old satellite ignores them.

## Testing

- Unit tests on the Pi, as ever: the drawing; the state sequence from satellite events
  (press, stop_listening, reply stream starting and ending, allow with nothing said,
  follow-up listen, announcement, music and chimes not counting); the settings'
  validation and reaching the state message; the stand-in off Windows.
- On the PC, a check by me over ssh can't show a window on his desktop (ssh runs in
  another session), so the look is his check: a press, a question with a follow-up,
  a reply, a reminder; over a borderless game; on a moved taskbar.
- Settings page tests for the three new settings, and its preview to see them drawn.

## Left to the builder

- The module's and functions' names, the exact frame rate (25-60), and the fade timing.
- How the lobes' motion is turned from CSS keyframes into functions of time.
- Exactly how long speaking lingers before the fade (around 0.6 s).
