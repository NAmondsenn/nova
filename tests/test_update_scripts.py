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
import shlex
import time
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------------------
# scripts/update_brain.sh and the units around it
# ---------------------------------------------------------------------------------------

BRAIN_SCRIPT = ROOT / "scripts" / "update_brain.sh"

# update.sh as the brain's clone has it: notes how it was run, and passes, fails, or hangs.
BRAIN_STUB = """#!/bin/bash
echo "$* from=$NOVA_UPDATED_FROM head=$(git -C "$(dirname "$0")" rev-parse HEAD)" >> "$NOVA_DATA/stub.log"
case "$STUB_MODE" in
    fail) echo "first line"; echo "It broke here, at the last step"; exit 3 ;;
    hang) sleep 30 ;;
esac
"""

SETUP_STUB = "install_system_packages() {\n    apt-get install -y python3-venv\n}\n\nother() {\n    :\n}\n"


def brain_commit(repo, name, files=None):
    (repo / "file").write_text(name)
    (repo / "update.sh").write_text(BRAIN_STUB)
    if not (repo / "setup.sh").exists():
        (repo / "setup.sh").write_text(SETUP_STUB)
    for path, text in (files or {}).items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def brain_world(tmp_path):
    """A bare origin with main at two commits, and the brain's clone on main at the first."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    one = brain_commit(seed, "one")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return seed, origin, clone, one


def publish(seed, origin, name, files=None):
    """Adds a commit to the world's main, and returns it."""
    new = brain_commit(seed, name, files)
    git(seed, "push", "-q", str(origin), "main")
    return new


def run_brain_script(tmp_path, clone, mode="", signal_after=None):
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True, exist_ok=True)
    (data / "update" / "request").write_text("")
    env = dict(os.environ, NOVA_DATA=str(data), HOME=str(tmp_path), STUB_MODE=mode)
    process = subprocess.run(["bash", str(BRAIN_SCRIPT)], cwd=clone, env=env, capture_output=True,
                             text=True, stdin=subprocess.DEVNULL)
    status_file = data / "update" / "brain.json"
    status = json.loads(status_file.read_text()) if status_file.exists() else None
    return process, data / "update", status


