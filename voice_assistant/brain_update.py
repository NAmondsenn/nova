"""
Brain Update Module.

The brain's side of updating itself, from "Update Nova" (the settings page's button or his
voice) to "I'm updated." The brain's sandbox can't write its own code, run sudo or start a
unit, so it only asks: it waits until Nova is free - nothing recording, speaking, in a
conversation, ringing or waiting for an approval - then drops the file `request` in its
own data folder, which root's nova-update.path answers by starting the updater
(scripts/update_brain.sh). The updater fast-forwards the clone from GitHub and says how it
went in `brain.json`: "current" (nothing new, so the satellites behind are offered the
brain's commit instead), "failed" or "needs_you" (the code wasn't moved, or was taken
back), or "ready" (the new code is in place, and `previous` holds the commit it came from).

On "ready" the brain waits for Nova to be free again, with no limit this time, as giving
up would leave the new code for the next crash to start, notes where he asked, and
restarts itself by exiting 75 for systemd to start the new code. After the restart,
after_start reads what the updater and the take-back left: `taken_back` (the new code
wouldn't start, so nova-takeback.service moved it back) is a failure; `previous` with
the brain on a newer commit is done, and `previous` is removed once the new code has run
for a minute, so the take-back can't revert it on some later, unrelated crash loop. The
satellite he asked at hears "I'm updated." as it is caught up.

Every outcome is kept in `outcome.json` for the settings page, and logged with why.
See docs/decisions/0040.
"""

import datetime
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from voice_assistant import code_version, paths, storage
from voice_assistant.lists import joined

logger = logging.getLogger(__name__)

# What Nova says, exactly as constraints have it.
STARTING = "Updating. I'll be back in a minute."
ALREADY_UPDATING = "I'm already updating."
CURRENT = "Already up to date."
CURRENT_UPDATING = "Already up to date; updating {}."
BUSY = "I couldn't update, as I was busy for {}. Ask again when I'm free."
FAILED = "The update didn't work, so I'm still on the old version. The settings page says why."
NEEDS_YOU = "The update needs you: the settings page says what to run."
UPDATED = "I'm updated."
UPDATED_NEEDS_YOU = " One thing needs you on the settings page."

# Why, as the settings page and the log show it.
TAKEN_BACK = "The new code didn't start, so it was taken back."
NOT_INSTALLED = "The brain's updater isn't installed yet."
NOT_A_SERVICE = "Nova isn't running as a service, so it can't restart itself."
RESTART_COMMAND = "sudo systemctl restart nova-backend"
UPDATER_STUCK = "The brain's updater didn't finish within 30 minutes."
UPDATER_FAILED = "The brain's updater failed without saying why."
UPDATER_SILENT = "The brain's updater stopped without saying how it went."
UPDATER_LOG = "sudo journalctl -u nova-update"
NOT_RESTARTED = "The new code is in place, but Nova hasn't been free for {} to restart into it."
RESTART_BROKE = "The new code is in place, and will start the next time Nova restarts."
BROKE = "Something went wrong in the brain while it updated."

# How often it looks whether Nova is free, in seconds.
IDLE_LOOK = 2.0
# How many looks in a row must find Nova free. Two, so a moment free between a turn's
# answer and its reply being said - the reply to an approval is said only once the action
# has run - isn't taken for free.
FREE_LOOKS = 2
# A minute in seconds - the idle wait's setting is in minutes. A name of its own so the
# tests can shorten it.
MINUTE = 60.0
# The idle wait when the setting is missing, and the limits it's held to.
IDLE_WAIT_MINUTES = 10
LEAST_MINUTES, MOST_MINUTES = 1, 60
# How often brain.json is read while the updater runs, in seconds.
STATUS_LOOK = 2.0
# The longest the brain waits for its updater, in seconds.
UPDATER_LONGEST = 1800
# The updater takes the request away as its first step, so one still there after this
# long means nothing is there to take it: the updater isn't installed yet.
PICKED_UP_WITHIN = 30
# The updater writes "checking" as soon as it has taken the request, so a request taken
# with nothing heard for this long means the updater stopped without a word.
UNHEARD_AFTER = 60
# How long the new code must run before it counts as started, in seconds.
STARTED_AFTER = 60
# How long the note of where he asked is kept for, in seconds: the satellite he asked
# at is caught up within a minute of the restart, and "I'm updated." an hour later
# would be news of nothing.
ASKED_KEPT = 3600

