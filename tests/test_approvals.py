"""Tests for the approval service: asking every way at once, the first answer settling it."""

import logging
import os
import threading
import time

import pytest

from tests.fake_authenticator import ORIGIN, FakeAuthenticator
from voice_assistant import approvals as approvals_module
from voice_assistant.approvals import (ALREADY, NO_WAY, REFUSED_SAID, RUN_OUT, TIMED_OUT_SAID, Approvals,
                                       pending_said)
from voice_assistant.passkeys import Passkey, PasskeyStore
from voice_assistant.webauthn import b64url

PHONE_TOPIC = "phone-topic"


class FakeClock:
    """Monotonic and wall time, moved by hand."""

    def __init__(self):
        self.mono = 1000.0
        self.epoch = 1_800_000_000.0

    def now(self):
        return self.mono

    def wall(self):
        return self.epoch

    def move(self, seconds):
        self.mono += seconds
        self.epoch += seconds


class FakeNtfy:
    def __init__(self, phone=PHONE_TOPIC):
        self.phone = phone
        self.sent = []

    def send_soon(self, topic, message, title="", priority=0, click=""):
        self.sent.append((topic, message, title, priority, click))


class FakeApprover:
    """A PC: records what it was asked and withdrawn; an ask blocks until its event is set."""

    def __init__(self, name, answer=None, delay_event=None):
        self.name = name
        self.answer = answer
        self.event = delay_event or threading.Event()
        if delay_event is None:
            self.event.set()
        self.asks = []
        self.withdrawn = []
        self.showing = 0
        self.most_showing = 0
        self._lock = threading.Lock()

    def ask_approval(self, id, description, wait):
        with self._lock:
            self.asks.append(id)
            self.showing += 1
            self.most_showing = max(self.most_showing, self.showing)
        self.event.wait(5)
        with self._lock:
            self.showing -= 1
            if id in self.withdrawn:
                return None
        return self.answer

    def withdraw_approval(self, id):
        with self._lock:
            self.withdrawn.append(id)
        self.event.set()
        return True


def _until(check, seconds=5.0):
    """Waits for something done on another thread."""
    give_up = time.monotonic() + seconds
    while not check():
        if time.monotonic() > give_up:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def quick_checks(monkeypatch):
    monkeypatch.setattr(approvals_module, "CHECK_EVERY", 0.01)


@pytest.fixture
def made():
    """Every service a test makes, stopped at the end so no thread is left waiting."""
    services = []
    yield services
    for each in services:
        each.stop()


class World:
    """One service and what it talks to."""

    def __init__(self, tmp_path, made, *, passkey=True, approvers=(), ntfy=True, settings=None):
        self.tmp_path = tmp_path
        self.made = made
        self.clock = FakeClock()
        self.ntfy = FakeNtfy() if ntfy else None
        self.approvers = list(approvers)
        self.settings = settings or {"trust_minutes": 5, "wait_minutes": 2, "album_count": 3}
        self.store = PasskeyStore(str(tmp_path / "passkeys.json"))
        self.said = []
        self.auth = None
        if passkey:
            self.auth = self.add_passkey("Phone")
        self.service = self.new_service()

    def add_passkey(self, name):
        auth = FakeAuthenticator()
        assert self.store.add(Passkey(id=b64url(auth.credential_id), name=name, public_key=b64url(auth.public_key_der),
                                      algorithm=auth.algorithm, counter=0, origin=ORIGIN,
                                      added=time.time() + len(self.store.all()))) is None
        return auth

    def new_service(self):
        service = Approvals(lambda: self.settings, self.store, ntfy=self.ntfy, approvers=lambda: self.approvers,
                            spoken=lambda name: "the PC",
                            say=lambda satellite, message: self.said.append((satellite, message)),
                            log_path=str(self.tmp_path / "approvals.json"), now=self.clock.now, wall=self.clock.wall)
        self.made.append(service)
        return service

    def approve(self, request, auth=None, challenge=None):
        auth = auth or self.auth
        assertion = auth.assert_(challenge or request.challenge)
        return self.post(request, assertion)

    def post(self, request, assertion):
        return self.service.answer_by_passkey(request.id, b64url(assertion["credential_id"]),
                                              assertion["client_data_json"], assertion["authenticator_data"],
                                              assertion["signature"])

    def last_id(self):
        return self.ntfy.sent[-1][4].rsplit("/", 1)[1]


