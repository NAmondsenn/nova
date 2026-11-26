"""
Update Install Module (the bundle part).

How the Pi installs the update its brain sent: the git bundle is written to the update
folder, checked to hold exactly the commit the brain said, and handed to
scripts/update_satellite.sh in a detached `systemd-run --user` job. The job is detached
because installing stops voice-assistant, and a child of the service would be stopped
with it. The script is copied out of the clone first, so moving the clone's files
underneath it can't change the script while it runs.
"""

import logging
import os
import shutil
import subprocess
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The installer the running code carries, which is the one that runs.
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "update_satellite.sh")

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
