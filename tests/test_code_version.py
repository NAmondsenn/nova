"""Tests for finding which commit the code on this machine is, and how it can be updated."""

import subprocess

from voice_assistant import code_version

COMMIT = "c" * 40


def _git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "GIT_COMMITTER_DATE": "2026-11-26T12:00:00", "PATH": "/usr/bin:/bin"})


def _repository(folder):
    """Makes a git repository with one commit, returning its HEAD."""
    _git(folder, "init", "-q")
    (folder / "file").write_text("x")
    _git(folder, "add", "file")
    _git(folder, "commit", "-q", "-m", "first")
    return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_a_commit_file_wins_over_git(tmp_path):
    _repository(tmp_path)
    (tmp_path / "COMMIT").write_text(COMMIT + "\n")
    assert code_version.running_version(str(tmp_path)) == COMMIT


def test_a_repository_gives_its_head(tmp_path):
    head = _repository(tmp_path)
    assert code_version.running_version(str(tmp_path)) == head


def test_neither_commit_file_nor_git_gives_none(tmp_path):
    assert code_version.running_version(str(tmp_path)) is None


def test_a_commit_file_holding_junk_falls_back_to_git(tmp_path):
    head = _repository(tmp_path)
    (tmp_path / "COMMIT").write_text("not a commit\n")
    assert code_version.running_version(str(tmp_path)) == head
    (tmp_path / "COMMIT").write_text("C" * 40)
    assert code_version.running_version(str(tmp_path)) == head


def test_the_update_format_is_bundle_with_a_git_folder_and_none_on_linux_without(tmp_path):
    assert code_version.update_format(str(tmp_path)) is None
    (tmp_path / "COMMIT").write_text(COMMIT)
    # A COMMIT file alone only means a zip install on Windows.
    assert code_version.update_format(str(tmp_path)) is None
    (tmp_path / ".git").mkdir()
    assert code_version.update_format(str(tmp_path)) == "bundle"


def test_the_update_format_is_zip_on_windows_with_a_commit_file(tmp_path, monkeypatch):
    monkeypatch.setattr(code_version.sys, "platform", "win32")
    assert code_version.update_format(str(tmp_path)) is None
    (tmp_path / "COMMIT").write_text(COMMIT)
    assert code_version.update_format(str(tmp_path)) == "zip"


def test_the_commit_date(tmp_path):
    head = _repository(tmp_path)
    assert code_version.commit_date(str(tmp_path), head) == "2026-11-26"
    assert code_version.commit_date(str(tmp_path), COMMIT) is None
    assert code_version.commit_date(str(tmp_path), None) is None


def test_short_is_seven_characters_or_unknown():
    assert code_version.short(COMMIT) == "ccccccc"
    assert code_version.short(None) == "unknown"