class Doing:
    """A big action: counts its runs."""

    def __init__(self, message="Done it."):
        self.runs = 0
        self.message = message

    def __call__(self):
        self.runs += 1
        return {"success": True, "message": self.message}


def test_no_way_refuses_with_the_words(tmp_path, made):
    do = Doing()
    world = World(tmp_path, made, passkey=False)
    assert world.service.gate("Remove 'Dentist'", do, "kitchen") == {"success": False, "message": NO_WAY}
    # A passkey with no phone topic to tell is no way either.
    world = World(tmp_path / "b", made, ntfy=False)
    assert world.service.gate("Remove 'Dentist'", do, "kitchen") == {"success": False, "message": NO_WAY}
    assert do.runs == 0
    assert world.service.request("Remove 'Dentist'").outcome == "no_way"
    assert NO_WAY == ("That needs your OK, but there's no way to give it yet. "
                      "Add a passkey on Nova's settings page.")


def test_the_pending_words_name_each_way(tmp_path, made):
    phone = World(tmp_path / "phone", made)
    assert phone.service.gate("x", Doing(), "kitchen")["message"] == "That needs your OK. Check your phone."
    pc = World(tmp_path / "pc", made, passkey=False, approvers=[FakeApprover("pc", delay_event=threading.Event())])
    assert pc.service.gate("x", Doing(), "kitchen") == {"success": True, "message": "That needs your OK. Check the PC."}
    both = World(tmp_path / "both", made, approvers=[FakeApprover("pc", delay_event=threading.Event())])
    assert both.service.gate("x", Doing(), "kitchen")["message"] == "That needs your OK. Check your phone or the PC."
    assert pending_said(["your phone"]) == "That needs your OK. Check your phone."


def test_the_phone_is_notified_with_a_link(tmp_path, made):
    world = World(tmp_path, made)
    request = world.service.request("Remove 'Dentist', Tue 25 Nov 10:00")
    assert world.ntfy.sent == [(PHONE_TOPIC, "Remove 'Dentist', Tue 25 Nov 10:00", "Approve?", 4,
                                "https://nova.example.ts.net/approve/" + request.id)]
    assert len(request.id) == 32 and len(request.challenge) == 32
    assert request.asked == ("your phone",)
    assert request.credentials == (b64url(world.auth.credential_id),)


def test_an_approval_by_passkey_runs_it_and_says_its_result(tmp_path, made):
    world = World(tmp_path, made)
    do, done = Doing("Downloading Blue."), []
    assert world.service.gate("Download Blue", do, "kitchen", done=done.append)["success"] is True
    request = world.service.lookup(world.last_id())
    assert world.approve(request) == {"success": True, "message": "Approved."}
    _until(lambda: world.said)
    assert world.said == [("kitchen", "Downloading Blue.")]
    assert do.runs == 1
    assert done == [{"success": True, "message": "Downloading Blue."}]
    assert world.store.get(b64url(world.auth.credential_id)).counter == 1
    assert request.outcome == "approved" and request.by == "Phone"


def test_a_bad_assertion_leaves_it_open(tmp_path, made):
    world = World(tmp_path, made)
    request = world.service.request("Remove 'Dentist'")
    assert world.approve(request, challenge=os.urandom(32)) == {"success": False, "message": "That didn't check out."}
    assert world.service.lookup(request.id).outcome is None
    assert world.approve(request) == {"success": True, "message": "Approved."}


def test_a_passkey_not_asked_cant_answer(tmp_path, made):
    world = World(tmp_path, made)
    other = world.add_passkey("Spare")
    request = world.service.request("Remove the spare passkey", without=b64url(other.credential_id))
    assert request.credentials == (b64url(world.auth.credential_id),)
    assert world.approve(request, auth=other) == {"success": False, "message": "That passkey can't answer this."}
    # Nor one which was never added.
    assert world.approve(request, auth=FakeAuthenticator()) == {"success": False,
                                                                 "message": "That passkey can't answer this."}
    assert request.outcome is None
    assert world.approve(request) == {"success": True, "message": "Approved."}


def test_refusal_says_okay_i_wont_and_opens_no_window(tmp_path, made):
    world = World(tmp_path, made)
    do = Doing()
    world.service.gate("Remove 'Dentist'", do, "kitchen")
    assert world.service.refuse_by_phone(world.last_id()) == {"success": True, "message": "Refused."}
    _until(lambda: world.said)
    assert world.said == [("kitchen", "Okay, I won't.")] and REFUSED_SAID == "Okay, I won't."
    assert world.service.trusted() is False
    assert do.runs == 0
    assert world.service.refuse_by_phone(world.last_id()) == {"success": False, "message": ALREADY}
    assert world.service.refuse_by_phone("nope") == {"success": False, "message": "There's no such request."}


