"""
Update State Test
What a satellite remembers about its updates.
"""

import os

from voice_assistant.update_state import UpdateState


def test_confirm_only_for_the_pending_version(tmp_path):
    state = UpdateState(str(tmp_path))
    assert state.confirm("a" * 40) is False
    state.begin("a" * 40, "p" * 40)
    assert state.confirm("b" * 40) is False
    assert not (tmp_path / "confirmed").exists()
    assert state.confirm("a" * 40) is True
    assert (tmp_path / "confirmed").read_text() == "a" * 40
    assert not (tmp_path / "pending.json").exists()
    assert state.result_to_send() == {"version": "a" * 40, "ok": True, "needs_you": []}


def test_confirm_carries_the_needs_you_notes(tmp_path):
    state = UpdateState(str(tmp_path))
    state.begin("a" * 40, None)
    (tmp_path / "notes").write_text("why one\tcmd one\n\nwhy two\tcmd two\nthree\tc3\nfour\tc4\n")
    assert state.confirm("a" * 40) is True
    needs = state.result_to_send()["needs_you"]
    assert needs[0] == {"why": "why one", "command": "cmd one"}
    assert len(needs) == 3
    assert not (tmp_path / "notes").exists()


def test_a_failure_is_remembered_for_its_version(tmp_path):
    state = UpdateState(str(tmp_path))
    assert state.failed_before("a" * 40) is None
    state.note_result({"version": "a" * 40, "ok": False, "error": "It broke."})
    assert state.failed_before("a" * 40) == "It broke."
    assert state.failed_before("b" * 40) is None
    state.note_result({"version": "b" * 40, "ok": False, "error": "Again."})
    assert state.failed_before("a" * 40) is None
    # A success doesn't clear or overwrite the latest failure.
    state.note_result({"version": "c" * 40, "ok": True})
    assert state.failed_before("b" * 40) == "Again."


def test_a_result_stays_until_sent(tmp_path):
    state = UpdateState(str(tmp_path))
    state.note_result({"version": "a" * 40, "ok": True})
    assert UpdateState(str(tmp_path)).result_to_send() == {"version": "a" * 40, "ok": True}
    state.sent()
    assert state.result_to_send() is None
    state.sent()


def test_a_broken_file_reads_as_nothing_and_pending_clears(tmp_path):
    state = UpdateState(str(tmp_path))
    (tmp_path / "result.json").write_text("{broken")
    (tmp_path / "failed.json").write_text("[1]")
    assert state.result_to_send() is None
    assert state.failed_before("a" * 40) is None
    state.begin("a" * 40, None)
    state.clear_pending()
    assert not os.path.exists(tmp_path / "pending.json")
    assert state.confirm("a" * 40) is False
