"""
Brain Update Test
Checks the brain's own update: it waits until Nova is free, asks its updater by dropping a
request, reads what the updater says, restarts into the new code, and once it is back says
how it went where he asked - with the waits shortened, and the updater played by the test.
"""

import datetime
import json
import os
import threading
import time

import pytest

from voice_assistant import brain_update as brain_update_module
from voice_assistant.brain_update import BrainUpdate

OLD = "a" * 40
NEW = "b" * 40


def wait_for(condition, seconds=5):
    """Waits for something done on another thread."""
    give_up = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > give_up:
            return False
        time.sleep(0.01)
    return True


class FakeBrain:
    """Stands in for the brain, keeping what was said, offered, kept and restarted."""

    def __init__(self):
        # What busy_reason gives at each look, in turn; once used up, `busy` is given.
        self.looks = []
        self.busy = None
        self.looked = 0
        self.said = []
        self.behind = []
        self.offers = 0
        self.kept = 0
        self.resumed = 0
        self.restarts = 0
        self.service = True
        self._lock = threading.Lock()

    def busy_reason(self):
        with self._lock:
            self.looked += 1
            if self.looks:
                return self.looks.pop(0)
        return self.busy() if callable(self.busy) else self.busy

    def offer_all(self, retry=True):
        self.offers += 1
        return list(self.behind)

    def spoken_name(self, name):
        return {"pi": "the Pi", "pc": "the PC"}.get(name, name)

    def _say_update(self, satellite, message):
        self.said.append((satellite, message))

    def keep_music_for_update(self):
        self.kept += 1

    def resume_kept_music(self):
        self.resumed += 1

    def restart_for_update(self):
        self.restarts += 1
        return self.service


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(brain_update_module, "IDLE_LOOK", 0.01)
    monkeypatch.setattr(brain_update_module, "STATUS_LOOK", 0.01)
    monkeypatch.setattr(brain_update_module, "MINUTE", 0.2)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "update"
    path.mkdir()
    return path


def now_iso(later=0):
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=later)
    return moment.isoformat(timespec="seconds")


def write_status(folder, state, **rest):
    """Writes brain.json as the updater does."""
    status = {"state": state, "from": OLD, "to": NEW, "at": now_iso()}
    status.update(rest)
    (folder / "brain.json.tmp").write_text(json.dumps(status))
    os.replace(folder / "brain.json.tmp", folder / "brain.json")


def updater(folder, *states, **rest):
    """Plays the updater on a thread: waits for the request, takes it, and writes each state in turn."""
    taken = threading.Event()

    def run():
        if not wait_for(lambda: (folder / "request").exists()):
            return
        os.remove(folder / "request")
        taken.set()
        for state in states:
            write_status(folder, state, **(rest if state != "checking" else {}))
            time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()
    return taken


def make(brain, folder, version=OLD, minutes=10, project="/home/nova/Home-Assistant"):
    return BrainUpdate(brain, version, str(folder), lambda: {"idle_wait_minutes": minutes}, project=project)


def test_nothing_is_asked_of_the_updater_while_busy(folder):
    brain = FakeBrain()
    brain.busy = "talking at kitchen"
    update = make(brain, folder)
    assert update.start("kitchen") == "Updating. I'll be back in a minute."
    assert update.running
    assert wait_for(lambda: brain.looked > 5)
    assert not (folder / "request").exists()
    # Free again, so the updater is asked.
    updater(folder, "checking", "current")
    brain.busy = None
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "Already up to date.")]


def test_the_turn_that_asked_holds_it_only_while_it_lasts(folder):
    # The asking turn is busy as it is answered ("thinking", then "speaking" the reply), then
    # free: a moment free part way through isn't taken for free, as it takes two looks in a row.
    brain = FakeBrain()
    brain.looks = ["talking at kitchen", None, "talking at kitchen", None]
    update = make(brain, folder)
    seen = []

    def run():
        assert wait_for(lambda: (folder / "request").exists())
        seen.append(brain.looked)
        os.remove(folder / "request")
        write_status(folder, "current")

    threading.Thread(target=run, daemon=True).start()
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert seen == [5]
    assert brain.said == [("kitchen", "Already up to date.")]