# The states outcome() gives.
STATES = ("current", "done", "failed", "needs_you", "busy")
FIELDS = ("state", "at", "from", "to", "reason", "command")


def _now_iso() -> str:
    """The time now, as the updater writes it."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _commit(value) -> Optional[str]:
    """A commit from brain.json: "" (not known, while checking or after a failed fetch) is None."""
    return value if isinstance(value, str) and value else None


def _needs(value) -> List[Dict]:
    """brain.json's needs_you, as a list of {why, command}, leaving out anything else."""
    if not isinstance(value, list):
        return []
    return [{"why": str(entry.get("why") or ""), "command": str(entry.get("command") or "")}
            for entry in value if isinstance(entry, dict)]


class BrainUpdate:
    """Updates the brain when it is free, and says how it went.

    Safe to use from any thread: start is under a lock, so a press of the page's button and
    a voice command together run one update.
    """

    def __init__(self, brain, version: Optional[str], folder: str, settings: Callable[[], Dict],
                 project: str = paths.PROJECT_DIRECTORY):
        """
        Args:
            brain: The brain, for busy_reason, offer_all, spoken_name, _say_update,
                keep_music_for_update and restart_for_update.
            version: The commit the brain runs, read once as it started, or None if it
                can't tell.
            folder: The update folder, shared with the updater and the take-back.
            settings: Gives the `updates` settings as they are now, so a change on the
                settings page is used straight away.
            project: The brain's project folder, for the command that installs the updater.
        """
        self.brain = brain
        self.version = version
        self.folder = folder
        self._settings = settings
        self.project = project
        self._lock = threading.Lock()
        self._running = False
        # What is said, and where, once the satellite he asked at is caught up after a
        # restart - see after_start and caught_up.
        self._to_say: Optional[Tuple[str, str]] = None

    def _path(self, name: str) -> str:
        return os.path.join(self.folder, name)

    @property
    def running(self) -> bool:
        """Whether an update is going on, from start until it ends - or the brain restarts."""
        with self._lock:
            return self._running

    # Starting.

    def start(self, asked_at: Optional[str]) -> str:
        """
        Starts updating the brain, unless it already is.

        Args:
            asked_at: The satellite he asked at, where the outcome is said - or None for
                the settings page, where it is shown instead.

        Returns:
            What to tell him.
        """
        with self._lock:
            if self._running:
                return ALREADY_UPDATING
            self._running = True
        where = f"the {asked_at} satellite" if asked_at else "the settings page"
        logger.info(f"Updating the brain, as asked at {where}")
        try:
            threading.Thread(target=self._run_and_end, args=(asked_at,), daemon=True, name="brain-update").start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return STARTING

    def _run_and_end(self, asked_at: Optional[str]):
        """Runs the update, and ends it however it went - unless the brain is restarting into the new code."""
        restarting = False
        try:
            restarting = self._run(asked_at)
        except Exception:
            logger.exception("The brain's update went wrong")
            self._keep("failed", BROKE)
            self._say(asked_at, FAILED)
        finally:
            # Still running while the brain restarts, so a press meanwhile starts nothing.
            if not restarting:
                with self._lock:
                    self._running = False

    def _idle_minutes(self) -> int:
        """The idle wait's setting, held to its limits, or its default if it isn't a whole number."""
        try:
            minutes = int((self._settings() or {}).get("idle_wait_minutes", IDLE_WAIT_MINUTES))
        except (TypeError, ValueError):
            minutes = IDLE_WAIT_MINUTES
        return max(LEAST_MINUTES, min(MOST_MINUTES, minutes))

    def _wait_free(self, longest: Optional[float], overdue: Optional[Callable[[], None]] = None) -> bool:
        """
        Waits until Nova is free: busy_reason gives None on FREE_LOOKS looks in a row.

        The turn which asked by voice is busy itself - "thinking" as the action runs,
        then "speaking" its reply - so the wait simply goes on looking until it has
        ended; it only takes those seconds of the limit.

        Args:
            longest: The longest to wait, in seconds, or None for no limit.
            overdue: If given, called once the limit has passed, and the wait carries
                on with no limit rather than giving up.

        Returns:
            Whether Nova is free; False if it was still busy once the limit passed.
        """
        give_up = None if longest is None else time.monotonic() + longest
        free = 0
        last = None
        failure = None
        while True:
            try:
                reason = self.brain.busy_reason()
                failure = None
            except Exception as e:
                # Not knowing is counted as busy, so nothing restarts under a recording -
                # logged only as it starts or changes, so one which goes on doesn't fill the log.
                if repr(e) != failure:
                    logger.exception("Couldn't tell whether Nova is busy, so taking it for busy")
                failure = repr(e)
                reason = "something unknown"
            if reason is None:
                free += 1
                if free >= FREE_LOOKS:
                    return True
            else:
                free = 0
                if reason != last:
                    logger.info(f"The brain's update is waiting for {reason}")
            last = reason
            if give_up is not None and time.monotonic() >= give_up:
                if overdue is None:
                    return False
                overdue()
                give_up = None
            time.sleep(IDLE_LOOK)

    def _run(self, asked_at: Optional[str]) -> bool:
        """
        The update itself, on a thread of its own.

        Returns:
            Whether the brain is restarting into the new code.
        """
        minutes = self._idle_minutes()
        said = "1 minute" if minutes == 1 else f"{minutes} minutes"
        if not self._wait_free(minutes * MINUTE):
            self._keep("busy", f"Nova was busy for {said}, so the update didn't start.")
            self._say(asked_at, BUSY.format(said))
            return False

        status = self._ask_updater()
        state = status.get("state")
        source, target = _commit(status.get("from")), _commit(status.get("to"))
        needs = _needs(status.get("needs_you"))
        if state == "current":
            names = self.brain.offer_all()
            self._keep("current", None, source=source, target=target)
            if names:
                self._say(asked_at, CURRENT_UPDATING.format(joined([self.brain.spoken_name(name) for name in names])))
            else:
                self._say(asked_at, CURRENT)
            return False
        if state == "needs_you":
            self._keep("needs_you", status.get("error"), needs, source, target)
            self._say(asked_at, NEEDS_YOU)
            return False
        if state != "ready":
            self._keep("failed", status.get("error") or UPDATER_FAILED, needs, source, target)
            self._say(asked_at, FAILED)
            return False

        # The new code is in place: the restart waits until Nova is free however long that
        # takes, as giving up would leave the new code for the next crash to start. Past the
        # idle limit, the page says so and what to run, and the wait carries on; the
        # outcome is replaced as the brain is back.
        logger.info(f"The brain's new code ({code_version.short(target)}) is in place, so restarting into it "
                    f"once Nova is free")

        def overdue():
            why = NOT_RESTARTED.format(said)
            self._keep("needs_you", why, [{"why": why, "command": RESTART_COMMAND}], source, target)

        self._wait_free(minutes * MINUTE, overdue)
        try:
            self.brain.keep_music_for_update()
        except Exception:
            logger.exception("Couldn't keep the music playing for after the update")
        try:
            storage.save_json(self._path("asked.json"), {"satellite": asked_at, "at": time.time(), "said": False})
        except (OSError, TypeError) as e:
            # Only "I'm updated." is lost; the page still shows how it went.
            logger.warning(f"Couldn't note where the update was asked for: {e}")
        try:
            restarting = self.brain.restart_for_update()
        except Exception:
            # Not "still on the old version": the new code is on disk, and starts at the next restart.
            logger.exception("Couldn't restart into the brain's new code")
            self._resume_music()
            self._remove("asked.json")
            self._keep("needs_you", RESTART_BROKE, [{"why": RESTART_BROKE, "command": RESTART_COMMAND}], source,
                       target)
            self._say(asked_at, NEEDS_YOU)
            return False
        if restarting:
            logger.info(f"Restarting into the brain's new code ({code_version.short(target)})")
            return True
        self._resume_music()
        self._remove("asked.json")
        self._keep("needs_you", NOT_A_SERVICE, [{"why": NOT_A_SERVICE, "command": RESTART_COMMAND}], source, target)
        self._say(asked_at, NEEDS_YOU)
        return False

    def _resume_music(self) -> None:
        """Plays on the music kept for a restart which isn't going to happen, never raising."""
        try:
            self.brain.resume_kept_music()
        except Exception:
            logger.exception("Couldn't carry on the music kept for the update")

    def _status(self) -> Tuple[Optional[Tuple[int, int]], Dict]:
        """
        brain.json as it is now: which file it is (its inode and change time, as the
        updater replaces it whole each time) and what it says - ({} for nothing, or a
        file which can't be read, never raising).
        """
        path = self._path("brain.json")
        try:
            stat = os.stat(path)
            loaded = storage.load_json(path, {})
        except (OSError, ValueError):
            return None, {}
        return (stat.st_ino, stat.st_mtime_ns), loaded if isinstance(loaded, dict) else {}

    def _ask_updater(self) -> Dict:
        """
        Drops the request, and waits for the updater to say how it went.

        brain.json may be left from an earlier run, so only one written since - another
        file, as the updater replaces it whole each time - which isn't "checking" is this
        run's answer. Its `at` isn't compared, as the clock may have been put back.

        Returns:
            What the updater said: brain.json, or one made here when it never answered.
        """
        before_file, _ = self._status()
        try:
            storage.save_text(self._path("request"), _now_iso() + "\n")
        except OSError as e:
            logger.warning(f"Couldn't ask the brain's updater: {e}")
            return {"state": "failed", "error": "The brain couldn't ask its updater.", "from": self.version}
        logger.info("Asked the brain's updater")
        asked = time.monotonic()
        heard = False
        taken_at = None
        while True:
            file, status = self._status()
            if file is not None and file != before_file:
                if status.get("state") != "checking":
                    logger.info(f"The brain's updater says {status.get('state')}")
                    return status
                heard = True
            now = time.monotonic()
            waited = now - asked
            unanswered = not heard and os.path.exists(self._path("request"))
            if not heard and not unanswered:
                taken_at = taken_at or now
                if now - taken_at >= UNHEARD_AFTER:
                    return {"state": "failed", "error": UPDATER_SILENT, "from": self.version,
                            "needs_you": [{"why": UPDATER_SILENT, "command": UPDATER_LOG}]}
            if (unanswered and waited >= PICKED_UP_WITHIN) or waited >= UPDATER_LONGEST:
                self._remove("request")
                if heard:
                    return {"state": "failed", "error": UPDATER_STUCK, "from": self.version}
                return {"state": "needs_you", "error": NOT_INSTALLED, "from": self.version,
                        "needs_you": [{"why": NOT_INSTALLED,
                                       "command": f"sudo bash {os.path.join(self.project, 'update.sh')}"}]}
            time.sleep(STATUS_LOOK)

    # After a restart.

    def after_start(self) -> None:
        """
        Reads what an update left, once the brain is ready: the take-back's note that the
        new code wouldn't start, or `previous`, which the new code running says is done.
        Notes what to say at the satellite he asked at, for caught_up. Never raises.
        """
        try:
            self._after_start()
        except Exception:
            logger.exception("Couldn't read how the brain's last update went")

    def _after_start(self):
        _, status = self._status()
        source, target = _commit(status.get("from")), _commit(status.get("to"))
        needs = _needs(status.get("needs_you"))
        previous = self._read("previous")
        taken_back = self._read("taken_back")
        if taken_back is not None and taken_back != self.version:
            # From some earlier take-back, as the brain doesn't run the commit it went back
            # to: counted, it would keep previous for ever.
            logger.warning(f"Ignoring an old note of a take-back to {code_version.short(taken_back)}, as the brain "
                           f"runs {code_version.short(self.version)}")
            self._remove("taken_back")
            taken_back = None
        asked, said_already = self._asked()
        if taken_back is not None or (previous is not None and previous == self.version):
            # The take-back moved the code back - noted, or not, if its note never came. The
            # failure is said where he asked even if "I'm updated." was, as that was wrong.
            self._keep("failed", TAKEN_BACK, source=source, target=target)
            self._remove("taken_back")
            said = FAILED
            said_already = False
        elif previous is not None:
            self._keep("done", needs[0]["why"] if needs else None, needs, previous, self.version)
            said = UPDATED + (UPDATED_NEEDS_YOU if needs else "")
        else:
            # An ordinary start, so there's nothing to say - and a note of where an update
            # was asked for is from one which never restarted.
            self._remove("asked.json")
            return
        if previous is not None and previous == self.version:
            # Nothing is left to take back.
            self._remove("previous")
            self._remove("asked.json")
        elif previous is not None:
            # Only once the new code has run a while: until then the take-back may still
            # need it, should the new code fail to start again.
            timer = threading.Timer(STARTED_AFTER, self._started, args=(previous,))
            timer.daemon = True
            timer.start()
        if asked is not None and not said_already:
            with self._lock:
                self._to_say = (asked, said)
        elif asked is None:
            self._remove("asked.json")

    def _started(self, previous: str):
        """
        Removes `previous` once the new code has run STARTED_AFTER seconds - if it is still
        the same one - and the note of where he asked with it, as nothing can now be taken back.
        """
        if self._read("previous") == previous:
            self._remove("previous")
            self._remove("asked.json")
            logger.info(f"The brain's new code has run for {STARTED_AFTER} seconds, so it is no longer taken "
                        f"back if it stops")

    def _asked(self) -> Tuple[Optional[str], bool]:
        """
        The satellite the update was asked at, from asked.json, if it was noted within
        ASKED_KEPT, and whether "I'm updated." has been said there already.
        """
        try:
            loaded = storage.load_json(self._path("asked.json"), {})
        except (OSError, ValueError):
            return None, False
        if not isinstance(loaded, dict):
            return None, False
        satellite, at = loaded.get("satellite"), loaded.get("at")
        if not isinstance(satellite, str) or not isinstance(at, (int, float)) or time.time() - at > ASKED_KEPT:
            return None, False
        return satellite, loaded.get("said") is True

    def caught_up(self, name: str) -> None:
        """
        Says how the update went at the satellite he asked at, once it is caught up after
        the restart - once only, on a thread of its own so its session isn't held up. The
        note of where he asked is kept, marked said, until the new code has run its minute,
        so a crash and start meanwhile doesn't say it again, and a take-back still says
        that it failed.

        Args:
            name: The satellite just caught up.
        """
        with self._lock:
            if self._to_say is None or self._to_say[0] != name:
                return
            satellite, message = self._to_say
            self._to_say = None
        if message == FAILED:
            self._remove("asked.json")
        else:
            try:
                loaded = storage.load_json(self._path("asked.json"), {})
                if isinstance(loaded, dict):
                    storage.save_json(self._path("asked.json"), dict(loaded, said=True))
            except (OSError, ValueError, TypeError) as e:
                logger.warning(f"Couldn't note that the update was said: {e}")
        threading.Thread(target=self._say, args=(satellite, message), daemon=True, name="update-said").start()

    # What's kept.

    def outcome(self) -> Dict:
        """
        How the last update went, for the settings page.

        Returns:
            {"state", "at", "from", "to", "reason", "command", "needs_you"}: state is one
            of STATES, or None (with everything else None) if there has been none, or its
            file can't be read. needs_you lists each {why, command}; reason and command
            are the first's, or why it failed.
        """
        empty = {field: None for field in FIELDS}
        empty["needs_you"] = []
        try:
            loaded = storage.load_json(self._path("outcome.json"), {})
        except (OSError, ValueError):
            return empty
        if not isinstance(loaded, dict) or loaded.get("state") not in STATES:
            return empty
        kept = {field: loaded.get(field) for field in FIELDS}
        kept["needs_you"] = _needs(loaded.get("needs_you"))
        return kept

    def _keep(self, state: str, reason: Optional[str], needs: Optional[List[Dict]] = None,
              source: Optional[str] = None, target: Optional[str] = None):
        """Keeps an outcome in outcome.json, and logs it with why. Never raises."""
        needs = needs or []
        first = needs[0] if needs else {}
        outcome = {"state": state, "at": _now_iso(), "from": source or self.version, "to": target,
                   "reason": reason or first.get("why"), "command": first.get("command"), "needs_you": needs}
        moved = f"{code_version.short(outcome['from'])} to {code_version.short(target)}"
        if state in ("current", "done") and not needs:
            logger.info(f"The brain's update: {state} ({moved})")
        else:
            commands = "; ".join(entry["command"] for entry in needs)
            logger.warning(f"The brain's update: {state} ({moved}): {outcome['reason']}"
                           + (f" - run: {commands}" if commands else ""))
        try:
            storage.save_json(self._path("outcome.json"), outcome)
        except (OSError, TypeError) as e:
            logger.warning(f"Couldn't keep how the brain's update went: {e}")

    def _say(self, satellite: Optional[str], message: str):
        """Says something at the satellite he asked at, through the brain. Never raises."""
        try:
            self.brain._say_update(satellite, message)
        except Exception:
            logger.exception("Couldn't say how the brain's update went")

    def _read(self, name: str) -> Optional[str]:
        """A small file in the update folder, stripped, or None if it isn't there or can't be read."""
        try:
            with open(self._path(name)) as file:
                return file.read().strip() or None
        except OSError:
            return None

    def _remove(self, name: str):
        """Removes a file from the update folder, if it is there."""
        try:
            os.remove(self._path(name))
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Couldn't remove update/{name}: {e}")
