"""
Update Install Test
Checks the bundle part of the Pi's installer: a bundle is only handed to the detached
installer if its head is the commit the brain said, and the installer is the script the
running code carries, copied out of the clone it is about to move.
"""

import os
import subprocess

import pytest

from voice_assistant import update_install


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_bundle(tmp_path):
    """A repo with one commit, and the bytes of its bundle; returns (version, bytes)."""
    repo = tmp_path / "brain"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    bundle = tmp_path / "made.bundle"
    git(repo, "bundle", "create", "-q", str(bundle), "HEAD")
    return git(repo, "rev-parse", "HEAD"), bundle.read_bytes()


class Runs:
    """A run stand-in noting each command."""

    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "git":
            return subprocess.run(command, **kwargs)
        return subprocess.CompletedProcess(command, self.code, "", "systemd-run said no")


def test_a_bundle_of_the_right_commit_starts_the_detached_install(tmp_path):
    version, archive = make_bundle(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q")
    folder = tmp_path / "updates"
    run = Runs()
    update_install.install_bundle(str(project), str(folder), version, archive, run=run)
    started = [c for c in run.calls if c[0] == "systemd-run"][0]
    assert started[:4] == ["systemd-run", "--user", "--collect", "--quiet"]
    assert f"nova-update-{version[:12]}" in started
    script = folder / "update_satellite.sh"
    assert script.exists()
    assert started[-6:] == ["/bin/bash", str(script), str(project), version, str(folder / "nova.bundle"),
                            str(folder)][-6:]
    assert (folder / "nova.bundle").read_bytes() == archive


def test_a_bundle_of_another_commit_is_refused_and_starts_nothing(tmp_path):
    version, archive = make_bundle(tmp_path)
    run = Runs()
    with pytest.raises(update_install.InstallRefused) as refused:
        update_install.install_bundle(str(tmp_path), str(tmp_path / "updates"), "b" * 40, archive, run=run)
    assert refused.value.reason == "The update's code isn't the commit it said."
    assert not [c for c in run.calls if c[0] == "systemd-run"]


def test_a_failed_systemd_run_is_refused(tmp_path):
    version, archive = make_bundle(tmp_path)
    with pytest.raises(update_install.InstallRefused):
        update_install.install_bundle(str(tmp_path), str(tmp_path / "updates"), version, archive,
                                      run=Runs(code=1))


def test_a_refused_bundle_is_removed(tmp_path):
    version, archive = make_bundle(tmp_path)
    with pytest.raises(update_install.InstallRefused):
        update_install.install_bundle(str(tmp_path), str(tmp_path / "updates"), "b" * 40, archive, run=Runs())
    assert not (tmp_path / "updates" / "nova.bundle").exists()


def make_zip(version, files=None):
    """A zip of a few files whose comment is a commit, as git archive makes."""
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as made:
        for name, text in (files or {"satellite.py": "x"}).items():
            made.writestr(name, text)
        made.comment = version.encode()
    return buffer.getvalue()


class TaskRuns:
    """A run stand-in for schtasks, noting each command and its keyword arguments."""

    def __init__(self, code=0):
        self.calls = []
        self.kwargs = []
        self.code = code

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        self.kwargs.append(kwargs)
        return subprocess.CompletedProcess(command, self.code, "", "ERROR: no such task")


def test_a_zip_of_the_right_commit_is_unpacked_and_the_task_run(tmp_path):
    import json
    version = "a" * 40
    folder = tmp_path / "update"
    run = TaskRuns()
    update_install.install_zip("p", str(folder), version, "b" * 40, make_zip(version), run=run)
    assert (folder / "new" / "satellite.py").read_text() == "x"
    for name in ("update_pc.ps1", "stop_nova.ps1"):
        assert (folder / name).read_bytes() == open(
            os.path.join(update_install.WINDOWS_SCRIPTS, name), "rb").read()
    assert json.loads((folder / "job.json").read_text()) == {
        "version": version, "previous": "b" * 40, "source": str(folder / "new")}
    assert run.calls == [["schtasks", "/Run", "/TN", "Nova update"]]
    # Never a window on Windows; on this machine the constant isn't there, so 0.
    assert run.kwargs[0]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert not [n for n in os.listdir(folder) if n.endswith(".tmp")]


def test_a_zip_of_another_commit_is_refused_and_runs_nothing(tmp_path):
    folder = tmp_path / "update"
    run = TaskRuns()
    with pytest.raises(update_install.InstallRefused) as refused:
        update_install.install_zip("p", str(folder), "b" * 40, None, make_zip("a" * 40), run=run)
    assert refused.value.reason == "The update's code isn't the commit it said."
    assert run.calls == []
    assert not (folder / "new").exists() and not (folder / "job.json").exists()


def test_no_update_task_needs_a_deploy_by_hand(tmp_path):
    version = "a" * 40
    folder = tmp_path / "update"
    with pytest.raises(update_install.InstallRefused) as refused:
        update_install.install_zip("p", str(folder), version, None, make_zip(version), run=TaskRuns(code=1))
    assert refused.value.needs_you == {"why": "The PC has no update task yet, so it needs one deploy by hand.",
                                       "command": "bash scripts/deploy_pc.sh"}
    # What was written for a task that won't run is not left to be taken for a job.
    assert not (folder / "new").exists() and not (folder / "job.json").exists()


def test_an_unwritable_folder_is_refused_with_a_reason(tmp_path):
    version = "a" * 40
    blocker = tmp_path / "update"
    blocker.write_text("a file, not a folder")
    with pytest.raises(update_install.InstallRefused) as refused:
        update_install.install_zip("p", str(blocker), version, None, make_zip(version), run=TaskRuns())
    assert refused.value.reason.startswith("The update couldn't be prepared")
