# Nova on the PC as a proper app

20 November 2026. Shaped in conversation; each of his calls is marked as his, and
everything else is mine and his to overturn.

## Why

What Nova does on the PC is basically finished (his words): push-to-talk (0026), Windows'
media controls (0031), opening things and PC commands (0034), the microphone's mute, and
the orb on the screen (0035). What's left is polish. Today Nova on the PC is a windowless
`pythonw` started by a logon task: nothing in the system tray, no way to quit or restart
it by hand, the volume mixer and Task Manager say "Python" with Python's icon, the
Start-menu "Nova" does nothing while it runs, and the key that makes it listen lives in
his own AutoHotkey script rather than in Nova.

He wants it to look and behave like an app of its own. It is one of the last pieces for
the PC: fingerprint approval comes after and will live in this app, and then the Mac
version is made to the same shape.

## What he sees

```
 tray icon (the orb, small)        hover: "Nova"
 ┌──────────────────────────────┐
 │ Listen             Ctrl+Alt+N │
 │ Play / Pause                  │
 │ Next                          │
 │ Mute microphone             ✓ │
 ├──────────────────────────────┤
 │ Settings                      │
 │ Start with Windows          ✓ │
 │ Restart Nova                  │
 │ Quit                          │
 └──────────────────────────────┘
```

He approved this menu as drawn ("yes that looks good").

- **One icon everywhere, named "Nova":** the system tray, the Start menu, the volume
  mixer, Windows' media pop-up, Task Manager, and Windows' "is using your microphone"
  notice.
- **The icon is the orb as it is on the screen** (his pick, option A of four shown): the
  five lobes and the glow, the same thing as the moving orb, still and small.
- **Three states of the tray icon:** ready; microphone muted (a red slash across it);
  can't reach the brain (grey). The hover text says the same in words: "Nova", "Nova:
  microphone muted", "Nova: not connected".
- **A click on the tray icon, left or right, opens the menu** (his: "tapping the system
  tray icon would give options").
- **A keyboard shortcut of Nova's own** makes it listen, Ctrl+Alt+N until he changes it on
  the settings page (his ask: "a default one which can be changed"). Numpad 3 goes on
  working exactly as now.
- **No window.** The tray icon, the menu and the orb are the whole app.

## Goals

- Everything in "What he sees".
- The orb (0035) is part of this app and ships with it: this work is built on the orb's
  branch.
- The menu, the shortcut and the icon's states are defined once, in plain Python with no
  Windows in it, so the Mac version (piece 4) only adds its own drawing of them.
- Nothing new to install on the PC, as 0026 kept it, unless the first task below shows
  there's no other way - and then he is asked first.
- Updates stay one `scripts/deploy_pc.sh` from CT 109.

## Non-goals

- The Mac's menu-bar app (piece 4), and fingerprint approval (piece 3). This only leaves
  room for them: a named process with a window handle of its own is what a Windows Hello
  prompt needs to say "Nova".
- A settings window. Every setting stays on the one settings page.
- An installer, or an entry in Windows' installed apps list. That is the bundled
  approach below, which can be added on top later.
- Notifications (toasts) from Nova.
- A main window, a taskbar button, or anything in Alt-Tab.
- Changing what the orb looks like or does.
- Removing Numpad 3 from `flow100.ahk`.

## Anti-goals (a failure even if it works)

- A press, the chime, a reply or music ever waiting on the tray icon, the menu or the
  shortcut, or failing because of them. If the tray or the shortcut can't be set up,
  that's logged once and Nova carries on as it does today, Numpad 3 included.
- Stealing focus or a keystroke from a game or from typing. The menu takes focus only
  while it is open, as every tray menu does.
- A console window flashing up at logon, on a restart or on a deploy.
- Quit leaving something behind: after Quit there is no Nova process, no tray icon, the
  microphone is released, and nothing starts it again until the next logon or until he
  opens Nova from the Start menu.
- Two Novas running at once, however it's started.
- A deploy from CT 109 that can no longer find and stop the running app.
- The tray icon staying on "ready" when the microphone is muted or the brain is gone.

## Approaches

1. **Grow the satellite that's already there (chosen; he left the how to me and didn't
   object).** The tray icon, menu, shortcut and names are added to the satellite itself
   through Windows' own calls (ctypes), the way the orb's window is, with a small
   `Nova.exe` which runs it so Windows names the process Nova. Nothing new to install,
   deploys unchanged, and the shared parts are plain Python the Mac reuses. The cost is
   more Windows-only code, and `Nova.exe` has to be proven on his PC.
