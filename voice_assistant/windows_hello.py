"""
Windows Hello Module.

The PC satellite's way to give the user's OK to a big action: the brain sends an
approval, and this asks Windows Hello (fingerprint, face or PIN) with the action's
description as the prompt's message. Only a verified answer is a yes, and only his own
Cancel is a no: anything else - Hello busy, the reader missing, too many tries, a fault,
a prompt withdrawn - is no answer at all, so the brain goes on waiting for the phone
(21 November: a refusal nobody gave settled a request before he could use the phone).

LIVE CHECK PENDING: the test of Hello from the windowless satellite on the PC (Task 0 of
the approvals plan) hasn't been run. So this tries, in turn, what should work: first the
interop which asks for a window of the satellite's own, a hidden one made here
(IUserConsentVerifierInterop::RequestVerificationForWindowAsync), and if that can't be
had or fails, the plain RequestVerificationAsync, which a desktop program with no window
may show behind other windows. Which of them brings the prompt to the front is for the
deploy's hand check. Cancelling the prompt's operation was seen on 21 November to leave
the prompt on the screen, so a withdrawn prompt's own window - one of the credential UI's
that came with it - is closed too, as its X would. If neither works, the PC's
"Can approve" is switched off on the settings page and the phone approves alone.

It goes through winrt, the Python projection of Windows' own API, which is only on the
PC: it's imported on first use, and only on Windows. Without it, off Windows, or with
Hello not set up, WindowsHello isn't available and the satellite's hello says it doesn't
approve.
"""

import collections
import logging
import ntpath
import queue
import sys
import threading
import time
import uuid
from typing import Dict, FrozenSet, Optional, Set, Tuple

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

# The only results which answer: verified is a yes, and his own Cancel a no. Every other -
# DEVICE_BUSY, DEVICE_NOT_PRESENT, NOT_CONFIGURED_FOR_USER, DISABLED_BY_POLICY,
# RETRIES_EXHAUSTED, a result with no name, a prompt stopped - leaves the phone to answer.
ANSWERS = {"VERIFIED": True, "CANCELED": False}

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
        self._broker: Optional["_BrokerWindows"] = None

    def set_up(self):
        """
        Makes the hidden window the prompts are asked for - or, if it can't be, leaves the
        plain call to be used - and what finds the prompt's own windows, to close them.
        """
        try:
            self._window = _OwnerWindow(self.UserConsentVerifier)
        except Exception as e:
            logger.info(f"Windows Hello is asked without a window of the satellite's own ({type(e).__name__})")
            self._window = None
        try:
            self._broker = _BrokerWindows()
        except Exception as e:
            logger.info(f"Windows Hello's prompts can't be closed by their window ({type(e).__name__})")
            self._broker = None

    def prompt_windows(self) -> FrozenSet[int]:
        """
        The visible top-level windows of Windows' credential UI, which shows Hello's prompt.

        Raises:
            OSError: They can't be found here.
        """
        if self._broker is None:
            raise OSError("no way to find Windows Hello's prompt")
        return self._broker.windows()

    def close_window(self, hwnd: int) -> bool:
        """Asks one of prompt_windows' windows to close, as its own X would. Returns whether it was asked."""
        if self._broker is None:
            return False
        return self._broker.close(hwnd)

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
            if status != _STARTED and self._window is not None:
                self._window.hide()
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
                # The operation cancelled, by a withdraw or a close - not his Cancel, which
                # completes it with the result CANCELED.
                operation.close()
                return "STOPPED"
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


def _centre(left: int, top: int, right: int, bottom: int) -> Tuple[int, int]:
    """Where the owner window goes in a work area: its middle, as Hello shows its prompt by its owner."""
    return (left + right) // 2, (top + bottom) // 2