def test_no_answer_in_time_says_so(tmp_path, made):
    world = World(tmp_path, made)
    do = Doing()
    world.service.gate("Remove 'Dentist'", do, "kitchen")
    request = world.service.lookup(world.last_id())
    world.clock.move(119)
    time.sleep(0.05)
    assert request.outcome is None
    world.clock.move(2)
    _until(lambda: world.said)
    assert world.said == [("kitchen", "No answer, so I haven't.")] and TIMED_OUT_SAID == "No answer, so I haven't."
    assert request.outcome == "timeout"
    assert world.approve(request) == {"success": False, "message": RUN_OUT}
    assert do.runs == 0


def test_an_answer_after_the_wait_but_before_the_waiter_looks_is_run_out(tmp_path, made):
    world = World(tmp_path, made)
    request = world.service.request("Remove 'Dentist'")
    world.clock.move(121)
    assert world.approve(request) == {"success": False, "message": RUN_OUT}
    assert request.outcome == "timeout"


def test_first_answer_wins_and_the_late_one_is_already_answered(tmp_path, made):
    pc = FakeApprover("pc", answer=True, delay_event=threading.Event())
    world = World(tmp_path, made, approvers=[pc])
    do = Doing()
    world.service.gate("Remove 'Dentist'", do, "kitchen")
    request = world.service.lookup(world.last_id())
    _until(lambda: pc.asks)
    pc.event.set()
    _until(lambda: request.outcome)
    assert world.approve(request) == {"success": False, "message": ALREADY}
    _until(lambda: world.said)
    time.sleep(0.05)
    assert do.runs == 1 and world.said == [("kitchen", "Done it.")]
    assert request.by == "pc"


def test_the_others_are_withdrawn(tmp_path, made):
    pc = FakeApprover("pc", answer=True, delay_event=threading.Event())
    world = World(tmp_path, made, approvers=[pc])
    request = world.service.request("Remove 'Dentist'")
    _until(lambda: pc.asks)
    assert world.approve(request) == {"success": True, "message": "Approved."}
    _until(lambda: pc.withdrawn)
    time.sleep(0.05)
    assert pc.withdrawn == [request.id]
    assert request.by == "Phone"


def test_a_pc_which_goes_away_leaves_the_phone_to_answer(tmp_path, made):
    pc = FakeApprover("pc", answer=None)
    world = World(tmp_path, made, approvers=[pc])
    request = world.service.request("Remove 'Dentist'")
    _until(lambda: pc.asks)
    time.sleep(0.05)
    assert request.outcome is None
    assert world.approve(request) == {"success": True, "message": "Approved."}
    # And with no answer from anywhere it still runs out on time.
    world.settings["trust_minutes"] = 0
    second = world.service.request("Remove 'Gym'")
    _until(lambda: len(pc.asks) == 2)
    world.clock.move(121)
    assert world.service.wait(second) == "timeout"


def test_one_prompt_at_a_time_on_a_pc(tmp_path, made):
    pc = FakeApprover("pc", answer=True, delay_event=threading.Event())
    world = World(tmp_path, made, passkey=False, approvers=[pc])
    first = world.service.request("Remove 'Dentist'")
    second = world.service.request("Remove 'Gym'")
    _until(lambda: pc.asks)
    time.sleep(0.1)
    assert pc.asks == [first.id]
    pc.event.set()
    _until(lambda: second.outcome)
    time.sleep(0.1)
    assert first.outcome == "approved" and first.by == "pc"
    assert second.outcome == "approved" and second.by == "trust"
    assert pc.asks == [first.id] and pc.most_showing == 1


def test_an_approval_approves_the_others_still_waiting(tmp_path, made):
    world = World(tmp_path, made)
    one, two = Doing("Removed it."), Doing("Removed that too.")
    world.service.gate("Remove 'Dentist'", one, "kitchen")
    first = world.service.lookup(world.last_id())
    world.service.gate("Remove 'Gym'", two, "office")
    assert world.approve(first) == {"success": True, "message": "Approved."}
    _until(lambda: len(world.said) == 2)
    assert sorted(world.said) == [("kitchen", "Removed it."), ("office", "Removed that too.")]
    assert one.runs == 1 and two.runs == 1
    recent = world.service.recent()
    assert [(each["description"], each["by"], each["outcome"]) for each in recent] == [
        ("Remove 'Gym'", "trust", "approved"), ("Remove 'Dentist'", "Phone", "approved")]