2. **Ready-made tray and hotkey libraries** (pystray, pynput or keyboard). Less code for
   two pieces, but new packages (pystray needs Pillow), less say over the menu, a fight
   for the main thread on the Mac, and none of them names the process, the mixer or the
   media pop-up, so most of approach 1 remains.
3. **Bundle Nova as an installed program** (PyInstaller and an installer). It would be
   in the installed apps list, but every update would have to be built on Windows, deploys
   get heavier, unsigned bundles trip antivirus, and he'd see little of it day to day.
   Worth it only if Nova is ever given to someone else.

Dropped: a separate native app (C# or Swift) wrapped round the satellite - two codebases
on each system.

## How it works

### First task: prove the names on his PC

Before anything else is built, a throwaway test on the PC settles what can't be known
from here, as 0031's test did. It needs his OK as a named job, since it runs a test
program in his session, and anything that shows on his screen is his check (ssh runs in
another session and can't see his desktop, as the orb's design found).

To prove, in this order of preference:

1. **A `Nova.exe` that is the satellite's own process.** The route to try first: a tiny
   program compiled on the PC at install by Windows' own C# compiler (install.ps1 already
   compiles C# with `Add-Type` for the Start-menu shortcut, so nothing is installed for
   it), with Nova's icon and the description "Nova", which loads Python's DLL from the
   venv's base install and runs `satellite.py` inside itself with the venv's packages.
   Wanted: Task Manager, the volume mixer and Windows' microphone notice all say "Nova"
   with the icon, the satellite behaves exactly as under `pythonw` (sound, the key,
   media controls, the orb), and it still ends cleanly.
2. **If that can't be made to work:** keep `pythonw` as the process, and name what can be
   named from inside it - the audio session's display name and icon (Core Audio's
   `IAudioSessionControl`, through `comtypes`, which pc_volume.py already uses), the tray
   icon, the Start-menu shortcut's icon and the media pop-up. Task Manager and the
   microphone notice would then still say Python.

If only 2 works, stop and tell him exactly what would still say Python, before building
on. The same test also shows a tray icon and a registered shortcut working from the
windowless process the logon task starts, and gives him his first look at the tray icon
at real size on his taskbar.

### One process tree, every part of it Nova

Today the logon task runs the venv's `pythonw launcher.pyw`, which starts
`pythonw satellite.py` and starts it again whenever it exits (the launcher's docstring
says why Task Scheduler's own restart isn't used). That shape stays: a launcher which
restarts the satellite, one launcher at a time by its mutex. What changes is that both
are started through `Nova.exe` when the first task proves it, so nothing of Nova's in
Task Manager says Python.

- The logon task, the Start-menu shortcut and `deploy_pc.sh` all point at the new
  program. `deploy_pc.sh` finds the running app by the new process too, and still by the
  old `pythonw` command lines so the first deploy over an old install stops it.
- The Start-menu shortcut keeps its AppUserModelID `Nova.Satellite` (0031) and gains the
  icon, which is also what puts the icon in the media pop-up.
