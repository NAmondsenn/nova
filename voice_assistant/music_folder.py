"""
Music Folder Module
Sets a clean song aside in a hidden holding folder inside the user's music folder, and puts
it back (decision 0019, section 4, step 1). It is the only code in Nova that touches the
music files, so it keeps to three rules: it never deletes a file, never overwrites one, and
never moves anything outside the music folder or anywhere but `.nova-held/` and back.

A move is a hard link, which refuses a name that is taken, and then an unlink of the old
name: if anything fails part way, what moved is linked back, so a song is never left split
between the two folders unseen.
"""

import os
import stat
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# The hidden folder inside the music folder where a song waits. Navidrome is kept out of it
# by a blank `.ndignore` there, so a song set aside stops being listed.
HELD = ".nova-held"

# Files SoulSync leaves beside a song with the same name ("Endless.mp3" and "Endless.lrc"),
# which move with it. Never the cover or anything else in the folder.
SIDECARS = (".lrc", ".txt")

# What counts as a song when the held ones are counted.
AUDIO = frozenset((".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".alac",
                   ".ape", ".wv", ".aiff", ".aif"))

# How many names ("Endless (2).mp3", "Endless (3).mp3" ...) to try in the holding folder.
MAX_NAMES = 100

PUT_BACK = "put back"
NEW_COPY = "a new copy is there"
HELD_GONE = "the held copy has gone"


class MusicFolderError(Exception):
    """A song that was refused or couldn't be moved; the message says why and is fit to show the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Taken(Exception):
    """A name the move would have used is already there (even if it only appeared a moment ago); nothing was replaced."""


@dataclass
class Held:
    """A song set aside, with what's needed to put it back.

    Attributes:
        original: The song's path relative to the music folder, e.g. "Gunna/Album/01 - Endless.mp3".
        held: Where it waits, relative to the holding folder: the same path, or with " (2)" and so
            on before the extension if the name was taken, e.g. "Gunna/Album/01 - Endless (2).mp3".
        sidecars: (original, held) for each lyrics file moved with it, both relative as above.
    """
    original: str
    held: str
    sidecars: List[Tuple[str, str]] = field(default_factory=list)


def _clean(rel) -> str:
    """The relative path tidied, or MusicFolderError unless it is plainly inside a folder.

    Args:
        rel: A path that must be relative, without "..", e.g. "Gunna/Album/01 - Endless.mp3".

    Returns:
        The path as os.path.normpath writes it.
    """
    if not isinstance(rel, str) or not rel or "\0" in rel:
        raise MusicFolderError("that isn't a path inside the music folder")
    if os.path.isabs(rel):
        raise MusicFolderError("that path isn't relative to the music folder")
    if ".." in rel.split("/") or ".." in rel.split(os.sep):
        raise MusicFolderError("that path climbs out of the music folder")
    clean = os.path.normpath(rel)
    if clean == ".":
        raise MusicFolderError("that isn't a path inside the music folder")
    return clean


def _is_real_file(path: str) -> bool:
    """Whether path is a plain file; a symlink, wherever it points, is not."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _inside(root_real: str, path: str) -> bool:
    """Whether path, with every symlink followed, lies strictly inside the resolved folder."""
    return os.path.realpath(path).startswith(root_real + os.sep)


def _link_move(src: str, dst: str) -> None:
    """Moves one file without ever replacing dst: a hard link (which refuses an existing name), then unlink.

    Raises:
        _Taken: dst exists.
        OSError: Anything else. If src could not be unlinked the new link is removed again,
            unless src has gone or changed, when the link is kept as it may be the only copy,
            or the new link has changed, when it is left alone; the message says so.
    """
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise _Taken(dst) from None
    linked = os.lstat(dst)
    try:
        os.unlink(src)
    except OSError as error:
        try:
            same = os.path.samestat(os.lstat(src), linked)
        except OSError:
            same = False
        # The same check for the new link: if it has been replaced since, it is not ours to remove.
        try:
            same = same and os.path.samestat(os.lstat(dst), linked)
        except OSError:
            same = False
        if not same:
            raise OSError(f"{error}; {dst} was left as it is") from error
        try:
            os.unlink(dst)
        except OSError as undo:
            raise OSError(f"{error}; the new link {dst} could not be removed either ({undo})") from error
        raise


