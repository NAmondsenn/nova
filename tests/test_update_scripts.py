"""
Update Scripts Test
Checks scripts/update_satellite.sh against a scratch clone whose update.sh is a stub:
it moves the clone to the brain's commit (forward, or back if the Pi is ahead), runs
update.sh, waits to be told the new code reconnected, and takes the code back, and says
why in result.json, if anything goes wrong. And that setup.sh's needs_you notes a
"needs you" line for the result.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "update_satellite.sh"
SETUP = ROOT / "setup.sh"

# update.sh as the clone has it: notes how it was run, and either confirms, fails, or does nothing.
STUB = """#!/bin/bash
folder=$(dirname "$NOVA_UPDATE_NOTES")
echo "$* from=$NOVA_UPDATED_FROM head=$(git -C "$(dirname "$0")" rev-parse HEAD) hold=$NOVA_UPDATE_HOLD" >> "$folder/stub.log"
case "$STUB_MODE" in
    confirm) git -C "$(dirname "$0")" rev-parse HEAD > "$folder/confirmed" ;;
    dirty) echo mine > "$(dirname "$0")/file" ;;
    fail) echo "first line"; echo "It broke here, at the last step"; exit 3 ;;
esac
"""


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name):
    (repo / "file").write_text(name)
    (repo / "update.sh").write_text(STUB)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def setup_world(tmp_path):
    """A brain repo with commits one, two; the Pi's clone at one; returns the pieces."""
    brain = tmp_path / "brain"
    brain.mkdir()
    git(brain, "init", "-q", "-b", "main")
    one = commit(brain, "one")
    pi = tmp_path / "pi"
    subprocess.run(["git", "clone", "-q", str(brain), str(pi)], check=True)
    two = commit(brain, "two")
    return brain, pi, one, two


def bundle_of(repo, tmp_path, name="nova.bundle"):
    path = tmp_path / name
    git(repo, "bundle", "create", "-q", str(path), "HEAD")
    return path


def run_script(tmp_path, pi, version, bundle, mode="confirm", seed_confirmed=None):
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    systemctl = bin_directory / "systemctl"
    systemctl.write_text(f'#!/bin/bash\necho "$*" >> "{tmp_path / "systemctl.log"}"\n')
    systemctl.chmod(0o755)
    folder = tmp_path / "updates"
    folder.mkdir(exist_ok=True)
    if seed_confirmed:
        (folder / "confirmed").write_text(seed_confirmed)
    env = dict(os.environ, PATH=f"{bin_directory}:{os.environ['PATH']}", NOVA_CONFIRM_WAIT="2",
               STUB_MODE=mode)
    process = subprocess.run(["bash", str(SCRIPT), str(pi), version, str(bundle), str(folder)],
                             env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    result_file = folder / "result.json"
    result = json.loads(result_file.read_text()) if result_file.exists() else None
    return process, folder, result


def calls(tmp_path):
    log = tmp_path / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def test_a_newer_commit_is_fast_forwarded_installed_and_confirmed(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle)
    assert result is None
    assert git(pi, "rev-parse", "HEAD") == two
    assert f"--no-pull from={one} head={two}" in (folder / "stub.log").read_text()
    assert not bundle.exists()


def test_a_pi_ahead_of_the_brain_is_moved_back_to_it(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    git(pi, "pull", "-q")
    three = commit(pi, "three")
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle)
    assert result is None
    assert git(pi, "rev-parse", "HEAD") == two
    assert f"from={three} head={two}" in (folder / "stub.log").read_text()


def test_local_changes_need_you_and_change_nothing(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    (pi / "file").write_text("mine")
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle)
    assert git(pi, "rev-parse", "HEAD") == one
    assert (pi / "file").read_text() == "mine"
    assert result == {"version": two, "ok": False,
                      "error": "The Pi's code has local changes, so it wasn't updated.",
                      "needs_you": [{"why": "The Pi's code has local changes, so it wasn't updated.",
                                     "command": f"cd {pi} && git status"}]}
    assert not (folder / "stub.log").exists()
    assert not bundle.exists()
    # Nothing changed, but the old code is restarted so it reconnects and sends the result.
    assert "--user restart voice-assistant" in calls(tmp_path)


def test_a_clone_gone_its_own_way_needs_you(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    mine = commit(pi, "mine")
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle)
    assert git(pi, "rev-parse", "HEAD") == mine
    assert result["ok"] is False
    assert result["needs_you"] == [{"why": "The Pi's code has gone its own way, so it wasn't updated.",
                                    "command": f"cd {pi} && git log --oneline -3"}]
    assert not (folder / "stub.log").exists()


def test_a_bundle_of_another_commit_changes_nothing(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, "c" * 40, bundle)
    assert git(pi, "rev-parse", "HEAD") == one
    assert result["ok"] is False
    assert not (folder / "stub.log").exists()
    assert not bundle.exists()


def test_a_failed_update_sh_is_reported_with_its_last_line(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="fail")
    assert result["ok"] is False and result["version"] == two
    assert result["error"].endswith("It broke here, at the last step")
    # HEAD was still the new version after the failure, so the script took it back.
    assert git(pi, "rev-parse", "HEAD") == one
    assert "--user restart voice-assistant" in calls(tmp_path)


def test_no_confirmation_takes_the_code_back_and_restarts(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="silent")
    assert git(pi, "rev-parse", "HEAD") == one
    assert "--user restart voice-assistant" in calls(tmp_path)
    assert result == {"version": two, "ok": False,
                      "error": "The new code didn't reconnect to the brain within 3 minutes, so it was taken back."}
    assert not bundle.exists()


def test_needs_you_goes_to_the_notes_file(tmp_path):
    notes = tmp_path / "notes"
    process = subprocess.run(
        ["bash", "-c", f'source "{SETUP}"; export NOVA_UPDATE_NOTES="{notes}"; needs_you "Why it is" "do this"'],
        capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path), NOVA_CONFIG=str(tmp_path / "c")))
    assert "Why it is" in process.stderr
    assert notes.read_text() == "Why it is\tdo this\n"
    again = subprocess.run(["bash", "-c", f'source "{SETUP}"; unset NOVA_UPDATE_NOTES; needs_you a b'],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(tmp_path), NOVA_CONFIG=str(tmp_path / "c")))
    assert again.returncode == 0