def test_busy_too_long_gives_up_and_says_so(folder):
    brain = FakeBrain()
    brain.busy = "a conversation"
    update = make(brain, folder, minutes=1)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "I couldn't update, as I was busy for 1 minute. Ask again when I'm free.")]
    assert not (folder / "request").exists()
    assert update.outcome()["state"] == "busy"
    # Plural for more than one.
    brain.said.clear()
    update = make(brain, folder, minutes=3)
    update.start("kitchen")
    assert wait_for(lambda: not update.running, 10)
    assert brain.said == [("kitchen", "I couldn't update, as I was busy for 3 minutes. Ask again when I'm free.")]


def test_already_current_updates_the_satellites_behind(folder):
    brain = FakeBrain()
    brain.behind = ["pi"]
    update = make(brain, folder)
    updater(folder, "checking", "current", to=OLD)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.offers == 1
    assert brain.said == [("kitchen", "Already up to date; updating the Pi.")]
    assert update.outcome()["state"] == "current"
    brain.behind, brain.said = ["pi", "pc"], []
    updater(folder, "checking", "current", to=OLD)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "Already up to date; updating the Pi and the PC.")]


def test_already_current_with_none_behind(folder):
    brain = FakeBrain()
    update = make(brain, folder)
    updater(folder, "checking", "current", to=OLD)
    update.start(None)
    assert wait_for(lambda: not update.running)
    assert brain.offers == 1 and brain.said == [(None, "Already up to date.")]
    assert brain.restarts == 0


def test_an_earlier_runs_status_isnt_taken_for_this_ones(folder):
    # brain.json from an earlier run says failed: this run waits for its own answer.
    write_status(folder, "failed", error="Old news", at="2026-11-01T10:00:00+00:00")
    brain = FakeBrain()
    update = make(brain, folder)
    taken = updater(folder, "checking", "current", to=OLD)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert taken.is_set()
    assert brain.said == [("kitchen", "Already up to date.")]


def test_ready_restarts_into_the_new_code_once_idle(folder):
    brain = FakeBrain()
    update = make(brain, folder)
    taken = updater(folder, "checking", "ready")
    free = threading.Event()
    # Busy from the moment the updater has the request: ready, but no restart until it's
    # free, however long that takes.
    brain.busy = lambda: "something ringing" if taken.is_set() and not free.is_set() else None
    update.start("kitchen")
    assert wait_for(lambda: (folder / "brain.json").exists()
                    and json.loads((folder / "brain.json").read_text())["state"] == "ready")
    time.sleep(0.5)
    assert brain.restarts == 0 and brain.kept == 0
    free.set()
    assert wait_for(lambda: brain.restarts == 1)
    assert brain.kept == 1
    assert json.loads((folder / "asked.json").read_text())["satellite"] == "kitchen"
    # Still updating until the brain has gone, so a press meanwhile starts nothing.
    assert update.running
    assert update.start(None) == "I'm already updating."
    time.sleep(0.1)
    assert brain.restarts == 1 and brain.said == []


def test_a_failed_update_says_so_and_doesnt_restart(folder):
    brain = FakeBrain()
    update = make(brain, folder)
    updater(folder, "checking", "failed", error="Couldn't fetch the update from GitHub.", to="")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "The update didn't work, so I'm still on the old version. "
                                      "The settings page says why.")]
    assert brain.restarts == 0
    outcome = update.outcome()
    assert outcome["state"] == "failed" and outcome["reason"] == "Couldn't fetch the update from GitHub."
    # "" is not knowing where it would have gone.
    assert outcome["from"] == OLD and outcome["to"] is None
    # Kept across a restart.
    assert make(brain, folder).outcome() == outcome


def test_needs_you_says_so(folder):
    brain = FakeBrain()
    update = make(brain, folder)
    needs = [{"why": "The brain's code has local changes, so it wasn't updated.",
              "command": "cd /home/nova/Home-Assistant && git status"},
             {"why": "Another thing.", "command": "true"}]
    updater(folder, "checking", "needs_you", error=needs[0]["why"], needs_you=needs)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "The update needs you: the settings page says what to run.")]
    outcome = update.outcome()
    assert outcome["state"] == "needs_you" and outcome["needs_you"] == needs
    assert outcome["reason"] == needs[0]["why"] and outcome["command"] == needs[0]["command"]
    assert brain.restarts == 0


