"""
Approvals Module.

Big actions - removing a calendar event, a big music download, raising an approval
setting - need his OK first. The approval service asks every way he has at once: a
notification to his phone, whose page answers with a passkey, and a Windows Hello
prompt on each PC which can approve. The first answer settles the request; any later
one changes nothing and is told "Already answered."

Each request has its own single-use challenge, so an approval for one action can't be
used for another, and everything waiting - requests, challenges and the trust window -
lives in memory only, so a brain restart refuses whatever was waiting rather than
running it. An approval opens a trust window of a few minutes (a setting), in which
anything big goes ahead without asking again, and approves the others still waiting.

The action asking is never held up while it waits: `gate` answers at once with where
to look, and waits on a thread of its own, saying the outcome to the satellite which
asked when it comes - the same way a download says when it's in the library.

Nothing here logs a challenge, an assertion, a credential id or a full request id (the
id is the approval page's address). The description is logged only as a transcript
line, as other words of his are.
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.passkeys import PasskeyStore
from voice_assistant.storage import load_json, save_json
from voice_assistant.webauthn import NotVerified, b64url, check_assertion, from_b64url

logger = logging.getLogger(__name__)

# What's said, and shown on the approval page.
NO_WAY = ("That needs your OK, but there's no way to give it yet. "
          "Add a passkey on Nova's settings page.")
# Said of a change to how approval works when a way to approve is set up but none can be
# asked now - the phone's topic blank and the PC asleep, say: refused, never let through.
CANT_ASK = ("Nova can't reach your phone or the PC to ask: set the phone's ntfy topic or "
            "wake the PC first.")
REFUSED_SAID = "Okay, I won't."
TIMED_OUT_SAID = "No answer, so I haven't."
FAILED_SAID = "That didn't work."
ALREADY = "Already answered."
RUN_OUT = "This has run out."
NO_SUCH = "There's no such request."
CANT_ANSWER = "That passkey can't answer this."
DIDNT_CHECK = "That didn't check out."
APPROVED = "Approved."
REFUSED = "Refused."

# The phone, as it is named in "Check your phone or the PC."
PHONE = "your phone"
# Who refused, for the recent list, when it was the phone's page (refusing needs no passkey).
PHONE_PAGE = "phone"
# Who approved a request the trust window covered.
TRUST = "trust"

# Long enough for "Remove 'Dentist', Tue 25 Nov 10:00, and the 4 after it" with room to
# spare, short enough for a notification and one line of the recent list.
MOST_DESCRIPTION = 200
# The approvals listed on the settings page, kept across restarts.
RECENT_KEPT = 20
# A settled request is still looked up this long, so a late answer is told "Already
# answered." rather than "There's no such request."
KEPT_SETTLED = 3600

# The longest a waiter sleeps before looking at the clock again: the clock is the
# service's own (moved by hand in tests), so the wait can't simply sleep until expiry.
CHECK_EVERY = 1.0

# The settings' own defaults, should the section be missing a value.
TRUST_MINUTES = 5
WAIT_MINUTES = 2

# Outcomes.
OUTCOMES = ("approved", "refused", "timeout", "no_way", "stopped")


def pending_said(asked: Sequence[str]) -> str:
    """What's said while a request waits, naming each way it can be answered."""
    return "That needs your OK. Check " + " or ".join(asked) + "."


def _cut(description: str) -> str:
    """The description, cut to MOST_DESCRIPTION characters with "..." if it's longer."""
    description = str(description)
    if len(description) > MOST_DESCRIPTION:
        return description[:MOST_DESCRIPTION - 3] + "..."
    return description


def _short(id: str) -> str:
    """Enough of a request id to tell requests apart in the log, but not its page's address."""
    return id[:8]