class _OwnerWindow:
    """
    A window of the satellite's own which Hello's prompts are asked for through
    IUserConsentVerifierInterop - made, pumped and destroyed on the owning thread.

    It is one pixel, fully transparent and let clicks through, so never seen. Left at 0,0
    and hidden, Hello's prompt showed in the top-left corner of the screen (21 November),
    so before each prompt it is put in the middle of the main screen's work area, shown
    and brought to the front, and hidden again once the prompt is answered.
    """

    # Windows' own numbers for what's asked of it.
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_POPUP = 0x80000000
    LWA_ALPHA = 0x2
    SPI_GETWORKAREA = 0x0030
    HWND_TOPMOST = -1
    SWP_SHOWWINDOW = 0x0040
    SW_HIDE = 0
    ASFW_ANY = 0xFFFFFFFF

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
        user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
        user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._kernel32 = kernel32
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
        self._hwnd = user32.CreateWindowExW(self.WS_EX_TOOLWINDOW | self.WS_EX_LAYERED | self.WS_EX_TRANSPARENT,
                                            "STATIC", "Nova", self.WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.SetLayeredWindowAttributes(self._hwnd, 0, 0, self.LWA_ALPHA)

    def _work_area(self) -> Tuple[int, int, int, int]:
        """The main screen's work area (without the taskbar): left, top, right, bottom."""
        area = self._wintypes.RECT()
        if not self._user32.SystemParametersInfoW(self.SPI_GETWORKAREA, 0, self._ctypes.byref(area), 0):
            raise OSError("The screen's work area couldn't be read")
        return area.left, area.top, area.right, area.bottom

    def to_front(self):
        """
        Puts the window in the middle of the main screen, shown, and in front, so the
        prompt asked for it is too. Best effort: a failure is logged, and the prompt is
        asked all the same.
        """
        user32 = self._user32
        try:
            x, y = _centre(*self._work_area())
            user32.SetWindowPos(self._hwnd, self.HWND_TOPMOST, x, y, 1, 1, self.SWP_SHOWWINDOW)
            user32.AllowSetForegroundWindow(self.ASFW_ANY)
            if user32.SetForegroundWindow(self._hwnd):
                return
            # Windows' foreground lock stops a background program taking the front; joined
            # to the input of the thread in front for a moment, it can, as pc_windows does.
            front = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            own = self._kernel32.GetCurrentThreadId()
            joined = bool(front) and front != own and bool(user32.AttachThreadInput(front, own, True))
            try:
                if not user32.SetForegroundWindow(self._hwnd):
                    logger.info("Windows wouldn't let Hello's prompt come to the front")
            finally:
                if joined:
                    user32.AttachThreadInput(front, own, False)
        except Exception as e:
            logger.info(f"Hello's prompt couldn't be put in the middle, in front ({type(e).__name__})")

    def hide(self):
        """Hides the window again, once its prompt has gone."""
        try:
            self._user32.ShowWindow(self._hwnd, self.SW_HIDE)
        except Exception as e:
            logger.info(f"Hello's prompt's window couldn't be hidden ({type(e).__name__})")

    def request(self, message: str) -> _RawOperation:
        """RequestVerificationForWindowAsync, for this window, once it's in the middle and in front."""
        self.to_front()
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


class _BrokerWindows:
    """
    The windows of CredentialUIBroker.exe, the Windows process which shows Hello's prompt:
    found, and closed with WM_CLOSE, as cancelling the prompt's operation leaves it on the
    screen (seen on the PC on 21 November). Nothing of any other process is ever touched.
    """

    EXE = "credentialuibroker.exe"
    WM_CLOSE = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
        self._each = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
        for function, arguments, result in (
                (user32.EnumWindows, [self._each, wintypes.LPARAM], BOOL),
                (user32.IsWindowVisible, [HWND], BOOL),
                (user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(DWORD)], DWORD),
                (user32.PostMessageW, [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], BOOL),
                (kernel32.OpenProcess, [DWORD, BOOL, DWORD], wintypes.HANDLE),
                (kernel32.QueryFullProcessImageNameW, [wintypes.HANDLE, DWORD, wintypes.LPWSTR,
                                                       ctypes.POINTER(DWORD)], BOOL),
                (kernel32.CloseHandle, [wintypes.HANDLE], BOOL)):
            function.argtypes = arguments
            function.restype = result
        self._user32, self._kernel32 = user32, kernel32

    def _pid(self, hwnd) -> int:
        pid = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return pid.value

    def _is_broker(self, pid: int) -> bool:
        handle = self._kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            size = self._wintypes.DWORD(32768)
            buffer = self._ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self._ctypes.byref(size)):
                return False
            return ntpath.basename(buffer.value).lower() == self.EXE
        finally:
            self._kernel32.CloseHandle(handle)

    def windows(self) -> FrozenSet[int]:
        found = []
        brokers: Dict[int, bool] = {}

        def each(hwnd, _):
            try:
                if hwnd and self._user32.IsWindowVisible(hwnd):
                    pid = self._pid(hwnd)
                    if pid not in brokers:
                        brokers[pid] = self._is_broker(pid)
                    if brokers[pid]:
                        found.append(hwnd)
            except Exception:
                pass
            return True

        self._user32.EnumWindows(self._each(each), 0)
        return frozenset(found)

    def close(self, hwnd) -> bool:
        # Looked at again just before, so a window gone and its handle reused is never closed.
        if not self._user32.IsWindowVisible(hwnd) or not self._is_broker(self._pid(hwnd)):
            return False
        return bool(self._user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))


def _short(id: str) -> str:
    """Enough of an approval's id ("approval-" and the request's) to tell it apart in the log."""
    return id.removeprefix("approval-")[:8]


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
        # True verified, False his own Cancel, None no answer.
        self.answer: Optional[bool] = None
        self.cancelled = False
        self.done = threading.Event()


