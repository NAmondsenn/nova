# Approval for big actions

Phase 6 piece 3 of 4 (after the PC satellite and PC commands; before the Mac). Decision record to be 0036.

## Why

Nova can now do things that cost money, run code or delete his data. He wants real proof it's him before those happen, proof that a misheard request, someone else in the room, a TV, or a prompt slipped into something Claude read can't give. His call of 16 Nov 2026: a phone passkey, "real proof, works away from home, costs nothing". On 20 Nov he added the PC's Windows Hello (his hand is bandaged; he'll enrol a finger of the other hand, and both ways fall back to a PIN).

## What he decided (20 Nov 2026)

- **Big actions**:
  - large downloads: 3 or more albums;
  - anything that runs Claude Code or the lab agent;
  - deleting playlists or calendar events;
  - in future, anything that reaches outside his room (unlocking doors, sending messages). None exist yet: the mechanism must make it a one-line mark for a new feature.
- **Not big**: shutting down or restarting the PC. These also stop asking "Shut down the PC?": they just happen, confirmed by the success chime (separate small change, same deploy).
- **Approach 1**: a passkey (WebAuthn) on Nova's own page for the phone, and Windows Hello for the PC. Not Home Assistant's "unlock to tap" notification (the proof would only be "HA says so"), and not a passkey in a browser at the PC.
- **Where it's asked**: everywhere at once. The phone always gets a notification. The PC, if it's on and connected, shows a Hello prompt at the same moment. Whichever is answered first counts, and the other is withdrawn. The Mac's Touch ID joins the same way once the Mac satellite exists.
- **Trust for five minutes**: one approval covers any big action for the next five minutes.
- **What Nova says**: "That needs your OK. Check your phone." (or "your phone or the PC" when the PC is on), then it waits. A refusal or no answer in time means nothing happens, and Nova says so.

## Goals

1. One brain-side approval service: `needs approval` is a mark on an action (a tool flag or an explicit call), plus a description ("Download 4 albums by X", "Remove 'Dentist' from your calendar") that is shown on every prompt.
2. **Phone**: a push notification through ntfy (already used) whose tap opens an approval page on Nova's settings site (Tailscale HTTPS address). The page shows the description and Approve / Refuse. Approve runs a WebAuthn assertion against the passkey he registered once on the settings page. The brain verifies the signature, the challenge, the origin and the counter before going ahead.
3. **PC**: the satellite shows Windows Hello's own consent prompt (UserConsentVerifier) with the description as its message, and answers approved or refused. The brain trusts the satellite's answer over its authenticated connection.
4. **First answer wins**: the first approval or refusal from any device settles the request. The others are withdrawn: the PC's prompt is cancelled, and the phone page shows "Already answered" (a notification can't be unsent, so it's left).
5. **Trust**: an approval opens a five-minute window in which further big actions go ahead without asking.
6. **Settings page**:
   - register or remove the passkey;
   - the trust minutes (5);
   - how long to wait for an answer (2 minutes);
   - the album count that makes a download big (3);
   - per-device "Can approve" switches (the PC on by default).

## Non-goals

- An approval API for outside callers (the lab agent, Claude Code launched elsewhere). Nova has no feature that runs Claude Code or the lab agent yet. The service is shaped so a later caller is one call, but no external endpoint is built now.
- Doors, messages, deleting a whole playlist: none exist. They get the mark when built.
- Proof of presence in the room, voice ID, or HA's notification actions.
- Approving from the Pi or the clock display (no fingerprint reader).

## Anti-goals

- A big action going ahead without a fresh verified approval, or inside a still-open trust window. That includes a crash, a restart, a timeout, or a reply from a device that wasn't asked.
- An approval for one action being used for another (replay). Each request has its own single-use challenge, tied to its description and expiry.
- Claude being able to skip, fake or pre-approve: the check happens in the executor, after Claude has chosen the action, never in the prompt.
- The approval page being usable by anyone but him: it sits behind the settings page's Tailscale login allow-list, and the passkey is the proof.
- Secrets or the passkey's private material anywhere but his phone. The brain keeps only the public key, credential id and counter. Nothing personal goes in the public repo.

## Constraints

