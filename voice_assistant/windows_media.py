"""
Windows Media Module.

Nova's music in Windows' own media controls, on the PC satellite - see
docs/decisions/0031. The brain says what the satellite's music is doing (now_playing),
and this shows it as a music session: the track, artist, album and cover, playing or
paused, in the volume pop-up and wherever else Windows shows what's playing - and so in
HASS.Agent's media player, which reads them. Stopped, it's cleared and switched off. The
media keys, Numpad Enter's play/pause among them, and the pop-up's buttons then come
here, and go to the brain as media.

It goes through winrt, the Python projection of Windows' own API, which is only on the
PC: it's imported on first use, and only on Windows, so the Pi and the brain never
need it. Without it, or off Windows, WindowsMedia does nothing and says it isn't
available, and the satellite's hello says it has no media controls.

Windows names a session after its program, "pythonw.exe", unless the process says
which app it is: it says Nova.Satellite, the AppUserModelID of the Start-menu shortcut
"Nova" which scripts/windows/install.ps1 makes, so Windows shows "Nova".
"""

import asyncio
import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# What this process says it is, so Windows names its media session after the Start-menu
# shortcut with the same AppUserModelID - see install.ps1.
APP_ID = "Nova.Satellite"

# What show is given for a cover which is as it was.
UNCHANGED = object()


class _Winrt:
    """
    The few parts of winrt WindowsMedia uses, imported only when this is made - which
    raises ImportError where winrt isn't installed. Tests give WindowsMedia a fake of
    this instead.
    """

    def __init__(self):
        from winrt.windows.media import (MediaPlaybackStatus, MediaPlaybackType,
                                         SystemMediaTransportControlsButton)
        from winrt.windows.media.playback import MediaPlayer
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream, RandomAccessStreamReference

        self.MediaPlayer = MediaPlayer
        self.MediaPlaybackStatus = MediaPlaybackStatus
        self.MediaPlaybackType = MediaPlaybackType
        self.SystemMediaTransportControlsButton = SystemMediaTransportControlsButton
        self._DataWriter = DataWriter
        self._InMemoryRandomAccessStream = InMemoryRandomAccessStream
        self._RandomAccessStreamReference = RandomAccessStreamReference

    @staticmethod
    def set_app_id(app_id: str):
        """Says which app this process is, before anything Windows names after it is made."""
        import ctypes

        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        if result != 0:
            raise OSError(f"SetCurrentProcessExplicitAppUserModelID failed ({result:#x})")

    def thumbnail(self, picture: bytes):
        """
        A cover for the display, from the picture's bytes: written into a stream in
        memory, which is read from its start.
        """
        stream = self._InMemoryRandomAccessStream()
        writer = self._DataWriter(stream)
        writer.write_bytes(picture)

        async def store():
            await writer.store_async()
            await writer.flush_async()

        # Called on the satellite link's thread, which has no event loop of its own.
        asyncio.run(store())
        writer.detach_stream()
        stream.seek(0)
        return self._RandomAccessStreamReference.create_from_stream(stream)


def _load_backend():
    """winrt's parts, on Windows with winrt installed - else None, said in the log."""
    if sys.platform != "win32":
        return None
    try:
        return _Winrt()
    except ImportError as e:
        logger.info(f"winrt isn't installed, so Nova's music isn't shown in Windows' media controls ({e})")
    except Exception as e:
        logger.warning(f"winrt couldn't be loaded, so Nova's music isn't shown in Windows' media controls ({e})")
    return None


# The most seconds the satellite waits for Windows' media controls to be set up.
SET_UP_WAIT = 10


