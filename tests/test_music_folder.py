"""
Music Folder Test
Checks that a song is set aside inside the music folder and put back without ever deleting,
overwriting or leaving the folder (decision 0022, section 4, step 1). Everything happens in
pytest's temporary folders, never in a real music folder.
"""

import os

import pytest

from voice_assistant import music_folder
from voice_assistant.music_folder import HELD, Held, MusicFolder, MusicFolderError


def make(root, rel, content=b"audio"):
    """Writes a file under root, with its folders, and returns its path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    return folder


@pytest.fixture
def song(root):
    """A song with its two sidecars and a cover, as SoulSync leaves them."""
    make(root, "Gunna/Album/01 - Endless.mp3", b"mp3")
    make(root, "Gunna/Album/01 - Endless.lrc", b"lrc")
    make(root, "Gunna/Album/01 - Endless.txt", b"txt")
    make(root, "Gunna/Album/cover.jpg", b"jpg")
    return "Gunna/Album/01 - Endless.mp3"


def listing(root):
    """Every file under root, as relative paths with their content, for before-and-after checks."""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestSetAside:
    def test_moves_the_audio_and_its_sidecars_but_not_the_cover(self, root, song):
        held = MusicFolder(str(root)).set_aside(song)

        assert held.original == song
        assert held.held == "Gunna/Album/01 - Endless.mp3"
        assert sorted(held.sidecars) == [("Gunna/Album/01 - Endless.lrc", "Gunna/Album/01 - Endless.lrc"),
                                         ("Gunna/Album/01 - Endless.txt", "Gunna/Album/01 - Endless.txt")]
        assert listing(root) == {
            "Gunna/Album/cover.jpg": b"jpg",
            ".nova-held/.ndignore": b"",
            ".nova-held/Gunna/Album/01 - Endless.mp3": b"mp3",
            ".nova-held/Gunna/Album/01 - Endless.lrc": b"lrc",
            ".nova-held/Gunna/Album/01 - Endless.txt": b"txt",
        }

    def test_a_song_with_no_sidecars_moves_alone(self, root):
        make(root, "a.flac")

        held = MusicFolder(str(root)).set_aside("a.flac")

        assert held.sidecars == []
        assert not (root / "a.flac").exists()
        assert (root / HELD / "a.flac").read_bytes() == b"audio"

    def test_never_overwrites_an_existing_ndignore(self, root):
        make(root, HELD + "/.ndignore", b"keep me")
        make(root, "a.mp3")

        MusicFolder(str(root)).set_aside("a.mp3")

        assert (root / HELD / ".ndignore").read_bytes() == b"keep me"

    def test_a_second_song_of_the_same_name_gets_a_number(self, root):
        folder = MusicFolder(str(root))
        make(root, "x/Endless.mp3", b"one")
        make(root, "x/Endless.lrc", b"one lrc")
        first = folder.set_aside("x/Endless.mp3")
        make(root, "x/Endless.mp3", b"two")
        make(root, "x/Endless.lrc", b"two lrc")
        second = folder.set_aside("x/Endless.mp3")
        make(root, "x/Endless.mp3", b"three")
        third = folder.set_aside("x/Endless.mp3")

        assert first.held == "x/Endless.mp3"
        assert second.held == "x/Endless (2).mp3"
        assert second.original == "x/Endless.mp3"
        # The sidecar keeps the audio's number, so they still share a stem.
        assert second.sidecars == [("x/Endless.lrc", "x/Endless (2).lrc")]
        assert third.held == "x/Endless (3).mp3"
        assert (root / HELD / "x/Endless.mp3").read_bytes() == b"one"
        assert (root / HELD / "x/Endless (2).mp3").read_bytes() == b"two"
        assert (root / HELD / "x/Endless (2).lrc").read_bytes() == b"two lrc"
        assert (root / HELD / "x/Endless (3).mp3").read_bytes() == b"three"

    def test_a_taken_sidecar_name_also_moves_to_the_next_number(self, root):
        # Only the .lrc is in the way: the pair must still move together, under (2).
        make(root, HELD + "/x/Endless.lrc", b"old lrc")
        make(root, "x/Endless.mp3", b"new")
        make(root, "x/Endless.lrc", b"new lrc")

        held = MusicFolder(str(root)).set_aside("x/Endless.mp3")

        assert held.held == "x/Endless (2).mp3"
        assert (root / HELD / "x/Endless.lrc").read_bytes() == b"old lrc"
        assert (root / HELD / "x/Endless (2).lrc").read_bytes() == b"new lrc"
        assert not (root / "x/Endless.mp3").exists()

    def test_gives_up_after_a_hundred_names(self, root, monkeypatch):
        make(root, "a.mp3")
        links = []

        def always_taken(src, dst, **kwargs):
            links.append(dst)
            raise FileExistsError(dst)

        monkeypatch.setattr(os, "link", always_taken)

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("a.mp3")

        assert len(links) == 100
        assert (root / "a.mp3").read_bytes() == b"audio"

    @pytest.mark.parametrize("rel", ["../x.mp3", "a/../../x.mp3", "/etc/passwd", "", ".", "a/.."])
    def test_refuses_a_path_that_is_not_inside(self, root, tmp_path, rel):
        make(tmp_path, "x.mp3")
        make(root, "a.mp3")
        before = listing(root)

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside(rel)

        assert listing(root) == before
        assert (tmp_path / "x.mp3").exists()
        assert not (root / HELD).exists()

    def test_refuses_a_symlink_pointing_outside(self, root, tmp_path):
        outside = make(tmp_path, "outside/secret.mp3")
        os.symlink(outside, root / "link.mp3")

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("link.mp3")

        assert outside.read_bytes() == b"audio"
        assert os.path.islink(root / "link.mp3")
        assert not (root / HELD).exists()

    def test_refuses_a_symlink_pointing_inside_too(self, root):
        make(root, "real.mp3")
        os.symlink(root / "real.mp3", root / "link.mp3")

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("link.mp3")

        assert (root / "real.mp3").exists() and os.path.islink(root / "link.mp3")

    def test_refuses_a_file_reached_through_a_folder_link_that_leaves(self, root, tmp_path):
        make(tmp_path, "elsewhere/song.mp3")
        os.symlink(tmp_path / "elsewhere", root / "dir")

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("dir/song.mp3")

        assert (tmp_path / "elsewhere/song.mp3").exists()

    def test_refuses_a_path_under_the_holding_folder(self, root):
        make(root, HELD + "/a.mp3")
        before = listing(root)

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside(HELD + "/a.mp3")

        assert listing(root) == before

    def test_refuses_a_folder_link_into_the_holding_folder(self, root):
        make(root, HELD + "/a.mp3")
        os.symlink(root / HELD, root / "back door")

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("back door/a.mp3")

        assert (root / HELD / "a.mp3").exists()

    def test_refuses_a_missing_file_and_a_folder(self, root):
        make(root, "dir/a.mp3")

        for rel in ("nope.mp3", "dir"):
            with pytest.raises(MusicFolderError):
                MusicFolder(str(root)).set_aside(rel)

        assert (root / "dir/a.mp3").exists()

    def test_refuses_a_holding_folder_that_leaves_the_music_folder(self, root, tmp_path):
        (tmp_path / "away").mkdir()
        os.symlink(tmp_path / "away", root / HELD)
        make(root, "a.mp3")

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("a.mp3")

        assert list((tmp_path / "away").iterdir()) == []
        assert (root / "a.mp3").exists()

    def test_leaves_a_sidecar_link_where_it_is(self, root, tmp_path):
        make(root, "a.mp3")
        outside = make(tmp_path, "lyrics.lrc", b"far")
        os.symlink(outside, root / "a.lrc")

        held = MusicFolder(str(root)).set_aside("a.mp3")

        assert held.sidecars == []
        assert os.path.islink(root / "a.lrc")
        assert outside.read_bytes() == b"far"

    def test_a_failing_second_link_rolls_the_first_back(self, root, song, monkeypatch):
        real_link = os.link
        calls = []

        def second_fails(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk trouble")
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", second_fails)
        before = listing(root)

        with pytest.raises(MusicFolderError, match="disk trouble"):
            MusicFolder(str(root)).set_aside(song)

        monkeypatch.setattr(os, "link", real_link)
        after = listing(root)
        # Everything is back where it was; the only addition is the blank .ndignore.
        assert {k: v for k, v in after.items() if k != ".nova-held/.ndignore"} == before

    def test_a_failed_rollback_is_named(self, root, song, monkeypatch):
        real_link = os.link
        calls = []

        def breaks_after_the_first(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 1:
                return real_link(src, dst, **kwargs)
            raise OSError("disk trouble")

        monkeypatch.setattr(os, "link", breaks_after_the_first)

        with pytest.raises(MusicFolderError) as error:
            MusicFolder(str(root)).set_aside(song)

        # The audio is safe in the holding folder, and the message says so.
        assert "01 - Endless.mp3 was left in the holding folder" in str(error.value)
        assert ".nova-held" in str(error.value)
        assert (root / HELD / "Gunna/Album/01 - Endless.mp3").read_bytes() == b"mp3"

    def test_an_unlink_that_fails_leaves_the_song_where_it_was(self, root, monkeypatch):
        make(root, "a.mp3")
        real_unlink = os.unlink

        def refuses_the_song(path, *args, **kwargs):
            if str(path).endswith("/music/a.mp3"):
                raise PermissionError("read-only")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", refuses_the_song)

        with pytest.raises(MusicFolderError):
            MusicFolder(str(root)).set_aside("a.mp3")

        monkeypatch.setattr(os, "unlink", real_unlink)
        assert (root / "a.mp3").read_bytes() == b"audio"
        assert not (root / HELD / "a.mp3").exists()

    def test_uses_a_hard_link_that_does_not_follow_links(self, root, monkeypatch):
        make(root, "a.mp3")
        seen = []
        real_link = os.link

        def spy(src, dst, **kwargs):
            seen.append(kwargs)
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", spy)

        MusicFolder(str(root)).set_aside("a.mp3")

        assert seen == [{"follow_symlinks": False}]


    def test_refuses_a_file_that_is_not_audio(self, root):
        make(root, "a.lrc")
        make(root, "b.jpg")

        for rel in ("a.lrc", "b.jpg", "noextension"):
            if rel == "noextension":
                make(root, rel)
            with pytest.raises(MusicFolderError, match="audio"):
                MusicFolder(str(root)).set_aside(rel)

        assert (root / "a.lrc").exists() and (root / "b.jpg").exists() and (root / "noextension").exists()
        assert not (root / HELD).exists()

    def test_an_unlink_that_fails_leaves_a_changed_link_alone(self, root, monkeypatch):
        make(root, "a.mp3")
        other = make(root, "other.bin", b"someone else's")
        real_unlink = os.unlink

        def fails_and_swaps(path, *args, **kwargs):
            if str(path).endswith("/music/a.mp3"):
                # Something replaces the new link while the old name can't be removed.
                os.replace(other, root / HELD / "a.mp3")
                raise PermissionError("read-only")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fails_and_swaps)

        with pytest.raises(MusicFolderError, match="read-only"):
            MusicFolder(str(root)).set_aside("a.mp3")

        monkeypatch.setattr(os, "unlink", real_unlink)
        assert (root / HELD / "a.mp3").read_bytes() == b"someone else's"
        assert (root / "a.mp3").read_bytes() == b"audio"


class TestPutBack:
    def test_restores_all_three_files(self, root, song):
        folder = MusicFolder(str(root))
        before = listing(root)
        held = folder.set_aside(song)

        assert folder.put_back(held) == "put back"

        assert listing(root) == {**before, ".nova-held/.ndignore": b""}
        assert not (root / HELD / "Gunna").exists() or not any((root / HELD / "Gunna").rglob("*.*"))

    def test_restores_a_numbered_copy_to_the_plain_name(self, root):
        folder = MusicFolder(str(root))
        make(root, "x/Endless.mp3", b"one")
        folder.set_aside("x/Endless.mp3")
        make(root, "x/Endless.mp3", b"two")
        make(root, "x/Endless.lrc", b"two lrc")
        second = folder.set_aside("x/Endless.mp3")

        assert folder.put_back(second) == "put back"

        assert (root / "x/Endless.mp3").read_bytes() == b"two"
        assert (root / "x/Endless.lrc").read_bytes() == b"two lrc"
        assert (root / HELD / "x/Endless.mp3").read_bytes() == b"one"

    def test_makes_the_folders_again_if_they_have_gone(self, root, song):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        for path in (root / "Gunna/Album/cover.jpg", root / "Gunna/Album", root / "Gunna"):
            path.unlink() if path.is_file() else path.rmdir()

        assert folder.put_back(held) == "put back"

        assert (root / song).read_bytes() == b"mp3"

    def test_a_new_copy_at_the_original_path_changes_nothing(self, root, song):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        make(root, song, b"new explicit copy")
        before = listing(root)

        assert folder.put_back(held) == "a new copy is there"

        assert listing(root) == before

    def test_a_new_copy_found_only_when_linking_keeps_both(self, root, song, monkeypatch):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        real_link = os.link
        calls = []

        def lands_late(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                # The new copy's lyrics appear between the check and the move.
                make(root, "Gunna/Album/01 - Endless.lrc", b"new lrc")
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", lands_late)
        before_held = listing(root / HELD)

        assert folder.put_back(held) == "a new copy is there"

        monkeypatch.setattr(os, "link", real_link)
        assert (root / "Gunna/Album/01 - Endless.lrc").read_bytes() == b"new lrc"
        assert listing(root / HELD) == before_held
        assert not (root / song).exists()

    def test_a_missing_held_copy_is_a_reason_not_an_exception(self, root, song):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        os.unlink(root / HELD / held.held)

        assert folder.put_back(held) == "the held copy has gone"

        assert not (root / song).exists()
        assert (root / HELD / "Gunna/Album/01 - Endless.lrc").exists()

    def test_a_sidecar_that_has_gone_is_skipped(self, root, song):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        os.unlink(root / HELD / "Gunna/Album/01 - Endless.txt")

        assert folder.put_back(held) == "put back"

        assert (root / song).exists()
        assert (root / "Gunna/Album/01 - Endless.lrc").exists()
        assert not (root / "Gunna/Album/01 - Endless.txt").exists()

    @pytest.mark.parametrize("original", ["../x.mp3", "/etc/passwd", HELD + "/a.mp3", ""])
    def test_refuses_an_original_outside_or_in_the_holding_folder(self, root, original):
        make(root, HELD + "/a.mp3")
        before = listing(root)

        result = MusicFolder(str(root)).put_back(Held(original, "a.mp3", []))

        assert result not in ("put back", "a new copy is there")
        assert listing(root) == before

    @pytest.mark.parametrize("held_rel", ["../a.mp3", "/etc/passwd", ""])
    def test_refuses_a_held_path_that_leaves_the_holding_folder(self, root, tmp_path, held_rel):
        make(tmp_path, "a.mp3")

        result = MusicFolder(str(root)).put_back(Held("a.mp3", held_rel, []))

        assert result not in ("put back", "a new copy is there")
        assert (tmp_path / "a.mp3").exists()
        assert not (root / "a.mp3").exists()

    def test_refuses_a_held_file_that_is_a_link(self, root, tmp_path):
        outside = make(tmp_path, "secret.mp3")
        (root / HELD).mkdir()
        os.symlink(outside, root / HELD / "a.mp3")

        result = MusicFolder(str(root)).put_back(Held("a.mp3", "a.mp3", []))

        assert result not in ("put back", "a new copy is there")
        assert not (root / "a.mp3").exists()
        assert outside.read_bytes() == b"audio"

    def test_will_not_put_a_song_back_through_a_folder_link_that_leaves(self, root, tmp_path):
        make(root, HELD + "/dir/a.mp3")
        (tmp_path / "away").mkdir()
        os.symlink(tmp_path / "away", root / "dir")

        result = MusicFolder(str(root)).put_back(Held("dir/a.mp3", "dir/a.mp3", []))

        assert result not in ("put back", "a new copy is there")
        assert list((tmp_path / "away").iterdir()) == []
        assert (root / HELD / "dir/a.mp3").exists()

    def test_a_folder_made_again_takes_its_parents_mode_not_the_umasks(self, root, song):
        # The brain's unit has UMask=0077, which would leave "Gunna" unreadable to Navidrome and SoulSync.
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        for path in (root / "Gunna/Album/cover.jpg", root / "Gunna/Album", root / "Gunna"):
            path.unlink() if path.is_file() else path.rmdir()
        root.chmod(0o755)
        old_umask = os.umask(0o077)
        try:
            assert folder.put_back(held) == "put back"
        finally:
            os.umask(old_umask)

        assert (root / "Gunna").stat().st_mode & 0o777 == 0o755
        assert (root / "Gunna/Album").stat().st_mode & 0o777 == 0o755

    def test_folders_in_the_holding_folder_keep_the_umasks_mode(self, root, song):
        old_umask = os.umask(0o077)
        try:
            MusicFolder(str(root)).set_aside(song)
        finally:
            os.umask(old_umask)

        assert (root / HELD / "Gunna").stat().st_mode & 0o777 == 0o700

    def test_a_failing_second_link_puts_the_first_one_back_in_the_holding_folder(self, root, song, monkeypatch):
        folder = MusicFolder(str(root))
        held = folder.set_aside(song)
        real_link = os.link
        calls = []

        def second_fails(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk trouble")
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", second_fails)

        result = folder.put_back(held)

        monkeypatch.setattr(os, "link", real_link)
        assert "disk trouble" in result
        assert not (root / song).exists()
        assert (root / HELD / held.held).read_bytes() == b"mp3"


class TestPutBackSidecars:
    """A Held is stored as JSON, so a corrupt or edited one can reach put_back."""

    @pytest.fixture
    def waiting(self, root):
        make(root, HELD + "/a.mp3", b"mp3")
        make(root, HELD + "/a.lrc", b"lrc")
        make(root, HELD + "/b.lrc", b"other lrc")
        return root

    def test_a_sidecar_path_through_a_folder_link_that_leaves_is_refused(self, waiting, tmp_path):
        (tmp_path / "away").mkdir()
        os.symlink(tmp_path / "away", waiting / "dir")
        before = listing(waiting)

        result = MusicFolder(str(waiting)).put_back(Held("a.mp3", "a.mp3", [("dir/a.lrc", "a.lrc")]))

        assert result not in ("put back", "a new copy is there")
        assert list((tmp_path / "away").iterdir()) == []
        assert listing(waiting) == before

    def test_a_sidecar_path_through_a_link_into_the_holding_folder_is_refused(self, waiting):
        (waiting / HELD / "sub").mkdir()
        os.symlink(waiting / HELD, waiting / "door")
        before = listing(waiting)

        result = MusicFolder(str(waiting)).put_back(Held("a.mp3", "a.mp3", [("door/sub/a.lrc", "a.lrc")]))

        assert result not in ("put back", "a new copy is there")
        assert listing(waiting) == before
        assert list((waiting / HELD / "sub").iterdir()) == []

    @pytest.mark.parametrize("sidecars", [
        [("b.lrc", "b.lrc")],
        [("a.lrc", "b.lrc")],
        [("a.jpg", "a.jpg")],
        [("a.mp3", "a.mp3")],
        [("a.lrc", "a.txt")],
        [("x/a.lrc", "a.lrc")],
        [("a.lrc", "../a.lrc")],
    ])
    def test_a_sidecar_must_be_the_audios_stem_with_a_lyrics_extension(self, waiting, sidecars):
        before = listing(waiting)

        result = MusicFolder(str(waiting)).put_back(Held("a.mp3", "a.mp3", sidecars))

        assert result not in ("put back", "a new copy is there")
        assert listing(waiting) == before

    @pytest.mark.parametrize("original, held_rel", [
        ("x.mp3", ".ndignore"),
        ("x.mp3", "a.lrc"),
        ("a.lrc", "a.lrc"),
        ("x.flac", "a.mp3"),
        ("x", "a.mp3"),
    ])
    def test_the_song_must_be_audio_with_the_same_extension_both_ends(self, waiting, original, held_rel):
        # Read back from a file, a Held naming .ndignore would let Navidrome into the holding folder.
        (waiting / HELD / ".ndignore").write_bytes(b"")
        before = listing(waiting)

        result = MusicFolder(str(waiting)).put_back(Held(original, held_rel, []))

        assert result not in ("put back", "a new copy is there", "the held copy has gone")
        assert listing(waiting) == before

    def test_a_numbered_held_name_still_matches_its_own_stem(self, waiting):
        make(waiting, HELD + "/a (2).mp3", b"two")
        make(waiting, HELD + "/a (2).lrc", b"two lrc")

        result = MusicFolder(str(waiting)).put_back(Held("a.mp3", "a (2).mp3", [("a.lrc", "a (2).lrc")]))

        assert result == "put back"
        assert (waiting / "a.lrc").read_bytes() == b"two lrc"

    def test_a_failed_rollback_when_putting_back_says_it_could_not_go_back(self, waiting, monkeypatch):
        real_link = os.link
        calls = []

        def breaks_after_the_first(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 1:
                return real_link(src, dst, **kwargs)
            raise OSError("disk trouble")

        monkeypatch.setattr(os, "link", breaks_after_the_first)

        result = MusicFolder(str(waiting)).put_back(Held("a.mp3", "a.mp3", [("a.lrc", "a.lrc")]))

        assert "a.mp3 couldn't be returned to the holding folder" in result
        assert (waiting / "a.mp3").read_bytes() == b"mp3"


class TestUsable:
    def test_a_writable_folder_is_usable(self, root):
        assert MusicFolder(str(root)).usable() is None

    def test_a_missing_folder_gives_a_reason(self, tmp_path):
        assert "doesn't exist" in MusicFolder(str(tmp_path / "gone")).usable()

    def test_no_folder_set_gives_a_reason(self):
        assert MusicFolder("").usable()

    def test_a_file_is_not_a_folder(self, tmp_path):
        make(tmp_path, "file")

        assert "isn't a folder" in MusicFolder(str(tmp_path / "file")).usable()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere, so nothing to refuse")
    def test_a_read_only_folder_gives_a_reason(self, root):
        root.chmod(0o555)
        try:
            assert "write" in MusicFolder(str(root)).usable()
        finally:
            root.chmod(0o755)

    def test_a_holding_folder_that_is_a_file_gives_a_reason(self, root):
        make(root, HELD, b"in the way")

        assert MusicFolder(str(root)).usable()

    def test_a_holding_folder_that_is_a_link_gives_a_reason(self, root, tmp_path):
        (tmp_path / "away").mkdir()
        os.symlink(tmp_path / "away", root / HELD)

        assert "link" in MusicFolder(str(root)).usable()


class TestHeldCount:
    def test_counts_audio_only(self, root):
        folder = MusicFolder(str(root))
        for rel in ("a.mp3", "x/b.FLAC", "x/y/c.m4a", "d.opus", "a.lrc", "x/b.txt", "cover.jpg", ".ndignore"):
            make(root, HELD + "/" + rel)
        make(root, "outside.mp3")

        assert folder.held_count() == 4

    def test_a_holding_folder_that_is_a_link_is_not_walked(self, root, tmp_path):
        make(tmp_path, "away/a.mp3")
        os.symlink(tmp_path / "away", root / HELD)

        assert MusicFolder(str(root)).held_count() == 0

    def test_no_holding_folder_is_none_held(self, root):
        assert MusicFolder(str(root)).held_count() == 0

    def test_counts_what_set_aside_moved(self, root, song):
        folder = MusicFolder(str(root))
        folder.set_aside(song)

        assert folder.held_count() == 1


def test_the_module_names_only_the_two_sidecars():
    assert music_folder.SIDECARS == (".lrc", ".txt")
    assert HELD == ".nova-held"
