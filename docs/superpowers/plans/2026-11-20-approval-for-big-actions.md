# Approval for Big Actions - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Big actions (3 or more albums, removing calendar events, and later anything that runs code or reaches outside his room) go ahead only after a fresh approval from his phone's passkey or the PC's Windows Hello, with one approval trusted for five minutes; and the PC's power commands stop asking and confirm with the chime.

**Architecture:** One brain-side service (`voice_assistant/approvals.py`) holds each request in memory with its own single-use challenge, asks every way at once (an ntfy notification whose link opens `/approve/<id>` on the settings page, and a Hello prompt on each PC satellite that said `approves`), settles on the first verified answer, withdraws the rest and opens the trust window. A big tool works out what it will do first, then calls `Approvals.gate(description, do, satellite)`, which speaks "That needs your OK..." at once and runs `do` on its own thread once approved, saying the outcome on that satellite as a download's is. WebAuthn checks are plain functions over `cryptography` (`voice_assistant/webauthn.py`); the PC side is a long-lived winrt thread (`voice_assistant/windows_hello.py`), as `windows_media.py` is.

**Tech Stack:** Python 3.13, `cryptography` (new, brain only), winrt `Windows.Security.Credentials.UI` (new, PC only), the settings page's own HTTP server and plain JS (WebAuthn in the browser), ntfy, pytest on the Pi, node's test runner for the page.

**Spec:** `docs/superpowers/specs/2026-11-20-approval-for-big-actions-design.md` (read all of it, including "Settled after the builder check", which overrides the earlier "tool flag" wording: the check is a hook the big tool calls after its lookup). Also read `docs/decisions/0034-opening-things-and-commands-at-the-computer.md` and `0035-the-orb-on-the-computers-screen.md` for the current shape of the PC satellite.

## Global Constraints

- Tests run on the Pi only: `NOVA_TREE=/home/nathaniel/work/Home-Assistant-approvals NOVA_CHECK=approvals bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/pi_test.sh <files>`, and again with `TZ_OVERRIDE=UTC` in front for any task touching times. Written below as `PI_TEST <files>`.
- Ruff: `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/approvals && /tmp/nova-check-pytest/bin/ruff check .'` (after a `PI_TEST` run has copied the tree). Clean before every commit.
- JS: `cd /home/nathaniel/work/Home-Assistant-approvals && nice -n 19 timeout 600 ~/tools/node/bin/node --max-old-space-size=768 --test --test-concurrency=1 tests/web/*.test.mjs`, and `~/tools/node/bin/node --check` on every changed `.js` file.
- CT 109 load limits: `cat /proc/loadavg` first (wait while the first figure is above 3), `nice -n 19 timeout 600` on anything run locally, one suite at a time, no repeat runs of a passing suite. Never `ps` on the Pi (use `pgrep`). No `git stash`.
- No pushes, no deploys, no restarts of `nova-backend`, nothing installed on the brain or the PC by the builder: those are his named jobs. The one install the builder does is the test copy of `cryptography` on the Pi (Task 1), into `/tmp/nova-check-pytest` only.
- Public repo: no secrets, no personal names, logins, addresses or Tailscale host names in code, tests or docs. Test origins are `https://nova.example.ts.net`.
- House style (memory note working-style): match the surrounding docstrings (Google style, full sentences saying why), British spelling, " - " for dashes and never an em dash, one commit per task, each commit message ending with the trailer line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Spoken and shown words, exactly: `That needs your OK. Check your phone.` / `That needs your OK. Check your phone or the PC.` / `That needs your OK. Check the PC.`; `Okay, I won't.`; `No answer, so I haven't.`; `That needs your OK, but there's no way to give it yet. Add a passkey on Nova's settings page.`; the phone page's `Already answered.`; the notification title `Approve?`.
- Settings (config section `approvals`, page section "Approvals", used straight away): `trust_minutes` 5 ("Minutes one approval lasts", whole, 0 to 60), `wait_minutes` 2 ("Minutes to wait for an answer", whole, 1 to 10), `album_count` 3 ("Albums that make a download need approval", whole, 2 to 20). Raising any of them needs an approval when a way to approve exists; lowering is free.
- Everything approval-related on the settings page (the Approvals section, `/approve/<id>`, every `/api/approvals/...` and `/api/passkeys/...` path, and `approvals.*` keys in `/api/settings`) is refused on the NPM route, whatever its login.
- Requests, challenges and the trust window live in memory only: a brain restart refuses everything waiting and closes the window. `passkeys.json` (public key, credential id, counter, name, origin) and `approvals.json` (the last 20: time, description, who answered, outcome) go in `paths.DATA_DIR`, never the repo.
- Settings-page look changes get screenshots with `NOVA_TREE=/home/nathaniel/work/Home-Assistant-approvals bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/page-preview/shoot.sh` before he sees a deploy (memory note preview-before-saving).

## Review Focus

1. **Both devices answered at once** (phone approves a second after the PC): the action runs once, the late answer gets "Already answered." and settles nothing (Task 3 `test_first_answer_wins_and_the_late_one_is_already_answered`).
2. **The PC goes away mid-prompt** (sleep, network drop, satellite restart): its prompt thread ends without settling, the phone can still approve, and the request still times out on time (Task 3 `test_a_pc_which_goes_away_leaves_the_phone_to_answer`).
3. **The same assertion posted twice** (a double tap, a resent form, a replay): the second settles nothing, and an old assertion for another request never verifies against this one's challenge (Task 3 `test_a_replayed_assertion_settles_nothing_more`, Task 1 `test_an_assertion_for_another_challenge_is_refused`).
4. **Two big actions in one turn** (Claude calls `remove_from_calendar` twice): two requests with their own challenges and descriptions, one approval approves both through the trust window, logged per request; a refusal of one leaves the other waiting (Task 3 `test_an_approval_approves_the_others_still_waiting`, Task 7 `test_two_removals_in_one_turn_each_ask`).
5. **The NPM route** (or a page through it) reaching any approval path or saving `approvals.*`: 403, nothing changed (Task 9 `test_approvals_are_refused_through_npm`).

---

### Task 0: Hello from the windowless satellite, on his PC (his named job; the builder writes the script only)

**Files:**
- Create (outside the repo): `/tmp/claude-1000/hello-check/hello_check.pyw`

**Interfaces:**
- Produces: a written answer to three questions, which Task 5 follows: (A) does `UserConsentVerifier.request_verification_async(message)` from `pythonw.exe` with no window of its own show the Hello prompt in front of a full-screen window, and how long until it shows; (B) if not, does `IUserConsentVerifierInterop::RequestVerificationForWindowAsync` with a hidden owner window of the process's own do it; (C) does `.cancel()` on the pending operation close the prompt.

- [ ] **Step 1: Write the script.** It installs nothing itself. It imports `winrt.windows.security.credentials.ui` (`UserConsentVerifier`, `UserConsentVerifierAvailability`, `UserConsentVerificationResult`), writes to `%TEMP%\hello-check.log` the availability from `check_availability_async()`, then runs case A with the message "Nova test: approve?", logging the result name and the seconds taken; then case C (a second call, `.cancel()` after 5 s, logging whether the prompt went and what the result was). Case B only if he reports A failed: through ctypes, `RoGetActivationFactory` for `Windows.Security.Credentials.UI.UserConsentVerifier` with the interop's IID (take it from `UserConsentVerifierInterop.h` in the Windows SDK, not from memory), with an owner window made by `CreateWindowExW` (WS_EX_TOOLWINDOW, never shown).
- [ ] **Step 2: Hand it to him with the sentence for his named job.** It has to run in his desktop session, not over ssh (an ssh session has no desktop to show a prompt on): "Run the Hello check: copy hello_check.pyw to the PC, put `winrt-Windows.Security.Credentials.UI==4!10.0.28000.2957` into `%TEMP%\hello-check\lib` with the satellite's pip (`--target`, `--no-deps`), start it with the satellite's `pythonw.exe` from a shortcut while a full-screen game or video is in front, answer the prompts, then send me `%TEMP%\hello-check.log`." He deletes `%TEMP%\hello-check` after.
- [ ] **Step 3: Record what it showed** in the Task 5 notes and in decision 0036 (Task 12).