def test_an_updater_which_never_answers_needs_installing(folder, monkeypatch):
    monkeypatch.setattr(brain_update_module, "PICKED_UP_WITHIN", 0.2)
    brain = FakeBrain()
    update = make(brain, folder, project="/srv/nova")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "The update needs you: the settings page says what to run.")]
    outcome = update.outcome()
    assert outcome["state"] == "needs_you"
    assert outcome["reason"] == "The brain's updater isn't installed yet."
    assert outcome["command"] == "sudo bash /srv/nova/update.sh"
    # Taken away, so it doesn't start an update the moment the updater is installed.
    assert not (folder / "request").exists()


def test_not_a_service_cant_restart_and_says_what_to_run(folder):
    brain = FakeBrain()
    brain.service = False
    update = make(brain, folder)
    updater(folder, "checking", "ready")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.restarts == 1
    assert brain.said == [("kitchen", "The update needs you: the settings page says what to run.")]
    outcome = update.outcome()
    assert outcome["state"] == "needs_you"
    assert outcome["reason"] == "Nova isn't running as a service, so it can't restart itself."
    assert outcome["command"] == "sudo systemctl restart nova-backend"
    assert not (folder / "asked.json").exists()


def test_a_second_start_while_updating_doesnt_start_another(folder):
    brain = FakeBrain()
    brain.busy = "an approval"
    update = make(brain, folder)
    assert update.start(None) == "Updating. I'll be back in a minute."
    assert update.start("kitchen") == "I'm already updating."
    starts = []
    for _ in range(5):
        threading.Thread(target=lambda: starts.append(update.start("kitchen"))).start()
    assert wait_for(lambda: len(starts) == 5)
    assert starts == ["I'm already updating."] * 5
    updater(folder, "checking", "current", to=OLD)
    brain.busy = None
    assert wait_for(lambda: not update.running)
    # One run, said where the first start asked (the page: nowhere).
    assert brain.offers == 1 and brain.said == [(None, "Already up to date.")]


def back_from(folder, needs=None, satellite="kitchen"):
    """Leaves what an update which restarted the brain leaves."""
    (folder / "previous").write_text(OLD + "\n")
    write_status(folder, "ready", **({"needs_you": needs} if needs else {}))
    (folder / "asked.json").write_text(json.dumps({"satellite": satellite, "at": time.time()}))


def test_back_on_the_new_code_says_im_updated_where_he_asked(folder):
    back_from(folder)
    brain = FakeBrain()
    update = make(brain, folder, version=NEW)
    update.after_start()
    outcome = update.outcome()
    assert outcome["state"] == "done" and outcome["from"] == OLD and outcome["to"] == NEW
    update.caught_up("bedroom")
    time.sleep(0.1)
    assert brain.said == []
    update.caught_up("kitchen")
    assert wait_for(lambda: brain.said)
    update.caught_up("kitchen")
    time.sleep(0.1)
    assert brain.said == [("kitchen", "I'm updated.")]
    # Kept, marked said, until the new code has run its minute - see below.
    assert json.loads((folder / "asked.json").read_text())["said"] is True


def test_back_with_something_needing_him_says_so_too(folder):
    back_from(folder, needs=[{"why": "The update changes the system's own service files.",
                              "command": "sudo bash /home/nova/Home-Assistant/update.sh --no-pull"}])
    brain = FakeBrain()
    update = make(brain, folder, version=NEW)
    update.after_start()
    assert update.outcome()["command"] == "sudo bash /home/nova/Home-Assistant/update.sh --no-pull"
    update.caught_up("kitchen")
    assert wait_for(lambda: brain.said)
    assert brain.said == [("kitchen", "I'm updated. One thing needs you on the settings page.")]


