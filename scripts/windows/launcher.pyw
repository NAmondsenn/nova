"""
Launcher.

Runs Nova's satellite on the Windows PC, and starts it again whenever it exits. Task
Scheduler starts this at the user's logon, with the venv's pythonw.exe so there's no
window, because its own restart on failure doesn't reliably restart a process which
exits. Where the satellite is installed is install.ps1's business: everything is under
%LOCALAPPDATA%\\nova (app, config and data), the user's own.

Windows never sends SIGTERM, so a logoff, a shutdown or a deploy ends the satellite
without its clean-up, leaving its running mark. A fresh launcher follows each of those,
so it takes the mark away before its first start, and the brain isn't told of an unclean
stop; a satellite which crashes while the launcher runs still leaves it for the next.
"""

import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("launcher")

# A run shorter than this is a failure, and the wait before the next one doubles.
FAST_SECONDS = 60
# The wait after a run which lasted, and the start of the climb after one which didn't.
FIRST_WAIT = 2
# The longest wait, so a satellite which can't start (no password yet, the brain
# unreachable) is tried again every five minutes at most.
LONGEST_WAIT = 300
# The satellite's stderr log is moved aside when it reaches this size, so it and its one
# earlier copy hold about twice as much at most, however long the satellite runs.
STDERR_LOG_MOST = 1_000_000


class Backoff:
    """How long to wait before starting the satellite again."""

    def __init__(self):
        self.wait = FIRST_WAIT

    def next_wait(self, ran_for: float) -> float:
        """
        Args:
            ran_for: How long the satellite ran, in seconds.

        Returns:
            Seconds to wait before starting it again: 2 after a run of a minute or
            more, and after a shorter one the wait doubles each time, to 300.
        """
        if ran_for >= FAST_SECONDS:
            self.wait = FIRST_WAIT
            return self.wait
        wait = self.wait
        self.wait = min(self.wait * 2, LONGEST_WAIT)
        return wait


def open_stderr_log(path: str, most: int = STDERR_LOG_MOST) -> logging.Logger:
    """
    Args:
        path: The satellite's stderr log.
        most: The size in bytes at which it is moved to `<path>.1`, replacing that.

    Returns:
        A logger of its own which writes bare lines to the file, and not to the launcher's.
    """
    log = logging.getLogger("satellite-stderr")
    for old in list(log.handlers):
        log.removeHandler(old)
        old.close()
    handler = RotatingFileHandler(path, maxBytes=most, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


def copy_lines(stream, log: logging.Logger) -> None:
    """
    Writes each line the satellite puts on its stderr to a log, until it closes it. Reading
    to the end also keeps the pipe from filling and holding the satellite up.

    Args:
        stream: The satellite's stderr, opened in binary.
        log: Where each line goes.
    """
    try:
        for line in iter(stream.readline, b""):
            log.info(line.decode("utf-8", errors="replace").rstrip("\r\n"))
    except OSError as error:
        logger.warning(f"Couldn't read the satellite's stderr: {error}")


def main():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        sys.exit("LOCALAPPDATA isn't set, so there's nowhere to look for Nova")
    base = os.path.join(local, "nova")
    app = os.path.join(base, "app")
    data = os.path.join(base, "data")
    logs = os.path.join(data, "logs")
    os.makedirs(logs, exist_ok=True)

    handler = RotatingFileHandler(os.path.join(logs, "launcher.log"), maxBytes=1_000_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # paths.py reads these two to find the install's settings, keys and saved data.
    environment = dict(os.environ, NOVA_CONFIG=os.path.join(base, "config"), NOVA_DATA=data)
    command = [os.path.join(app, "venv", "Scripts", "pythonw.exe"), os.path.join(app, "satellite.py")]
    stderr_log = open_stderr_log(os.path.join(logs, "satellite-stderr.log"))
    backoff = Backoff()
    logger.info("Launcher started")
    # The satellite's RUNNING_MARK, left by the last logon's satellite or the one a deploy
    # ended, neither of which was a crash - see above.
    try:
        os.remove(os.path.join(data, "running"))
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.warning(f"Couldn't take away the last run's mark: {error}")
    while True:
        started = time.monotonic()
        try:
            # pythonw has no console to write to, so the satellite's own log is the record -
            # and what it writes to stderr, a failure before that log is set up included, is
            # copied to a log of its own. That holds a copy of the satellite's log too, so it
            # is kept to about 2 MB, however long the satellite runs.
            with subprocess.Popen(command, cwd=app, env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW) as satellite:
                copy_lines(satellite.stderr, stderr_log)
                code = satellite.wait()
        except OSError as error:
            logger.error(f"Couldn't start the satellite: {error}")
            code = None
        ran_for = time.monotonic() - started
        wait = backoff.next_wait(ran_for)
        logger.warning(f"The satellite exited (code {code}) after {ran_for:.0f} s; starting it again in {wait:.0f} s")
        time.sleep(wait)


if __name__ == "__main__":
    main()