def test_a_refusal_settles_only_its_own(tmp_path, made):
    world = World(tmp_path, made)
    first = world.service.request("Remove 'Dentist'")
    second = world.service.request("Remove 'Gym'")
    assert world.service.refuse_by_phone(first.id)["message"] == "Refused."
    assert first.outcome == "refused" and second.outcome is None
    assert world.approve(second)["message"] == "Approved."


def test_trust_lasts_the_minutes_set_and_no_more(tmp_path, made):
    world = World(tmp_path, made)
    assert world.approve(world.service.request("Remove 'Dentist'"))["message"] == "Approved."
    assert world.service.trusted_until() == world.clock.wall() + 300
    world.clock.move(299)
    do = Doing("Removed it.")
    assert world.service.gate("Remove 'Gym'", do, "kitchen") == {"success": True, "message": "Removed it."}
    assert do.runs == 1
    assert world.service.recent()[0]["by"] == "trust"
    world.clock.move(2)
    assert world.service.trusted() is False and world.service.trusted_until() is None
    assert world.service.gate("Remove 'Run'", do, "kitchen")["message"] == "That needs your OK. Check your phone."
    assert do.runs == 1
    world.settings["trust_minutes"] = 0
    assert world.approve(world.service.request("Remove 'Swim'"))["message"] == "Approved."
    assert world.service.trusted() is False
    assert world.service.gate("Remove 'Walk'", do, "kitchen")["message"] == "That needs your OK. Check your phone."
    assert do.runs == 1


def test_a_replayed_assertion_settles_nothing_more(tmp_path, made):
    world = World(tmp_path, made, settings={"trust_minutes": 0, "wait_minutes": 2, "album_count": 3})
    first = world.service.request("Remove 'Dentist'")
    assertion = world.auth.assert_(first.challenge)
    assert world.post(first, assertion) == {"success": True, "message": "Approved."}
    assert world.post(first, assertion) == {"success": False, "message": ALREADY}
    second = world.service.request("Remove 'Gym'")
    assert world.post(second, assertion) == {"success": False, "message": "That didn't check out."}
    assert second.outcome is None


def test_stopping_refuses_everything_waiting(tmp_path, made):
    world = World(tmp_path, made)
    do = Doing()
    world.service.gate("Remove 'Dentist'", do, "kitchen")
    request = world.service.lookup(world.last_id())
    world.service.stop()
    assert request.outcome == "stopped"
    time.sleep(0.05)
    assert do.runs == 0 and world.said == []
    assert world.approve(request)["success"] is False
    # Anything asked once it has stopped is stopped too, and never runs.
    assert world.service.request("Remove 'Gym'").outcome == "stopped"
    assert world.service.gate("Remove 'Gym'", do, "kitchen")["success"] is False
    assert do.runs == 0
    # The trust window is in memory only.
    again = world.new_service()
    world.service = again
    assert world.approve(again.request("Remove 'Run'"))["message"] == "Approved."
    assert again.trusted() is True
    assert world.new_service().trusted() is False


def test_a_long_description_is_cut(tmp_path, made):
    world = World(tmp_path, made)
    request = world.service.request("x" * 300)
    assert len(request.description) == 200 and request.description == "x" * 197 + "..."
    assert world.ntfy.sent[-1][1] == request.description


def test_recent_keeps_twenty_across_a_restart(tmp_path, made):
    world = World(tmp_path, made)
    for n in range(25):
        world.clock.move(1)
        world.service.refuse_by_phone(world.service.request(f"Remove {n}").id)
    recent = world.service.recent()
    assert [each["description"] for each in recent] == [f"Remove {n}" for n in range(24, 4, -1)]
    assert recent[0] == {"at": world.clock.wall(), "description": "Remove 24", "by": "phone", "outcome": "refused"}
    assert world.new_service().recent() == recent


def test_a_broken_log_is_read_as_none(tmp_path, made, caplog):
    (tmp_path / "approvals.json").write_text("{not json")
    world = World(tmp_path, made)
    assert world.service.recent() == []