def test_a_brain_taken_back_shows_it_failed(folder):
    # The take-back moved the code back and noted it; previous is left, as if its removal failed.
    back_from(folder)
    (folder / "taken_back").write_text(OLD + "\n")
    brain = FakeBrain()
    update = make(brain, folder, version=OLD)
    update.after_start()
    outcome = update.outcome()
    assert outcome["state"] == "failed"
    assert outcome["reason"] == "The new code didn't start, so it was taken back."
    assert not (folder / "taken_back").exists() and not (folder / "previous").exists()
    update.caught_up("kitchen")
    assert wait_for(lambda: brain.said)
    assert brain.said == [("kitchen", "The update didn't work, so I'm still on the old version. "
                                      "The settings page says why.")]


def test_back_on_the_previous_code_is_taken_for_taken_back(folder):
    # The take-back moved the code back, but its note never came.
    back_from(folder)
    update = make(FakeBrain(), folder, version=OLD)
    update.after_start()
    assert update.outcome()["state"] == "failed"
    assert update.outcome()["reason"] == "The new code didn't start, so it was taken back."
    assert not (folder / "previous").exists()


def test_the_marker_stays_for_the_first_minute(folder, monkeypatch):
    monkeypatch.setattr(brain_update_module, "STARTED_AFTER", 0.5)
    back_from(folder)
    update = make(FakeBrain(), folder, version=NEW)
    update.after_start()
    time.sleep(0.2)
    assert (folder / "previous").exists()
    assert wait_for(lambda: not (folder / "previous").exists())


def test_an_ordinary_start_says_nothing(folder):
    update = make(FakeBrain(), folder, version=NEW)
    update.after_start()
    assert update.outcome()["state"] is None
    brain = update.brain
    update.caught_up("kitchen")
    time.sleep(0.1)
    assert brain.said == []


def test_a_broken_outcome_reads_as_nothing(folder):
    (folder / "outcome.json").write_text("{not json")
    update = make(FakeBrain(), folder)
    assert update.outcome() == {"state": None, "at": None, "from": None, "to": None, "reason": None,
                                "command": None, "needs_you": []}
    (folder / "outcome.json").write_text("[1, 2]")
    assert update.outcome()["state"] is None


# Round 1 of the review.

def test_a_busy_reason_which_keeps_failing_is_logged_once(folder, caplog):
    brain = FakeBrain()
    fails = [0]

    def busy():
        if fails[0] < 6:
            fails[0] += 1
            raise RuntimeError("no clock")
        return None

    brain.busy = busy
    update = make(brain, folder)
    updater(folder, "checking", "current", to=OLD)
    with caplog.at_level("INFO"):
        update.start("kitchen")
        assert wait_for(lambda: not update.running)
    assert sum("Couldn't tell whether Nova is busy" in record.getMessage() for record in caplog.records) == 1
    assert brain.said == [("kitchen", "Already up to date.")]


def test_new_code_waiting_too_long_to_restart_needs_him_but_still_waits(folder):
    brain = FakeBrain()
    update = make(brain, folder, minutes=1)
    taken = updater(folder, "checking", "ready")
    free = threading.Event()
    brain.busy = lambda: "talking at kitchen" if taken.is_set() and not free.is_set() else None
    update.start("kitchen")
    assert wait_for(lambda: update.outcome()["state"] == "needs_you")
    outcome = update.outcome()
    assert outcome["reason"] == ("The new code is in place, but Nova hasn't been free for 1 minute to restart "
                                 "into it.")
    assert outcome["command"] == "sudo systemctl restart nova-backend"
    # Still waiting, not given up.
    assert update.running and brain.restarts == 0
    free.set()
    assert wait_for(lambda: brain.restarts == 1)
    # Replaced once back on the new code.
    back = make(FakeBrain(), folder, version=NEW)
    (folder / "previous").write_text(OLD + "\n")
    back.after_start()
    assert back.outcome()["state"] == "done"


def test_im_updated_isnt_said_again_and_a_later_take_back_still_says_it_failed(folder):
    back_from(folder)
    brain = FakeBrain()
    update = make(brain, folder, version=NEW)
    update.after_start()
    update.caught_up("kitchen")
    assert wait_for(lambda: brain.said)
    # Kept, marked said, until the new code has run its minute.
    assert json.loads((folder / "asked.json").read_text())["said"] is True
    # The new code crashed and started again within the minute: nothing said twice.
    again = FakeBrain()
    update = make(again, folder, version=NEW)
    update.after_start()
    update.caught_up("kitchen")
    time.sleep(0.1)
    assert again.said == []
    # Then the take-back: the failure is said where he asked, though "I'm updated." was.
    (folder / "taken_back").write_text(OLD + "\n")
    os.remove(folder / "previous")
    last = FakeBrain()
    update = make(last, folder, version=OLD)
    update.after_start()
    update.caught_up("kitchen")
    assert wait_for(lambda: last.said)
    assert last.said == [("kitchen", "The update didn't work, so I'm still on the old version. "
                                     "The settings page says why.")]


