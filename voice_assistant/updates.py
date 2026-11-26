"""
Updates Module.

The brain's side of "update once, everything updates". The brain's own commit is the one
every satellite should run, so each satellite which connects on another commit is offered
the brain's, in the format it said it takes, proved with the keys of that connection. This
also keeps what the settings page shows: which commit each satellite runs, whether it is
behind, being updated, failed or needs the user to run a command, kept in a file so a
satellite which is away is still listed.
"""

import collections
import logging
import os
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from voice_assistant import code_version, protocol, storage
from voice_assistant.remote import RemoteSatellite
from voice_assistant.update_archive import ArchiveError, make_bundle, make_zip

logger = logging.getLogger(__name__)

# How many bundles are kept, each made from one satellite's commit, so satellites on
# the same old commit connecting together are not each given a fresh one.
BUNDLES_KEPT = 4

# Said of a satellite whose hello didn't say it takes updates, as it never will until
# its code is replaced by hand once.
OLD_REASON = "Update by hand once"


class Updates:
    """Offers satellites the brain's commit, and knows how each one's update is going."""

    def __init__(self, project: str, version: Optional[str], password_for: Callable[[str], Optional[str]],
                 folder: str):
        """
        Args:
            project: The brain's project folder, which the archives are made from.
            version: The commit the brain runs, read once as it started - or None if it
                can't tell, in which case nothing is offered.
            password_for: Gives a satellite's password by its name, or None if it has none.
            folder: Where satellites.json is kept, made if it isn't there.
        """
        self.project = project
        self.version = version
        self.password_for = password_for
        self.folder = folder
        # Guards everything below and the file. Never held while an archive is made or sent.
        self._lock = threading.Lock()
        self._zip: Optional[Tuple[str, bytes]] = None
        self._bundles: "collections.OrderedDict[Tuple[str, Optional[str]], bytes]" = collections.OrderedDict()
        # How each satellite's update is going: {"state", "reason", "command"}.
        self._states: Dict[str, Dict] = {}
        self._known: Dict[str, Dict] = {}
        # The satellites an offer is being made or sent to, so two archives are never
        # interleaved on one connection.
        self._offering: set = set()
        try:
            loaded = storage.load_json(self._path(), {})
            if isinstance(loaded, dict):
                # Nothing is connected until it says hello, whatever the file last said.
                self._known = {name: dict(entry, connected=False) for name, entry in loaded.items()
                               if isinstance(name, str) and isinstance(entry, dict)}
        except (OSError, ValueError) as e:
            logger.warning(f"Couldn't read the satellites' versions: {e}")

    def _path(self) -> str:
        return os.path.join(self.folder, "satellites.json")

    def _save(self):
        """Writes what's known of the satellites. The caller holds the lock."""
        try:
            os.makedirs(self.folder, exist_ok=True)
            storage.save_json(self._path(), self._known)
        except (OSError, TypeError) as e:
            logger.warning(f"Couldn't save the satellites' versions: {e}")

    def paused(self) -> bool:
        """
        Whether the brain's code has moved under it: the updater has changed the clone
        and the brain hasn't restarted, so what it would offer isn't what it runs.
        """
        return code_version.running_version(self.project) != self.version

    def seen(self, satellite: RemoteSatellite) -> None:
        """Notes a satellite which has connected - what it runs and how it updates."""
        with self._lock:
            self._known[satellite.name] = {"version": satellite.version, "updates": satellite.updates,
                                           "format": satellite.update_format, "connected": True}
            # It has come back, so an update which was going on has ended one way or another.
            if self._states.get(satellite.name, {}).get("state") == "updating":
                del self._states[satellite.name]
            self._save()

    def gone(self, name: str) -> None:
        """Notes a satellite which has disconnected."""
        with self._lock:
            if name in self._known:
                self._known[name]["connected"] = False
                self._save()

    def _archive(self, format: str, base: Optional[str]) -> bytes:
        """
        The brain's code in a format, from the cache if it was made already.

        Raises:
            ArchiveError: If it can't be made.
        """
        version = self.version
        with self._lock:
            if format == "zip":
                if self._zip is not None and self._zip[0] == version:
                    return self._zip[1]
            elif (version, base) in self._bundles:
                self._bundles.move_to_end((version, base))
                return self._bundles[(version, base)]
        # Without the lock, as git can take a while.
        if format == "zip":
            made = make_zip(self.project, version)
        else:
            made = make_bundle(self.project, version, base)
        with self._lock:
            if format == "zip":
                self._zip = (version, made)
            else:
                self._bundles[(version, base)] = made
                while len(self._bundles) > BUNDLES_KEPT:
                    self._bundles.popitem(last=False)
        return made

    def _set(self, name: str, state: str, reason: Optional[str] = None, command: Optional[str] = None):
        with self._lock:
            self._states[name] = {"state": state, "reason": reason, "command": command}

    def offer(self, satellite: RemoteSatellite, retry: bool = False) -> bool:
        """
        Offers a satellite the brain's commit, if it is behind and can take it.

        Args:
            satellite: The connected satellite.
            retry: Whether this is the second go at the same version.

        Returns:
            Whether the update was sent. False, with nothing sent, if the satellite
            doesn't take updates, takes none of the formats, is on the brain's commit,
            the brain has no commit or has moved from it, or its password or nonces
            aren't known; or if the archive couldn't be made or the satellite went.
        """
        name = satellite.name
        if (not satellite.updates or satellite.update_format not in protocol.UPDATE_FORMATS
                or self.version is None or satellite.version == self.version):
            return False
        if self.paused():
            logger.info(f"Not offering the {name} satellite an update, as the brain's code has moved and it "
                        f"hasn't restarted")
            return False
        if satellite.nonces is None:
            logger.warning(f"Not offering the {name} satellite an update, as its connection has no nonces")
            return False
        password = self.password_for(name)
        if not password:
            logger.warning(f"Not offering the {name} satellite an update, as the brain has no password for it")
            return False
        with self._lock:
            if name in self._offering:
                busy = True
            else:
                busy = False
                self._offering.add(name)
        if busy:
            logger.info(f"Not offering the {name} satellite an update, as one is already being sent to it")
            return False
        try:
            try:
                archive = self._archive(satellite.update_format, satellite.version)
            except ArchiveError as e:
                logger.warning(f"Couldn't make the {name} satellite's update: {e}")
                # Not the satellite's failure if the brain's code moved while it was made.
                if not self.paused():
                    self._set(name, "failed", "The brain couldn't make the update")
                return False
            proof = protocol.update_proof(password, *satellite.nonces, self.version, satellite.update_format,
                                          archive)
            self._set(name, "updating")
            logger.info(f"Offered the {name} satellite {code_version.short(self.version)}")
            if not satellite.send_update(self.version, satellite.update_format, archive, proof, retry):
                logger.warning(f"The {name} satellite went while its update was being sent")
                self._set(name, "failed", "The connection went while the update was being sent")
                return False
            return True
        finally:
            with self._lock:
                self._offering.discard(name)

    def result(self, name: str, message: Dict) -> None:
        """
        Notes how a satellite's update went, as it said.

        Args:
            name: The satellite.
            message: Its update_result: version, ok, and error or needs_you.
        """
        version = message.get("version")
        needs = message.get("needs_you") or []
        if message.get("ok") and version == self.version:
            if needs:
                first = needs[0]
                self._set(name, "needs_you", first.get("why"), first.get("command"))
                logger.info(f"The {name} satellite is on {code_version.short(version)}, but needs you: "
                            f"{first.get('why')}")
            else:
                self._set(name, "current")
                logger.info(f"The {name} satellite is updated to {code_version.short(version)}")
            return
        error = message.get("error") or "It is not on the brain's commit"
        self._set(name, "failed", error)
        logger.warning(f"The {name} satellite's update to {code_version.short(version)} failed: {error}")

    def installing(self, name: str, version: str) -> None:
        """Notes a satellite which has begun to install."""
        self._set(name, "updating")
        logger.info(f"The {name} satellite is installing {code_version.short(version)}")

    def behind(self) -> List[str]:
        """The connected satellites which take updates and aren't on the brain's commit."""
        with self._lock:
            return [name for name, entry in self._known.items()
                    if entry.get("connected") and entry.get("updates") and entry.get("version") != self.version]

    def versions(self, known: Iterable[str]) -> Dict:
        """
        What the settings page shows.

        Args:
            known: The satellites' names, in the order to list them.

        Returns:
            {"brain": {"version", "short", "date"}, "satellites": [{"name", "version",
            "short", "date", "connected", "state", "reason", "command"}]}. State is
            "current", "behind", "updating", "failed", "needs_you" or "old".
        """
        with self._lock:
            known_entries = {name: dict(entry) for name, entry in self._known.items()}
            states = {name: dict(entry) for name, entry in self._states.items()}
        satellites = []
        for name in known:
            entry = known_entries.get(name, {})
            version = entry.get("version")
            reason = command = None
            kept = states.get(name)
            if entry.get("updates") is False:
                state, reason = "old", OLD_REASON
            elif kept is not None and not (kept["state"] == "failed" and version == self.version):
                state, reason, command = kept["state"], kept["reason"], kept["command"]
            elif self.version is not None and version != self.version:
                state = "behind"
            else:
                state = "current"
            satellites.append({"name": name, "version": version, "short": code_version.short(version),
                               "date": code_version.commit_date(self.project, version),
                               "connected": bool(entry.get("connected")), "state": state,
                               "reason": reason, "command": command})
        return {"brain": {"version": self.version, "short": code_version.short(self.version),
                          "date": code_version.commit_date(self.project, self.version)},
                "satellites": satellites}
