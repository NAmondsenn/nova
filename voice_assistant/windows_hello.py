"""
Windows Hello Module.

The PC satellite's way to give the user's OK to a big action: the brain sends an
approval, and this asks Windows Hello (fingerprint, face or PIN) with the action's
description as the prompt's message. Only a verified answer is a yes.

LIVE CHECK PENDING: the test of Hello from the windowless satellite on the PC (Task 0 of
the approvals plan) hasn't been run. So this tries, in turn, what should work: first the
interop which asks for a window of the satellite's own, a hidden one made here
(IUserConsentVerifierInterop::RequestVerificationForWindowAsync), and if that can't be
had or fails, the plain RequestVerificationAsync, which a desktop program with no window
may show behind other windows. Which of them brings the prompt to the front, and whether
cancelling one closes it, is for the deploy's hand check. If neither works, the PC's
"Can approve" is switched off on the settings page and the phone approves alone.

It goes through winrt, the Python projection of Windows' own API, which is only on the
PC: it's imported on first use, and only on Windows. Without it, off Windows, or with
Hello not set up, WindowsHello isn't available and the satellite's hello says it doesn't
approve.
"""

import collections
import logging
import queue
import sys
import threading
import time
import uuid
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# The most of a description shown as the prompt's message.
MESSAGE_MOST = 200

# The most seconds the satellite waits for Hello to be set up and its availability seen.
SET_UP_WAIT = 10
# How often, in seconds, the owning thread looks at a prompt showing, to see whether it's
# been answered or should be cancelled.
POLL = 0.1
# How long a cancelled prompt has to go before the owning thread stops waiting on it -
# so a prompt Windows won't cancel can't hold up every one after it for ever.
CANCEL_WAIT = 10
# The most seconds the satellite waits for Hello to close as it stops: longer than a
# prompt takes to go when cancelled, but never as long as one which won't.
CLOSE_WAIT = 5
# How many withdrawn ids are remembered, so an approval which arrives after its withdraw
# is never shown.
WITHDRAWN_KEPT = 50

# The interop's IID, from UserConsentVerifierInterop.h in the Windows SDK (as its metadata
# gives it).
INTEROP_IID = uuid.UUID("39e050c3-4e74-441a-8dc0-b81104df949c")
# IAsyncInfo's IID, from the Windows SDK.
ASYNC_INFO_IID = uuid.UUID("00000036-0000-0000-c000-000000000046")
# The signature of IAsyncOperation<UserConsentVerificationResult>, whose IID the interop
# is asked for.
OPERATION_SIGNATURE = ("pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};"
                       "enum(Windows.Security.Credentials.UI.UserConsentVerificationResult;i4))")
# The namespace WinRT works out a parameterised interface's IID in.
_PINTERFACE_NAMESPACE = uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee")


def _parameterised_iid(signature: str) -> uuid.UUID:
    """A parameterised interface's IID, as WinRT works it out: a version 5 UUID of its signature."""
    return uuid.uuid5(_PINTERFACE_NAMESPACE, signature)


OPERATION_IID = _parameterised_iid(OPERATION_SIGNATURE)

# An operation's AsyncStatus, as Windows gives it.
_STARTED, _COMPLETED, _CANCELED, _ERROR = 0, 1, 2, 3