class MusicFolder:
    """The user's music folder, and the holding folder inside it."""

    def __init__(self, root: str):
        """
        Args:
            root: The music folder, as Nova sees it (the same folder Navidrome's library is).
        """
        self.root = root

    def usable(self) -> Optional[str]:
        """
        Whether songs can be set aside here.

        Returns:
            None if the music folder exists and Nova can write to it, else the reason.
        """
        if not self.root:
            return "no music folder is set"
        if not os.path.exists(self.root):
            return "the music folder doesn't exist"
        if not os.path.isdir(self.root):
            return "the music folder isn't a folder"
        if not os.access(self.root, os.W_OK | os.X_OK):
            return "Nova can't write to the music folder"
        held = os.path.join(self.root, HELD)
        if os.path.lexists(held):
            if os.path.islink(held):
                return f"{HELD} in the music folder is a link, and Nova won't write through it"
            if not os.path.isdir(held):
                return f"{HELD} in the music folder isn't a folder"
            if not os.access(held, os.W_OK | os.X_OK):
                return f"Nova can't write to {HELD} in the music folder"
        return None

    def held_count(self) -> int:
        """
        Returns:
            How many songs (audio files, not lyrics) wait in the holding folder.
        """
        held = os.path.join(self.root, HELD)
        count = 0
        # A link here is never followed: os.walk would otherwise count whatever it points at.
        if os.path.islink(held):
            return 0
        for folder, _dirs, names in os.walk(held):
            for name in names:
                if os.path.splitext(name)[1].lower() in AUDIO and _is_real_file(os.path.join(folder, name)):
                    count += 1
        return count

    def set_aside(self, rel: str) -> Held:
        """
        Moves a song, and the .lrc and .txt beside it, into the holding folder.

        Args:
            rel: The song's path relative to the music folder, e.g. "Gunna/Album/01 - Endless.mp3".
                It must be a real file (not a symlink) that really lies inside the music folder
                and not already in the holding folder.

        Returns:
            What was moved, for put_back.

        Raises:
            MusicFolderError: The path was refused, or a move failed. Whatever moved first has
                been put back; a file that couldn't be put back is named in the message.
        """
        rel = self._song(rel)
        if os.path.splitext(rel)[1].lower() not in AUDIO:
            raise MusicFolderError("that isn't an audio file")
        # Files with the audio's stem move with it; a link there is left alone, as only real files are moved.
        stem, extension = os.path.splitext(rel)
        sidecars = [ext for ext in SIDECARS if _is_real_file(os.path.join(self.root, stem + ext))]
        held_base = self._holding_folder()

        for number in range(1, MAX_NAMES + 1):
            # A taken name gets a number before the extension, the sidecars with it so they share a stem:
            # "Endless (2).mp3" and "Endless (2).lrc".
            numbered = stem if number == 1 else f"{stem} ({number})"
            moves = [(rel, numbered + extension)] + [(stem + ext, numbered + ext) for ext in sidecars]
            if any(os.path.lexists(os.path.join(held_base, to)) for _from, to in moves):
                continue
            for _from, to in moves:
                self._make_folders(held_base, os.path.dirname(to), None)
            try:
                self._move([(os.path.join(self.root, a), os.path.join(held_base, b)) for a, b in moves], True)
            except _Taken:
                continue
            return Held(original=rel, held=moves[0][1], sidecars=moves[1:])
        raise MusicFolderError(f"{MAX_NAMES} songs of that name are already in {HELD}")

    def put_back(self, held: Held) -> str:
        """
        Moves a song set aside, and its lyrics, back where they were. Nothing is overwritten.

        Args:
            held: What set_aside returned.

        Returns:
            "put back"; "a new copy is there" if something now sits at the song's path (or its
            lyrics'), when both are kept and nothing moves; "the held copy has gone" if the
            song isn't in the holding folder any more; or why it couldn't be moved.
        """
        try:
            return self._put_back(held)
        except MusicFolderError as error:
            return error.reason

    def _put_back(self, held: Held) -> str:
        held_base = os.path.join(self.root, HELD)
        held_real = os.path.join(self._root_real(), HELD)
        audio, audio_held = _clean(held.original), _clean(held.held)
        pairs = [(audio, audio_held)]
        for original, held_rel in held.sidecars:
            original, held_rel = _clean(original), _clean(held_rel)
            # A Held is read back from a file, so a lyrics file that isn't the song's own (a different
            # name, or "dir/a.lrc" for "a.mp3") is never trusted to go anywhere.
            for path, song in ((original, audio), (held_rel, audio_held)):
                stem, extension = os.path.splitext(path)
                if stem != os.path.splitext(song)[0] or extension not in SIDECARS:
                    raise MusicFolderError("a lyrics file in that record isn't the song's own")
            if os.path.splitext(original)[1] != os.path.splitext(held_rel)[1]:
                raise MusicFolderError("a lyrics file in that record isn't the song's own")
            pairs.append((original, held_rel))
        moves = []
        for index, (original, held_rel) in enumerate(pairs):
            if original.split(os.sep)[0] == HELD:
                raise MusicFolderError("that song's path is in the holding folder")
            source = os.path.join(held_base, held_rel)
            if not os.path.lexists(source):
                # A lyrics file that has gone is no loss to the song; the song itself matters.
                if index == 0:
                    return HELD_GONE
                continue
            if not _is_real_file(source) or not _inside(held_real, source):
                raise MusicFolderError("the held copy isn't a plain file inside the holding folder")
            moves.append((source, os.path.join(self.root, original)))
        if any(os.path.lexists(to) for _from, to in moves):
            return NEW_COPY
        for _from, to in moves:
            self._make_folders(self.root, os.path.dirname(self._label(to)), held_real)
        try:
            self._move(moves, False)
        except _Taken:
            return NEW_COPY
        return PUT_BACK

    def _root_real(self) -> str:
        """The music folder with symlinks resolved, or MusicFolderError if it isn't a folder."""
        if not self.root or not os.path.isdir(self.root):
            raise MusicFolderError("the music folder doesn't exist")
        return os.path.realpath(self.root)

    def _song(self, rel) -> str:
        """Checks a song's path and returns it tidied, or raises MusicFolderError.

        It must be a real file, not a symlink pointing anywhere, that resolves inside the music
        folder and outside the holding folder.
        """
        rel = _clean(rel)
        root_real = self._root_real()
        if rel.split(os.sep)[0] == HELD:
            raise MusicFolderError("that song is already in the holding folder")
        path = os.path.join(self.root, rel)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise MusicFolderError("that song isn't in the music folder") from None
        if stat.S_ISLNK(mode):
            raise MusicFolderError("that song is a symlink, and Nova only moves real files")
        if not stat.S_ISREG(mode):
            raise MusicFolderError("that isn't a file")
        real = os.path.realpath(path)
        if not real.startswith(root_real + os.sep):
            raise MusicFolderError("that song resolves outside the music folder")
        if real.startswith(os.path.join(root_real, HELD) + os.sep):
            raise MusicFolderError("that song is already in the holding folder")
        return rel

    def _holding_folder(self) -> str:
        """Makes the holding folder and its blank .ndignore if they aren't there; returns its path.

        Raises:
            MusicFolderError: The holding folder can't be made, or isn't really inside the music folder.
        """
        root_real = self._root_real()
        held = os.path.join(self.root, HELD)
        try:
            if not os.path.lexists(held):
                os.mkdir(held)
            if not os.path.isdir(held) or os.path.realpath(held) != os.path.join(root_real, HELD):
                raise MusicFolderError(f"{HELD} in the music folder isn't a plain folder")
            # Exclusive, so a .ndignore that is already there is never opened for writing.
            try:
                descriptor = os.open(os.path.join(held, ".ndignore"),
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
                os.close(descriptor)
            except FileExistsError:
                pass
        except OSError as error:
            raise MusicFolderError(f"couldn't make {HELD} in the music folder: {error}") from error
        return held

    def _make_folders(self, base: str, folder: str, avoid: Optional[str]) -> None:
        """Makes the folders a file needs under base, one level at a time.

        Args:
            base: A folder that exists, the music folder or the holding folder.
            folder: Relative to base, e.g. "Gunna/Album"; "" or "." for base itself.
            avoid: A resolved folder the result must not lie in (the holding folder, when
                putting back), or None.

        Raises:
            MusicFolderError: A level can't be made, or a symlink would lead outside base
                (or into avoid); nothing is made past that level.
        """
        base_real = os.path.realpath(base)
        current = base
        for part in [] if folder in ("", ".") else folder.split(os.sep):
            current = os.path.join(current, part)
            try:
                if not os.path.lexists(current):
                    os.mkdir(current)
            except OSError as error:
                raise MusicFolderError(f"couldn't make the folder {part}: {error}") from error
            real = os.path.realpath(current)
            if not os.path.isdir(current) or not (real + os.sep).startswith(base_real + os.sep) \
                    or (avoid is not None and (real + os.sep).startswith(avoid + os.sep)):
                raise MusicFolderError(f"{part} isn't a plain folder inside the music folder")

    def _move(self, moves: List[Tuple[str, str]], setting_aside: bool) -> None:
        """Moves each (from, to) in turn, and moves back any that already went if one fails.

        Args:
            moves: (from, to) full paths.
            setting_aside: True if the files are going into the holding folder, False if out of it;
                only for saying which way a file that couldn't be moved back was left.

        Raises:
            _Taken: A destination was taken and everything is back where it was.
            MusicFolderError: Anything else failed; the message names any file that couldn't be put back.
        """
        done: List[Tuple[str, str]] = []
        failure: Optional[BaseException] = None
        for source, destination in moves:
            try:
                _link_move(source, destination)
            except (_Taken, OSError) as error:
                failure = error
                break
            done.append((source, destination))
        if failure is None:
            return
        stuck = []
        for source, destination in reversed(done):
            try:
                _link_move(destination, source)
            except (_Taken, OSError) as error:
                if setting_aside:
                    stuck.append(f"{self._label(destination)} was left in the holding folder ({error})")
                else:
                    stuck.append(f"{self._label(destination)} couldn't be returned to the holding folder ({error})")
        if isinstance(failure, _Taken) and not stuck:
            raise failure
        reason = f"couldn't move the song: {failure}" if not isinstance(failure, _Taken) \
            else "couldn't move the song: a name was taken"
        raise MusicFolderError("; ".join([reason] + stuck))

    def _label(self, path: str) -> str:
        """A path as the user would know it, relative to the music folder."""
        return os.path.relpath(path, self.root)