def test_asked_goes_with_previous_after_the_first_minute(folder, monkeypatch):
    monkeypatch.setattr(brain_update_module, "STARTED_AFTER", 0.5)
    back_from(folder)
    brain = FakeBrain()
    update = make(brain, folder, version=NEW)
    update.after_start()
    update.caught_up("kitchen")
    assert wait_for(lambda: brain.said)
    assert (folder / "asked.json").exists()
    assert wait_for(lambda: not (folder / "previous").exists())
    assert wait_for(lambda: not (folder / "asked.json").exists())


def test_a_stale_taken_back_doesnt_keep_previous_for_ever(folder, monkeypatch):
    monkeypatch.setattr(brain_update_module, "STARTED_AFTER", 0.3)
    back_from(folder)
    (folder / "taken_back").write_text("c" * 40 + "\n")
    update = make(FakeBrain(), folder, version=NEW)
    update.after_start()
    assert update.outcome()["state"] == "done"
    assert not (folder / "taken_back").exists()
    assert wait_for(lambda: not (folder / "previous").exists())


def test_an_updater_which_stops_without_a_word_failed(folder, monkeypatch):
    monkeypatch.setattr(brain_update_module, "UNHEARD_AFTER", 0.3)
    brain = FakeBrain()
    update = make(brain, folder)
    updater(folder)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    outcome = update.outcome()
    assert outcome["state"] == "failed"
    assert outcome["reason"] == "The brain's updater stopped without saying how it went."
    assert outcome["command"] == "sudo journalctl -u nova-update"
    assert brain.said == [("kitchen", "The update didn't work, so I'm still on the old version. "
                                      "The settings page says why.")]


def test_a_status_written_with_an_earlier_clock_is_still_heard(folder):
    # The last run's brain.json says a later time than now (the clock was put back): the
    # new file is this run's answer all the same.
    write_status(folder, "failed", error="Old news", at=now_iso(later=3600))
    brain = FakeBrain()
    update = make(brain, folder)
    updater(folder, "checking", "current", to=OLD)
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.said == [("kitchen", "Already up to date.")]


def test_a_restart_which_breaks_says_the_new_code_waits_for_the_next(folder):
    brain = FakeBrain()

    def broken():
        raise RuntimeError("no timer")

    brain.restart_for_update = broken
    update = make(brain, folder)
    updater(folder, "checking", "ready")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    outcome = update.outcome()
    assert outcome["state"] == "needs_you"
    assert outcome["reason"] == "The new code is in place, and will start the next time Nova restarts."
    assert outcome["command"] == "sudo systemctl restart nova-backend"
    assert brain.said == [("kitchen", "The update needs you: the settings page says what to run.")]
    assert not (folder / "asked.json").exists()


def test_a_restart_which_breaks_plays_the_kept_music_on(folder):
    brain = FakeBrain()

    def broken():
        raise RuntimeError("no timer")

    brain.restart_for_update = broken
    update = make(brain, folder)
    updater(folder, "checking", "ready")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.kept == 1 and brain.resumed == 1


def test_a_brain_which_isnt_a_service_plays_its_kept_music_on(folder):
    brain = FakeBrain()
    brain.service = False
    update = make(brain, folder)
    updater(folder, "checking", "ready")
    update.start("kitchen")
    assert wait_for(lambda: not update.running)
    assert brain.kept == 1 and brain.resumed == 1


def test_a_restarting_brain_leaves_its_kept_music_for_after(folder):
    brain = FakeBrain()
    update = make(brain, folder)
    updater(folder, "checking", "ready")
    update.start("kitchen")
    assert wait_for(lambda: brain.restarts == 1)
    time.sleep(0.1)
    assert brain.kept == 1 and brain.resumed == 0