class _Winrt:
    """
    The parts of winrt and Windows WindowsHello uses, imported only when this is made -
    which raises ImportError where winrt isn't installed. Everything here but __init__ is
    called on WindowsHello's owning thread. Tests give WindowsHello a fake of this
    instead, with the same methods.
    """

    def __init__(self):
        from winrt.windows.security.credentials.ui import (UserConsentVerificationResult, UserConsentVerifier,
                                                           UserConsentVerifierAvailability)

        self.UserConsentVerifier = UserConsentVerifier
        self.UserConsentVerifierAvailability = UserConsentVerifierAvailability
        self.UserConsentVerificationResult = UserConsentVerificationResult
        self._window: Optional["_OwnerWindow"] = None

    def set_up(self):
        """Makes the hidden window the prompts are asked for - or, if it can't be, leaves the plain call to be used."""
        try:
            self._window = _OwnerWindow(self.UserConsentVerifier)
        except Exception as e:
            logger.info(f"Windows Hello is asked without a window of the satellite's own ({type(e).__name__})")
            self._window = None

    def availability(self) -> str:
        """Hello's availability's name, e.g. "AVAILABLE"."""
        operation = _WinrtOperation(self.UserConsentVerifier.check_availability_async())
        while True:
            status = operation.status()
            if status != _STARTED:
                break
            time.sleep(POLL)
        if status != _COMPLETED:
            raise OSError(f"Hello's availability couldn't be seen (status {status})")
        return self.UserConsentVerifierAvailability(operation.results()).name

    def request(self, message: str):
        """Shows the prompt, and gives its operation - through the owner window if there is one, else plainly."""
        if self._window is not None:
            try:
                return self._window.request(message)
            except Exception as e:
                logger.warning(f"Windows Hello couldn't be asked for the satellite's window, so it's asked plainly "
                               f"({type(e).__name__})")
        return _WinrtOperation(self.UserConsentVerifier.request_verification_async(message))

    def wait(self, operation, seconds: float) -> Optional[str]:
        """
        Waits up to `seconds` for the prompt to be answered, passing on the owner window's
        messages meanwhile.

        Returns:
            The result's name, e.g. "VERIFIED", or None while it's still showing.

        Raises:
            OSError: The prompt failed.
        """
        end = time.monotonic() + seconds
        while True:
            if self._window is not None:
                self._window.pump()
            status = operation.status()
            if status == _COMPLETED:
                try:
                    result = self.UserConsentVerificationResult(operation.results()).name
                except ValueError:
                    # A result Windows has no name for is never taken as a yes.
                    result = "UNKNOWN"
                finally:
                    operation.close()
                return result
            if status == _CANCELED:
                operation.close()
                return "CANCELED"
            if status == _ERROR:
                operation.close()
                raise OSError("Windows Hello's prompt failed")
            if time.monotonic() >= end:
                return None
            time.sleep(min(0.02, seconds))

    def close(self):
        """Lets the owner window go."""
        window, self._window = self._window, None
        if window is not None:
            window.close()


class _WinrtOperation:
    """A winrt IAsyncOperation, looked at the way _RawOperation is."""

    def __init__(self, operation):
        self._operation = operation

    def status(self) -> int:
        return int(self._operation.status)

    def results(self) -> int:
        return int(self._operation.get_results())

    def cancel(self):
        self._operation.cancel()

    def close(self):
        try:
            self._operation.close()
        except Exception:
            pass


def _method(pointer: int, index: int, restype, *argtypes):
    """A COM method of an interface, by its place in the interface's table of methods."""
    import ctypes

    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])


def _guid(value: uuid.UUID):
    """A GUID for ctypes, laid out as Windows has it."""
    import ctypes

    return (ctypes.c_ubyte * 16).from_buffer_copy(value.bytes_le)


class _RawOperation:
    """
    The IAsyncOperation<UserConsentVerificationResult> the interop gives, as a bare COM
    pointer, which winrt has no way to wrap: looked at through IAsyncInfo, with ctypes.
    """

    def __init__(self, pointer: int):
        import ctypes

        self._ctypes = ctypes
        self._pointer = pointer
        info = ctypes.c_void_p()
        try:
            _method(pointer, 0, ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                pointer, ctypes.byref(_guid(ASYNC_INFO_IID)), ctypes.byref(info))
        except BaseException:
            # Released, as nothing else will.
            _method(pointer, 2, ctypes.c_ulong)(pointer)
            raise
        self._info = info.value

    def status(self) -> int:
        ctypes = self._ctypes
        status = ctypes.c_int()
        _method(self._info, 7, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))(self._info, ctypes.byref(status))
        return status.value

    def results(self) -> int:
        ctypes = self._ctypes
        # Started at -1, no result Windows has: 0 is VERIFIED, so a result never written
        # must not read as a yes.
        result = ctypes.c_int32(-1)
        _method(self._pointer, 8, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int32))(self._pointer, ctypes.byref(result))
        return result.value

    def cancel(self):
        _method(self._info, 9, self._ctypes.HRESULT)(self._info)

    def close(self):
        """Releases both interfaces, once."""
        ctypes = self._ctypes
        for pointer in (self._info, self._pointer):
            if pointer:
                _method(pointer, 2, ctypes.c_ulong)(pointer)
        self._info = self._pointer = None


