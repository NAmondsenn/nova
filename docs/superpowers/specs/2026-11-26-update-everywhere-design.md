# Update once, everything updates: design

26 November 2026. Approved approach: 1, the brain hands out the code. Status: design, nothing built.

## Why

Nova runs on several machines: the brain (CT 107), the Pi, the PC, and later the Mac. One repository and one `satellite.py` serve every satellite, so a feature added once should reach all of them. In practice each machine is updated a different way, by a different job, and they drift: on 22 November the Pi was found on b48f678, 132 commits behind the brain and the PC (a51ec25). He wants one action from him to bring every machine to the same version, so nothing lands on one satellite and not the others.

## What he decided

- **He starts it.** Updating is never automatic. He starts it from a button on the settings page or by voice ("Nova, update yourself").
- **Voice needs approval.** The spoken form asks for his approval first (decision 0037: phone or PC fingerprint, trust window as usual), since it changes the code on every machine.
- **Versions on the page.** The settings page shows each machine's version and flags any that are behind.
- **One action replaces today's jobs.** One update does what "pull and restart nova-backend", the Pi's `update.sh` and `deploy_pc.sh` do today.
- **Late machines catch up by themselves.** A satellite that is off, asleep or away when he starts the update updates the moment it reconnects. It does not wait for another press.
- **The brain hands out the code** (approach 1, below).
- **The brain's lock stays (his "b").** The brain's sandbox (`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges=yes`) keeps its own code, venv and models read-only to it, so a break-in through the page can't rewrite Nova. That stays. A separate updater does the brain's update: a oneshot unit (e.g. `nova-update.service`, run as `nova`, outside the sandbox) that only updates. Root sets it up once, with a narrow rule letting `nova` start that one unit and nothing else (a polkit rule or a single sudoers line; the builder's call). The brain only asks for it to start.
- **Music doesn't hold an update back (his "update now").** Since he asked, the update goes ahead during music. The music each satellite was playing resumes where it was once the brain is back. A satellite's own install also goes ahead during music, and it too resumes after.

## Goals

- One action updates the brain to GitHub's latest main, then every satellite to exactly the brain's commit.
- Every machine ends on the brain's commit. No machine is ever ahead of the brain.
- Machines that were away catch up on reconnect, with no action from him.
- The settings page shows, for the brain and each satellite: its version (short commit and date), and one of up to date / behind / updating / failed / needs you, with the reason.
- The Mac, when it exists, fits the same scheme with only a self-installer of its own.

## Non-goals

- Automatic updating, or checking GitHub for new versions on a schedule.
- Choosing a version or branch. It is always GitHub's main, and going back stays a hand job.
- Doing steps that need admin rights: system packages, `/etc/asound.conf`, the brain's systemd unit. Nova reports these as "needs you" with the one command to run.
- Updating Home Assistant, the dashboards or anything outside this repository.

## Anti-goals (a failure even if it works)

- **A dead satellite.** An update that leaves a machine not running Nova, needing him at the keyboard to bring it back. Each machine must fall back to the code it had if the new code won't start.
- **Code from anyone but the brain.** The link between the brain and a satellite is plain TCP: only the handshake is authenticated (HMAC proofs, `protocol.py`), and the messages after it are not. A satellite must never install code it cannot prove came from its brain unaltered.
- **An update mid-use.** No machine restarts during a recording, a reply, a conversation, a ringing alarm or timer, or an approval in progress. It waits until those are over. Music is not "in use" for this; see above.
- **Silent failure.** A machine that couldn't update must show it on the page, and in the brain's log, with why.
- **A brain that won't start.** The new code must pass the same settings check `update.sh` does before the brain restarts into it. Otherwise it is taken back out. And if it passes but then fails to start (crashes or hangs while loading), the brain goes back to the previous commit by itself (my call, same rule as the satellites): a "pending update, previous commit" marker checked at start, with the unit's start hook or `OnFailure`, set up in the same one-time root step.

## How it works today (for the builder)

- **Brain:** a git clone at `/home/nova/Home-Assistant`, run as user `nova` by the system service `nova-backend`. `update.sh` (as root) pulls, installs Python packages into the venv, downloads models, runs a settings check, reinstalls the service file and restarts. The brain can already restart itself: the settings page's restart exits with status 75, and the unit's `RestartForceExitStatus=75` brings it back (`Brain.restart`, `brain.py`). The `nova` user has no root.
- **Pi:** a git clone at `~/Home-Assistant`, user service `voice-assistant` (systemd --user, no root). `update.sh` there pulls, installs packages, checks settings, restarts, and takes the code back if a step fails (`take_back`).
- **PC:** no git. `scripts/deploy_pc.sh` on CT 109 sends a zip of the brain's commit over `ssh pc` and runs `scripts/windows/install.ps1`. It stops both scheduled tasks, the launcher and the satellite, installs into `%LOCALAPPDATA%\nova\app` and the venv, rebuilds Nova.exe, and restarts only if Nova was running. The brain cannot reach the PC by ssh.
- **Link:** `protocol.py`, one TCP connection per satellite, JSON lines plus payloads (`MAX_LINE` 4096, audio payloads capped). The hello carries capability flags. There is no version in it today.
- **Archive size:** `git archive --format=zip HEAD` is about 7 MB.

## Approaches considered

1. **The brain hands out the code (chosen).** The brain updates itself from GitHub, then sends each satellite its own exact commit over the existing link. Satellites report their version in the hello, so a reconnecting satellite that is behind is updated then: catch-up comes free. There is one source of truth, and no GitHub access is needed on satellites. Cost: a self-installer per kind of satellite, and an unchanging "update" part of the protocol.
2. **Each machine fetches from GitHub when told.** This needs GitHub access, and on the PC a zip download with no git. It has two moving parts, and a risk of fetching a newer commit than the brain's unless pinned. Rejected for that.
3. **Chain today's scripts on CT 109.** Nova would depend on the Claude container, and nothing would notice a satellite waking. Rejected.

## The design

### Starting it

- **The settings page** gets an "Update Nova" button beside the versions list. The page's own login covers it, so it needs no extra approval (my call).
- **By voice:** "update yourself", "update Nova" and similar. This runs approval (0037) first, then answers along the lines of "Updating. I'll be back in a minute." The exact phrases and wording are the builder's.
- **If the brain is already on GitHub's latest main,** it still checks every satellite and updates any that are behind. The answer says so ("Already up to date; updating the Pi.").

### The brain

1. Wait until it is idle (see anti-goals). If it isn't idle within a while, tell him and give up; the builder picks the limit. Note what each satellite is playing, to resume after.
2. The brain starts the updater unit, which, as `nova` outside the sandbox: fetch GitHub main and fast-forward only. A local change or a diverged branch fails with "needs you".
3. Install Python packages into the venv and download any new models, as `update.sh` does. These are the steps that need no root.
4. Run the settings check. If it fails, take the code back as `update.sh` does, report "failed", and stay on the old code.
5. If the commit changed the systemd unit file or anything else that needs root, note "needs you" with the command (`sudo bash update.sh --no-pull`). Still restart into the new code if it can run without it, or don't update at all if it can't; that one is the builder's call, but it must be stated.
6. The updater restarts `nova-backend` into the new code, or the brain exits 75 (the builder's call), with the take-back marker set.
7. On the way back up, clear the marker, note the outcome so the page shows it, resume the music, and say "I'm updated" on the satellite the request came from once it reconnects.

Reusing `update.sh`'s functions rather than rewriting them is preferred; how is the builder's call.

### Satellites

- **The hello gains `version`** (the full commit id the satellite runs) and `updates: true` (it knows the update messages). Old satellites send neither: they are "behind, needs a hand update once" on the page.
- **After the handshake,** if a satellite's version differs from the brain's commit and the satellite said `updates`, the brain offers the update. This happens on every connection, and it is what makes catch-up automatic. It needs no memory of a pending update.
- **The code is the brain's own commit:** `git archive` of the brain's HEAD, the same thing `deploy_pc.sh` sends today. On the Pi, which keeps its git clone, the builder may send a git bundle instead so the clone stays a clone. Either way the Pi ends with its checkout at the brain's commit.
- **Transfer:** in chunks within the protocol's limits (a new message kind, e.g. `update_chunk`, with its own size cap).
- **Proof of origin (required):** with the code, the brain sends an HMAC-SHA256 of the whole archive and the commit id, keyed from that satellite's password and bound to this connection's two handshake nonces. That is the same material `protocol.proof` uses, so a replay on another connection fails. The satellite checks it before writing anything and refuses on a mismatch, logging and reporting "failed: not from the brain". A plain hash over the same link is not enough.
- **The satellite installs when idle,** with its own installer. It always keeps the previous code and goes back to it if the new code won't start.
  - **Pi:** lay the code down, then run the same steps as `update.sh --no-pull` as its user. It already takes back on failure. The asound.conf warning becomes "needs you".
  - **PC:** a detached helper (it must outlive the satellite and Nova.exe, which it replaces) runs the same steps as `install.ps1`, then starts "Nova start" if Nova was running, as `deploy_pc.sh` does. If the new code won't start, it restores the previous `app` folder and venv state and starts that. Whether the venv is snapshotted, or only `app` with packages tolerated as `update.sh` does, is the builder's call.
  - **Mac:** a self-installer of its own when the Mac satellite is built.
- **Reporting:** the satellite says how it went (`update_result {version, ok, error?}`) when it reconnects on the new code, or straight away on a refusal or a failure before restart.
- **The update messages are frozen:** the hello's `version` and `updates`, the offer, the chunks, the proof and the result must stay compatible for ever, so any old updating satellite can always be updated by any newer brain. A test should pin their shape.

### Order and in-between

- The brain updates first. Its restart drops every satellite, and they reconnect within seconds and are updated then. So one press updates everything with no extra orchestration.
- A satellite one version behind must still be able to connect, receive and install. It should also keep working while it waits for idle. If the new brain cannot serve an old satellite's protocol, the satellite is still updated, because the update part is frozen.

### Settings page

A versions list: the brain, then each satellite by name. For each, the short commit, its date, and the state: up to date, behind, updating, failed (reason), needs you (one command), or "old, update by hand once". Plus the Update Nova button and the time of the last update. The layout is drawn and shown to him before going live (preview-before-saving rule).

### Bootstrap (once)

The Pi and the PC run code that has no `updates`, and the brain has no updater unit. So the first version goes on by hand, as his named jobs: push; on the brain, root runs `update.sh` (installs the updater unit, its permission rule and the start-failure take-back; I have no root there, so this is his or root from pve2 on his word); the Pi's `update.sh`; `deploy_pc.sh`. From then on, Update Nova does it all.

## Constraints

- No new third-party packages unless unavoidable; use the standard library and what the venvs already have.
- Nothing secret in the repo or the logs. Log the archive's HMAC check as pass or fail only.
- The repo is public.
- Tests on the Pi via `pi_test.sh`, London and UTC; CT 109 load limits apply.
- Settings that may change go on the settings page: for example the idle wait limit, if it is made one.
- A decision record (next free number, likely 0040) and hand checks in `docs/release-checklist.md` (next free numbers after 137).

## Left to the builder

- The Pi's take-back after a failed start (today `update.sh` only takes back on a failed settings check), and running its install detached from the service it stops.
- How the PC's helper outlives the tasks it stops.
- Local changes in the Pi's clone (refuse with "needs you" is the default).
- "Reconnected to the brain on the new commit" as the test of a successful start.

- Exact phrases, spoken wording and message names.
- Chunk size, the idle wait limit, the archive format per satellite (zip or git bundle).
- How to share steps with `update.sh` and `install.ps1` rather than copying them.
- The PC's rollback mechanics, within "never a dead satellite".
- Whether a brain commit needing root restarts into the new code or holds back (state it in the decision).
