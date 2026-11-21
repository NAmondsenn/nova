"""
Open Conversation Module.

Whether an open conversation is on, where, and the facts that end it - see
docs/superpowers/specs/2026-11-21-conversation-mode-design.md. In one Nova listens after
every reply, with no wake word or press, until he ends it or it ends itself. There is one
for the whole brain, as only one conversation is allowed at a time, and sessions, the
language model, Home Assistant's switch and the brain's own threads all ask it and tell
it, each on a thread of its own.

OpenConversation only keeps the facts and works out from them: it plays no sound, sends
nothing and writes nothing to disk, so what was said in a conversation is let go when it
ends, after the step given to it has had the whole of it. What each fact does - saying
"I'll leave it there.", the falling sound, closing the microphone - is for whoever calls
end() to do, so there is one place which decides a conversation is over and many which
carry it out.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# The most turns in a row Claude may judge not meant for Nova before the conversation ends.
# Each costs a request with the whole history, and a film playing to an empty room says
# nothing a quiet spell would catch, as these turns don't put it off.
NOT_FOR_ME_MOST = 3


class OpenConversation:
    """Whether a conversation is on, at which satellite, and what has been said in it."""

    def __init__(self, settings: Callable[[], Dict], now: Callable[[], float] = time.monotonic,
                 ended: Optional[Callable[[List[Dict]], None]] = None):
        """
        Args:
            settings: Returns the `conversation` section of the config as it is now. Read
                each time a value is needed, so a save on the settings page is used at once.
            now: The time, by time.monotonic() - given so tests can move it.
            ended: Called with the exchanges of a conversation as it ends - the hook a plan
                is saved from. None means nothing is called.
        """
        self._settings = settings
        self._now = now
        self._ended = ended
        # When the last conversation at each satellite ended, by `now` - see ended_at.
        self._ended_at: Dict[str, float] = {}
        # Sessions, the brain's threads and Home Assistant's switch all come here.
        self._lock = threading.Lock()
        self._reset(None)

    def _reset(self, satellite: Optional[str]):
        """Starts afresh at a satellite, or with none on (None). Call with the lock held."""
        self._satellite = satellite
        self._exchanges: List[Dict] = []
        self._not_for_me = 0
        self._bigger = False
        # Counted from the start, so a conversation nobody speaks in still ends.
        self._last_answered = self._now()

    @property
    def satellite(self) -> Optional[str]:
        """The satellite a conversation is on at, or None if none is."""
        with self._lock:
            return self._satellite

    def ended_at(self, satellite: str) -> Optional[float]:
        """
        When the last conversation at a satellite ended, as `now` has it, or None if none
        has. The language model, which isn't told when one ends, counts the age of what was
        said in it from here. Kept for each satellite, as each one's language model notices
        an end only when it next uses its history, by when another satellite's may have ended.
        """
        with self._lock:
            return self._ended_at.get(satellite)

    @property
    def enabled(self) -> bool:
        """Whether conversations can be started at all, as `enabled` is set now."""
        return bool(self._settings().get("enabled", True))

    @property
    def switch_itself(self) -> bool:
        """Whether Claude may move the conversation up to the bigger model by itself, as `switch_itself` is set now."""
        return bool(self._settings().get("switch_itself", True))

    @property
    def bigger(self) -> bool:
        """Whether the conversation has moved up to the bigger model."""
        with self._lock:
            return self._bigger

    @bigger.setter
    def bigger(self, value: bool):
        with self._lock:
            # A turn still being answered when the conversation ended from another thread
            # must not leave the bigger model on for the ordinary requests which follow.
            if self._satellite is not None:
                self._bigger = value

    def on_at(self, satellite: Optional[str]) -> bool:
        """Whether a conversation is on at this satellite. False for None."""
        with self._lock:
            return satellite is not None and self._satellite == satellite

    def start(self, satellite: str) -> str:
        """
        Starts a conversation at a satellite, if none is on and they are allowed.

        Returns:
            "started", "already here" (one is on at this satellite), "elsewhere" (one is on
            at another) or "off" (`enabled` is false).
        """
        enabled = self._settings().get("enabled", True)
        with self._lock:
            if not enabled:
                return "off"
            if self._satellite is None:
                self._reset(satellite)
                return "started"
            return "already here" if self._satellite == satellite else "elsewhere"

    def end(self, reason: str, satellite: Optional[str] = None) -> bool:
        """
        Ends the conversation, handing what was said in it to the `ended` step.

        Args:
            reason: Why: "said", "switch", "idle", "not for nova", "muted", "gone" or
                "failed" (a turn went wrong).
            satellite: Where it is to be ended. If given and the conversation is on
                elsewhere, it is left be - a satellite going just as one starts at another
                can't end that one.

        Returns:
            False if none was on (there, if given), when nothing is called.
        """
        cap = self._cap()
        with self._lock:
            if self._satellite is None or satellite not in (None, self._satellite):
                return False
            exchanges = self._exchanges[-cap:]
            self._ended_at[self._satellite] = self._now()
            self._reset(None)
        # Outside the lock, as the step may take a while or come back here.
        if self._ended is not None:
            try:
                self._ended(exchanges)
            except Exception as e:
                # What ends a conversation is its own business: a plan which couldn't be
                # saved must not leave the microphone open.
                logger.error(f"The step for a conversation which ended ({reason}) failed: {type(e).__name__}")
        return True

    def answered(self, user: str, assistant: str, model: str, satellite: Optional[str] = None):
        """
        Notes a turn Nova answered, which puts off the quiet spell and ends the run of turns
        not meant for it. Does nothing if no conversation is on.

        Args:
            user: What he said.
            assistant: What Nova replied.
            model: The model which answered.
            satellite: Where the turn was heard. If given and the conversation is on
                elsewhere, the turn is from one which has ended, and is left out of this one.
        """
        cap = self._cap()
        with self._lock:
            if self._satellite is None or satellite not in (None, self._satellite):
                return
            self._exchanges.append({"user": user, "assistant": assistant,
                                    "at": datetime.now().isoformat(timespec="seconds"), "model": model})
            # Past the cap the oldest go, as the history is sent with every turn.
            del self._exchanges[:-cap]
            self._not_for_me = 0
            self._last_answered = self._now()

    def not_for_nova(self, satellite: Optional[str] = None) -> bool:
        """
        Notes a turn Claude judged not meant for Nova. It doesn't put off the quiet spell.

        Args:
            satellite: Where the turn was heard. If given and the conversation is on
                elsewhere, the turn is from one which has ended, and isn't counted.

        Returns:
            True if it is the NOT_FOR_ME_MOST-th in a row, when the caller is to end the
            conversation. False if none is on.
        """
        with self._lock:
            if self._satellite is None or satellite not in (None, self._satellite):
                return False
            self._not_for_me += 1
            return self._not_for_me >= NOT_FOR_ME_MOST

    def idle_in(self) -> float:
        """
        Returns:
            Seconds until the quiet spell since the last turn Nova answered ends the
            conversation - 0 or less if it is due, infinity if none is on.
        """
        minutes = self._settings().get("idle_minutes", 5)
        with self._lock:
            if self._satellite is None:
                return float("inf")
            return self._last_answered + minutes * 60 - self._now()

    def model(self, usual: str) -> str:
        """Returns the bigger model while a conversation is on and has moved up, else the usual one."""
        bigger_model = self._settings().get("bigger_model", "claude-sonnet-5-5")
        with self._lock:
            # Only in a conversation, so an ordinary request is never answered by the bigger model.
            return bigger_model if self._bigger and self._satellite is not None else usual

    def _cap(self) -> int:
        """Returns the most exchanges one conversation remembers."""
        return max(1, int(self._settings().get("history_exchanges", 100)))
