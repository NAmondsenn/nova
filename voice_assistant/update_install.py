"""
Update Install Module (the bundle part and the zip part).

How the Pi installs the update its brain sent: the git bundle is written to the update
folder, checked to hold exactly the commit the brain said, and handed to
scripts/update_satellite.sh in a detached `systemd-run --user` job. The job is detached
because installing stops voice-assistant, and a child of the service would be stopped
with it. The script is copied out of the clone first, so moving the clone's files
underneath it can't change the script while it runs.

The PC has no git, so it gets a zip: the zip is unpacked into the update folder's `new`,
and scripts/windows/update_pc.ps1, run by the scheduled task "Nova update", does the rest.
It is a task of its own because installing stops "Nova start" and the launcher, and a child
of either would be stopped with them.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Callable, Dict, Optional

from voice_assistant.update_archive import ArchiveError, unpack_zip, zip_commit

logger = logging.getLogger(__name__)

# The installer the running code carries, which is the one that runs.
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "update_satellite.sh")

# The Windows scripts the running code carries; the helper and what it dot-sources.
WINDOWS_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scripts", "windows")
HELPER_FILES = ("update_pc.ps1", "stop_nova.ps1")

# The scheduled task which runs the helper.
UPDATE_TASK = "Nova update"

# How long git or systemd-run may take to answer.
RUN_TIMEOUT = 60


class InstallRefused(Exception):
    """
    The update can't be installed.

    Attributes:
        reason: What to tell the brain, in a sentence.
        needs_you: Optional {"why", "command"} for something he has to do.
    """

    def __init__(self, reason: str, needs_you: Optional[Dict] = None):
        super().__init__(reason)
        self.reason = reason
        self.needs_you = needs_you


def install_bundle(project: str, folder: str, version: str, archive: bytes,
                   run: Callable = subprocess.run) -> None:
    """
    Checks a received git bundle and starts the detached install of it.

    Args:
        project: The clone Nova runs from.
        folder: The update folder, where the bundle and the script's copy are put.
        version: The commit the brain said the bundle holds.
        archive: The bundle's bytes.
        run: subprocess.run, or a stand-in.

    Raises:
        InstallRefused: If the bundle isn't a bundle of that commit, or the job won't start.
    """
    os.makedirs(folder, exist_ok=True)
    bundle = os.path.join(folder, "nova.bundle")
    script = os.path.join(folder, "update_satellite.sh")
    try:
        # Temporary names moved into place, so nothing reads a half-written file.
        with open(bundle + ".tmp", "wb") as file:
            file.write(archive)
        os.replace(bundle + ".tmp", bundle)
        _check_and_start(project, folder, version, bundle, script, run)
    except InstallRefused:
        # A bundle nothing will use is not left lying about.
        try:
            os.remove(bundle)
        except OSError:
            pass
        raise


def _check_and_start(project: str, folder: str, version: str, bundle: str, script: str,
                     run: Callable) -> None:
    """The checks and the start of install_bundle, once the bundle is written."""
    try:
        listed = run(["git", "-C", project, "bundle", "list-heads", bundle, "HEAD"],
                     capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Couldn't read the update's bundle: %s", error)
        raise InstallRefused("The update's code isn't the commit it said.")
    heads = listed.stdout.split() if listed.returncode == 0 else []
    if not heads or heads[0] != version:
        logger.warning("The update's bundle isn't commit %s", version[:12])
        raise InstallRefused("The update's code isn't the commit it said.")
    try:
        shutil.copyfile(SCRIPT, script + ".tmp")
        os.replace(script + ".tmp", script)
    except OSError as error:
        logger.warning("Couldn't copy the installer: %s", error)
        raise InstallRefused("The installer couldn't be copied, so the update wasn't started.")
    command = ["systemd-run", "--user", "--collect", "--quiet", "--unit", f"nova-update-{version[:12]}",
               "/bin/bash", script, project, version, bundle, folder]
    try:
        started = run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("systemd-run didn't start: %s", error)
        raise InstallRefused("The installer couldn't be started.")
    if started.returncode != 0:
        logger.warning("systemd-run refused: %s", (started.stderr or "").strip()[-200:])
        raise InstallRefused("The installer couldn't be started.")


def install_zip(project: str, folder: str, version: str, previous: Optional[str], archive: bytes,
                run: Callable = subprocess.run) -> None:
    """
    Checks a received zip and starts the PC's helper to install it.

    The zip is unpacked into folder/new, the running code's copies of the helper and
    stop_nova.ps1 are put in the folder (so moving the installed code can't change them
    while they run), job.json says what to do, and the task "Nova update" is run.

    Args:
        project: The code Nova runs from (unused on the PC, kept as install_bundle's is).
        folder: The update folder.
        version: The commit the brain said the zip holds.
        previous: The commit running now, which the helper restores to, or None.
        archive: The zip's bytes.
        run: subprocess.run, or a stand-in.

    Raises:
        InstallRefused: If the zip isn't a zip of that commit, can't be written, or the
            task won't start. Nothing of the attempt is left in the folder.
    """
    if zip_commit(archive) != version:
        logger.warning("The update's zip isn't commit %s", version[:12])
        raise InstallRefused("The update's code isn't the commit it said.")
    new = os.path.join(folder, "new")
    try:
        os.makedirs(folder, exist_ok=True)
        try:
            unpack_zip(archive, new)
        except ArchiveError as error:
            logger.warning("Couldn't unpack the update: %s", error)
            raise InstallRefused(f"The update couldn't be unpacked: {error}"[:200])
        for name in HELPER_FILES:
            # Temporary names moved into place, so the helper never reads half a file.
            shutil.copyfile(os.path.join(WINDOWS_SCRIPTS, name), os.path.join(folder, name) + ".tmp")
            os.replace(os.path.join(folder, name) + ".tmp", os.path.join(folder, name))
        job = os.path.join(folder, "job.json")
        with open(job + ".tmp", "w", encoding="utf-8") as file:
            json.dump({"version": version, "previous": previous, "source": new}, file)
        os.replace(job + ".tmp", job)
        _start_task(run)
    except OSError as error:
        _clean(folder)
        logger.warning("Couldn't prepare the update: %s", error)
        raise InstallRefused(f"The update couldn't be prepared: {error.strerror or type(error).__name__}"[:200])
    except InstallRefused:
        _clean(folder)
        raise


def _start_task(run: Callable) -> None:
    """Runs the task "Nova update"; InstallRefused, with the one deploy to run, if it won't."""
    # The command is run on the machine that deploys, not on the PC.
    needs_you = {"why": "The PC has no update task yet, so it needs one deploy by hand.",
                 "command": "bash scripts/deploy_pc.sh"}
    try:
        started = run(["schtasks", "/Run", "/TN", UPDATE_TASK], capture_output=True, text=True,
                      timeout=RUN_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("schtasks didn't start: %s", error)
        raise InstallRefused("The update task couldn't be started.", needs_you)
    if started.returncode != 0:
        logger.warning("schtasks refused: %s", (started.stderr or "").strip()[-200:])
        raise InstallRefused("The update task couldn't be started.", needs_you)


def _clean(folder: str) -> None:
    """Removes what an install_zip left in the folder: the unpacked code, job and temporary files."""
    shutil.rmtree(os.path.join(folder, "new"), ignore_errors=True)
    for name in ("job.json", "job.json.tmp") + tuple(n + ".tmp" for n in HELPER_FILES):
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