class _OwnerWindow:
    """
    A hidden window of the satellite's own, never shown, which Hello's prompts are asked
    for through IUserConsentVerifierInterop - made, pumped and destroyed on the owning
    thread.
    """

    def __init__(self, verifier_class):
        import ctypes
        from ctypes import wintypes

        from winrt.runtime import interop

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32 = user32
        combase = ctypes.WinDLL("combase")
        combase.WindowsCreateString.restype = ctypes.HRESULT
        combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        combase.WindowsDeleteString.restype = ctypes.HRESULT
        combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
        self._combase = combase
        # The factory first, so a Windows without the interop leaves no window behind.
        self._factory = interop.get_activation_factory(verifier_class, INTEROP_IID)
        self._pointer = interop.interface_pointer(self._factory)
        ws_ex_toolwindow, ws_popup = 0x00000080, 0x80000000
        self._hwnd = user32.CreateWindowExW(ws_ex_toolwindow, "STATIC", "Nova", ws_popup, 0, 0, 0, 0,
                                            None, None, None, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def request(self, message: str) -> _RawOperation:
        """RequestVerificationForWindowAsync, for this window."""
        ctypes, wintypes = self._ctypes, self._wintypes
        text = ctypes.create_unicode_buffer(message)
        hstring = ctypes.c_void_p()
        self._combase.WindowsCreateString(text, len(message.encode("utf-16-le")) // 2, ctypes.byref(hstring))
        try:
            operation = ctypes.c_void_p()
            _method(self._pointer, 6, ctypes.HRESULT, wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(self._pointer, self._hwnd, hstring, ctypes.byref(_guid(OPERATION_IID)),
                                                     ctypes.byref(operation))
        finally:
            self._combase.WindowsDeleteString(hstring)
        if not operation.value:
            raise OSError("The interop gave no operation")
        return _RawOperation(operation.value)

    def pump(self):
        """Passes on whatever messages this thread's windows have waiting."""
        ctypes, wintypes = self._ctypes, self._wintypes
        message = wintypes.MSG()
        pm_remove = 0x0001
        while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, pm_remove):
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def close(self):
        if self._hwnd:
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        self._factory = None


def _load_backend():
    """winrt's parts, on Windows with winrt installed - else None, said in the log."""
    if sys.platform != "win32":
        return None
    try:
        return _Winrt()
    except ImportError as e:
        logger.info(f"winrt's Hello isn't installed, so this satellite can't approve ({e})")
    except Exception as e:
        logger.warning(f"winrt's Hello couldn't be loaded, so this satellite can't approve ({type(e).__name__})")
    return None


class _Ask:
    """One approval asked: its message, and how it ended."""

    def __init__(self, id: str, message: str):
        self.id = id
        self.message = message
        self.approved = False
        self.cancelled = False
        self.done = threading.Event()


class WindowsHello:
    """
    Asks Windows Hello for the user's OK, one prompt at a time, for as long as the
    satellite runs. Never raises into its callers: a prompt which fails is a no, logged.
    """

    def __init__(self, backend=None):
        """
        Args:
            backend: winrt's parts, as _Winrt has them - left out, they're loaded, on
                Windows with winrt installed. Tests give a fake.
        """
        self._backend = backend if backend is not None else _load_backend()
        # Guards the asks and the withdrawn ids, used from the link's thread, the
        # approvals' own and the owning one.
        self._lock = threading.Lock()
        self._asks: Dict[str, _Ask] = {}
        self._withdrawn: "collections.deque[str]" = collections.deque(maxlen=WITHDRAWN_KEPT)
        self._available = False
        self._closing = False
        # The kind of failure last logged, so one which keeps happening is logged once
        # until a prompt works again.
        self._failing: Optional[str] = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._owner = None
        if self._backend is None:
            return
        # Everything to do with Windows is done on one thread of its own, which lives as
        # long as the satellite, as for Windows' media controls (windows_media.py): seen on
        # the PC on 19 November, winrt calls made on a thread which then ended, or on the
        # satellite's own thread after PortAudio, didn't work. One thread also means one
        # prompt at a time, the others waiting their turn.
        self._owner = threading.Thread(target=self._serve, daemon=True, name="windows-hello")
        self._owner.start()
        done = threading.Event()
        self._jobs.put((self._set_up, done))
        if not done.wait(SET_UP_WAIT):
            logger.warning("Windows Hello took too long to set up, so this satellite can't approve")

    def _serve(self):
        """The owning thread: sets up, shows each prompt in turn and closes, until told to stop."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            if isinstance(job, _Ask):
                self._show(job)
                continue
            work, done = job
            try:
                work()
            except Exception as e:
                logger.warning(f"Something to do with Windows Hello failed ({type(e).__name__})")
            finally:
                done.set()

    def _set_up(self):
        """Sets the backend up and sees whether Hello can be used - on the owning thread."""
        self._backend.set_up()
        try:
            availability = self._backend.availability()
        except Exception as e:
            logger.info(f"Windows Hello's availability couldn't be seen, so this satellite can't approve "
                        f"({type(e).__name__})")
            return
        self._available = availability == "AVAILABLE"
        if not self._available:
            logger.info(f"Windows Hello isn't available ({availability}), so this satellite can't approve")

    @property
    def available(self) -> bool:
        """Whether Hello can be asked - and so whether the hello says the satellite approves."""
        return self._available

    def ask(self, id: str, description: str) -> bool:
        """
        Asks Hello for the user's OK, and waits for the answer - in turn after any prompts
        before it.

        Args:
            id: The approval's id, as the brain sent it.
            description: What would be done, shown as the prompt's message, cut to
                MESSAGE_MOST characters.

        Returns:
            True only if the user was verified: False for anything else, for one withdrawn
            or asked already, and when Hello isn't available.
        """
        if not self._available:
            return False
        with self._lock:
            if self._closing or id in self._withdrawn or id in self._asks:
                return False
            ask = _Ask(id, description[:MESSAGE_MOST])
            self._asks[id] = ask
        self._jobs.put(ask)
        ask.done.wait()
        return ask.approved

    def withdrawn(self, id: str) -> bool:
        """Whether the brain withdrew this approval - so its answer, if any, is no longer wanted."""
        with self._lock:
            return id in self._withdrawn

    def cancel(self, id: str):
        """
        Withdraws an approval: one still waiting its turn ends now with False and never
        shows; the one showing is cancelled, as far as Windows lets it be; and one not yet
        asked is remembered, so it never shows. Never waits for Windows.
        """
        with self._lock:
            self._withdrawn.append(id)
            ask = self._asks.get(id)
            if ask is None:
                return
            ask.cancelled = True
            # Its answer is False now: the brain has stopped waiting for it. The owning
            # thread cancels its prompt if it's showing, or skips it at its turn.
            ask.done.set()

    def _show(self, ask: _Ask):
        """Shows one prompt and waits for its answer, cancelling it if withdrawn - on the owning thread."""
        try:
            if ask.done.is_set():
                return
            operation = self._backend.request(ask.message)
            cancelled_at = None
            while True:
                with self._lock:
                    cancel = ask.cancelled or self._closing
                if cancel and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    try:
                        operation.cancel()
                    except Exception as e:
                        logger.info(f"Windows Hello's prompt couldn't be cancelled ({type(e).__name__})")
                result = self._backend.wait(operation, POLL)
                if result is not None:
                    break
                if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_WAIT:
                    logger.warning("Windows Hello's prompt didn't go when cancelled, so it's left")
                    return
            with self._lock:
                if not ask.done.is_set():
                    ask.approved = result == "VERIFIED"
            self._failing = None
        except Exception as e:
            kind = type(e).__name__
            if self._failing != kind:
                logger.warning(f"Windows Hello couldn't ask for approval, so it's a no ({kind})")
            self._failing = kind
        finally:
            with self._lock:
                self._asks.pop(ask.id, None)
            ask.done.set()

    def close(self):
        """Ends every approval waiting with False, cancels the one showing, and lets the owning thread go."""
        with self._lock:
            self._closing = True
            asks = list(self._asks.values())
        for ask in asks:
            ask.done.set()
        if self._owner is None:
            return
        done = threading.Event()
        self._jobs.put((self._backend.close, done))
        self._jobs.put(None)
        if not done.wait(CLOSE_WAIT):
            logger.warning("Windows Hello took too long to close")