class WindowsMedia:
    """
    One music session in Windows' media controls, for as long as the satellite runs:
    shows what the brain says the music is doing, and passes on the buttons pressed.
    """

    def __init__(self, on_button: Callable[[str], None], backend=None):
        """
        Args:
            on_button: Called with "play", "pause", "next" or "previous" for each of those
                buttons pressed - on a thread of Windows', so it mustn't wait long.
            backend: winrt's parts, as _Winrt has them - left out, they're loaded, on
                Windows with winrt installed. Tests give a fake.
        """
        self._on_button = on_button
        # Show and close come from the link's thread and the satellite's own.
        self._lock = threading.Lock()
        self._backend = backend if backend is not None else _load_backend()
        self._controls = None
        self._token = None
        # Whether the controls are switched on, showing music.
        self._enabled = False
        # Whether the last show failed, so a display which keeps failing is logged once -
        # and likewise the last cover.
        self._failing = False
        self._cover_failing = False
        if self._backend is None:
            return
        # On a fresh thread, waited for: the satellite's own has started PortAudio by now,
        # which leaves it in a single-threaded apartment with no message pump, and a handler
        # registered there was only given Windows' buttons once the thread ended (seen on the
        # PC on 19 November). A fresh one is in the multithreaded apartment, where they come
        # at once, on a thread of Windows'. What's made works from any thread after.
        setting_up = threading.Thread(target=self._set_up, daemon=True, name="windows-media")
        setting_up.start()
        setting_up.join(SET_UP_WAIT)

    def _set_up(self):
        """Makes the player and its controls, and registers the button handler - see __init__."""
        try:
            backend = self._backend
            # First, as Windows takes the name from the process as the controls are made.
            # Without it they still work, named "pythonw.exe".
            try:
                backend.set_app_id(APP_ID)
            except Exception as e:
                logger.warning(f"Couldn't name the satellite {APP_ID} for Windows: {e}")
            self._player = backend.MediaPlayer()
            # The player plays nothing: its own handling of the buttons is off, so they
            # come to the handler below.
            self._player.command_manager.is_enabled = False
            controls = self._player.system_media_transport_controls
            controls.is_enabled = False
            buttons = backend.SystemMediaTransportControlsButton
            self._names = {buttons.PLAY: "play", buttons.PAUSE: "pause", buttons.NEXT: "next",
                           buttons.PREVIOUS: "previous"}
            self._token = controls.add_button_pressed(self._pressed)
            self._controls = controls
        except Exception as e:
            logger.warning(f"Windows' media controls couldn't be set up, so Nova's music isn't shown there: {e}")

    @property
    def available(self) -> bool:
        """Whether Nova's music can be shown - and so whether the hello says it has media controls."""
        return self._controls is not None

    def show(self, state: str, track: str, artist: str, album: str, cover=UNCHANGED):
        """
        Shows what the music is doing, as the brain's now_playing says. Never raises: a
        display which fails is logged, once until it works again.

        Args:
            state: "playing", "paused" or "stopped" - which clears it and switches it off.
            track: What's playing.
            artist: Who it's by.
            album: What it's from.
            cover: The new cover's picture, None to take the last away, or left out to
                keep it.
        """
        with self._lock:
            if self._controls is None:
                return
            try:
                if state == "stopped":
                    self._clear()
                else:
                    self._show(state, track, artist, album, cover)
                self._failing = False
            except Exception as e:
                if not self._failing:
                    logger.warning(f"Couldn't show Nova's music in Windows' media controls: {e}")
                self._failing = True

    def close(self):
        """Lets go of the buttons and switches the controls off, as the satellite stops."""
        with self._lock:
            controls, self._controls = self._controls, None
            if controls is None:
                return
            try:
                controls.remove_button_pressed(self._token)
                controls.is_enabled = False
            except Exception as e:
                logger.warning(f"Couldn't switch Windows' media controls off: {e}")

    def _show(self, state: str, track: str, artist: str, album: str, cover):
        """Shows the music, and switches the controls on if they're off. Called with the lock held."""
        backend, controls = self._backend, self._controls
        # Switched on first, then the display and the status, the order the test on the PC
        # on 19 November proved.
        if not self._enabled:
            controls.is_enabled = True
            # Each on a line of its own: set together in one chained line, Windows left
            # next and previous off without an error (docs/decisions/0031).
            controls.is_play_enabled = True
            controls.is_pause_enabled = True
            controls.is_next_enabled = True
            controls.is_previous_enabled = True
            self._enabled = True
        updater = controls.display_updater
        # Each time, as clearing forgets it.
        updater.type = backend.MediaPlaybackType.MUSIC
        properties = updater.music_properties
        properties.title = track
        properties.artist = artist
        properties.album_title = album
        if cover is not UNCHANGED:
            updater.thumbnail = self._thumbnail(cover)
        updater.update()
        playing = state == "playing"
        controls.playback_status = (backend.MediaPlaybackStatus.PLAYING if playing
                                    else backend.MediaPlaybackStatus.PAUSED)

    def _thumbnail(self, cover: Optional[bytes]):
        """
        The cover for the display, or None for none - also when it can't be made, which is
        logged once until a cover can be made again.
        """
        if not cover:
            return None
        try:
            thumbnail = self._backend.thumbnail(cover)
        except Exception as e:
            if not self._cover_failing:
                logger.warning(f"Couldn't show the cover in Windows' media controls: {e}")
            self._cover_failing = True
            return None
        self._cover_failing = False
        return thumbnail

    def _clear(self):
        """Clears what's shown and switches the controls off, if they're on. Called with the lock held."""
        if not self._enabled:
            return
        controls = self._controls
        controls.playback_status = self._backend.MediaPlaybackStatus.STOPPED
        controls.is_enabled = False
        self._enabled = False
        updater = controls.display_updater
        updater.clear_all()
        updater.update()

    def _pressed(self, sender, args):
        """A button pressed, on a thread of Windows': the four Nova acts on are passed on, the rest ignored."""
        try:
            action = self._names.get(args.button)
            if action is not None:
                self._on_button(action)
        except Exception:
            logger.exception("Couldn't pass on a button of Windows' media controls")