- `install.ps1` stays safe to run again, and still never writes `.env`.
- `install.ps1` mirrors the code into `app\` and deletes whatever isn't in the source,
  the venv apart. So `Nova.exe` lives in the venv (`venv\Scripts`, where it also finds
  `pyvenv.cfg`) or is left out of the mirror, and the deploy stops it before copying.
- The launcher keeps its mutex's name (`Local\NovaSatelliteLauncher`), so a new launcher
  and an old `pythonw` one still shut each other out.
- **The tray lives in the satellite, not the launcher (my call).** The satellite stays
  up and keeps trying when the brain is away, so the grey icon covers that. Only when the
  satellite can't start at all (no password yet, a broken install) is there no icon,
  as there is none today, with the reason in the launcher's log; and the icon is gone for
  about two seconds over a Restart. A launcher with an icon of its own would need the
  satellite's state passed between two processes for a case that rare.

### The tray icon and its menu

- A new module that only does anything on Windows, like `screen_orb.py`, `pc_volume.py`
  and `windows_media.py`: elsewhere it's a stand-in, so the Pi and the tests are
  unchanged. A push-to-talk satellite on Windows makes it; the Pi never does.
- A thread of its own owns a hidden window and waits on Windows' messages properly, so a
  click or the shortcut is acted on at once. The satellite only ever tells it things
  ("muted", "connected", "playing"), which never waits. The orb keeps its own thread;
  the tray works whether the orb is on or off.
- The icon is put back when Explorer restarts (Windows' `TaskbarCreated` message), and
  the right size is used for the screen's scaling.
- Windows tucks a new tray icon behind its arrow until he drags it out once. An app
  can't reliably force that, so it's a line in the release checklist for him.

What each item does:

| Item | Does | Greyed when |
|---|---|---|
| Listen | The same as a press of the key: starts a recording; pressed while recording, sends what was said; over a reply, stops it and listens. Shows the shortcut beside it, or nothing if there's none or it's taken. | never |
| Play / Pause | The same as the media key (0031): `media` play or pause to the brain, for this computer's own music. Reads "Pause" while music plays here and "Play" otherwise, from the `now_playing` the brain already sends a satellite with media controls; without those it reads "Play / Pause". | not connected |
| Next | `media` next. | not connected |
| Mute microphone | Mutes or unmutes the PC's own microphone in Windows, exactly as "mute the mic" said at the PC does (the mute-means-the-microphone design, part "The PC's own microphone"): the same sound, the same time limit (`computer.mic_mute_minutes`), and a press still opens it for one turn. Ticked whenever Windows' microphone is muted, by Nova or by hand. Works with the brain away. | Windows' volume can't be reached (`system_volume` is None) |
| Settings | Opens the settings page in his browser at its Satellites section. | no address is known |
| Start with Windows | Switches whether Nova starts at his logon. Ticked while it does. | never |
| Restart Nova | Ends the satellite cleanly; the launcher starts it again in about two seconds. | never |
| Quit | Ends the satellite and the launcher. | never |

- **The mute from the menu is done on the PC itself**, so it works with the brain away:
  the satellite mutes Windows' microphone, starts the time limit and plays `mic_off.wav`
  or `mic_on.wav` itself (spoken, it's the brain which has them played).
- **The tick and the muted icon mean Windows' microphone**, muted by Nova or by hand
  (his mic's touch button, Windows). The brain's own mute (`state.muted`) plays no part
  on a push-to-talk satellite. Today the satellite only looks every 30 seconds, and only
  while a mute of Nova's runs; the icon needs it looked at always, often enough that a
  mute by hand shows within a few seconds, and afresh as the menu opens.
- **The mute's minutes** have to be known without asking the brain, so the brain sends
  `computer.mic_mute_minutes` with the state it already sends (as the orb's settings
  are), and the satellite uses the last it was told, or its default.
- **The settings page's address** is the one the page already holds ("This page's
  address", `wrapped.link`). The brain sends it with the state; the menu opens that
  address with `#satellites`, which the page already opens at its Satellites section.
  It is set on the live brain today (checked 20 Nov). There is no second address setting. If the builder moves
  that setting to a more general place on the page, it stays one setting.
- **Opening Nova from the Start menu:** while Nova runs, it opens the settings page, as
  the menu's Settings does (the second launcher, instead of just exiting, asks the
  running one to). When Nova isn't running, after a Quit, it starts it.
- **Quit, and Windows ending it** (logoff, shutdown, a deploy), must not be counted by
  the next start as a crash: Quit and Restart end the satellite through its own clean-up
  (the launcher already clears the mark a logoff or a deploy leaves).
- **Start with Windows is Nova's own note, not the task's on/off (my call).** The
  logon task is registered from an administrator's ssh session, which his own session
  may not be allowed to change, and `install.ps1` registers it afresh on every deploy,
  which would switch it back on. So the choice is kept in Nova's data folder, a start at
  logon reads it and stops at once when it's off, install never touches it, and the
  task stays there for a deploy to use. The first task on the PC checks that a start at
  logon can be told from a deploy's start and from the Start menu's.
- **A deploy starts Nova again only if it was running when the deploy began, or on a
  first install (my call).** If he had quit it, it stays quit, with the new code ready
  for the next start. With Start with Windows off and Nova running, a deploy still
  starts it again.

### The icon

- Drawn from the orb's own drawing (`orb_drawing.py`) at one fixed, good-looking moment,
  by a script kept in the repo, so it can be made again and the Mac's formats can come
  from the same script. The icon files it makes are committed; nothing draws them on the
  PC at run time.
- Sizes 16, 20, 24, 32, 48 and 256. Below 32 the same orb is drawn tighter and stronger
  (less glow, more body) so it doesn't fade into a smudge, and it must read on a light
  taskbar as well as a dark one - the weakness option A showed when tiny, which he was
  told about when he picked it.
- Three versions for the tray: ready, muted (a red slash), not connected (grey). The
  Start menu, mixer and `Nova.exe` use the ready one.
