# Nova view in Home Assistant

18 November 2026. Agreed in chat with Nathaniel.

## Why

Nova's settings page holds everything, but it is a Tailscale page with its own login and looks nothing like his Home Assistant dashboards. Every device he has opens Home Assistant easily. He wants a Nova page there that matches his other views and works as a quick controller: the things he would check or press most often, not the whole settings page.

Success: from the phone or the desktop he opens Nova in the bottom bar and can see whether Nova is up and what it is doing, mute it, stop it, change its volumes, see its timers and next alarm, control what it is playing, and type a request to it as if he had said it.

## Goals

1. A Nova view on the desktop dashboard (`dashboard-home`) and the phone dashboard (`dashboard-mobile`), in the `glass` theme, laid out like his other views.
2. On it:
   - **Status:** online (the existing Backend binary sensor and the device's availability), what Nova is doing (Status sensor: idle, listening, thinking, speaking), and the Microphone muted switch.
   - **Controls:** Stop, and Speak (a text box; the Pi says what is typed, through the existing Speak notify entity).
   - **Volumes:** the three existing volume numbers as sliders.
   - **Timers and next alarm:** the existing Timers and Next alarm sensors.
   - **Music:** what Nova is playing on the satellite: title, artist, cover, play/pause, next, previous, and a playlist picker.
   - **Ask Nova:** a text box. Whatever is typed goes to Nova as a request, exactly as if it had been said to that satellite after "Hey Nova": "play Fashion Killa", "get the new Kendrick album", "set a timer for 10 minutes". The reply is spoken on the satellite.
3. Nova sends the new music and Ask Nova pieces to Home Assistant on the satellite's existing MQTT device (`voice_assistant/ha_device.py`), the same route as the eleven controls, the usage device and the music device.
4. Home Assistant wraps the music pieces into one media player (`universal` platform, in a package), so ordinary media cards work with it.

## Non-goals

- Football. It stays on the Home views as it is (his words).
- Listening stats. The Music view already has them.
- A search with a results list (option B in chat). Ask Nova covers searching by asking.
- Restart on the view. The button exists on the device; it is not asked for here.
- Adding Nova to the Home view's combined Spotify and Jellyfin player. Possible later, since the universal media player makes it easy, but not asked.
- Homepage, the rack screen, any new service or container.
- Anything for the PC satellite, which is being built separately (branch `pc-satellite`). The device code is per satellite, so whatever is added here must work for any satellite's device without special cases; the view shows the Pi's device ("Nova").

## Anti-goals

- **Anything said to Nova, or any answer, reaching Home Assistant beyond what this design names.** Decision 0005: HA is told how Nova is set and what it is doing, never what is said to it nor its answers. Ask Nova sends text from HA to Nova, which 0005 allows (HA is the one saying it). The reply is spoken only and never published. The Ask Nova text entity must not echo what was typed back into its state topic for longer than HA needs (see "Left to the builder"), and nothing typed goes to any MQTT topic beyond the command topic. In Nova's own logs and on the clock display, typed text is treated exactly like heard text: the existing transcripts setting decides whether it is logged, as for speech (my call, he agreed; a separate never-log path would touch shared code that `pc-satellite` also changes, for no gain he asked for). Now-playing title, artist and cover are what Nova is doing, not what was said, so they are allowed, as the clock display already shows them.
- A typed request that can do more than a spoken one. It goes through the same path as speech after transcription, with the same guards (the Assist guard, confirmations, the voice check's equivalents where they apply). Where a spoken request would be refused or asked to confirm, a typed one is too.
- Making the satellite slower or the brain heavier: now-playing updates must be sent only on change (as NovaDevice already does), the cover only when the track changes, not every second.
- A view of default HA cards that looks nothing like the rest.

## Constraints

- British spelling, no em dashes, no emojis in code comments, UI text and commits. Nova's long plain-English docstrings and comments.
- Nova uses HA for anything HA can do; new entities go through MQTT discovery on the existing broker with the existing NovaDevice code.
- The view is storage-mode: edit `ha-dashboard/config.yaml` and `phone.yaml`, save over the websocket as `nova` on CT 107, read back, commit (`ha-dashboard/README.md`, vault `home-assistant`). The universal media player goes in a package in `ha-dashboard/homeassistant/packages/` and is copied to `/config/packages/`.
- Lab writes (deploy to the brain, HA config copy and restart, dashboard saves) are his named jobs. Never merge or push Nova's local main; deploys and pushes on his word.
- CT 109 load limits: nice, timeout, single worker, one suite at a time, no repeat runs. Tests through `.superpowers/tools/pi_test.sh` with `NOVA_TREE` set to this worktree.
- Other sessions have worktrees open on `pc-satellite`, `journeys`, `which-pc` and `football-8`. This branch is `nova-view` off main e15e3b0; expect a merge with `pc-satellite` later, which also touches the satellite's device.
- Bounded, light: the satellite device is announced per satellite; nothing new is stored on disk.

## Approaches

1. **Nova sends it all over its existing HA device (chosen).** New entities on each satellite's device: now-playing sensors, a cover image, play/pause, next and previous buttons, a playlist select, and an Ask Nova text entity. HA wraps the music ones in a universal media player. Fits how the Pi's controls, the usage device and the music device already reach HA.
2. **HA reads Nova's settings page API (`/api/music`, `/api/cover`) by REST.** Less Nova work for reading, but that page is Tailscale-only behind a login, and controls would need a new authenticated route into it. Unlike anything else in the lab. Rejected.
3. **The view first from what HA already has, music and typing later.** Quick but half the ask. Taken as the delivery order inside approach 1, not as an approach of its own.

His call: approach 1, delivered as view first, then music and Ask Nova.

## Decided

- **Look:** `glass` theme, `card_mod: class: glass`, grey monochrome like the AI view; Bubble Card where his other views use it.
- **Delivery order:** (a) the view on both dashboards from the existing entities (status, mute, Stop, Speak, volumes, timers, next alarm); (b) Nova's new music entities and the universal media player, added to the view; (c) Ask Nova, added to the view.
- **Ask Nova's reply:** spoken on the satellite only, never shown in HA (my proposal, to keep decision 0005; he agreed). If he later wants replies on the page, that is a deliberate change to 0005 and a new decision.
- **Navbar:** a Nova button after AI on both bars (desktop Homelab, AI, Nova, Devices, Home, Planner, Music; phone Homelab, AI, Nova, Devices, Home, Music, Planner), and on the phone the view sits in the same place so swipes follow the bar. My call; he agreed.
- **Which satellite:** the view shows the Pi's device. Ask Nova and the music controls act on the satellite whose device they belong to.
- **Follow-ups and confirmations (my call, he agreed):** when Nova asks a question back, whether the request was spoken or typed, a typed message that arrives while it is listening for the answer ends that listen and counts as the answer, inside the same conversation, so a typed "yes" confirms. Otherwise a typed request is a fresh turn and waits behind any turn in progress, as Speak does.
- **While the microphone is muted (my call, he agreed):** a typed request is still acted on and its reply spoken, since typing uses no microphone; if the reply asks a question, the microphone stays shut and only a typed answer is taken. Mute keeps meaning "Nova does not listen".
- **Who can use Ask Nova:** any Home Assistant user, with no per-user hiding or broker change. Only he has a login to Home Assistant or Nova, and HA is reachable only at home or over Tailscale (his answer). The decision record says so, so the call is revisited if anyone else is ever given a login.
- **A new decision record** in `docs/decisions/` (next free number) for Ask Nova: typed requests from HA, spoken replies, 0005 kept.

## Left to the builder

- Entity names and object ids, following the existing ones (`{node_id}_{control}`).
- How the cover reaches HA: an MQTT image entity with the bytes, or a URL. Bytes over MQTT on track change is the obvious fit (the music device already sends artist pictures as images).
- How the Ask Nova text entity's state is handled. HA's MQTT text entity shows the last state published to its state topic; the simplest is to publish nothing (optimistic text) or clear it after accepting, so the typed request does not sit in HA's state or history. Keep it out of the recorder if it would otherwise be stored.
- Where a typed request joins the session: the point after speech-to-text, so intents, Claude and the guards see it as heard text, with the follow-up behaviour under Decided.
- The playlist select's options (the user's playlists, as the settings page lists them) and what "nothing playing" shows.
- The exact card layout on each dashboard, within the look above; the phone view stacked in one column.
- Tests in Nova's style for every new entity and the typed path, including that nothing typed or answered is published.
