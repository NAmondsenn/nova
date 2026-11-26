"""
Update Archive Test
Checks the brain can make zip and git-bundle archives of its code, and that a satellite
unpacks a zip only if every name in it stays inside the folder it was told to fill.
"""

import io
import os
import subprocess
import zipfile

import pytest

from voice_assistant import update_archive
from voice_assistant.update_archive import ArchiveError


def _git(repo, *arguments):
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments], cwd=repo,
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "brain"
    path.mkdir()
    _git(path, "init", "-q")
    commits = []
    for number in range(1, 4):
        (path / "file.txt").write_text(f"version {number}\n" * 200 + os.urandom(2000).hex())
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"commit {number}")
        commits.append(_git(path, "rev-parse", "HEAD"))
    return str(path), commits


def test_a_zip_carries_its_commit(repo):
    path, commits = repo
    assert update_archive.zip_commit(update_archive.make_zip(path, commits[2])) == commits[2]


def test_a_zip_of_an_older_commit_is_that_commit(repo):
    path, commits = repo
    archive = update_archive.make_zip(path, commits[0])
    assert update_archive.zip_commit(archive) == commits[0]
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        assert opened.read("file.txt").startswith(b"version 1")


def test_a_thin_bundle_from_an_ancestor_fetches_into_a_clone_at_that_ancestor(repo, tmp_path):
    path, commits = repo
    clone = str(tmp_path / "clone")
    _git(path, "clone", "-q", path, clone)
    _git(clone, "reset", "-q", "--hard", commits[0])
    thin = update_archive.make_bundle(path, commits[2], commits[0])
    full = update_archive.make_bundle(path, commits[2], None)
    assert len(thin) < len(full)
    bundle = tmp_path / "thin.bundle"
    bundle.write_bytes(thin)
    _git(clone, "bundle", "verify", str(bundle))
    _git(clone, "fetch", "-q", str(bundle), "HEAD")
    assert _git(clone, "rev-parse", "FETCH_HEAD") == commits[2]


def test_a_base_the_brain_doesnt_have_gives_a_full_bundle(repo):
    path, commits = repo
    full = update_archive.make_bundle(path, commits[2], None)
    assert len(update_archive.make_bundle(path, commits[2], "f" * 40)) == len(full)


def test_a_base_ahead_of_the_brain_gives_a_full_bundle(repo):
    path, commits = repo
    _git(path, "reset", "-q", "--hard", commits[0])
    full = update_archive.make_bundle(path, commits[0], None)
    ahead = update_archive.make_bundle(path, commits[0], commits[2])
    assert len(ahead) == len(full)


def test_a_bundle_is_refused_while_head_isnt_the_version(repo):
    path, commits = repo
    with pytest.raises(ArchiveError):
        update_archive.make_bundle(path, commits[0], None)


def test_a_git_failure_is_an_archive_error(tmp_path):
    with pytest.raises(ArchiveError):
        update_archive.make_zip(str(tmp_path), "f" * 40)


@pytest.mark.parametrize("name", ["/etc/x", "../x", "a/../../x", "C:/x", "a\\x"])
def test_unpack_refuses_unsafe_names_and_writes_nothing(tmp_path, name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fine.txt", "ok")
        archive.writestr(name, "bad")
    into = tmp_path / "into"
    into.mkdir()
    with pytest.raises(ArchiveError):
        update_archive.unpack_zip(buffer.getvalue(), str(into))
    assert list(into.iterdir()) == []
    assert not (tmp_path / "x").exists()


def test_unpack_replaces_what_was_there(tmp_path):
    into = tmp_path / "into"
    (into / "old").mkdir(parents=True)
    (into / "old" / "gone.txt").write_text("old")
    (into / "stale.txt").write_text("old")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sub/new.txt", "new")
    update_archive.unpack_zip(buffer.getvalue(), str(into))
    assert sorted(p.relative_to(into).as_posix() for p in into.rglob("*")) == ["sub", "sub/new.txt"]
    assert (into / "sub" / "new.txt").read_text() == "new"


def test_not_a_zip_has_no_commit():
    assert update_archive.zip_commit(b"not a zip") is None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = b"hello"
    assert update_archive.zip_commit(buffer.getvalue()) is None


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), EOFError(), NotImplementedError()])
def test_unpack_reports_every_way_extracting_can_fail(tmp_path, monkeypatch, error):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "a")

    def fail(self, *arguments, **keywords):
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "extractall", fail)
    with pytest.raises(ArchiveError, match="couldn't unpack"):
        update_archive.unpack_zip(buffer.getvalue(), str(tmp_path / "into"))