- He sees the finished icons, at tray size on a dark and a light taskbar, before they go
  live (the preview-before-saving rule).

### The shortcut

- The app registers it with Windows itself (`RegisterHotKey`) on the tray's thread. A
  press of it is the same press as Numpad 3's: it calls what the press listener calls.
- **Setting:** one per computer, on the settings page under that computer in Satellites
  (where its "Also called" names are, and kept where those are, in `SatelliteRooms`),
  written like `ctrl+alt+n`. Saving a per-computer setting doesn't send the state again
  today, so that is added. Default `ctrl+alt+n`.
  Empty means no shortcut. It can be typed, so it can be set from a phone; a field that
  also takes the keys as they're pressed on a keyboard is welcome but not required.
- **What's allowed:** at least one of Ctrl, Alt, Shift or Win with one other key, or a
  function key (F1 to F24) alone. Anything else is refused on the page with a plain
  reason. Numpad keys alone aren't offered: with Num Lock off they are Page Down and the
  like, which is why `flow100.ahk` keeps Numpad 3.
- The brain sends it to that satellite with its state, and a change on the page reaches
  the PC at once: the old one is let go and the new one registered.
- **Taken by another program:** Windows refuses it. The satellite tells the brain, the
  page says beside the setting that the shortcut is in use by something else on that
  computer, and the menu's Listen shows no shortcut. Nova goes on working by Numpad 3
  and the menu.
- An old brain which sends no shortcut leaves the default; an old satellite ignores it.
- Known limit: a few full-screen games read the keyboard in a way that hides it from
  registered shortcuts. Numpad 3 through AutoHotkey still works there.

### The shared part, for the Mac

Plain Python, tested anywhere: the menu as a list of items (label, ticked, greyed, what
it does) worked out from the satellite's state; the icon's state (ready, muted, not
connected); reading and checking a shortcut's text. The Windows module draws the menu
and registers the shortcut from that. Names in settings and messages say "computer" and
"shortcut", never "PC" or "Windows", as the orb's settings do. On a Mac "alt" will mean
Option; that mapping is piece 4's.

### Settings page

| Setting | Where | Choices | Default |
|---|---|---|---|
| The shortcut that makes Nova listen | Satellites, per computer | text like `ctrl+alt+n`, or empty | `ctrl+alt+n` |

Start with Windows is in the tray menu rather than on the page: it belongs to that
computer's logon, and has to work with the brain away. The orb's three settings and the
microphone's minutes are already on the page.

## Testing

- Unit tests on the Pi, as ever, with fake Windows parts: the menu worked out from each
  state (connected or not, muted or not, playing or not, shortcut taken or none); what
  each item calls; the icon's state; shortcut text read, refused and written back; the
  setting's validation and its reaching the state message; "taken" reaching the page;
  the launcher's second start asking the first to open the page; Quit and Restart ending
  cleanly and not counted as a crash; the stand-in off Windows.
- Settings page tests for the new setting, and its preview to see it drawn.
- On the PC, by script over ssh where a script can tell: the process's name, the audio
  session's name, the task switched on and off, the shortcut registered or refused.
- His checks, added to the release checklist: the icon in the tray, the menu and each
  item, the three icon states, the shortcut from the desktop and from inside a game, the
  mixer, Task Manager, the media pop-up, the Start menu while running and after Quit,
  logging off and on, and a deploy over the running app. Any check that plays a sound in
  his room is announced to him first.

## Order of work

1. The orb's branch (`screen-indicator`) finishes its review. This branch, `pc-app`, was
   cut from it and takes its later commits.
2. The first task on the PC (his named job), and his look at the result.
3. The build, subagent-driven with Opus reviewers.
4. His named jobs to go live: push main, restart the brain (it sends the new settings),
   `deploy_pc.sh`.

## Left to the builder

- Module, class and function names, and the next free decision number (0036 as of today).
- How `Nova.exe` is made and how it finds Python, within what the first task proves.
- Whether the tray's thread and the orb's stay two or become one, so long as the tray
  answers at once and the orb's own rules (0035) hold.
- How a start at logon is told from any other start (a second task with no trigger, for
  the deploy, is one way).
- How the second launcher asks the running app to open the page (the press listener's
  local port is one way), and how "shortcut taken" travels to the brain.
- The exact moment of the orb the icon is drawn from, and how much tighter the small
  sizes are - shown to him before going live.
- The widget for the shortcut on the page.
- The Windows task's name, and whether the launcher stays a `.pyw`.