def test_what_do_raises_is_said_not_thrown(tmp_path, made, caplog):
    world = World(tmp_path, made)

    def do():
        raise RuntimeError("the event's secret name")

    world.service.gate("Remove 'Dentist'", do, "kitchen")
    with caplog.at_level(logging.INFO):
        assert world.approve(world.service.lookup(world.last_id()))["message"] == "Approved."
        _until(lambda: world.said)
    assert world.said == [("kitchen", "That didn't work.")]
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
    assert not any("secret name" in record.getMessage() for record in caplog.records)


def test_nothing_secret_is_logged(tmp_path, made, caplog):
    world = World(tmp_path, made)
    with caplog.at_level(logging.DEBUG):
        request = world.service.request("Remove 'Dentist'")
        world.approve(request, challenge=os.urandom(32))
        world.approve(request)
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (request.id, b64url(request.challenge), request.challenge.hex(), b64url(world.auth.credential_id)):
        assert secret not in text
    plain = [record for record in caplog.records if "Dentist" in record.getMessage()]
    assert plain and all(getattr(record, "transcript", False) for record in plain)


def test_a_pc_answering_after_the_wait_runs_nothing_and_opens_no_window(tmp_path, made, monkeypatch):
    pc = FakeApprover("pc", answer=True, delay_event=threading.Event())
    world = World(tmp_path, made, approvers=[pc])
    do = Doing()
    world.service.gate("Remove 'Dentist'", do, "kitchen")
    request = world.service.lookup(world.last_id())
    _until(lambda: pc.asks)
    # The waiter is asleep, so the PC's answer comes before it looks at the clock again.
    monkeypatch.setattr(approvals_module, "CHECK_EVERY", 60)
    time.sleep(0.05)
    world.clock.move(121)
    pc.event.set()
    _until(lambda: request.outcome)
    assert request.outcome == "timeout"
    assert world.service.trusted() is False
    assert do.runs == 0
    pc.answer = None
    assert world.service.request("Remove 'Gym'").outcome is None


def test_an_approval_doesnt_approve_others_past_their_wait(tmp_path, made):
    world = World(tmp_path, made)
    old = world.service.request("Remove 'Dentist'")
    world.clock.move(60)
    new = world.service.request("Remove 'Gym'")
    world.clock.move(61)
    assert world.approve(new)["message"] == "Approved."
    assert old.outcome == "timeout"


def test_the_trust_window_never_covers_a_request_without_a_passkey(tmp_path, made):
    world = World(tmp_path, made)
    spare = world.add_passkey("Spare")
    spare_id = b64url(spare.credential_id)
    # The spare approves something, then its own removal still asks the other passkey.
    assert world.approve(world.service.request("Remove 'Dentist'"), auth=spare)["message"] == "Approved."
    assert world.service.trusted() is True
    removal = world.service.request("Remove the passkey Spare", without=spare_id)
    assert removal.outcome is None
    assert world.approve(removal, auth=spare)["message"] == "That passkey can't answer this."
    assert world.approve(removal)["message"] == "Approved."


def test_an_approval_leaves_a_request_without_its_passkey_waiting(tmp_path, made):
    world = World(tmp_path, made, settings={"trust_minutes": 5, "wait_minutes": 2, "album_count": 3})
    spare = world.add_passkey("Spare")
    other = world.service.request("Remove 'Dentist'")
    removal = world.service.request("Remove the passkey Spare", without=b64url(spare.credential_id))
    assert world.approve(other, auth=spare)["message"] == "Approved."
    assert removal.outcome is None


def test_a_slow_withdraw_holds_up_no_other_passkey_answer(tmp_path, made):
    pc = FakeApprover("pc", answer=None, delay_event=threading.Event())
    release = threading.Event()
    withdraw = pc.withdraw_approval

    def slow_withdraw(id):
        release.wait(5)
        return withdraw(id)

    pc.withdraw_approval = slow_withdraw
    world = World(tmp_path, made, approvers=[pc], settings={"trust_minutes": 0, "wait_minutes": 2, "album_count": 3})
    first = world.service.request("Remove 'Dentist'")
    second = world.service.request("Remove 'Gym'")
    _until(lambda: pc.asks)
    answered = []
    threading.Thread(target=lambda: answered.append(world.approve(first)), daemon=True).start()
    _until(lambda: first.outcome)
    # The first answer is stuck withdrawing the PC's prompt; the second goes through.
    assert world.approve(second)["message"] == "Approved."
    assert answered == []
    release.set()
    _until(lambda: answered)
    assert answered == [{"success": True, "message": "Approved."}]
