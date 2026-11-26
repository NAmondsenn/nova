"""
Update Archive Module.

Makes the archives the brain sends a satellite to update it, and unpacks the one a
satellite receives. A machine installed from a git checkout gets a git bundle; a machine
installed from a zip gets a zip made by `git archive`, whose comment is the commit it was
made from. Unpacking refuses any name that could land outside the folder it was told to
fill, before anything is written.
"""

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from typing import Optional

from voice_assistant.protocol import COMMIT

logger = logging.getLogger(__name__)

# How long git may take, so a broken checkout can't hold up an update for ever.
GIT_TIMEOUT = 120

# A name starting with a drive letter, which Windows would read as an absolute path.
DRIVE = re.compile(r"[A-Za-z]:")


class ArchiveError(Exception):
    """An archive couldn't be made, or isn't safe to unpack."""


def _git(project: str, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """
    Runs git in the project.

    Args:
        project: The checkout to run in.
        *arguments: What to give git.
        check: Whether a non-zero exit is an ArchiveError. When False the caller reads the
            return code itself.

    Returns:
        The finished process, with its output as text.

    Raises:
        ArchiveError: Git couldn't run, took too long, or (when check) failed. Only a short
            reason, never a raw CalledProcessError.
    """
    try:
        result = subprocess.run(["git", *arguments], cwd=project, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ArchiveError(f"git {arguments[0]} took too long") from None
    except OSError as e:
        raise ArchiveError(f"git {arguments[0]} couldn't run: {e.strerror or e}") from None
    if check and result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise ArchiveError(f"git {arguments[0]} failed: {lines[-1] if lines else result.returncode}")
    return result


def _through_temp_file(project: str, *arguments: str) -> bytes:
    """
    Has git write an archive to a temp file, reads it back and removes it.

    The file goes in the temp folder, which is the one place the brain's service may write.
    It is removed whether or not git succeeds.

    Args:
        project: The checkout to run git in.
        *arguments: The git command, with "{}" where the output file goes.

    Returns:
        The file's bytes.

    Raises:
        ArchiveError: Git failed, or the file couldn't be read.
    """
    descriptor, path = tempfile.mkstemp(dir=tempfile.gettempdir(), prefix="nova-update-")
    os.close(descriptor)
    try:
        _git(project, *[path if argument == "{}" else argument for argument in arguments])
        with open(path, "rb") as file:
            return file.read()
    except OSError as e:
        raise ArchiveError(f"couldn't read the archive: {e.strerror or e}") from None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def make_zip(project: str, version: str) -> bytes:
    """
    Makes a zip of the code at a commit.

    Args:
        project: The checkout to archive.
        version: The commit id to archive. It ends up as the zip's comment.

    Returns:
        The zip.

    Raises:
        ArchiveError: Git couldn't make it.
    """
    return _through_temp_file(project, "archive", "--format=zip", "-o", "{}", version)


def make_bundle(project: str, version: str, base: Optional[str]) -> bytes:
    """
    Makes a git bundle of HEAD, as thin as the satellite's commit allows.

    A bundle needs a ref, so it is always HEAD, and so refused unless HEAD is the version
    the brain means to send.

    Args:
        project: The checkout to bundle.
        version: The commit id the satellite is to end up on, which has to be HEAD.
        base: The commit the satellite is on. When it is a strict ancestor of HEAD the
            bundle holds only what comes after it; when it isn't (the brain doesn't have
            it, or it is ahead or level) the bundle is whole.

    Returns:
        The bundle.

    Raises:
        ArchiveError: HEAD isn't the version, or git couldn't make it.
    """
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    if head != version:
        raise ArchiveError("the brain's HEAD isn't the version to send")
    arguments = ["bundle", "create", "{}", "HEAD"]
    if base and base != head and COMMIT.fullmatch(base) \
            and _git(project, "merge-base", "--is-ancestor", base, "HEAD", check=False).returncode == 0:
        arguments.append(f"^{base}")
    return _through_temp_file(project, *arguments)


def zip_commit(archive: bytes) -> Optional[str]:
    """
    Reads the commit a zip was made from.

    Args:
        archive: The zip's bytes.

    Returns:
        The zip's comment if it is a commit id, else None (also for anything not a zip).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            comment = opened.comment.decode()
    except (zipfile.BadZipFile, UnicodeDecodeError):
        return None
    return comment if COMMIT.fullmatch(comment) else None


def _unsafe(name: str) -> bool:
    """Whether a zip member's name could land outside the folder it is unpacked into."""
    return (name.startswith("/") or "\\" in name or bool(DRIVE.match(name))
            or ".." in name.split("/"))


def unpack_zip(archive: bytes, into: str) -> None:
    """
    Empties a folder and unpacks a zip into it.

    Every name is checked first, so a bad zip writes nothing and removes nothing.

    Args:
        archive: The zip's bytes.
        into: The folder to fill. It is kept, but whatever was in it is removed.

    Raises:
        ArchiveError: Not a zip, or a member's name is absolute, has a drive letter or a
            backslash, or has a ".." part.
    """
    try:
        opened = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile:
        raise ArchiveError("the archive isn't a zip") from None
    with opened:
        for name in opened.namelist():
            if _unsafe(name):
                raise ArchiveError("the archive has a name outside its folder")
        # Emptying and extracting can fail on a locked file, a too-long path, a full disk
        # or a damaged member, and the caller only handles ArchiveError.
        try:
            os.makedirs(into, exist_ok=True)
            for entry in os.listdir(into):
                path = os.path.join(into, entry)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            opened.extractall(into)
        except (OSError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, ValueError) as e:
            if isinstance(e, (zipfile.BadZipFile, zlib.error, EOFError)):
                reason = "damaged data"
            else:
                reason = getattr(e, "strerror", None) or type(e).__name__
            raise ArchiveError(f"couldn't unpack the archive: {reason}") from None
