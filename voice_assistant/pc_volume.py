"""
PC Volume Module.

Windows' own volume and mute, on the PC satellite - the master volume of the default
output, which the volume keys change and which scales everything the PC plays, Nova
included. See docs/superpowers/specs/2026-11-19-pc-open-close-and-commands-design.md.

It goes through Core Audio's IAudioEndpointVolume, by COM, with comtypes: the two short
interfaces it needs are written out here rather than taken from another package.
comtypes is only on the PC, imported only on Windows and only on the volume's own
thread, so the Pi and the brain never need it.
"""

import logging
import queue
import sys
import threading
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The most seconds the satellite waits for Windows' volume to be reached when it starts,
# and for each change to it after.
SET_UP_WAIT = 10
JOB_WAIT = 5

# How far "up" or "down" moves it, in percent, when the brain doesn't say.
STEP = 10


def _apply(endpoint, direction: Optional[str], step: Optional[int], level: Optional[int],
           mute: Optional[bool]) -> Dict:
    """
    Changes the volume as asked, as Windows' own keys do: a new level, up or down, also
    unmutes.

    Args:
        endpoint: The default output's volume - _Endpoint, or a fake in the tests.
        direction: "up" or "down", or None.
        step: How far up or down, in percent - STEP if None.
        level: The level to set, in percent, kept to 0 to 100 - or None.
        mute: True to mute, False to unmute, or None to leave it.

    Returns:
        {"level": percent, "muted": bool}, as Windows has them now.
    """
    if level is None and direction in ("up", "down"):
        now = round(endpoint.level() * 100)
        change = STEP if step is None else step
        level = now + change if direction == "up" else now - change
    if level is not None:
        endpoint.set_level(max(0, min(100, level)) / 100)
        if endpoint.muted():
            endpoint.set_mute(False)
    if mute is not None:
        endpoint.set_mute(bool(mute))
    return {"level": round(endpoint.level() * 100), "muted": bool(endpoint.muted())}


class SystemVolume:
    """
    Windows' master volume and mute, reached from one thread of its own for as long as
    the satellite runs.
    """

    def __init__(self, backend: Optional[Callable[[], object]] = None):
        """
        Args:
            backend: Makes what reaches Windows' volume, on the volume's own thread -
                _CoreAudio, which is used when this is left out on Windows. Off Windows,
                left out, there's none. Tests give a fake.
        """
        self._make = backend if backend is not None else (_CoreAudio if sys.platform == "win32" else None)
        self._backend = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._owner: Optional[threading.Thread] = None
        if self._make is None:
            return
        # Everything to do with COM is done on this one thread, as with Windows' media
        # controls (see windows_media.py): on the PC on 19 November, COM used from the
        # satellite's main thread once PortAudio had started there misbehaved. COM's
        # pointers are made and let go of here too.
        self._owner = threading.Thread(target=self._serve, daemon=True, name="windows-volume")
        self._owner.start()
        try:
            self._do(self._set_up, SET_UP_WAIT)
        except Exception as e:
            logger.warning(f"Couldn't reach Windows' volume, so Nova can't change it ({type(e).__name__})")

    def _set_up(self):
        """Makes the backend, on the owning thread."""
        self._backend = self._make()

    @property
    def available(self) -> bool:
        """Whether Windows' volume can be changed - and so whether the hello says there's a system volume."""
        return self._backend is not None

    def _serve(self):
        """The thread which owns COM: does each job handed to it, in turn, until told to stop."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            work, done, answer = job
            try:
                answer["result"] = work()
            except Exception as e:
                answer["error"] = e
            finally:
                done.set()

    def _do(self, work, wait: float):
        """
        Has the owning thread do something, and waits for it.

        Returns:
            What it gave.

        Raises:
            TimeoutError: It took longer than `wait` seconds.
            Exception: Whatever it raised.
        """
        done, answer = threading.Event(), {}
        self._jobs.put((work, done, answer))
        if not done.wait(wait):
            raise TimeoutError("Windows' volume took too long to answer")
        if "error" in answer:
            raise answer["error"]
        return answer.get("result")

    def change(self, direction: Optional[str] = None, step: Optional[int] = None, level: Optional[int] = None,
               mute: Optional[bool] = None) -> Dict:
        """
        Changes Windows' volume, or with nothing asked only says what it is.

        Args:
            direction: "up" or "down", or None.
            step: How far up or down, in percent.
            level: The level to set, in percent.
            mute: True to mute, False to unmute.

        Returns:
            {"level": percent, "muted": bool}, as Windows has them now.

        Raises:
            RuntimeError: There's no volume to change here.
            TimeoutError or OSError: Windows didn't answer, or refused.
        """
        if self._backend is None:
            raise RuntimeError("Windows' volume can't be reached")
        backend = self._backend
        return self._do(lambda: _apply(backend.endpoint(), direction, step, level, mute), JOB_WAIT)

    def close(self):
        """Lets the owning thread go, as the satellite stops."""
        if self._owner is not None:
            self._jobs.put(None)


class _Endpoint:
    """The default output's master volume, as 0 to 1, and its mute."""

    def __init__(self, volume):
        self._volume = volume

    def level(self) -> float:
        return float(self._volume.GetMasterVolumeLevelScalar())

    def set_level(self, level: float):
        self._volume.SetMasterVolumeLevelScalar(level, None)

    def muted(self) -> bool:
        return bool(self._volume.GetMute())

    def set_mute(self, mute: bool):
        self._volume.SetMute(mute, None)