def test_a_stale_confirmed_file_does_not_confirm_the_new_code(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="silent", seed_confirmed=two)
    assert result["ok"] is False
    assert git(pi, "rev-parse", "HEAD") == one


def test_a_refused_take_back_says_so_and_still_restarts(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="dirty")
    assert git(pi, "rev-parse", "HEAD") == two
    assert result["error"] == "The new code couldn't be taken back."
    assert result["needs_you"] == [{"why": "The new code couldn't be taken back.",
                                    "command": f"cd {pi} && git reset --keep {one}"}]
    assert "--user restart voice-assistant" in calls(tmp_path)


def test_with_nothing_to_go_back_to_it_does_not_claim_a_take_back(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    git(pi, "pull", "-q")
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="silent")
    assert "taken back" not in result["error"]
    assert "--user restart voice-assistant" in calls(tmp_path)


def test_being_stopped_part_way_takes_the_code_back(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    folder = tmp_path / "updates"
    folder.mkdir()
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    systemctl = bin_directory / "systemctl"
    systemctl.write_text(f'#!/bin/bash\necho "$*" >> "{tmp_path / "systemctl.log"}"\n')
    systemctl.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_directory}:{os.environ['PATH']}", NOVA_CONFIRM_WAIT="30",
               STUB_MODE="silent")
    process = subprocess.Popen(["bash", str(SCRIPT), str(pi), two, str(bundle), str(folder)], env=env,
                               stdin=subprocess.DEVNULL)
    for _ in range(100):
        if (folder / "stub.log").exists():
            break
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    assert git(pi, "rev-parse", "HEAD") == one
    result = json.loads((folder / "result.json").read_text())
    assert result["error"] == "The update was stopped part way, so it was taken back."
    assert "--user restart voice-assistant" in calls(tmp_path)


def test_a_failure_is_kept_in_failed_json_for_the_brains_next_offer(tmp_path):
    from voice_assistant.update_state import UpdateState
    brain, pi, one, two = setup_world(tmp_path)
    bundle = bundle_of(brain, tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle, mode="fail")
    assert UpdateState(str(folder)).failed_before(two) == result["error"]


def test_update_sh_is_told_where_the_satellite_holds_it_off(tmp_path):
    brain, pi, one, two = setup_world(tmp_path)
    process, folder, result = run_script(tmp_path, pi, two, bundle_of(brain, tmp_path))
    assert result is None
    assert f"hold={folder}/hold" in (folder / "stub.log").read_text()