**Fallback if Task 0 isn't run before Task 5:** build Task 5 on case A (plain `request_verification_async`, no owner window) and `.cancel()` as best effort. The deploy's hand check "approve at the PC" decides: if the prompt doesn't come to the front, he switches the PC's "Can approve" off on the settings page and approves by phone alone until the PC app (branch pc-app, another session's) gives the satellite a window to own the prompt. No code depends on that branch.

---

### Task 1: WebAuthn checks

**Files:**
- Create: `voice_assistant/webauthn.py`
- Modify: `requirements-backend.txt` (the brain's lock; see the note below)
- Test: `tests/test_webauthn.py`, with a helper `tests/fake_authenticator.py`

**Interfaces:**
- Produces, in `voice_assistant/webauthn.py`:
  - `ES256 = -7`, `RS256 = -257`; `class NotVerified(Exception)` (its message is the reason, for the log only).
  - `b64url(data: bytes) -> str` (no padding) and `from_b64url(text: str) -> bytes` (raises `NotVerified` on anything that isn't base64url, or over 4096 characters).
  - `rp_id(origin: str) -> str`: the host of an `https://` origin with no path, query or port other than 443; raises `NotVerified` otherwise.
  - `check_registration(challenge: bytes, origin: str, client_data_json: bytes, authenticator_data: bytes, public_key_der: bytes, algorithm: int) -> int`: the sign counter to store.
  - `check_assertion(challenge: bytes, origin: str, public_key_der: bytes, algorithm: int, stored_counter: int, client_data_json: bytes, authenticator_data: bytes, signature: bytes) -> int`: the new counter to store.
- Produces, in `tests/fake_authenticator.py`: `FakeAuthenticator(algorithm=ES256, origin="https://nova.example.ts.net")` with `credential_id: bytes`, `public_key_der: bytes`, `counter: int`, `register(challenge) -> dict` (`client_data_json`, `authenticator_data`, `public_key_der`, `algorithm`, `credential_id`) and `assert_(challenge, *, origin=None, type="webauthn.get", rp=None, flags=0x05, counter_step=1) -> dict` (`client_data_json`, `authenticator_data`, `signature`, `credential_id`), signing with `cryptography` the way a phone does.

**Note on the pin:** the spec says "pinned in requirements.lock, on the brain only", but `requirements.lock` is the satellite's (the Pi's) lock; the brain installs `requirements-backend.txt`. So `cryptography` goes in `requirements-backend.txt` only (its `cffi` 2.1.0 and `pycparser` 3.0 are already there), never in `requirements.lock` or `requirements-pc.txt`. Pin the newest release at least two weeks old on the day (check with `pip index versions cryptography` on the Pi, and its release date on PyPI), with a comment line saying why and the date, in the file's own style.

The registration takes the public key the browser gives (`AuthenticatorAttestationResponse.getPublicKey()`, SPKI DER) and `getPublicKeyAlgorithm()`, with attestation `"none"`, so no CBOR or COSE parsing is needed anywhere; the first approval proves the phone holds the key.

- [ ] **Step 1: Install the pinned `cryptography` for the Pi's test run.** `ssh -o BatchMode=yes pi '/home/nathaniel/Home-Assistant/venv/bin/python -m pip install --no-deps --target /tmp/nova-check-pytest cryptography==<pin>'`, then `ssh -o BatchMode=yes pi 'PYTHONPATH=/tmp/nova-check-pytest /home/nathaniel/Home-Assistant/venv/bin/python -c "import cryptography; print(cryptography.__version__)"'`. Expected: the pin. (`/tmp` is cleared when the Pi restarts; say so in the commit message so the next builder knows to repeat it. If auto mode refuses this, give him the sentence: "Install cryptography==<pin> into /tmp/nova-check-pytest on the Pi for the approvals tests.")

- [ ] **Step 2: Write the failing tests** in `tests/test_webauthn.py` (each makes a fresh `FakeAuthenticator`; `CHALLENGE = bytes(range(32))`; `ORIGIN = "https://nova.example.ts.net"`):
  - `test_a_good_assertion_gives_the_new_counter`: register then assert with `counter_step=1` from a stored counter of 0: returns 1.
  - `test_rs256_is_checked_too`: as above with `FakeAuthenticator(RS256)`.
  - `test_a_wrong_type_is_refused`: `type="webauthn.create"` raises `NotVerified`.
  - `test_an_assertion_for_another_challenge_is_refused`: signed over `bytes(32)`, checked against `CHALLENGE`: `NotVerified`.
  - `test_another_origin_is_refused`: `origin="https://evil.example.ts.net"` in the client data: `NotVerified`.
  - `test_another_rp_id_is_refused`: `rp="example.com"` in the authenticator data's hash: `NotVerified`.
  - `test_user_presence_and_verification_are_both_needed`: `flags=0x01` (no UV) and `flags=0x04` (no UP) each raise `NotVerified`.
  - `test_a_counter_which_hasnt_gone_up_is_refused`: stored 5, assertion with counter 5: `NotVerified`; stored 0 and counter 0 (an authenticator that keeps no counter) returns 0.
  - `test_a_tampered_signature_is_refused`: one byte of the signature flipped: `NotVerified`.
  - `test_a_short_or_odd_authenticator_data_is_refused`: 36 bytes; and client data that isn't JSON: `NotVerified`, never another exception.
  - `test_registration_checks_type_challenge_origin_and_flags`: a good `register()` returns its counter; with `type="webauthn.get"`, another challenge, another origin, or no UV each raise `NotVerified`.
  - `test_registration_refuses_a_key_of_another_kind`: a P-384 key, or an RSA key given with `ES256`: `NotVerified`.
  - `test_rp_id_wants_https`: `rp_id("http://nova.example.ts.net")` and `rp_id("https://nova.example.ts.net:8443")` raise; `rp_id(ORIGIN) == "nova.example.ts.net"`.

- [ ] **Step 3: Run them to see them fail.** `PI_TEST tests/test_webauthn.py`. Expected: FAIL, `ModuleNotFoundError: voice_assistant.webauthn`.

- [ ] **Step 4: Implement `voice_assistant/webauthn.py`.** `check_assertion`, in this order, each failure a `NotVerified` with a short reason (every parsing error caught and turned into one):

```
data = json.loads(client_data_json)            # a dict, else refuse
data["type"] == "webauthn.get"
hmac.compare_digest(from_b64url(data["challenge"]), challenge)
data["origin"] == origin and data.get("crossOrigin") is not True
len(authenticator_data) >= 37
authenticator_data[:32] == sha256(rp_id(origin).encode())
flags = authenticator_data[32]; flags & 0x01 and flags & 0x04     # user present, user verified
counter = int.from_bytes(authenticator_data[33:37], "big")
if counter or stored_counter: counter > stored_counter
signed = authenticator_data + sha256(client_data_json)
key = load_der_public_key(public_key_der)
ES256: isinstance(key, EllipticCurvePublicKey) and key.curve.name == "secp256r1"; key.verify(signature, signed, ECDSA(SHA256()))
RS256: isinstance(key, RSAPublicKey); key.verify(signature, signed, PKCS1v15(), SHA256())
InvalidSignature -> refuse; return counter
```

`check_registration` does the first five lines with `"webauthn.create"`, the flags, the key-kind check (by loading it), and returns the counter; no signature (attestation "none").

- [ ] **Step 5: Run them to see them pass.** `PI_TEST tests/test_webauthn.py`, then ruff. Expected: all pass, ruff clean.

- [ ] **Step 6: Commit.** `git add voice_assistant/webauthn.py tests/test_webauthn.py tests/fake_authenticator.py requirements-backend.txt` - "Check passkey registrations and approvals (WebAuthn)".

---

### Task 2: Passkeys and each satellite's "Can approve" on disk

**Files:**
- Create: `voice_assistant/passkeys.py`
- Modify: `voice_assistant/satellite_rooms.py` (`_blank`, the read in `__init__`, `seen`, new setters and getters)
- Test: `tests/test_passkeys.py`, `tests/test_satellite_rooms.py`

**Interfaces:**
- Produces, in `voice_assistant/passkeys.py`:
  - `MOST_PASSKEYS = 3`, `LONGEST_NAME = 30`.
  - `@dataclass Passkey`: `id: str` (base64url credential id), `name: str`, `public_key: str` (base64url SPKI DER), `algorithm: int`, `counter: int`, `origin: str`, `added: float` (epoch).
  - `PasskeyStore(path: str)`: `all() -> List[Passkey]` (oldest first), `get(id: str) -> Optional[Passkey]`, `add(passkey: Passkey) -> Optional[str]` (None once saved, else what to say: `"Nova keeps at most 3 passkeys."`, `"That passkey is already added."`), `remove(id: str) -> bool`, `set_counter(id: str, counter: int) -> None`. Saved with `storage.save_json`, mode 0o600; a file which can't be read is logged once and read as none. Thread-safe (one lock).
- Produces, in `SatelliteRooms`: `seen(name, push_to_talk, machine=None, saves_notes=False, approves=False)`; `approves(name) -> bool` (what it last said); `set_can_approve(name: str, on: bool)` (raises OSError like `set_opens_things`); `can_approve(name: str) -> bool` - the stored switch if he set one, else True only for `machine(name)` in `("pc", "mac")`. `_blank()` gains `"approves": False, "can_approve": None`; the file read keeps a non-bool `can_approve` as None and a non-bool `approves` as False.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_passkeys.py`: `test_a_passkey_is_kept_across_a_restart` (add, new store on the same path, `get` gives it back equal); `test_at_most_three` (fourth `add` gives the message, `all()` still 3); `test_the_same_one_twice_is_refused`; `test_remove_and_counter` (`remove` True then False; `set_counter` kept after reload); `test_an_unreadable_file_is_none_and_logged` (garbage in the file: `all() == []`, one error logged); `test_the_file_is_private` (mode `0o600`).
  - `tests/test_satellite_rooms.py`: `test_can_approve_defaults_on_for_a_pc_or_mac_only` (seen as `"pc"`, `"mac"`, `"pi"`, `None`: True, True, False, False); `test_can_approve_switch_outlasts_a_restart` (`set_can_approve("pc", False)`, reload: False; `set_can_approve("pi", True)`: True); `test_approves_is_what_it_last_said`; `test_an_older_file_reads_with_no_switch` (a file without the two fields reads `approves` False and `can_approve` the default).

- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_passkeys.py tests/test_satellite_rooms.py`. Expected: FAIL on the missing module and methods.
- [ ] **Step 3: Implement both.**
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Keep passkeys and each satellite's Can approve".

---

### Task 3: The approval service

**Files:**
- Create: `voice_assistant/approvals.py`
- Modify: `config.yaml` (new `approvals:` section after `computer:`, with a comment per setting in the file's style), `voice_assistant/config.py` (`LIMITS`)
- Test: `tests/test_approvals.py`, `tests/test_config.py` (`IN_AND_OUT` gains the three keys, which `test_config.py:362` requires)

**Interfaces:**
- Consumes: `PasskeyStore`, `Passkey` (Task 2); `check_assertion`, `from_b64url`, `b64url`, `NotVerified` (Task 1); an `Ntfy` (`phone` attribute, `send_soon(topic, message, title, priority, click)`).
- Produces, in `voice_assistant/approvals.py`:
  - Words: `NO_WAY`, `REFUSED_SAID = "Okay, I won't."`, `TIMED_OUT_SAID = "No answer, so I haven't."`, `ALREADY = "Already answered."`, `RUN_OUT = "This has run out."`, `def pending_said(asked: Sequence[str]) -> str` (`"That needs your OK. Check " + " or ".join(asked) + "."`). `PHONE = "your phone"`. `MOST_DESCRIPTION = 200` (longer is cut to 197 characters and `"..."`). `RECENT_KEPT = 20`.
  - Outcomes: `"approved"`, `"refused"`, `"timeout"`, `"no_way"`, `"stopped"`.
  - `class Request`: `id: str` (`secrets.token_hex(16)`), `challenge: bytes` (32 from `secrets.token_bytes`), `description: str`, `expires: float` (monotonic), `asked: Tuple[str, ...]` (what's said, e.g. `("your phone", "the PC")`), `credentials: Tuple[str, ...]` (the passkey ids it may be answered with), `approvers: Tuple[str, ...]` (satellite names asked), `outcome: Optional[str]`, `by: Optional[str]`; `settled: threading.Event`.
  - `class Approvals(settings: Callable[[], Dict], passkeys: PasskeyStore, ntfy=None, approvers: Callable[[], List] = lambda: [], spoken: Callable[[str], str] = lambda name: name, say: Callable[[Optional[str], str], None] = lambda satellite, message: None, log_path: Optional[str] = None, now=time.monotonic, wall=time.time)`. An approver is anything with `name`, `ask_approval(id: str, description: str, wait: float) -> Optional[bool]` and `withdraw_approval(id: str) -> bool` (Task 4's `RemoteSatellite`).
    - `trusted() -> bool`; `trusted_until() -> Optional[float]` (epoch, for the page).
    - `ways(without: Optional[str] = None) -> List[str]`: `"your phone"` while a passkey other than `without` exists and ntfy has a phone topic, then `spoken(name)` of each approver.
    - `request(description: str, without: Optional[str] = None) -> Request`: already settled `"approved"` by `"trust"` while trusted; settled `"no_way"` with no ways; else open: the phone notified (`ntfy.send_soon(phone, description, title="Approve?", priority=4, click=f"{origin}/approve/{id}")`, `origin` that of the newest passkey) and one thread per approver started.
    - `wait(request: Request) -> str`: blocks until settled or `expires`, settling `"timeout"` then; gives the outcome.
    - `ask(description: str, without: Optional[str] = None) -> bool`: `wait(request(...)) == "approved"` - for the settings page.
    - `gate(description: str, do: Callable[[], Dict], satellite: Optional[str], done: Optional[Callable[[Dict], None]] = None) -> Dict`: trusted, runs `do()` now and gives its reply; no way, `{"success": False, "message": NO_WAY}`; else `{"success": True, "message": pending_said(request.asked)}` at once, and on a thread of its own: `wait`, then approved, `reply = do()`, `done(reply)` if given, `say(satellite, reply["message"])`; refused, `say(satellite, REFUSED_SAID)`; timeout, `say(satellite, TIMED_OUT_SAID)`; stopped, nothing. Anything `do` raises is logged with its type and `"That didn't work."` is said.
    - `lookup(id: str) -> Optional[Request]` (open, or settled within the last hour, for the page to say "Already answered.").
    - `answer_by_passkey(id: str, credential_id: str, client_data_json: bytes, authenticator_data: bytes, signature: bytes) -> Dict` and `refuse_by_phone(id: str) -> Dict`: `{"success", "message"}` with the messages `"Approved."`, `"Refused."`, `ALREADY`, `RUN_OUT`, `"There's no such request."`, `"That passkey can't answer this."` (not in `request.credentials`, or the phone wasn't asked), `"That didn't check out."` (any `NotVerified`; the reason is logged, the request stays open).
    - `recent() -> List[Dict]`: newest first, each `{"at": epoch, "description", "by", "outcome"}`, kept in memory and in `log_path` (the last 20).
    - `stop()`: settles every open request `"stopped"`, for `Brain._clean_up`.
  - Logging: a plain line with the request id and outcome; the description only with `extra=TRANSCRIPT` (from `logging_setup`), as other words of his are.

Settling, under the service's one lock, then outside it: the first answer wins and any later one changes nothing; `"approved"` sets the trust window to `now() + trust_minutes * 60` and settles every other open request `"approved"` by `"trust"` (one log entry each); each approver with this request's prompt showing gets `withdraw_approval(id)`. A PC shows one prompt at a time: its threads take a per-approver lock, and a thread whose request has settled by the time it gets the lock asks nothing. An approver whose `ask_approval` gives None (gone, or no answer in time) settles nothing. A refusal settles only its own request.

- [ ] **Step 1: Write the failing tests** in `tests/test_approvals.py`, with a `FakeClock` (monotonic and wall, moved by hand), a `FakeNtfy` (records `send_soon`), a `FakeApprover(name, answer=None, delay_event=None)` (records asks and withdraws; `ask_approval` blocks on its event, then gives `answer`), a `PasskeyStore` in `tmp_path` holding one `FakeAuthenticator`'s registration, and `settings = {"trust_minutes": 5, "wait_minutes": 2, "album_count": 3}`:
  - `test_no_way_refuses_with_the_words`: no passkey, no approver: `gate(...)` gives `{"success": False, "message": NO_WAY}` and `do` never runs.
  - `test_the_pending_words_name_each_way`: phone only, PC only, both: `"That needs your OK. Check your phone."`, `"... Check the PC."`, `"... Check your phone or the PC."` (with `spoken=lambda name: "the PC"`).
  - `test_the_phone_is_notified_with_a_link`: one `send_soon` to the phone topic, title `"Approve?"`, priority 4, body the description, click `"https://nova.example.ts.net/approve/" + request.id`.
  - `test_an_approval_by_passkey_runs_it_and_says_its_result`: `answer_by_passkey` with a good assertion over `request.challenge`: `{"success": True, "message": "Approved."}`, `do` runs once, `say(satellite, do's message)`, the counter stored.
  - `test_a_bad_assertion_leaves_it_open`: wrong challenge: `"That didn't check out."`, still open, a good one after still approves.
  - `test_a_passkey_not_asked_cant_answer`: request made `without=` that passkey: `"That passkey can't answer this."`.
  - `test_refusal_says_okay_i_wont_and_opens_no_window`: `refuse_by_phone`: `say(..., "Okay, I won't.")`, `trusted()` False, `do` never runs.
  - `test_no_answer_in_time_says_so`: clock past 120 s: `say(..., "No answer, so I haven't.")`, outcome `"timeout"`; a passkey answer after gives `RUN_OUT`.
  - `test_first_answer_wins_and_the_late_one_is_already_answered`: PC approves, then the phone's good assertion: `ALREADY`, `do` ran once, `by` is the PC.
  - `test_the_others_are_withdrawn`: phone approves while the PC's prompt shows: the PC got `withdraw_approval(id)` once.
  - `test_a_pc_which_goes_away_leaves_the_phone_to_answer`: the approver gives None at once: still open, the phone's answer approves.
  - `test_one_prompt_at_a_time_on_a_pc`: two requests, one PC: the second `ask_approval` starts only after the first returns; once the first is approved the second is approved by trust and the PC is never asked it.
  - `test_an_approval_approves_the_others_still_waiting`: two open requests, one approved by passkey: both run, `recent()` has two entries, the second `by` `"trust"`.
  - `test_a_refusal_settles_only_its_own`: two open, one refused: the other still open.
  - `test_trust_lasts_the_minutes_set_and_no_more`: approved at t; at t+299 `gate` runs `do` at once; at t+301 it asks again; with `trust_minutes` 0 it always asks.
  - `test_a_replayed_assertion_settles_nothing_more`: the same good assertion posted twice: the second gives `ALREADY`; posted against a second, new request: `"That didn't check out."`.
  - `test_stopping_refuses_everything_waiting`: `stop()`: outcome `"stopped"`, `do` never runs, nothing said; a new `Approvals` on the same files has `trusted()` False.
  - `test_a_long_description_is_cut`: 300 characters in, the request's description is 200 ending `"..."`.
  - `test_recent_keeps_twenty_across_a_restart`: 25 settled: `recent()` 20 newest first, and the same from a new `Approvals` on `log_path`.
  - `test_what_do_raises_is_said_not_thrown`: `do` raises `RuntimeError`: `say(..., "That didn't work.")`, the log line names `RuntimeError` only.
- In `tests/test_config.py`, `IN_AND_OUT` gains `("approvals", "trust_minutes")`, `("approvals", "wait_minutes")`, `("approvals", "album_count")` with values in and out of their ranges (e.g. 0 and 61, 1 and 11, 2 and 1).

- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_approvals.py tests/test_config.py`. Expected: FAIL, no module.
- [ ] **Step 3: Implement `approvals.py`, the config section and `LIMITS`** (`Limit("number", 0, 60, True)`, `Limit("number", 1, 10, True)`, `Limit("number", 2, 20, True)`, each with a comment saying why its range).
- [ ] **Step 4: Run them to see them pass,** then with `TZ_OVERRIDE=UTC`, then ruff.
- [ ] **Step 5: Commit.** "Ask for approval on every way at once, the first answer settling it".

---

### Task 4: The PC's approvals in the protocol, brain side

**Files:**
- Modify: `voice_assistant/protocol.py` (module docstring's message list; `greet(..., approves: bool = False)` sending `approves: true` only when True; `admit` returning `"approves"` through `_flag`)
- Modify: `voice_assistant/remote.py` (`RemoteSatellite`)
- Test: `tests/test_protocol.py`, `tests/test_remote.py`

**Interfaces:**
- Produces, on `RemoteSatellite`: `approves: bool` (from the hello); `ask_approval(id: str, description: str, wait: float) -> Optional[bool]` - `_ask(f"approval-{id}", "approval", b"", wait, description=description)` and the answer's `approved`; None for no answer in time, or gone; `withdraw_approval(id: str) -> bool` - sends `approval_withdraw {"id": f"approval-{id}"}`. A handler `"approval_answer": self._approval_answer` which takes `{"id", "approved": bool}` only for an `approval-` id still waited on (`_waiting_for`), so an answer for an id never sent, sent to another satellite, or answered already is dropped with one warning.
- Messages: brain to satellite `approval {"id", "description"}` and `approval_withdraw {"id"}`; satellite to brain `approval_answer {"id", "approved"}`. Hello extra `approves?`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_protocol.py`: `test_approves_goes_in_the_hello_only_when_true` (greet with and without); `test_admit_reads_approves_and_a_nonsense_one_as_false` (`"yes"` gives False and one warning, the satellite still let in).
  - `tests/test_remote.py` (over its existing stream fakes): `test_ask_approval_sends_the_description_and_gives_the_answer` (the message sent is `approval` with `id` `approval-<id>` and the description; answering `approved: True` gives True, `False` gives False); `test_an_answer_for_an_id_never_asked_is_dropped`; `test_an_answer_counts_once` (a second answer for the same id after the first is dropped); `test_no_answer_in_time_or_gone_gives_none` (wait runs out; and `close()` mid-wait); `test_withdraw_sends_the_id`; `test_an_answer_which_isnt_true_or_false_is_dropped` (`approved: "yes"`).
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_protocol.py tests/test_remote.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Ask a satellite that approves for its OK, and take its answer once".

---

### Task 5: Windows Hello on the PC satellite

**Files:**
- Create: `voice_assistant/windows_hello.py`
- Modify: `satellite.py` (`__init__`, `_handlers`, `run`'s `SatelliteLink(...)`, `_clean_up`), `voice_assistant/satellite_link.py` (`approves` passed through to `greet`), `requirements-pc.txt` (`winrt-Windows.Security.Credentials.UI==4!10.0.28000.2957`, the comment saying what for and the date)
- Test: `tests/test_windows_hello.py`, `tests/test_satellite.py`, `tests/test_satellite_link.py`

**Interfaces:**
- Consumes: Task 0's answer (or its fallback); the protocol messages of Task 4.
- Produces, in `voice_assistant/windows_hello.py`: `class WindowsHello(backend=None)` built like `WindowsMedia`: on Windows with winrt it loads `_Winrt` (`UserConsentVerifier`, `UserConsentVerifierAvailability`, `UserConsentVerificationResult`), starts one long-lived owner thread, and checks availability there at start (`SET_UP_WAIT = 10`). `available: bool` (availability is `AVAILABLE`). `ask(id: str, description: str) -> bool` - blocks the caller until Hello answers, True only for `VERIFIED` (`CANCELED`, `DEVICE_BUSY`, `RETRIES_EXHAUSTED`, anything else, or an exception, False); the prompts are the owner thread's jobs, so one shows at a time, the others waiting in order. `cancel(id: str)`: a job still queued is dropped (its `ask` gives False); the one showing has `.cancel()` called on its operation, as best effort. `close()`. Off Windows or without winrt it is `available` False and imports nothing. If Task 0 found case B was needed, `_Winrt` gives the owner thread a hidden owner window and calls the interop instead; the interface is the same.
- In `satellite.py`: on Windows, `self.hello = WindowsHello()` kept if `available`, else None; the hello says `approves=self.hello is not None`. Handlers: `"approval"` starts a thread `name="approval"` which runs `self.hello.ask(id, description)` and sends `approval_answer {"id", "approved"}` (no hello, or no id: logged once, nothing sent); `"approval_withdraw"` calls `self.hello.cancel(id)`. The description is shown as Hello's message, cut to 200 characters. `_clean_up` closes it.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_windows_hello.py` with a fake backend (availability and a result it gives, an operation whose `cancel` it records, and an event to hold a prompt open): `test_off_windows_it_isnt_available_and_imports_nothing`; `test_verified_is_true_and_the_rest_false` (each result name); `test_one_prompt_at_a_time` (two `ask`s from two threads: the second starts after the first answers); `test_cancel_drops_a_queued_one_and_cancels_the_one_showing`; `test_unavailable_hello_says_so` (`NOT_CONFIGURED_FOR_USER`: `available` False); `test_an_exception_is_false_and_logged_once`.
  - `tests/test_satellite.py` with a `FakeWindowsHello` monkeypatched over `satellite.WindowsHello` and `_on_windows` True: `test_the_hello_says_approves_with_hello_only` (with and without); `test_an_approval_is_asked_and_answered` (brain sends `approval {"id": "approval-1", "description": "Remove 'Dentist', Tue 25 Nov 10:00"}`: the fake was asked that text, and `approval_answer {"id": "approval-1", "approved": True}` was sent); `test_withdraw_cancels` (`approval_withdraw {"id": "approval-1"}` calls the fake's `cancel("approval-1")`); `test_no_hello_answers_nothing`.
  - `tests/test_satellite_link.py`: `test_approves_reaches_the_hello`.
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_windows_hello.py tests/test_satellite.py tests/test_satellite_link.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Ask Windows Hello for approval at the PC".

---

### Task 6: The brain makes the service and gives it its ways

**Files:**
- Modify: `voice_assistant/brain.py` (`__init__`, `_serve_satellite`'s `rooms.seen`, a new `_approvers`, a new `_say_approval_outcome`, `_clean_up`), `voice_assistant/session.py` (the `ActionExecutor(...)` call gains `approvals=brain.approvals`)
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `Approvals` (Task 3), `PasskeyStore` (Task 2), `RemoteSatellite.approves` / `ask_approval` / `withdraw_approval` (Task 4), `SatelliteRooms.can_approve` (Task 2).
- Produces, on `Brain`: `passkeys: PasskeyStore` (`DATA_DIR/passkeys.json`) and `approvals: Approvals` (settings `lambda: self.config.section("approvals")`, `ntfy=self.ntfy`, `approvers=self._approvers`, `spoken=self.spoken_name`, `say=self._say_approval_outcome`, `log_path=DATA_DIR/approvals.json`), both made before `self.downloads` (Task 8 passes it there) and before any session. `_approvers() -> List[RemoteSatellite]`: each connected session's satellite whose `approves` is True and `rooms.can_approve(name)`. `_say_approval_outcome(satellite: Optional[str], message: str) -> None`: as `_say_download` (announce on that satellite, else to the phone, title `"Nova"`), never raising, with its own log words. `rooms.seen(..., approves=hello["approves"])`. `_clean_up` calls `self.approvals.stop()` before the sessions close.

- [ ] **Step 1: Write the failing tests** in `tests/test_brain.py`, over its existing fake sessions: `test_only_a_connected_satellite_that_approves_and_may_is_asked` (a PC saying approves: in `_approvers()`; the same with `can_approve` switched off: not; a Pi saying approves and never switched on: not); `test_an_approval_outcome_is_said_where_it_was_asked_else_sent_to_the_phone`; `test_stopping_the_brain_stops_the_approvals`; `test_every_session_gets_the_approvals` (`session.actions.approvals is brain.approvals`).
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_brain.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus `PI_TEST tests/test_session.py`, plus ruff.
- [ ] **Step 5: Commit.** "Give the brain its approvals, asked of the phone and each PC that may".

---

### Task 7: Removing calendar events needs approval

**Files:**
- Modify: `voice_assistant/google_calendar.py` (`remove` split; a new `Removal`; a new `_approval_when`)
- Modify: `voice_assistant/actions.py` (`ActionExecutor.__init__` gains `approvals=None`; `remove_from_calendar`; a new `_approved`)
- Test: `tests/test_google_calendar.py`, `tests/test_actions.py`, `tests/test_undo.py`

**Interfaces:**
- Consumes: `Approvals.gate` (Task 3).
- Produces, in `google_calendar.py`:
  - `class Removal(NamedTuple)`: `description: str`, `run: Callable[[], Dict]`.
  - `GoogleCalendar.find_removal(search: str, day=None, at=None, following=False, calendar=None, today=None, now=None) -> Union[Dict, Removal]`: everything `remove` does before writing (the lookup, "which one?", "I can't find ...", a day gone), giving that reply as now when it finds nothing to remove; else a `Removal` whose `run` does exactly what `remove` did with it (`_remove_one`, `_remove_all`, `_and_left`).
  - `remove(...)` keeps its signature and is `find_removal` then `run()`, so nothing else changes.
  - Descriptions, with `_approval_when(event)` giving `"Tue 25 Nov 10:00"` (`"%a %-d %b %H:%M"`, all day `"%a %-d %b"`, in the calendar's local time as `_when_changed` has it): one event `"Remove 'Dentist', Tue 25 Nov 10:00"`; one time of a series from then on `"Remove 'Gym', Tue 25 Nov 18:00, and every time after it"`; several (`_Found` with more than one unit) `"Remove 5 'Gym' events from Sat 22 Nov on"`.
- Produces, in `actions.py`: `ActionExecutor(..., approvals=None)`; `_approved(self, description: str, do: Callable[[], Dict], tool: Callable) -> Dict` - with no approvals, `{"success": False, "message": NO_WAY}` (never runs `do`); else `self.approvals.gate(description, do, self.satellite, done=lambda reply: self._keep_or_note(tool, reply))`, so the removal done after approval is the one "undo that" takes back. `remove_from_calendar` reads the day as `_calendar_event` does, calls `self.calendar.find_removal(...)`, and gives its reply as it is, or `self._approved(found.description, found.run, ActionExecutor.remove_from_calendar)`. `undo_last_change` is unchanged and never asks.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_google_calendar.py`, over its existing fake Google: `test_find_removal_describes_one_event` (`"Remove 'Dentist', Tue 25 Nov 10:00"`, nothing deleted until `run()`); `test_find_removal_describes_a_series_from_then_on`; `test_find_removal_describes_several`; `test_find_removal_answers_no_match_and_which_one_as_before` (the same messages as `remove` gives today, nothing written). The existing `remove` tests stay as they are and must still pass.
  - `tests/test_actions.py`, with a `FakeApprovals` (records `gate` calls; `"approve"` runs `do` and calls `done`; `"wait"` gives the pending words and keeps `do`; `"none"` gives `NO_WAY`): `test_removing_an_event_asks_first_with_what_it_found` (gate's description is the Removal's; nothing removed while it waits); `test_no_match_is_said_without_asking`; `test_with_no_approvals_nothing_is_removed`; `test_the_model_cant_skip_the_approval` (`from_model=True` goes through the same gate); `test_two_removals_in_one_turn_each_ask` (two `execute`s: two gate calls, two descriptions); `test_a_routine_and_a_local_command_ask_too` (the same `Action` run as a routine step and as a local one: gated both times). Update the existing calendar fakes (`RecordingChangeCalendar` and friends) to have `find_removal`, and give the existing remove tests a `FakeApprovals("approve")` so they test what they tested before.
  - `tests/test_undo.py`: `test_undoing_an_approved_removal_needs_no_approval` (remove approved, then "undo that": put back, and `FakeApprovals` saw one gate call only).
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_google_calendar.py tests/test_actions.py tests/test_undo.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** then `TZ_OVERRIDE=UTC` (calendar times), plus `PI_TEST tests/test_conversation.py`, plus ruff.
- [ ] **Step 5: Commit.** "Ask for approval before removing calendar events, naming what was found".

---

### Task 8: A third album needs approval

**Files:**
- Modify: `voice_assistant/downloads.py` (`Downloads.__init__` gains `approvals=None`; `_ask`; a new `_albums_lately`), `voice_assistant/brain.py` (`Downloads(..., approvals=self.approvals)`)
- Test: `tests/test_downloads.py`, `tests/test_conversation.py`

**Interfaces:**
- Consumes: `Approvals.gate` (Task 3); the `approvals` config section.
- Produces: in `Downloads._ask`, after every existing check and the Navidrome look, and only for `wanted.kind == "album"` with `asked` True: if the albums started in the last `trust_minutes` (kept as start times in `self._album_starts`, monotonic, under `self._lock`, appended by `start` for an album) plus this one reach `album_count`, the request goes through `self._approvals.gate(description, do, satellite)` with description `f"Download the album {wanted.name} ({count} albums in {minutes} minutes)"`, and `do` starting it as today and giving `{"success": True, "message": f"Downloading {wanted.name}.", "later": True}` ("already getting it" if it was asked for elsewhere meanwhile). With no approvals, a big one is refused with `NO_WAY`. Songs never count. `"artist"` stays refused as `WHOLE_ARTIST` is today (a comment there says it is big if it is ever allowed).

- [ ] **Step 1: Write the failing tests** in `tests/test_downloads.py`, with Task 7's `FakeApprovals` (move it to `tests/fake_approvals.py` in this task if Task 7 kept it in `test_actions.py`): `test_two_albums_go_ahead_and_the_third_asks` (album 3 within 5 minutes: gate called with `"Download the album Rumours by Fleetwood Mac (3 albums in 5 minutes)"`, not started while waiting); `test_albums_further_apart_than_the_trust_minutes_dont_add_up` (clock moved 301 s); `test_songs_never_count`; `test_the_album_count_is_the_setting` (`album_count` 5: the fifth asks); `test_an_approved_album_starts_and_says_downloading` (`do()` gives `"Downloading Rumours by Fleetwood Mac."`, the watch thread starts); `test_already_had_or_waiting_is_answered_without_asking`. In `tests/test_conversation.py`: `test_a_yes_to_a_third_album_offer_asks_for_approval` (a `request_download` from a yes goes through the gate too).
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_downloads.py tests/test_conversation.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Ask for approval for a third album within the trust minutes".

---

### Task 9: The settings page's approval endpoints

**Files:**
- Modify: `voice_assistant/settings_page.py` (`FILES` gains `/approve.js`; `EDITABLE` gains the three settings; new `SettingsPage` methods; `ACTIONS`; `_Handler.do_GET` and `do_POST`; `save_settings`)
- Test: `tests/test_settings_page.py`

**Interfaces:**
- Consumes: `brain.approvals`, `brain.passkeys` (Task 6), `check_registration`, `from_b64url`, `b64url`, `rp_id`, `NotVerified` (Task 1), `Passkey`, `MOST_PASSKEYS`, `LONGEST_NAME` (Task 2), `rooms.set_can_approve` / `can_approve` / `approves` (Task 2). Read `brain.approvals` with `getattr(..., None)` so the page's existing fake brains still work (each method then says approvals aren't set up).
- Produces:
  - `EDITABLE` entries `("approvals", "trust_minutes", "Minutes one approval lasts", "now")`, `("approvals", "wait_minutes", "Minutes to wait for an answer", "now")`, `("approvals", "album_count", "Albums that make a download need approval", "now")`.
  - In `save_settings`: a change raising any `approvals.*` setting above its running value, while `approvals.ways()` isn't empty, needs `approvals.ask(f"Change '{label}' to {value}")` first; refused or unanswered gives `{"success": False, "message": "That needs your OK first, so nothing was changed."}` and saves nothing.
  - `GET /approve/<32 hex>` serves `approve.html`; `GET /api/approvals` gives `approvals_state()`: `{"passkeys": [{"id", "name", "added"}], "satellites": [{"name", "connected", "can_approve"}]` (each satellite whose `rooms.approves(name)`), `"trusted_until": epoch or null, "recent": approvals.recent()}`; `GET /api/approvals/request?id=` gives `{"state": "open"|"answered"|"expired"|"unknown", "description", "challenge" (b64url, open only), "credentials" [ids], "rp_id"}`.
  - `POST /api/approvals/answer {id, credential_id, client_data, authenticator_data, signature}` (base64url) gives `approvals.answer_by_passkey(...)`; `POST /api/approvals/refuse {id}` gives `refuse_by_phone`.
  - `POST /api/passkeys/begin {}` gives `{"success": True, "challenge", "rp_id", "user_id", "exclude": [ids]}` after asking for approval when a passkey already exists (`ask("Add a passkey")`); the challenge is kept for 2 minutes and used once. `POST /api/passkeys/finish {name, credential_id, client_data, authenticator_data, public_key, algorithm}` checks the name (1 to `LONGEST_NAME` characters, no control characters), `check_registration(challenge, origin, ...)` with `origin` from the request's `Origin` header (which must be `https://`), and adds it; messages `"Added Phone."`, `"Nova keeps at most 3 passkeys."`, `"That passkey didn't check out."`, `"Start again: that took too long."`.
  - `POST /api/passkeys/remove {id}`: asks `ask(f"Remove the passkey '{name}'", without=id)` when `ways(without=id)` isn't empty; the last way can be removed alone. `"Removed Phone."`.
  - `POST /api/approvals/can-approve {name, on}`: switching on asks `ask(f"Let {spoken} approve")` when a way exists; off is free.
  - Tailnet only: in `do_GET` and `do_POST`, through NPM, `/approve/...`, `/api/approvals...`, `/api/passkeys...` and a `/api/settings` body naming an `approvals.` key get 403 `"Approvals are only at the page's Tailscale address."` before anything is read or changed.
  - `page_version` covers the new files (`PAGE_FILES` gains `approve.html`, `approve.js`).

- [ ] **Step 1: Write the failing tests** in `tests/test_settings_page.py`, over its real-server helpers, with a brain fake holding a real `Approvals` (fake clock, `FakeNtfy`), a `PasskeyStore` in `tmp_path` and `FakeAuthenticator` from Task 1:
  - `test_the_approve_page_is_served_for_a_request_id` (and 404 for `/approve/xyz`).
  - `test_an_open_request_is_described_with_its_challenge`; `test_an_answered_one_says_answered_and_gives_no_challenge`.
  - `test_approving_through_the_page_settles_it` (a good assertion posted: `"Approved."`, the gated `do` ran); `test_refusing_through_the_page_needs_no_passkey`.
  - `test_registering_the_first_passkey_needs_no_approval` (begin, finish: stored with the origin from the `Origin` header); `test_a_second_passkey_needs_an_approval` (begin waits on `ask`; refused: no challenge given); `test_a_registration_from_http_is_refused`; `test_a_challenge_is_used_once_and_runs_out`.
  - `test_removing_the_only_way_needs_nothing_and_another_needs_an_approval`.
  - `test_raising_the_trust_minutes_needs_an_approval_and_lowering_doesnt` (and a refusal leaves the file unchanged).
  - `test_switching_can_approve_on_needs_an_approval_and_off_doesnt`.
  - `test_approvals_are_refused_through_npm`: every new GET and POST path, and `/api/settings` with `{"changes": {"approvals.trust_minutes": 1}}`, through the NPM route with a good login: 403 with that message, nothing changed.
  - `test_a_change_needs_the_pages_own_header` (an approval POST without `X-Nova`: 403, as every change).
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_settings_page.py`.
- [ ] **Step 3: Implement.** Create placeholder `voice_assistant/web/approve.html` and `approve.js` (one line each) so `FILES` and `page_version` resolve; Task 10 fills them.
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Approve, refuse and add passkeys from the settings page, at its Tailscale address only".

---

### Task 10: The approval page and the Approvals section

**Files:**
- Modify: `voice_assistant/web/approve.html`, `voice_assistant/web/approve.js` (the phone's page), `voice_assistant/web/settings.html` (a `section-approvals` in the Nova group, after Satellites), `voice_assistant/web/settings.js` (rendering and actions), `voice_assistant/web/settings_list.js` (`SECTIONS` gains `{ id: "approvals", name: "Approvals", group: "nova", icon: "i-key", starts: ["approvals."] }` after Satellites, and `LINES.approvals`), `voice_assistant/web/settings.css` if needed
- Modify (outside the repo, the preview tool): `/home/nathaniel/work/Home-Assistant/.superpowers/tools/page-preview/stub.js` (an `/api/approvals` answer) and `shoot.sh` (`phone-approvals|400|860||approvals` and an approve-page shot)
- Test: `tests/web/approve.test.mjs`, `tests/web/settings_list.test.mjs`, `tests/web/settings_sections.test.mjs`

**Interfaces:**
- Consumes: Task 9's endpoints and shapes.
- Produces:
  - `approve.js` sets `globalThis.NovaApprove = { b64urlToBytes, bytesToB64url, idFromPath }` for the tests, and on load: reads the id from `location.pathname`, GETs `/api/approvals/request`, shows the description with `textContent` and Approve / Refuse; Approve runs `navigator.credentials.get({ publicKey: { challenge, rpId, allowCredentials: credentials.map(id => ({ type: "public-key", id })), userVerification: "required", timeout: 120000 } })` and POSTs the assertion's `id`, `clientDataJSON`, `authenticatorData` and `signature` as base64url with the `X-Nova: page` header; an answered or expired request shows only "Already answered." or "This has run out."; a cancelled passkey prompt leaves the buttons there.
  - The Approvals section (Tailnet only: hidden through NPM, where `/api/login-state` says `route: "npm"`, with the line "Only at the page's Tailscale address"): the passkeys (name, when added, Remove), "Add a passkey" with a name box (`navigator.credentials.create` with `rp: { id: rp_id, name: "Nova" }`, `user: { id: user_id, name: "nova", displayName: "Nova" }`, `pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }]`, `authenticatorSelection: { residentKey: "discouraged", userVerification: "required" }`, `attestation: "none"`, `excludeCredentials`, then `response.getPublicKey()`, `getPublicKeyAlgorithm()`, `getAuthenticatorData()`); the three settings (from `EDITABLE`, saved by the page's existing Save); a "Can approve" switch per satellite in the state; "Recent approvals" (time, description, who, outcome). While a request needing his OK is out, the control says "Check your phone or the PC." until it returns.
  - `LINES.approvals(view)`: `"No passkey yet"` (warn) with none and no satellite that can approve; else `"1 passkey"` / `"2 passkeys"`, with `", the PC"` style additions for satellites that can approve, e.g. `"1 passkey, the PC"`; `""` while not loaded.

- [ ] **Step 1: Write the failing tests.**
  - `tests/web/approve.test.mjs` (the vm stand-in style of `settings_sections.test.mjs`): `base64url round trips bytes`; `the id is read from the path` (`/approve/` + 32 hex gives it; anything else null); `an open request shows its description and both buttons`; `approving posts the assertion as base64url with the page's header`; `an answered request shows only Already answered.`; `a cancelled passkey prompt keeps the buttons`.
  - `tests/web/settings_list.test.mjs`: `the approvals section is listed after satellites`; `approvals line counts passkeys and names who can approve`; `approvals settings keys belong to the approvals section` (`sectionOf("approvals.trust_minutes") === "approvals"`).
  - `tests/web/settings_sections.test.mjs`: `the approvals section is hidden through npm`.
- [ ] **Step 2: Run them to see them fail.** The JS command in Global Constraints.
- [ ] **Step 3: Implement,** in the page's existing look (cards, switches, buttons as the Satellites section has them).
- [ ] **Step 4: Run them to see them pass,** `node --check` on each changed `.js`, and `PI_TEST tests/test_settings_page.py` (the page files' hash).
- [ ] **Step 5: Screenshots.** `NOVA_TREE=/home/nathaniel/work/Home-Assistant-approvals bash /home/nathaniel/work/Home-Assistant/.superpowers/tools/page-preview/shoot.sh`; look at `phone-approvals`, the approve page and `phone-list` in `shots/`, and `shots/console.txt` is empty. Keep them for him.
- [ ] **Step 6: Commit** (repo files only). "Add the approval page and the settings page's Approvals section".

---

### Task 11: The PC's power commands confirm with the chime

**Files:**
- Modify: `voice_assistant/pc.py` (module docstring; drop `ASKED`, `AGREED` and `press`; `act`), `voice_assistant/actions.py` (`pc_power`'s description; drop `press_pc_button`), `voice_assistant/usage.py` only if it names `press_pc_button` (it doesn't today)
- Test: `tests/test_pc.py`, `tests/test_actions.py`, `tests/test_conversation.py`, `tests/test_intents.py`

**Interfaces:**
- Produces: `PCPower.act(action, name=None, defer=False) -> Dict` with no `ask_first` for anything. Every success has `"chime": True` and its words as the message (for the log, and said instead when the chime is switched off): on `"Turning your PC on."`, off `"Shutting down the PC."`, restart `"Restarting the PC."`, sleep `"Putting your PC to sleep."`, screen off `"Turning your PC's screen off."`, lock `"Locking your PC."`. With `defer` (at the PC), off and restart join sleep, screen off and lock: `{"success": True, "message": ..., "chime": True, "after_said": <press>}`, so the chime plays first and the button is pressed after it (Session plays the chime and waits for it before `_run_after`; that order is unchanged). "Your PC's already on/off", "Your PC's off." and a missing button still speak. `pc_power`'s description: "Turn the user's PC on, shut it down, restart it, put it or its screen to sleep, lock it, or say whether it's on. What happens is confirmed for you, so just reply 'Okay.'". `status` keeps speaking.

- [ ] **Step 1: Write the failing tests, and take out the old ones.**
  - `tests/test_pc.py`: replace `test_shutting_down_and_restarting_ask_first_and_press_once_agreed` with `test_shutting_down_and_restarting_press_straight_away_with_the_chime` (the shutdown button pressed, `chime` True, no `ask_first`); `test_every_power_command_confirms_with_the_chime` (on, off, restart, sleep, screen_off, lock); `test_at_the_pc_the_chime_comes_before_the_press` (`defer=True`: nothing pressed until `after_said()` runs, for all five buttons); `test_failures_still_speak` (already off, a missing button, Home Assistant unreachable: no `chime`).
  - `tests/test_actions.py`: replace the `press_pc_button` tests (around lines 2630 to 2676) with `test_pc_power_never_asks_and_press_pc_button_is_gone` (`"press_pc_button" not in ActionExecutor.tools()`; Claude's `pc_power` off gives `confirm == "chime"`).
  - `tests/test_conversation.py`: replace `test_shutting_down_asks_first_and_only_a_yes_does_it`, `test_claudes_shutdown_asks_first_too` and `test_only_a_plain_yes_shuts_the_pc_down` with `test_shut_down_the_pc_just_chimes` (`reply.say == ""`, `reply.sounds == [("success_chime.wav", "general")]`, `listen_again` False) and `test_with_the_chime_off_it_says_shutting_down`.
  - `tests/test_intents.py`: `test_shut_down_bare_asks` renamed `test_shut_down_bare_goes_to_pc_power` with its comment corrected.
- [ ] **Step 2: Run them to see them fail.** `PI_TEST tests/test_pc.py tests/test_actions.py tests/test_conversation.py tests/test_intents.py`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them to see them pass,** plus ruff.
- [ ] **Step 5: Commit.** "Shut down and restart the PC without asking, and confirm every power command with the chime".

---

### Task 12: Decision 0036, the hand checks, and the whole suite

**Files:**
- Create: `docs/decisions/0036-approval-for-big-actions.md` (check `ls docs/decisions` and `git log origin/main -- docs/decisions` for the next free number first; another session may have taken 0036)
- Modify: `docs/decisions/0034-opening-things-and-commands-at-the-computer.md` (a dated paragraph, as its other later changes are: shut down and restart no longer ask; every power command confirms with the chime; the anti-goal "shutting down or restarting without the question and a yes" replaced), `docs/release-checklist.md` (a new "Approval for big actions" section, numbered on from the last check), `docs/roadmap.md` (Phase 6 piece 3 built)

- [ ] **Step 1: Write decision 0036** in 0035's shape: context, what he decided (the list, the not-big, approach 1 and why not 2 and 3, everywhere at once, five minutes, the words), what was built (the service, the phone and its page, the PC and what Task 0 found, the settings and what needs an approval to change, tailnet only, `cryptography` and why the key is taken as SPKI, the third-album rule), my calls, and not done (an outside API, doors and messages, deleting a whole playlist, the Mac's Touch ID).
- [ ] **Step 2: Write the hand checks** in the checklist's style, each marked "Makes sound." where Nova speaks or chimes:
  1. Right after the deploy, before anything else: on the phone, open the settings page at its Tailscale address, Approvals, "Add a passkey", name it "Phone", and complete the fingerprint or PIN. It's listed; "Add a passkey" again now asks for an approval first. Through the NPM address the section says it's only at the Tailscale address.
  2. Makes sound. Approve from the phone: "cancel the dentist" (a test event he makes first). Nova says "That needs your OK. Check your phone or the PC." (or "Check your phone." with the PC off); the notification "Approve?" names the event and time; tap it, Approve, fingerprint: Nova says "Removed ...". "Undo that" puts it back with no approval.
  3. Makes sound. Approve at the PC: the same at the PC with the phone left alone: Hello's prompt shows the description in front of what's open; approve with the finger or PIN. The phone's page, opened after, says "Already answered.".
  4. Makes sound. Both at once, and the trust window: two removals in a row; approve the first on the phone: the PC's prompt goes, both happen, and a third big action within 5 minutes goes ahead without asking. Recent approvals lists each, the second "by trust".
  5. Makes sound. Refuse: on the phone: "Okay, I won't." and the event stays. At the PC, cancel the Hello prompt: the same.
  6. Makes sound. Timeout: ask, answer nothing: after 2 minutes "No answer, so I haven't." and the phone page says "This has run out.". Then "download" three albums within 5 minutes: the third asks.
  7. Makes sound. The chime commands: at the PC, "lock" and "turn the screen off" chime then act; "go to sleep" chimes then sleeps (wake it by hand); "restart the PC" from the Pi chimes and restarts with no question (only when he means it); "turn my PC on" with it asleep chimes. "Is my PC on?" still speaks. With the chime switched off on the page, "lock" says "Locking your PC.".
  8. Read-only: the brain's log after the checks has no description in a plain line and no traceback; `passkeys.json` holds no private key.
- [ ] **Step 3: The whole suite, both time zones, one after the other.** `PI_TEST`, then `TZ_OVERRIDE=UTC PI_TEST`. Expected: everything passes in both. Then ruff, and the JS tests.
- [ ] **Step 4: Commit.** "Decision 0036: approval for big actions" (with the 0034, checklist and roadmap changes).

Deploy is not part of this plan. His named jobs after review: push to GitHub's main; on the brain, install `requirements-backend.txt` (for `cryptography`) and restart `nova-backend`; on the PC, `scripts/deploy_pc.sh` with `requirements-pc.txt` (for the Hello package); then the hand checks above, the passkey first.
