"""
Updates Test
Checks the brain offers each satellite on another commit its own, proved with that
connection's nonces, and keeps how each satellite's update is going for the settings page.
"""

import os
import subprocess

import pytest

from voice_assistant import protocol, update_archive
from voice_assistant.updates import Updates

NONCES = ("a" * 32, "b" * 32)


def _git(repo, *arguments):
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments], cwd=repo,
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commit(path, number):
    (path / "file.txt").write_text(f"version {number}\n" * 200 + os.urandom(2000).hex())
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"commit {number}")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "brain"
    path.mkdir()
    _git(path, "init", "-q")
    return path, [_commit(path, number) for number in range(1, 4)]


class FakeSatellite:
    """A satellite which keeps what it is sent."""

    def __init__(self, name="pi", version=None, updates=True, format="bundle", nonces=NONCES, sends=True):
        self.name, self.version, self.updates = name, version, updates
        self.update_format, self.nonces = format, nonces
        self.sent = []
        self.sends = sends

    def send_update(self, version, format, archive, proof, retry):
        self.sent.append((version, format, archive, proof, retry))
        return self.sends


def make(repo, tmp_path, password="secret"):
    path, commits = repo
    return Updates(str(path), commits[2], lambda name: password if name != "stranger" else None,
                   str(tmp_path / "update")), commits


def test_a_satellite_on_another_commit_is_offered_the_brains(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    satellite = FakeSatellite(version=commits[0], format="zip")
    assert updates.offer(satellite, retry=True) is True
    (version, format, archive, proof, retry), = satellite.sent
    assert (version, format, retry) == (commits[2], "zip", True)
    assert update_archive.zip_commit(archive) == commits[2]
    assert proof == protocol.update_proof("secret", *NONCES, commits[2], "zip", archive)


def test_a_satellite_on_the_brains_commit_isnt_offered(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    satellite = FakeSatellite(version=commits[2])
    assert updates.offer(satellite) is False
    assert satellite.sent == []


def test_an_old_satellite_isnt_offered_and_shows_as_old(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    satellite = FakeSatellite(version=None, updates=False, format=None)
    updates.seen(satellite)
    assert updates.offer(satellite) is False
    assert satellite.sent == []
    only, = updates.versions(["pi"])["satellites"]
    assert (only["state"], only["reason"]) == ("old", "Update by hand once")


def test_no_offer_while_the_brains_code_has_moved_under_it(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    _commit(repo[0], 4)
    assert updates.paused()
    satellite = FakeSatellite(version=commits[0])
    assert updates.offer(satellite) is False
    assert satellite.sent == []


def test_no_offer_without_a_password(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    satellite = FakeSatellite(name="stranger", version=commits[0])
    assert updates.offer(satellite) is False
    assert satellite.sent == []


def test_a_bundle_is_thin_from_the_satellites_commit(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    satellite = FakeSatellite(version=commits[1])
    assert updates.offer(satellite) is True
    archive = satellite.sent[0][2]
    assert archive == update_archive.make_bundle(repo[0], commits[2], commits[1])
    assert len(archive) < len(update_archive.make_bundle(repo[0], commits[2], None))


def test_an_archive_which_cant_be_made_marks_the_satellite_failed(repo, tmp_path, monkeypatch):
    updates, commits = make(repo, tmp_path)

    def broken(*arguments):
        raise update_archive.ArchiveError("git said no")

    monkeypatch.setattr("voice_assistant.updates.make_bundle", broken)
    satellite = FakeSatellite(version=commits[0])
    assert updates.offer(satellite) is False
    only, = updates.versions(["pi"])["satellites"]
    assert only["state"] == "failed" and only["reason"]


def test_a_zip_is_made_once_and_only_the_last_four_bundles_are_kept(repo, tmp_path, monkeypatch):
    updates, commits = make(repo, tmp_path)
    made = []
    real = update_archive.make_zip
    monkeypatch.setattr("voice_assistant.updates.make_zip", lambda *arguments: made.append(1) or real(*arguments))
    for name in ("pi", "pc"):
        updates.offer(FakeSatellite(name=name, version=commits[0], format="zip"))
    assert len(made) == 1
    for base in (None, commits[0], commits[1], None, commits[0], commits[1]):
        updates._archive("bundle", base)
    assert len(updates._bundles) <= 4


def test_results_show_as_states(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    for name in ("pi", "pc", "mac", "tv"):
        updates.seen(FakeSatellite(name=name, version=commits[0]))
    updates.result("pi", {"version": commits[2], "ok": True})
    updates.result("pc", {"version": commits[2], "ok": True,
                          "needs_you": [{"why": "Python is old", "command": "winget upgrade python"}]})
    updates.result("mac", {"version": commits[0], "ok": False, "error": "It wouldn't start"})
    updates.result("tv", {"version": commits[2], "ok": True, "needs_you": []})
    by_name = {each["name"]: each for each in updates.versions(["pi", "pc", "mac", "tv"])["satellites"]}
    assert by_name["pi"]["state"] == "current"
    assert by_name["pc"]["state"] == "needs_you"
    assert (by_name["pc"]["reason"], by_name["pc"]["command"]) == ("Python is old", "winget upgrade python")
    assert (by_name["mac"]["state"], by_name["mac"]["reason"]) == ("failed", "It wouldn't start")
    assert by_name["tv"]["state"] == "current"


def test_installing_and_an_offer_show_as_updating(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    updates.installing("pi", commits[2])
    assert updates.versions(["pi"])["satellites"][0]["state"] == "updating"


def test_a_satellite_away_is_still_listed(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    updates.seen(FakeSatellite(version=commits[0]))
    assert updates.behind() == ["pi"]
    updates.gone("pi")
    assert updates.behind() == []
    # A new brain reads the file back, so a satellite it has not heard from yet is listed.
    again = Updates(str(repo[0]), commits[2], lambda name: None, str(tmp_path / "update"))
    only, = again.versions(["pi", "never"])["satellites"][:1]
    assert (only["version"], only["connected"], only["state"]) == (commits[0], False, "behind")
    assert [each["name"] for each in again.versions(["never", "pi"])["satellites"]] == ["never", "pi"]


def test_the_brain_is_listed_with_its_commit(repo, tmp_path):
    updates, commits = make(repo, tmp_path)
    brain = updates.versions([])["brain"]
    assert brain["version"] == commits[2] and brain["short"] == commits[2][:7] and brain["date"]