- **WebAuthn needs a secure origin.** The settings page is served over HTTPS by Tailscale serve (`tailscale serve --https=443 http://127.0.0.1:8765` on the brain). That address is the relying party id. The NPM route (proxy_port) is not used for approvals.
- **The brain's venv has no `cryptography`.** Verifying ES256/RS256 signatures needs a pinned library (`cryptography` is the usual choice) or an equivalent. That's the builder's call, pinned in requirements.lock, on the brain only.
- **Windows Hello on the PC**: `Windows.Security.Credentials.UI.UserConsentVerifier` through winrt (one more pinned winrt package in requirements-pc.txt, matching the versions already there). A prompt raised from a windowless background process may open behind other windows or not at all: a throwaway test on the PC (his named job) proves the call before the build relies on it. The PC app (tray, Nova.exe; another session's branch pc-app) may give it a window to own the prompt. Coordinate, don't depend.
- **No GPU or hardware limits involved.**
- **Settings on the page; HA-first**: HA has no passkey verification, so this is Nova's own.
- **Load limits and test commands as in the repo's notes.**

## Approaches considered

1. **Passkey on Nova's page for the phone, Windows Hello from the satellite for the PC (chosen).** Real cryptographic proof from the phone. Native prompt at the PC. Touch ID fits later.
2. **HA companion "unlock required" notification**: least work and HA-first, but the proof is only an HA event that anything with HA admin could fake, and the unlock isn't bound to the request.
3. **Passkey everywhere, the PC included**: one mechanism, but a browser window at the PC is clunkier than Hello.

## The design

### Brain: the approval service (`voice_assistant/approvals.py`)

- `Approvals.ask(description, asked_at) -> Result`. It blocks the calling action's thread until approved, refused, or the timeout (2 min, setting).
  - If a trust window is open, it returns approved at once (logged).
  - Otherwise it creates a request with a fresh 32-byte random challenge, the description and an expiry. It stores it in memory only, so a brain restart refuses everything waiting.
  - It sends the phone notification and the PC prompt at once, and waits on an event.
  - The first verified answer settles it. Approved opens the trust window; refused or timeout does not.
  - Withdraw the others: send `approval_withdraw {id}` to every satellite that was asked. A late phone answer gets "Already answered".
- **Before waiting**, the action's reply is spoken first: "That needs your OK. Check your phone." / "... your phone or the PC.". The action then waits in the background and announces the outcome when done:
  - approved: the action's own result (e.g. "Downloading ...");
  - refused: "Okay, I won't.";
  - timeout: "No answer, so I haven't.".
  The builder finds the existing pattern for an action that answers later (downloads already announce "once it's in the library"). Holding the conversation lock for two minutes is not acceptable.
- **Marking an action**: a tool flag `approval=` (a callable giving the description from the call's arguments, or None when this call isn't big). The executor checks it before running the tool, for every path: local intents, Claude, routines, the settings page and "undo that".
  - `download_music` / `request_download`: big when the request covers 3 or more albums. Today one request is at most one album and "everything by an artist" is refused. So: big when this would be the third or later album download asked for within the trust minutes window (counted per request, not per track). Also, if "everything by an artist" is ever allowed, it is big. The album count is a setting.
  - `remove_from_calendar`: always big. Undoing its removal ("undo that") is not.
  - Deleting a whole playlist: no such tool yet. Taking a song off a playlist is not big (his "deleting playlists" means the playlist; my call).
- **Trust window**: one global window, so an approval on any device covers big actions asked anywhere for 5 minutes (setting). It is cleared on a brain restart.

### Phone: notification and approval page

- **Notification**: ntfy, the topic already used for his phone, high priority. Title "Approve?". Body is the description. Its click URL is `https://<settings page Tailscale address>/approve/<request id>`.
- **Approval page** (settings page server, same login allow-list):
  - It shows the description and two buttons.
  - **Approve** calls `navigator.credentials.get` with the request's challenge, `allowCredentials` = his registered credential and `userVerification: "required"`.
  - It posts the assertion. The brain verifies it: the challenge matches an open request; the type is `webauthn.get`; the origin and rpId hash match; the user-present and user-verified flags are set; the signature checks against the stored public key; the sign counter is greater than stored, or both are 0. The request is then settled.
  - **Refuse** settles it as refused (no passkey needed to refuse).
  - Expired or already answered requests show that and nothing else.
- **Registration** (settings page, Security section, new):
  - "Add a passkey" runs `navigator.credentials.create` (resident not required, user verification required, ES256 and RS256 offered). It stores the credential id, public key (COSE), counter and a name he types ("Phone").
  - Up to 3 passkeys (phone, and later the Mac's or a spare).
  - "Remove" deletes one, and needs an approval from another passkey or the PC if one exists; the last one can be removed from the page alone.
  - Stored in the brain's data folder (`passkeys.json`), never in the repo.

### PC: Windows Hello

- **Hello**: the hello gains `approves: true` when the satellite can reach UserConsentVerifier and its availability is `Available` (checked at start).
- **`approval {id, description}`**: the satellite calls `RequestVerificationAsync(description)` on its own long-lived COM/winrt thread (as windows_media does). It answers `approval_answer {id, approved: bool}` (Verified → true; Canceled, DeviceBusy, RetriesExhausted etc. → false).
- **`approval_withdraw {id}`**: cancels the pending call if Windows allows it. Otherwise the answer is ignored by the brain, which has already settled.
- **Who is asked**: the PC only when it's connected, its "Can approve" switch is on, and it said `approves`.
- **The brain** accepts `approval_answer` only for an id it sent to that satellite, once.

### Settings page

- **New section "Approvals"**:
  - passkeys (add, remove, names);
  - "Minutes one approval lasts" (5);
  - "Minutes to wait for an answer" (2);
  - "Albums that make a download need approval" (3);
  - per satellite that `approves`, a "Can approve" switch, on by default.
- **Recent approvals**: the last 20, with time, description, the device that answered and the outcome. Kept in memory and in a small rolling file; descriptions only, nothing else.

### The PC's power commands confirm with the chime (separate change, same deploy)

His words: shut down "should just say okay, or make the beep", and "make the confirmation chime for other similar tasks that don't have it already... consistent".

- **No question**: `pc_power` "off" and "restart" stop asking. They press the button straight away. `press_pc_button` and the ask-first path for these two are removed, or left unused if other code needs them.
- **Chime, not words**: every PC power command confirms a success with the success chime instead of words, as lights, pause and shuffle already do:
  - on (Wake-on-LAN): "Turning your PC on.";
  - off: "Shutting down the PC.";
  - restart;
  - sleep: "Putting your PC to sleep.";
  - screen off;
  - lock.
- **Sleep, screen off and lock at the PC**: the chime plays, and then the button is pressed, keeping 0034's "after it's said" order. The chime is what's waited for.
- **Failures still speak** ("Your PC's off.", "There's no shutdown button...").
- **Opening, closing and searching keep their short spoken reply** ("Opening Fortnite."), because it names what was matched, which is how a wrong match is caught. (My call: it's not the same kind of task.)
- **Unchanged from 0008/0034**: whole request only; the Pi only with the PC named; the PC only on a press. Decision 0034 records the change.

## My calls (his to overturn)

- **Trust window**: a single window across all devices, starting from the approval.
- **Big downloads**: the third album asked for within the trust window counts as "3 or more albums". Single songs never count.
- **Playlists**: taking a song off a playlist doesn't need approval; deleting a whole playlist will, once it exists.
- **Undo**: undoing a calendar removal doesn't need approval.
- **Refusing** needs no passkey.
- **After a refusal or timeout**, nothing is retried.
- **The phone notification** can't be unsent; its page says "Already answered".
- **Waiting requests** are refused on a brain restart.
- **Removing a passkey** needs an approval if another way to approve exists.

## Left to the builder

- The WebAuthn verification library (pinned) and the COSE key parsing.
- How the waiting action hands its outcome back to the satellite that asked: reuse the downloads announcement path or equivalent.
- The exact UI of the approval page and the Approvals section, in the settings page's existing look. Screenshots go to him before deploy, as for every visible change.
- The winrt call details and the window ownership of the Hello prompt, after the throwaway test.
- Tests: the service (approve, refuse, timeout, first answer wins, withdraw, trust window, restart refuses); WebAuthn verification (a good assertion; a wrong challenge, origin, rpId, flags or counter; a replayed assertion); the executor check on every path (local, Claude, routine, undo); the PC protocol; the settings page. Live checks: register on his phone, approve from the phone, approve at the PC, both at once, a refusal, a timeout.
