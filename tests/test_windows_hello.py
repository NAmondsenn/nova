"""Tests for WindowsHello, with a fake of winrt's parts - the real ones are only on the PC."""

import logging
import sys
import threading
import time

from voice_assistant import windows_hello
from voice_assistant.windows_hello import WindowsHello


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeOperation:
    """One prompt, as Windows would show it: answered once let go, or cancelled."""

    def __init__(self, message, result):
        self.message = message
        self.result = result
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeBackend:
    """
    Stands in for _Winrt: says an availability, gives a result for each prompt, and holds
    each prompt open until let go - keeping which thread each call came on.
    """

    def __init__(self, availability="AVAILABLE", result="VERIFIED", held=False, error=None):
        self._availability = availability
        self.result = result
        self.error = error
        # Set to let the prompts showing answer.
        self.go = threading.Event()
        if not held:
            self.go.set()
        self.operations = []
        self.threads = set()
        self.closed = False

    def _called(self):
        self.threads.add(threading.current_thread().name)

    def set_up(self):
        self._called()

    def availability(self):
        self._called()
        return self._availability

    def request(self, message):
        self._called()
        if self.error is not None:
            raise self.error
        operation = FakeOperation(message, self.result)
        self.operations.append(operation)
        return operation

    def wait(self, operation, seconds):
        self._called()
        if operation.cancelled:
            return "CANCELED"
        if not self.go.wait(seconds):
            return None
        return operation.result

    def close(self):
        self._called()
        self.closed = True


def asked_in_thread(hello, id, description, answers):
    thread = threading.Thread(target=lambda: answers.append((id, hello.ask(id, description))), daemon=True)
    thread.start()
    return thread


def test_off_windows_it_isnt_available_and_imports_nothing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for name in [name for name in sys.modules if name.startswith("winrt")]:
        monkeypatch.delitem(sys.modules, name)
    hello = WindowsHello()
    assert not hello.available
    assert not [name for name in sys.modules if name.startswith("winrt")]
    assert hello.ask("approval-1", "Remove 'Dentist'") is False
    hello.cancel("approval-1")
    hello.close()


def test_verified_is_true_and_the_rest_false():
    for result, approved in [("VERIFIED", True), ("CANCELED", False), ("DEVICE_BUSY", False),
                             ("RETRIES_EXHAUSTED", False), ("DEVICE_NOT_PRESENT", False),
                             ("NOT_CONFIGURED_FOR_USER", False), ("DISABLED_BY_POLICY", False)]:
        backend = FakeBackend(result=result)
        hello = WindowsHello(backend)
        assert hello.available
        assert hello.ask("approval-1", "Remove 'Dentist', Tue 25 Nov 10:00") is approved, result
        assert backend.operations[0].message == "Remove 'Dentist', Tue 25 Nov 10:00"
        hello.close()
        # Every call to Windows on the one thread which owns them.
        assert backend.threads == {"windows-hello"} and backend.closed


def test_the_message_is_cut_to_200_characters():
    backend = FakeBackend()
    hello = WindowsHello(backend)
    hello.ask("approval-1", "x" * 500)
    assert backend.operations[0].message == "x" * 200
    hello.close()


def test_one_prompt_at_a_time():
    backend = FakeBackend(held=True)
    hello = WindowsHello(backend)
    answers = []
    first = asked_in_thread(hello, "approval-1", "First", answers)
    assert wait_for(lambda: len(backend.operations) == 1)
    second = asked_in_thread(hello, "approval-2", "Second", answers)
    time.sleep(0.3)
    # The second waits for the first to be answered.
    assert len(backend.operations) == 1 and answers == []
    backend.go.set()
    first.join(5)
    second.join(5)
    assert [operation.message for operation in backend.operations] == ["First", "Second"]
    # In either order: each asker wakes when its own prompt is answered.
    assert sorted(answers) == [("approval-1", True), ("approval-2", True)]
    hello.close()


def test_cancel_drops_a_queued_one_and_cancels_the_one_showing():
    backend = FakeBackend(held=True)
    hello = WindowsHello(backend)
    answers = []
    showing = asked_in_thread(hello, "approval-1", "First", answers)
    assert wait_for(lambda: len(backend.operations) == 1)
    queued = asked_in_thread(hello, "approval-2", "Second", answers)
    time.sleep(0.2)

    # The queued one ends now, and never shows.
    hello.cancel("approval-2")
    queued.join(5)
    assert ("approval-2", False) in answers and hello.withdrawn("approval-2")

    # The one showing has its prompt cancelled, and gives False.
    hello.cancel("approval-1")
    showing.join(5)
    assert wait_for(lambda: backend.operations[0].cancelled)
    assert ("approval-1", False) in answers and hello.withdrawn("approval-1")
    backend.go.set()
    time.sleep(0.3)
    assert len(backend.operations) == 1
    hello.close()


def test_a_withdraw_before_its_approval_means_no_prompt():
    backend = FakeBackend()
    hello = WindowsHello(backend)
    hello.cancel("approval-1")
    assert hello.ask("approval-1", "Remove 'Dentist'") is False
    assert backend.operations == []
    # Another id still asks.
    assert not hello.withdrawn("approval-2")
    assert hello.ask("approval-2", "Remove 'Dentist'") is True
    hello.close()


def test_unavailable_hello_says_so():
    backend = FakeBackend(availability="NOT_CONFIGURED_FOR_USER")
    hello = WindowsHello(backend)
    assert not hello.available
    assert hello.ask("approval-1", "Remove 'Dentist'") is False
    assert backend.operations == []
    hello.close()


def test_an_availability_which_fails_is_unavailable(caplog):
    class Failing(FakeBackend):
        def availability(self):
            raise OSError("no Hello here")

    with caplog.at_level(logging.INFO, logger=windows_hello.__name__):
        hello = WindowsHello(Failing())
    assert not hello.available
    assert "OSError" in caplog.text
    hello.close()


def test_an_exception_is_false_and_logged_once(caplog):
    backend = FakeBackend(error=OSError("the prompt couldn't be shown"))
    hello = WindowsHello(backend)
    with caplog.at_level(logging.WARNING, logger=windows_hello.__name__):
        assert hello.ask("approval-1", "First") is False
        assert hello.ask("approval-2", "Second") is False
    failures = [record for record in caplog.records if "OSError" in record.getMessage()]
    assert len(failures) == 1
    hello.close()


def test_close_ends_the_one_showing():
    backend = FakeBackend(held=True)
    hello = WindowsHello(backend)
    answers = []
    showing = asked_in_thread(hello, "approval-1", "First", answers)
    assert wait_for(lambda: len(backend.operations) == 1)
    hello.close()
    showing.join(5)
    assert answers == [("approval-1", False)]
    assert wait_for(lambda: backend.operations[0].cancelled and backend.closed)


def test_the_operation_iid_is_worked_out_as_windows_does():
    # By the rule the Windows SDK's tools use for a parameterised interface (a version 5
    # UUID of its signature): IAsyncOperation<Boolean>'s is the one the SDK's headers give,
    # and IAsyncOperation<UserConsentVerificationResult>'s is pinned so a change is seen.
    assert str(windows_hello._parameterised_iid("pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};b1)")) == \
        "cdb5efb3-5788-509d-9be1-71ccb8a3362a"
    assert str(windows_hello.OPERATION_IID) == "fd596ffd-2318-558f-9dbe-d21df43764a5"