@dataclass
class Request:
    """One big action waiting for his OK."""

    id: str
    challenge: bytes
    description: str
    expires: float  # monotonic
    asked: Tuple[str, ...]  # the ways named when it's said, e.g. ("your phone", "the PC")
    credentials: Tuple[str, ...]  # the passkey ids which may answer it
    approvers: Tuple[str, ...]  # the satellites asked
    outcome: Optional[str] = None
    by: Optional[str] = None
    settled: threading.Event = field(default_factory=threading.Event, repr=False)
    settled_at: Optional[float] = None  # monotonic
    # A passkey id which mustn't answer it. The trust window never covers such a request:
    # removing a passkey needs a fresh answer from another way, not one the passkey gave.
    without: Optional[str] = None
    # A change to how approval itself works - adding a passkey, letting a PC approve,
    # loosening an approval setting - which needs an answer of its own: the trust window
    # never covers it, so a minute-old approval of something else can't loosen the guard.
    fresh: bool = False


class Approvals:
    """Asks for approval every way at once; the first answer settles it.

    Safe to use from any thread: requests are settled under one lock, and everything
    which talks to something else (the phone, a PC, the satellite which asked) is done
    outside it.
    """

    def __init__(self, settings: Callable[[], Dict], passkeys: PasskeyStore, ntfy=None,
                 approvers: Callable[[], List] = lambda: [],
                 guards: Callable[[], bool] = lambda: False,
                 spoken: Callable[[str], str] = lambda name: name,
                 say: Callable[[Optional[str], str], None] = lambda satellite, message: None,
                 log_path: Optional[str] = None, now=time.monotonic, wall=time.time):
        """
        Args:
            settings: Gives the `approvals` settings as they are now, so a change is used straight away.
            passkeys: His passkeys.
            ntfy: The ntfy server, whose `phone` topic is told of each request; None is no phone.
            approvers: Gives the satellites which may approve now. Each has a `name`,
                `ask_approval(id, description, wait)` giving True, False, or None for no
                answer, and `withdraw_approval(id)`.
            guards: Whether any satellite he has let approve is kept, connected or not -
                see guarded.
            spoken: How a satellite is named when it's said, e.g. "the PC".
            say: Says something to a satellite (None for wherever suits).
            log_path: Where the recent approvals are kept; None keeps them in memory only.
            now: Monotonic time, for waits and the trust window.
            wall: Epoch time, for the recent list and the page.
        """
        self._settings = settings
        self._passkeys = passkeys
        self._ntfy = ntfy
        self._approvers = approvers
        self._guards = guards
        self._spoken = spoken
        self._say = say
        self._log_path = log_path
        self._now = now
        self._wall = wall
        self._lock = threading.Lock()
        # Passkey answers one at a time, so a passkey's counter is read, checked and
        # stored before the next answer looks at it.
        self._answer_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._open: Dict[str, Request] = {}
        self._settled: Dict[str, Request] = {}
        self._approved_at: Optional[float] = None
        # Once stopped, every new request is settled "stopped" at once.
        self._stopped = False
        # Per approver name: one prompt showing at a time on each PC.
        self._approver_locks: Dict[str, threading.Lock] = {}
        # Per request id: the approvers whose prompt for it is showing now.
        self._showing: Dict[str, List] = {}
        self._recent: List[Dict] = self._load()

    # The settings.

    def _minutes(self, name: str, default: int) -> int:
        try:
            return int((self._settings() or {}).get(name, default))
        except (TypeError, ValueError):
            return default

    # The trust window.

    def _trust_ends(self) -> Optional[float]:
        """When the trust window closes (monotonic), or None. The caller holds the lock.

        It is worked out from the minutes as they are set now, so lowering them closes
        the window sooner straight away, and raising them lengthens a window already
        open (raising them needs an approval, which opens a fresh one anyway).
        """
        if self._approved_at is None:
            return None
        ends = self._approved_at + self._minutes("trust_minutes", TRUST_MINUTES) * 60
        return ends if self._now() < ends else None

    def trusted(self) -> bool:
        """Whether an approval a moment ago still covers anything big asked now."""
        with self._lock:
            return self._trust_ends() is not None

    def trusted_until(self) -> Optional[float]:
        """When the trust window closes, in epoch seconds for the page, or None if it's closed."""
        with self._lock:
            ends = self._trust_ends()
            return None if ends is None else self._wall() + (ends - self._now())

    # Asking.

    def _phone_keys(self, without: Optional[str]) -> List:
        """The passkeys which may answer, if the phone can be told; else none."""
        if self._ntfy is None or not getattr(self._ntfy, "phone", ""):
            return []
        return [each for each in self._passkeys.all() if each.id != without]

    def _approvers_now(self) -> List:
        try:
            return list(self._approvers() or [])
        except Exception as e:
            logger.error(f"Could not list the satellites which approve ({type(e).__name__})")
            return []

    def ways(self, without: Optional[str] = None) -> List[str]:
        """The ways an approval could be given now, as they're named when said.

        Args:
            without: A passkey id which mustn't count, e.g. the one being removed.
        """
        ways = [PHONE] if self._phone_keys(without) else []
        return ways + [self._spoken(each.name) for each in self._approvers_now()]

    def guarded(self, without: Optional[str] = None) -> bool:
        """Whether a way to approve is set up, reachable now or not: a passkey, or a satellite let approve.

        This, not ways, says whether a change to how approval works needs an approval:
        ways is only who can be asked now, so a blank phone topic or a sleeping PC would
        otherwise make loosening the guard free. A satellite list which can't be read
        counts as guarded, so a fault never opens it.

        Args:
            without: A passkey id which mustn't count, e.g. the one being removed.
        """
        if any(each.id != without for each in self._passkeys.all()):
            return True
        try:
            if self._passkeys.unreadable():
                return True
            return bool(self._guards())
        except Exception as e:
            logger.error(f"Could not tell whether a satellite may approve, so taken as one may ({type(e).__name__})")
            return True

    def request(self, description: str, without: Optional[str] = None, fresh: bool = False) -> Request:
        """Asks for approval of a big action, every way at once, without waiting for it.

        Args:
            description: What it will do, as he'll be shown it; cut to MOST_DESCRIPTION.
            without: A passkey id which mustn't answer it, e.g. the one being removed.
            fresh: Whether it needs an answer of its own, which the trust window never gives.

        Returns:
            The request: already approved by the trust window if it's open (unless
            made `without` a passkey or `fresh`), settled "no_way" if there's no way to approve,
            "stopped" once the service has stopped, else open.
        """
        description = _cut(description)
        keys = self._phone_keys(without)
        approvers = self._approvers_now()
        asked = ([PHONE] if keys else []) + [self._spoken(each.name) for each in approvers]
        request = Request(
            id=secrets.token_hex(16),
            challenge=secrets.token_bytes(32),
            description=description,
            expires=self._now() + self._minutes("wait_minutes", WAIT_MINUTES) * 60,
            asked=tuple(asked),
            credentials=tuple(each.id for each in keys),
            approvers=tuple(each.name for each in approvers),
            without=without,
            fresh=fresh,
        )
        with self._lock:
            if self._stopped:
                self._finish(request, "stopped", None)
            elif without is None and not fresh and self._trust_ends() is not None:
                self._finish(request, "approved", TRUST)
            elif not asked:
                self._finish(request, "no_way", None)
            else:
                self._open[request.id] = request
        if request.outcome is not None:
            self._announce([request])
            return request
        logger.info(f"Approval {_short(request.id)} asked of {len(asked)} way(s)")
        logger.info(f"Approval {_short(request.id)} is for: {description}", extra=TRANSCRIPT)
        if keys:
            newest = max(keys, key=lambda each: each.added)
            try:
                self._ntfy.send_soon(self._ntfy.phone, description, title="Approve?", priority=4,
                                     click=f"{newest.origin}/approve/{request.id}")
            except Exception as e:
                logger.error(f"Could not tell the phone of an approval ({type(e).__name__})")
        for approver in approvers:
            threading.Thread(target=self._prompt, args=(approver, request), name="approval-prompt",
                             daemon=True).start()
        return request

    def _prompt(self, approver, request: Request):
        """Asks one PC, after any prompt already showing there, unless it has been settled meanwhile."""
        with self._lock:
            lock = self._approver_locks.setdefault(approver.name, threading.Lock())
        with lock:
            with self._lock:
                left = request.expires - self._now()
                if request.outcome is not None or left <= 0:
                    return
                self._showing.setdefault(request.id, []).append(approver)
            try:
                answer = approver.ask_approval(request.id, request.description, left)
            except Exception as e:
                logger.error(f"Could not ask {approver.name} for approval ({type(e).__name__})")
                answer = None
            finally:
                with self._lock:
                    # Settled while it showed, the settle withdrew it; else nothing has.
                    withdrawn = request.outcome is not None
                    showing = self._showing.get(request.id, [])
                    if approver in showing:
                        showing.remove(approver)
                    if not showing:
                        self._showing.pop(request.id, None)
        # None is a PC which has gone away or wasn't answered in time: it settles nothing,
        # leaving the phone to answer. Its prompt is taken down all the same unless a settle
        # already did, as on a timeout the wait often runs out here before the waiter
        # settles it, and a prompt left showing could hide the next one.
        if answer is None:
            if not withdrawn:
                self._withdraw([(approver, request.id)])
        elif answer is True:
            self._settle(request, "approved", approver.name)
        elif answer is False:
            self._settle(request, "refused", approver.name)

    def wait(self, request: Request) -> str:
        """Waits until the request is settled, or settles it "timeout" once it has run out; gives the outcome."""
        while not request.settled.is_set():
            left = request.expires - self._now()
            if left <= 0:
                self._settle(request, "timeout", None)
                break
            request.settled.wait(min(left, CHECK_EVERY))
        return request.outcome

    def ask(self, description: str, without: Optional[str] = None, fresh: bool = False) -> bool:
        """Asks and waits, for the settings page. Returns whether it was approved."""
        return self.wait(self.request(description, without, fresh)) == "approved"

    def gate(self, description: str, do: Callable[[], Dict], satellite: Optional[str],
             done: Optional[Callable[[Dict], None]] = None) -> Dict:
        """Runs a big action once it's approved, without holding up whoever asked.

        Args:
            description: What it will do, as he'll be shown it.
            do: Does it, giving a reply with a "message".
            satellite: Where the outcome is said, once it comes.
            done: Given do's reply, once it has run after waiting, e.g. to keep it for "undo that".

        Returns:
            do's reply at once when the trust window is open; a refusal when there's no
            way to approve; else the words saying where to look, while the wait goes on
            on a thread of its own and its outcome is said to the satellite.
        """
        request = self.request(description)
        if request.outcome == "approved":
            return do()
        if request.outcome == "no_way":
            return {"success": False, "message": NO_WAY}
        if request.outcome == "stopped":
            return {"success": False, "message": FAILED_SAID}
        threading.Thread(target=self._carry_out, args=(request, do, satellite, done), name="approval-wait",
                         daemon=True).start()
        return {"success": True, "message": pending_said(request.asked)}

    def _carry_out(self, request: Request, do: Callable[[], Dict], satellite: Optional[str],
                   done: Optional[Callable[[Dict], None]]):
        """Waits for the request, then does the action or says why not."""
        outcome = self.wait(request)
        if outcome == "approved":
            try:
                reply = do()
                if done is not None:
                    done(reply)
                message = reply["message"]
            except Exception as e:
                # The type only: the message could hold the event's name or his words.
                logger.error(f"An approved action failed ({type(e).__name__})")
                message = FAILED_SAID
        elif outcome == "refused":
            message = REFUSED_SAID
        elif outcome == "timeout":
            message = TIMED_OUT_SAID
        else:
            return
        try:
            self._say(satellite, message)
        except Exception as e:
            logger.error(f"Could not say an approval's outcome ({type(e).__name__})")

    # Answering.

    def lookup(self, id: str) -> Optional[Request]:
        """The request with this id: open, or settled within the last hour; else None."""
        with self._lock:
            self._forget_old()
            return self._open.get(id) or self._settled.get(id)

    def _late(self, request: Request) -> Optional[Dict]:
        """The reply to an answer which comes too late, or None if the request is still open.

        A request past its wait which no waiter has settled yet is settled "timeout" now,
        so an answer can't slip in after the wait.
        """
        if request.outcome is None and self._now() >= request.expires:
            self._settle(request, "timeout", None)
        if request.outcome is None:
            return None
        if request.outcome in ("approved", "refused"):
            return {"success": False, "message": ALREADY}
        return {"success": False, "message": RUN_OUT}

    def answer_by_passkey(self, id: str, credential_id: str, client_data_json: bytes,
                          authenticator_data: bytes, signature: bytes) -> Dict:
        """Settles a request approved on the phone's page, once the passkey's assertion checks out.

        Args:
            id: The request's id, from the page's address.
            credential_id: The credential id the authenticator answered with, base64url.
            client_data_json: The browser's client data, as sent.
            authenticator_data: The authenticator data, as sent.
            signature: The assertion's signature.

        Returns:
            {"success", "message"} for the page.
        """
        request = self.lookup(id)
        if request is None:
            return {"success": False, "message": NO_SUCH}
        late = self._late(request)
        if late:
            return late
        try:
            # Written the one way the store writes it, padding or not.
            credential_id = b64url(from_b64url(credential_id))
        except NotVerified:
            return {"success": False, "message": CANT_ANSWER}
        if credential_id not in request.credentials:
            return {"success": False, "message": CANT_ANSWER}
        withdraw: List[Tuple[object, str]] = []
        try:
            return self._check_and_settle(request, credential_id, client_data_json, authenticator_data,
                                          signature, withdraw)
        finally:
            # Outside the answer lock, so a slow PC never holds up another passkey's answer.
            self._withdraw(withdraw)

    def _check_and_settle(self, request: Request, credential_id: str, client_data_json: bytes,
                          authenticator_data: bytes, signature: bytes, withdraw: List) -> Dict:
        """The passkey answer's checks and settling, one answer at a time; withdrawals are left in `withdraw`."""
        with self._answer_lock:
            passkey = self._passkeys.get(credential_id)
            if passkey is None:
                return {"success": False, "message": CANT_ANSWER}
            late = self._late(request)
            if late:
                return late
            try:
                counter = check_assertion(request.challenge, passkey.origin, from_b64url(passkey.public_key),
                                          passkey.algorithm, passkey.counter, client_data_json,
                                          authenticator_data, signature)
            except NotVerified as e:
                # The reason is a few words with nothing secret in it; the request stays open.
                logger.warning(f"Approval {_short(request.id)}: a passkey answer didn't check out ({e})")
                return {"success": False, "message": DIDNT_CHECK}
            try:
                self._passkeys.set_counter(credential_id, counter)
            except OSError as e:
                # The challenge is single-use, so the approval still can't be replayed.
                logger.error(f"Could not keep a passkey's counter ({type(e).__name__})")
            if not self._settle(request, "approved", passkey.name, withdraw):
                return self._late(request) or {"success": False, "message": ALREADY}
        return {"success": True, "message": APPROVED}

    def refuse_by_phone(self, id: str) -> Dict:
        """Settles a request refused on the phone's page, which needs no passkey.

        Returns:
            {"success", "message"} for the page.
        """
        request = self.lookup(id)
        if request is None:
            return {"success": False, "message": NO_SUCH}
        late = self._late(request)
        if late:
            return late
        if not request.credentials:
            return {"success": False, "message": CANT_ANSWER}
        if not self._settle(request, "refused", PHONE_PAGE):
            return self._late(request) or {"success": False, "message": ALREADY}
        return {"success": True, "message": REFUSED}

    def stop(self):
        """Settles everything waiting "stopped", so nothing runs after the brain stops."""
        with self._lock:
            self._stopped = True
            waiting = list(self._open.values())
            for request in waiting:
                self._finish(request, "stopped", None)
            withdraw = self._withdrawals(waiting)
        self._announce(waiting)
        self._withdraw(withdraw)

    # Settling.

    def _settle(self, request: Request, outcome: str, by: Optional[str],
                withdraw_later: Optional[List] = None) -> bool:
        """Settles a request, unless it already is. Returns whether this settled it as asked.

        An answer which comes once the request has run out settles it "timeout" instead
        (and gives False), so a late answer from anywhere neither runs the action nor
        opens the trust window. An approval opens the trust window and approves every
        other request still waiting and not run out, except one made `without` a
        passkey or `fresh`. Each PC showing a prompt for a request settled here has it withdrawn -
        now, or by the caller if it passes `withdraw_later`.
        """
        with self._lock:
            if request.outcome is not None:
                return False
            now = self._now()
            settled = True
            if outcome in ("approved", "refused") and now >= request.expires:
                outcome, by, settled = "timeout", None, False
            self._finish(request, outcome, by)
            finished = [request]
            if outcome == "approved":
                self._approved_at = now
                # With no trust window, an approval covers only its own request.
                if self._minutes("trust_minutes", TRUST_MINUTES) > 0:
                    for other in list(self._open.values()):
                        if now >= other.expires:
                            self._finish(other, "timeout", None)
                        elif other.without is None and not other.fresh:
                            self._finish(other, "approved", TRUST)
                        else:
                            continue
                        finished.append(other)
            withdraw = self._withdrawals(finished)
        self._announce(finished)
        if withdraw_later is None:
            self._withdraw(withdraw)
        else:
            withdraw_later.extend(withdraw)
        return settled

    def _finish(self, request: Request, outcome: str, by: Optional[str]):
        """Marks a request settled and keeps it in the recent list. The caller holds the lock."""
        request.outcome, request.by, request.settled_at = outcome, by, self._now()
        self._open.pop(request.id, None)
        self._settled[request.id] = request
        self._recent.insert(0, {"at": self._wall(), "description": request.description, "by": by,
                                "outcome": outcome})
        del self._recent[RECENT_KEPT:]
        self._forget_old()

    def _withdrawals(self, finished: List[Request]) -> List[Tuple[object, str]]:
        """The PCs showing a prompt for any of these requests. The caller holds the lock."""
        return [(approver, request.id) for request in finished for approver in self._showing.get(request.id, [])]

    def _forget_old(self):
        """Drops settled requests older than KEPT_SETTLED. The caller holds the lock."""
        cutoff = self._now() - KEPT_SETTLED
        for id in [id for id, each in self._settled.items() if each.settled_at < cutoff]:
            del self._settled[id]

    def _announce(self, finished: List[Request]):
        """What's done once requests are settled, outside the lock: wake their waiters, log and save."""
        for request in finished:
            request.settled.set()
            by = f" by {request.by}" if request.by else ""
            logger.info(f"Approval {_short(request.id)}: {request.outcome}{by}")
        self._save()

    def _withdraw(self, withdraw: List[Tuple[object, str]]):
        """Takes down the prompts still showing for settled requests, outside every lock."""
        for approver, id in withdraw:
            try:
                approver.withdraw_approval(id)
            except Exception as e:
                logger.error(f"Could not withdraw a prompt from {approver.name} ({type(e).__name__})")

    # The recent list.

    def recent(self) -> List[Dict]:
        """The last RECENT_KEPT settled requests, newest first: when, what, who answered, and how."""
        with self._lock:
            return [dict(each) for each in self._recent]

    def _load(self) -> List[Dict]:
        """The recent list as last saved; a file which can't be read is logged and read as none."""
        if not self._log_path:
            return []
        try:
            stored = load_json(self._log_path, [])
            if not isinstance(stored, list) or not all(isinstance(each, dict) for each in stored):
                raise ValueError("not a list of approvals")
            return [{"at": each.get("at"), "description": each.get("description"), "by": each.get("by"),
                     "outcome": each.get("outcome")} for each in stored][:RECENT_KEPT]
        except (OSError, ValueError) as e:
            logger.error(f"Could not read the recent approvals, so none are listed ({type(e).__name__})")
            return []

    def _save(self):
        """Keeps the recent list. Saves are one at a time, each of the list as it is then, so the last is the newest."""
        if not self._log_path:
            return
        with self._save_lock:
            with self._lock:
                recent = [dict(each) for each in self._recent]
            try:
                save_json(self._log_path, recent, indent=2)
            except (OSError, TypeError) as e:
                logger.error(f"Could not save the recent approvals ({type(e).__name__})")