class WindowsHello:
    """
    Asks Windows Hello for the user's OK, one prompt at a time, for as long as the
    satellite runs. Never raises into its callers: a prompt which fails is no answer, logged.
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

    def ask(self, id: str, description: str) -> Optional[bool]:
        """
        Asks Hello for the user's OK, and waits for the answer - in turn after any prompts
        before it.

        Args:
            id: The approval's id, as the brain sent it.
            description: What would be done, shown as the prompt's message, cut to
                MESSAGE_MOST characters.

        Returns:
            True only if the user was verified; False only if he pressed Cancel; None,
            no answer to send, for anything else: any other result, a failure, one
            withdrawn or asked already, and when Hello isn't available.
        """
        if not self._available:
            return None
        with self._lock:
            if self._closing or id in self._withdrawn or id in self._asks:
                return None
            ask = _Ask(id, description[:MESSAGE_MOST])
            self._asks[id] = ask
        self._jobs.put(ask)
        ask.done.wait()
        return ask.answer

    def withdrawn(self, id: str) -> bool:
        """Whether the brain withdrew this approval - so its answer, if any, is no longer wanted."""
        with self._lock:
            return id in self._withdrawn

    def cancel(self, id: str):
        """
        Withdraws an approval: one still waiting its turn ends now with no answer and never
        shows; the one showing is cancelled, as far as Windows lets it be; and one not yet
        asked is remembered, so it never shows. Never waits for Windows.
        """
        with self._lock:
            self._withdrawn.append(id)
            ask = self._asks.get(id)
            if ask is None:
                return
            ask.cancelled = True
            # It has no answer now: the brain has stopped waiting for it. The owning
            # thread cancels its prompt if it's showing, or skips it at its turn.
            ask.done.set()

    def _show(self, ask: _Ask):
        """Shows one prompt and waits for its answer, cancelling it if withdrawn - on the owning thread."""
        try:
            if ask.done.is_set():
                return
            # The credential UI's windows before the prompt, so only one which came with it
            # is ever closed; None if they can't be found, when none is.
            try:
                before: Optional[FrozenSet[int]] = self._backend.prompt_windows()
            except Exception:
                before = None
            operation = self._backend.request(ask.message)
            cancelled_at = None
            # The windows asked to close; 0, never a window, once a failure to is logged.
            closed: Set[int] = set()
            while True:
                with self._lock:
                    cancel = ask.cancelled or self._closing
                if cancel and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    try:
                        operation.cancel()
                    except Exception as e:
                        logger.info(f"Windows Hello's prompt couldn't be cancelled ({type(e).__name__})")
                if cancelled_at is not None and before is not None:
                    self._close_prompt(before, closed)
                result = self._backend.wait(operation, POLL)
                if result is not None:
                    break
                if cancelled_at is not None and time.monotonic() - cancelled_at > CANCEL_WAIT:
                    # Given up on, so the next prompt isn't held behind it.
                    logger.warning("Windows Hello's prompt didn't go when cancelled, so it's left")
                    return
            logger.info(f"Windows Hello's answer to approval {_short(ask.id)}: {result}")
            with self._lock:
                if not ask.done.is_set():
                    ask.answer = ANSWERS.get(result)
            self._failing = None
        except Exception as e:
            kind = type(e).__name__
            if self._failing != kind:
                logger.warning(f"Windows Hello couldn't ask for approval, so it's not answered ({kind})")
            self._failing = kind
        finally:
            with self._lock:
                self._asks.pop(ask.id, None)
            ask.done.set()

    def _close_prompt(self, before: FrozenSet[int], closed: Set[int]):
        """
        Closes the credential UI's windows which came with the prompt being withdrawn, as
        cancelling its operation leaves it on the screen. Best effort, on the owning
        thread: looked for at each poll until the prompt goes, each window asked once.

        Args:
            before: The credential UI's windows before the prompt was asked.
            closed: Those already asked to close, added to here.
        """
        try:
            new = self._backend.prompt_windows() - before - closed
            for hwnd in new:
                closed.add(hwnd)
                if self._backend.close_window(hwnd):
                    logger.info("Windows Hello's prompt was withdrawn, so its window was closed")
                else:
                    logger.info("Windows Hello's prompt was withdrawn, but its window wouldn't close")
        except Exception as e:
            # Once for the prompt, not at every look.
            if 0 not in closed:
                logger.info(f"Windows Hello's withdrawn prompt couldn't be closed ({type(e).__name__})")
            closed.add(0)

    def close(self):
        """Ends every approval waiting with no answer, cancels the one showing, and lets the owning thread go."""
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