class _CoreAudio:
    """
    Windows' Core Audio, by COM through comtypes - made, and only ever used, on
    SystemVolume's own thread. Raises ImportError where comtypes isn't installed.
    """

    # The public Windows SDK numbers of what's used: the device enumerator's class, and the
    # three interfaces (mmdeviceapi.h, endpointvolume.h).
    CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_DEVICE = "{D666063F-1587-4E43-81F1-B948E807363F}"
    IID_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
    # Sound out, and the default device for it - the one the volume keys change.
    RENDER = 0
    CONSOLE = 0

    def __init__(self):
        from ctypes import POINTER, c_float, c_int, c_uint, c_void_p
        from ctypes.wintypes import BOOL, DWORD

        # The first import of comtypes starts COM on the thread importing it - this one.
        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        # And in case something else imported it first, on another thread.
        comtypes.CoInitialize()
        context = POINTER(GUID)

        # Each interface's methods in Windows' order, as far as the last one used: COM finds
        # them by place.
        class IAudioEndpointVolume(IUnknown):
            _iid_ = GUID(self.IID_ENDPOINT_VOLUME)
            _methods_ = [
                COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
                COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "pNotify")),
                COMMETHOD([], HRESULT, "GetChannelCount", (["out", "retval"], POINTER(c_uint), "pnChannelCount")),
                COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "fLevelDB"),
                          (["in"], context, "pguidEventContext")),
                COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "fLevel"),
                          (["in"], context, "pguidEventContext")),
                COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out", "retval"], POINTER(c_float), "pfLevelDB")),
                COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out", "retval"], POINTER(c_float), "pfLevel")),
                COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint, "nChannel"),
                          (["in"], c_float, "fLevelDB"), (["in"], context, "pguidEventContext")),
                COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint, "nChannel"),
                          (["in"], c_float, "fLevel"), (["in"], context, "pguidEventContext")),
                COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint, "nChannel"),
                          (["out", "retval"], POINTER(c_float), "pfLevelDB")),
                COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint, "nChannel"),
                          (["out", "retval"], POINTER(c_float), "pfLevel")),
                COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "bMute"), (["in"], context, "pguidEventContext")),
                COMMETHOD([], HRESULT, "GetMute", (["out", "retval"], POINTER(BOOL), "pbMute")),
            ]

        class IMMDevice(IUnknown):
            _iid_ = GUID(self.IID_DEVICE)
            _methods_ = [
                COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"),
                          (["in"], c_void_p, "pActivationParams"),
                          (["out", "retval"], POINTER(POINTER(IUnknown)), "ppInterface")),
            ]

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID(self.IID_ENUMERATOR)
            _methods_ = [
                COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "dataFlow"),
                          (["in"], DWORD, "dwStateMask"), (["out", "retval"], POINTER(c_void_p), "ppDevices")),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "dataFlow"), (["in"], c_int, "role"),
                          (["out", "retval"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
            ]

        self._interface = IAudioEndpointVolume
        self._clsctx = comtypes.CLSCTX_ALL
        self._enumerator = comtypes.CoCreateInstance(GUID(self.CLSID_ENUMERATOR), IMMDeviceEnumerator,
                                                     comtypes.CLSCTX_INPROC_SERVER)
        # Reached once now, so a PC with no output says so as the satellite starts.
        self.endpoint().level()

    def endpoint(self) -> _Endpoint:
        """The default output's volume - looked up each time, as headphones plugged in change which it is."""
        device = self._enumerator.GetDefaultAudioEndpoint(self.RENDER, self.CONSOLE)
        activated = device.Activate(self._interface._iid_, self._clsctx, None)
        return _Endpoint(activated.QueryInterface(self._interface))
