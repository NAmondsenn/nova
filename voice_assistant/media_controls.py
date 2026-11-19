"""
Media Controls Module.

What the brain tells a satellite with Windows' media controls of its own music, for
Windows to show: whether it's playing, paused or stopped, the track, artist and album,
and the cover - each only when it changes, and the cover, which is the most of it, only
when it's a new one. See docs/decisions/0031. The session reads its player every
EVERY seconds, and straight after acting on one of the controls' buttons.
"""

import logging
from typing import Callable, Dict, Optional, Tuple

from voice_assistant.ha_device import MUSIC_EVERY
from voice_assistant.protocol import MAX_AUDIO

logger = logging.getLogger(__name__)

# How often the player is read, in seconds - as often as for Home Assistant's player.
EVERY = MUSIC_EVERY


def _text(value) -> str:
    """A track, artist or album as text - empty for none."""
    return "" if value is None else str(value)


class NowPlayingFeed:
    """
    Keeps one satellite's media controls showing what its own music is doing.

    Music the player says is elsewhere - Spotify on a phone, say, with "here" false - is
    as good as none here, so shows as stopped. Stopped, the satellite clears the controls,
    the cover with them, so the next cover is sent again whatever it is.
    """

    def __init__(self, send: Callable[..., bool]):
        """
        Args:
            send: Sends the satellite a now_playing - RemoteSatellite.send_now_playing,
                given the state, track, artist and album, and the cover only when it
                changes: an image, or None to take the last away. Says whether it was sent.
        """
        self._send = send
        # What was last sent, as (state, track, artist, album) - None before anything was.
        self._sent: Optional[Tuple[str, str, str, str]] = None
        # The id of the cover the satellite shows - None for none.
        self._shown: Optional[str] = None
        # The id of the last cover too big to send, so it's logged once and not read again.
        self._too_big: Optional[str] = None

    def update(self, playing: Optional[Dict], cover: Callable[[], Optional[Tuple[bytes, str]]],
               force: bool = False):
        """
        Sends the satellite what its music is doing now, if anything it shows has changed.

        Args:
            playing: What the player's now_playing() says - None with nothing playing.
            cover: The player's cover(), read only when the cover id is a new one.
            force: Send it even if nothing has changed - straight after a button, so
                the satellite shows what the brain made of the press.
        """
        if playing and playing.get("here", True) is False:
            playing = None
        if playing:
            now = ("playing" if playing.get("playing") else "paused", _text(playing.get("track")),
                   _text(playing.get("artist")), _text(playing.get("album")))
            wanted = playing.get("cover") or None
        else:
            now = ("stopped", "", "", "")
            wanted = None
        changed_cover, shown = {}, self._shown
        if now[0] == "stopped":
            # Cleared on the satellite along with everything else.
            shown = None
        elif wanted != self._shown:
            picture = self._picture(wanted, cover)
            if picture is not None:
                changed_cover, shown = {"cover": picture}, wanted
            elif self._shown is not None:
                # The last song's cover would be wrong for this one.
                changed_cover, shown = {"cover": None}, None
        if now == self._sent and not changed_cover and not force:
            return
        if self._send(*now, **changed_cover):
            self._sent, self._shown = now, shown

    def _picture(self, wanted: Optional[str], cover: Callable[[], Optional[Tuple[bytes, str]]]) -> Optional[bytes]:
        """
        The cover with this id, if there is one which can be sent - else None, to be asked
        for again next time unless it was too big.
        """
        if wanted is None or wanted == self._too_big:
            return None
        picture = cover()
        if not picture or not picture[0]:
            return None
        if len(picture[0]) > MAX_AUDIO:
            logger.warning(f"A cover of {len(picture[0])} bytes is too big to send to Windows' media controls")
            self._too_big = wanted
            return None
        return picture[0]
