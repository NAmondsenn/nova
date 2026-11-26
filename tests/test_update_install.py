"""
Update Install Test
Checks the bundle part of the Pi's installer: a bundle is only handed to the detached
installer if its head is the commit the brain said, and the installer is the script the
running code carries, copied out of the clone it is about to move.
"""

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
