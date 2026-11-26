"""
Update State Test
What a satellite remembers about its updates.
"""

import json
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


def test_a_bad_notes_file_never_stops_a_confirm(tmp_path):
    state = UpdateState(str(tmp_path))
    state.begin("a" * 40, "b" * 40)
    (tmp_path / "notes").write_bytes(b"caf\xe9 \xff\tcmd\n" + b"w" * 500 + b"\t" + b"c" * 500 + b"\n")
    assert state.confirm("a" * 40) is True
    needs = state.result_to_send()["needs_you"]
    assert len(needs) == 2
    assert len(needs[1]["why"]) == 200 and len(needs[1]["command"]) == 200


def test_a_failed_result_is_kept_whole_for_its_version(tmp_path):
    state = UpdateState(str(tmp_path))
    assert state.failed_result("a" * 40) is None
    needs = [{"why": "Local changes.", "command": "git -C ~/Home-Assistant status"}]
    state.note_result({"version": "a" * 40, "ok": False, "error": "It broke.", "needs_you": needs})
    # The whole result, so replaying it keeps the command.
    assert state.failed_result("a" * 40) == {"version": "a" * 40, "ok": False, "error": "It broke.",
                                             "needs_you": needs}
    assert state.failed_result("b" * 40) is None
    # As the installers' scripts write it, and with what makes no sense left out.
    (tmp_path / "failed.json").write_text(json.dumps({"version": "b" * 40, "ok": False, "error": "x"}))
    assert state.failed_result("b" * 40) == {"version": "b" * 40, "ok": False, "error": "x"}
    (tmp_path / "failed.json").write_text(json.dumps({"version": "b" * 40, "ok": True, "error": 5,
                                                      "needs_you": [{"why": 1}, "no"]}))
    assert state.failed_result("b" * 40) == {"version": "b" * 40, "ok": False, "needs_you": []}
    (tmp_path / "failed.json").write_text("{broken")
    assert state.failed_result("b" * 40) is None


def test_a_result_is_cleaned_to_its_own_fields():
    from voice_assistant.update_state import clean_result
    assert clean_result({"version": "a" * 40, "ok": True, "type": "x", "error": "e" * 400,
                         "needs_you": [{"why": "w", "command": "c", "more": 1}, 5]}) == \
        {"version": "a" * 40, "ok": True, "error": "e" * 300, "needs_you": [{"why": "w", "command": "c"}]}
    for junk in ({"ok": True}, {"version": "not a commit", "ok": True}, {"version": "a" * 40, "ok": "yes"}):
        assert clean_result(junk) is None