def test_the_brain_already_on_main_is_current(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "current" and status["from"] == one and status["to"] == one
    assert "T" in status["at"]
    assert not (folder / "request").exists()
    assert not (folder / "previous").exists()


def test_a_newer_main_is_fast_forwarded_installed_and_ready(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    two = publish(seed, origin, "two")
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "ready", process.stderr
    assert (status["from"], status["to"]) == (one, two) and "needs_you" not in status
    assert git(clone, "rev-parse", "HEAD") == two
    assert (folder / "previous").read_text().strip() == one
    assert not (folder / "request").exists()
    log = (tmp_path / "data" / "stub.log").read_text()
    assert f"--no-pull --no-restart from={one} head={two}" in log
    assert not list(folder.glob("*.tmp"))


def test_a_failed_install_is_reported_and_leaves_the_old_code(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two")
    process, folder, status = run_brain_script(tmp_path, clone, mode="fail")
    assert status["state"] == "failed" and status["from"] == one
    assert "It broke here, at the last step" in status["error"]
    assert git(clone, "rev-parse", "HEAD") == one
    assert not (folder / "previous").exists()


def test_the_brains_local_changes_need_you_and_change_nothing(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two")
    (clone / "file").write_text("mine")
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "needs_you"
    assert status["needs_you"] == [{"why": "The brain's code has local changes, so it wasn't updated.",
                                    "command": f"cd {clone} && git status"}]
    assert git(clone, "rev-parse", "HEAD") == one and (clone / "file").read_text() == "mine"
    assert not (tmp_path / "data" / "stub.log").exists()


def test_a_branch_other_than_main_needs_you(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two")
    git(clone, "checkout", "-q", "-b", "work")
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "needs_you"
    assert status["needs_you"][0]["why"] == "The brain's code is on work, not main."
    assert status["needs_you"][0]["command"] == f"cd {clone} && git checkout main"
    assert git(clone, "rev-parse", "HEAD") == one


def test_a_diverged_clone_needs_you(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two")
    mine = brain_commit(clone, "mine")
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "needs_you"
    assert status["needs_you"][0]["why"] == "The brain's code has gone its own way, so it wasn't updated."
    assert status["needs_you"][0]["command"] == f"cd {clone} && git log --oneline -3"
    assert git(clone, "rev-parse", "HEAD") == mine


def test_a_changed_unit_file_is_ready_and_needs_you_for_root(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    two = publish(seed, origin, "two", {"nova-update.path": "changed"})
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "ready" and git(clone, "rev-parse", "HEAD") == two
    assert len(status["needs_you"]) == 1
    assert status["needs_you"][0]["command"] == f"sudo bash {clone}/update.sh --no-pull"
    assert (folder / "previous").read_text().strip() == one


def test_a_changed_system_package_list_needs_you_for_root(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two", {"setup.sh": SETUP_STUB.replace("python3-venv", "python3-venv sox")})
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "ready"
    assert status["needs_you"][0]["command"] == f"sudo bash {clone}/update.sh --no-pull"


def test_a_change_elsewhere_in_setup_sh_needs_no_root(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two", {"setup.sh": SETUP_STUB.replace("    :\n", "    echo hi\n")})
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "ready" and "needs_you" not in status


def test_an_update_stopped_part_way_is_taken_back(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    publish(seed, origin, "two")
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True)
    (data / "update" / "request").write_text("")
    env = dict(os.environ, NOVA_DATA=str(data), HOME=str(tmp_path), STUB_MODE="hang")
    process = subprocess.Popen(["bash", str(BRAIN_SCRIPT)], cwd=clone, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        for _ in range(100):
            if (data / "stub.log").exists():
                break
            time.sleep(0.1)
        assert (data / "stub.log").exists()
        # The whole group, as systemd stops a unit: the script and the step it is waiting on.
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    status = json.loads((data / "update" / "brain.json").read_text())
    assert status["state"] == "failed"
    assert status["error"] == "The update was stopped part way, so it was taken back."
    assert git(clone, "rev-parse", "HEAD") == one


def test_a_refused_take_back_needs_you(tmp_path):
    seed, origin, clone, one = brain_world(tmp_path)
    # The new update.sh fails after editing a file the new commit also changed, so the
    # reset back to where it was is refused rather than throwing the edit away.
    publish(seed, origin, "two", {"update.sh": BRAIN_STUB + 'echo mine > "$(dirname "$0")/file"\nexit 3\n'})
    process, folder, status = run_brain_script(tmp_path, clone)
    assert status["state"] == "needs_you"
    assert status["needs_you"][0]["command"] == f"cd {clone} && git reset --keep {one}"


def unit(name):
    return (ROOT / name).read_text()


def test_the_updater_starts_only_on_its_request_file():
    text = unit("nova-update.path")
    assert "PathExists=__HOME__/.local/share/nova/update/request\n" in text
    assert "Unit=nova-update.service\n" in text and "WantedBy=multi-user.target\n" in text


def test_the_updater_is_outside_the_brains_sandbox_but_never_root():
    text = unit("nova-update.service")
    for line in ("Type=oneshot", "User=__USER__", "WorkingDirectory=__PROJECT_DIRECTORY__",
                 "ExecStart=/bin/bash __PROJECT_DIRECTORY__/scripts/update_brain.sh", "UMask=0077",
                 "NoNewPrivileges=yes", "TimeoutStartSec=1800"):
        assert f"\n{line}\n" in text
    assert "\nProtectSystem" not in text and "\nProtectHome" not in text
    assert "ProtectSystem" in text and "ProtectHome" in text  # the comment saying why


def test_the_brain_takes_back_after_three_failed_starts():
    text = unit("nova-backend.service")
    unit_section = text[text.index("[Unit]"):text.index("[Service]")]
    for line in ("StartLimitIntervalSec=600", "StartLimitBurst=3", "OnFailure=nova-takeback.service"):
        assert f"\n{line}\n" in unit_section
    # The lock stays.
    for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=read-only"):
        assert f"\n{line}\n" in text


def _takeback_run(tmp_path, previous):
    """Runs the take-back unit's inline script as this user, against a clone and a data folder."""
    text = unit("nova-takeback.service")
    line = next(row for row in text.replace("\\\n", " ").splitlines()
                if row.startswith("ExecStart=-/usr/sbin/runuser"))
    assert line.startswith("ExecStart=-/usr/sbin/runuser -u __USER__ -- /bin/sh -c '")
    inline = line.split("/bin/sh -c ", 1)[1]
    script = shlex.split(inline)[0].replace("$$", "$")
    seed, origin, clone, one = brain_world(tmp_path)
    two = publish(seed, origin, "two")
    git(clone, "pull", "-q")
    data = tmp_path / "home" / ".local" / "share" / "nova" / "update"
    data.mkdir(parents=True)
    if previous is not None:
        (data / "previous").write_text(previous.replace("ONE", one))
    script = script.replace("__HOME__", str(tmp_path / "home")).replace("__PROJECT_DIRECTORY__", str(clone))
    done = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, cwd=tmp_path)
    return done, data, clone, one, two


def test_the_take_back_only_takes_a_commit_id_and_starts_the_brain_again(tmp_path):
    text = unit("nova-takeback.service")
    assert "\nType=oneshot\n" in text
    assert "\nExecStart=/usr/bin/systemctl reset-failed nova-backend.service\n" in text
    assert "\nExecStart=/usr/bin/systemctl start --no-block nova-backend.service\n" in text
    assert text.index("reset-failed") < text.index("start --no-block")
    done, data, clone, one, two = _takeback_run(tmp_path, "ONE\n")
    assert done.returncode == 0, done.stderr
    assert git(clone, "rev-parse", "HEAD") == one
    assert (data / "taken_back").read_text().strip() == one and not (data / "previous").exists()


@pytest.mark.parametrize("hostile", ["--hard\n", "HEAD~1\n", "abc123\n", "$(touch pwned)\n", "ONE;touch pwned\n",
                                     "ONEONE\n", "ONE\nONE\n"[:0] + "\n", "ABCDEF" + "0" * 34 + "\n"])
def test_the_take_back_refuses_anything_but_forty_hex_digits(tmp_path, hostile):
    done, data, clone, one, two = _takeback_run(tmp_path, hostile)
    assert git(clone, "rev-parse", "HEAD") == two
    assert not (data / "taken_back").exists() and not (tmp_path / "pwned").exists()


def test_a_refused_take_back_writes_nothing_and_keeps_previous(tmp_path):
    done, data, clone, one, two = _takeback_run(tmp_path, "0" * 40 + "\n")
    assert git(clone, "rev-parse", "HEAD") == two
    assert not (data / "taken_back").exists() and (data / "previous").exists()


def test_the_take_back_doesnt_follow_a_link(tmp_path):
    done, data, clone, one, two = _takeback_run(tmp_path, None)
    (data / "elsewhere").write_text(one + "\n")
    (data / "previous").symlink_to(data / "elsewhere")
    again = subprocess.run(["/bin/sh", "-c", done.args[2]], capture_output=True, text=True, cwd=tmp_path)
    assert again.returncode == 0 and git(clone, "rev-parse", "HEAD") == two
    assert not (data / "taken_back").exists()
