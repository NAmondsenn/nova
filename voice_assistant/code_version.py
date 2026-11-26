"""
Code Version Module.

Which commit of Nova's code this machine runs, and how it can be updated, so a satellite
can say both in its hello and the brain can tell which satellites are behind. A machine
installed from a git checkout (the Pi, the brain) says its HEAD and updates from a git
bundle. A machine installed from a zip (the PC) has a COMMIT file written by whatever
installed it, and updates from a zip.
"""

import logging
import os
import subprocess
import sys
from typing import Optional

from voice_assistant import paths
from voice_assistant.protocol import COMMIT

logger = logging.getLogger(__name__)

# How long git may take to answer, so a broken checkout can't hold up the hello.
GIT_TIMEOUT = 10


def _git(project: str, *arguments: str) -> Optional[str]:
    """
    Runs git in the project, giving what it printed.

    Args:
        project: The project's folder.
        arguments: What to give git after "-C project".

    Returns:
        Its output, stripped, or None if git failed, isn't installed or took too long.
    """
    try:
        done = subprocess.run(["git", "-C", project, *arguments], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def running_version(project: str = paths.PROJECT_DIRECTORY) -> Optional[str]:
    """
    The commit this code is.

    Args:
        project: The project's folder.

    Returns:
        The COMMIT file's contents if it holds a commit id, else git's HEAD for the
        folder if it has one, else None. A COMMIT file with anything else in it is
        ignored, as a half-written one must not become the version.
    """
    try:
        with open(os.path.join(project, "COMMIT"), encoding="ascii") as file:
            written = file.read().strip()
    except (OSError, UnicodeDecodeError):
        written = ""
    if COMMIT.fullmatch(written):
        return written
    head = _git(project, "rev-parse", "HEAD")
    if head is not None and COMMIT.fullmatch(head):
        return head
    return None


def update_format(project: str = paths.PROJECT_DIRECTORY) -> Optional[str]:
    """
    How this machine can be updated.

    Args:
        project: The project's folder.

    Returns:
        "bundle" if the folder is a git checkout, "zip" on Windows with a COMMIT file
        (the PC's install), else None, which says the machine can't be updated.
    """
    if os.path.isdir(os.path.join(project, ".git")):
        return "bundle"
    if sys.platform == "win32" and os.path.exists(os.path.join(project, "COMMIT")):
        return "zip"
    return None


def commit_date(project: str, version: Optional[str]) -> Optional[str]:
    """
    The day a commit was made, for the settings page.

    Args:
        project: The project's folder.
        version: The commit id, or None.

    Returns:
        E.g. "2026-11-26", or None if the commit is unknown here or there is none.
    """
    if version is None or not COMMIT.fullmatch(version):
        return None
    date = _git(project, "log", "-1", "--format=%cs", version)
    return date or None


def short(version: Optional[str]) -> str:
    """
    A commit id as people read it.

    Args:
        version: The commit id, or None.

    Returns:
        Its first 7 characters, or "unknown".
    """
    return version[:7] if version else "unknown"
