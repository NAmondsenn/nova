"""
Tray Module.

Nova's icon in the Windows system tray, its menu, and the keyboard shortcut which makes Nova
listen. A click on the icon, left or right, opens the menu; the icon and the text shown when
the pointer rests on it follow the satellite's state (ready, microphone muted, not
connected); and the shortcut is registered with Windows itself. See
docs/superpowers/specs/2026-11-20-pc-app-design.md.

Nothing the satellite does ever waits on the tray or fails because of it, so everything
here is told what has happened and returns at once, as the screen's orb is. What the menu
holds and which icon shows are worked out in app_menu.py, and the shortcut's text is read
in shortcut.py. The icon itself (_Win32Tray) is the only part which needs Windows, and is
made only there: the rest is plain Python, tested anywhere with a fake icon.
"""

import itertools
import logging
import os
import queue
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

from voice_assistant import app_menu
from voice_assistant.app_menu import AppState, Item
from voice_assistant.shortcut import Shortcut

logger = logging.getLogger(__name__)

# The file in the icons folder for each of app_menu.icon()'s answers.
ICON_FILES = {
    "ready": "nova.ico",
    "muted": "nova-muted.ico",
    "offline": "nova-offline.ico",
}

# The most seconds the satellite waits for the icon's window to be made as it starts, and
# for the tray's thread to finish as it stops.
SET_UP_WAIT = 5
CLOSE_WAIT = 2

# The most seconds the tray's thread waits for Windows between looks at its notes. Every
# note ends the wait at once, so this only bounds how long one could go unseen if a wake
# were ever lost.
WAIT = 1.0

# For how long, in seconds, after the menu closes a click on the icon is taken to be the
# one which closed it. A click on the icon while the menu is up ends the menu, and Windows
# then passes that same click on to the icon, which would open the menu again at once;
# and Enter on the icon can come twice. A deliberate second click takes longer than this.
REOPEN_GAP = 0.3

# The note which tells the tray's thread to take the icon away and finish.
_CLOSE = object()


class Tray:
    """
    Nova's tray icon, its menu and the shortcut, owned by one thread of its own for as long
    as the satellite runs.

    The satellite tells it that something has changed and which shortcut to use, from
    whichever thread it is on, and each call only leaves a note for the tray's thread:
    nothing the satellite does ever waits on the tray. The thread reads the state from the
    satellite itself, on its own thread, when it shows the icon and again just before it
    draws the menu, so the menu is never out of date. It calls the satellite back, on its
    own thread, with what was chosen, a press of the shortcut, and whether the shortcut is
    taken; while the menu is up, the thread is in it, and notes wait until it closes.

    If the icon's window can't be made, that is logged once and the tray does nothing for
    the rest of the run, and so if waiting on Windows ever fails. Anything else which fails
    - the icon refused while the taskbar isn't there yet, the menu, a callback which raises
    - is logged the first time and the tray carries on.
    """

    def __init__(self, read_state: Callable[[bool], AppState], on_choice: Callable[[str], None],
                 on_shortcut: Callable[[], None], on_taken: Callable[[bool], None], icons: str,
                 backend: Optional[Callable[[], object]] = None):
        """
        Args:
            read_state: Gives the state the icon and the menu are worked out from. Called on
                the tray's thread, with True just before the menu is drawn and False
                otherwise.
            on_choice: Called on the tray's thread with the id of the menu's item chosen.
            on_shortcut: Called on the tray's thread when the shortcut is pressed.
            on_taken: Called on the tray's thread with True when Windows refuses the
                shortcut, and False once one is registered or there is none.
            icons: The folder which holds the icon files in ICON_FILES.
            backend: Makes the icon, on the tray's own thread - _Win32Tray, which is used
                when this is left out on Windows. Off Windows, left out, there's none, and
                every call does nothing. Tests give a fake.
        """
        self._read_state = read_state
        self._on_choice = on_choice
        self._on_shortcut = on_shortcut
        self._on_taken = on_taken
        self._icons = icons
        self._make = backend if backend is not None else (_Win32Tray if sys.platform == "win32" else None)
        self._notes: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._available = False
        # The icon, once made, so a note can end its wait; None before and after.
        self._backend = None
        # Set, from any thread, once the tray has nothing more to do: there's no icon, it
        # failed, or it was closed. Every call then returns without leaving a note.
        self._quiet = self._make is None
        # The tray's thread's own: the icon and hover text shown, as (file, text), or None
        # when it is to be shown afresh; the shortcut registered; when the menu last
        # closed; and which kinds of failure have been logged. Nothing else touches these.
        self._shown: Optional[Tuple[str, str]] = None
        self._held: Optional[Shortcut] = None
        self._menu_closed_at: Optional[float] = None
        self._logged: set = set()
        if self._make is None:
            return
        made = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(made,), daemon=True, name="tray")
        self._thread.start()
        if made.wait(SET_UP_WAIT):
            self._available = not self._quiet
        else:
            self._go_quiet("Windows took too long to make Nova's tray icon, so there isn't one")
            self._notes.put(_CLOSE)

    @property
    def available(self) -> bool:
        """Whether the icon's window could be made, so the icon can be shown."""
        return self._available

    def changed(self) -> None:
        """Says something in the state has changed, so the icon is worked out afresh. Never waits."""
        if not self._quiet:
            self._notes.put(("changed",))
            self._wake()

    def set_shortcut(self, shortcut: Optional[Shortcut]) -> None:
        """
        Changes the shortcut, letting the old one go. Never waits.

        Args:
            shortcut: The shortcut to register, or None for none. on_taken is told how it
                went; the shortcut already registered, set again, is simply kept.
        """
        if not self._quiet:
            self._notes.put(("shortcut", shortcut))
            self._wake()

    def close(self) -> None:
        """
        Takes the icon away, lets the shortcut go and ends the tray's thread, waiting at most
        CLOSE_WAIT seconds. From the tray's own thread, as when Quit is chosen, it doesn't
        wait: the thread finishes as soon as the callback it is in returns.
        """
        if self._thread is None:
            return
        self._quiet = True
        self._notes.put(_CLOSE)
        self._wake()
        if threading.current_thread() is not self._thread:
            self._thread.join(CLOSE_WAIT)

    def _wake(self) -> None:
        """Ends the tray's thread's wait for Windows, so it looks at its notes."""
        backend = self._backend
        if backend is None:
            # Not made yet: the thread looks at its notes as soon as it is.
            return
        try:
            backend.wake()
        except Exception:
            logger.debug("Couldn't wake Nova's tray", exc_info=True)

    def _go_quiet(self, why: str) -> None:
        """Stops the tray for the rest of the run, logging why the first time."""
        if not self._quiet:
            self._quiet = True
            logger.warning(why)

    def _failed(self, kind: str, why: str, error: Exception) -> None:
        """Logs a failure the tray carries on after: a warning the first time of each kind."""
        if kind in self._logged:
            logger.debug(why, exc_info=error)
        else:
            self._logged.add(kind)
            logger.warning(f"{why} ({type(error).__name__})", exc_info=error)

    def _run(self, made: threading.Event) -> None:
        """The tray's thread: makes the icon, serves it until closed, then takes it away."""
        try:
            backend = self._make()
        except Exception as e:
            self._go_quiet(f"Couldn't make Nova's tray icon, so there isn't one ({type(e).__name__})")
            made.set()
            return
        self._backend = backend
        made.set()
        try:
            self._serve(backend)
        except Exception as e:
            self._go_quiet(f"Nova's tray icon failed, so it's gone until the satellite restarts "
                           f"({type(e).__name__})")
            logger.debug("How Nova's tray icon failed", exc_info=True)
        finally:
            self._quiet = True
            self._backend = None
            # Destroying it takes the icon out of the tray and lets the shortcut go, so
            # neither is left behind by a failure.
            try:
                backend.destroy()
            except Exception:
                logger.debug("Couldn't take Nova's tray icon away", exc_info=True)

    def _serve(self, backend) -> None:
        """Shows the icon, then takes the notes left for the tray and what Windows tells it, until told to close."""
        self._refresh(backend)
        while True:
            changed = False
            # Everything waiting is taken at once, so a burst of changes is one look.
            while True:
                try:
                    note = self._notes.get_nowait()
                except queue.Empty:
                    break
                if note is _CLOSE:
                    return
                if note[0] == "changed":
                    changed = True
                else:
                    self._set_shortcut(backend, note[1])
            if changed:
                self._refresh(backend)
            for event in backend.wait(WAIT):
                # Once closed, as by Quit from the menu, nothing more is acted on.
                if self._quiet:
                    break
                if event == "click":
                    self._open_menu(backend)
                elif event == "shortcut":
                    self._call("on_shortcut", "Nova's shortcut failed", self._on_shortcut)
                elif event == "taskbar":
                    # The taskbar was made again, as when Explorer restarts, and the icon
                    # went with the old one.
                    self._shown = None
                    self._refresh(backend)

    def _call(self, kind: str, why: str, function: Callable, *arguments) -> None:
        """Calls one of the satellite's callbacks, which may raise; that is logged, never let through."""
        try:
            function(*arguments)
        except Exception as e:
            self._failed(kind, why, e)

    def _read(self, opening: bool) -> Optional[Tuple[Tuple[str, str], List[Item]]]:
        """
        Reads the state from the satellite and works out the icon and the menu from it.

        Args:
            opening: Whether the menu is about to be drawn, passed on to read_state.

        Returns:
            ((icon file, hover text), the menu's items), or None if the state couldn't be read.
        """
        try:
            state = self._read_state(opening)
            look = (os.path.join(self._icons, ICON_FILES[app_menu.icon(state)]), app_menu.hover(state))
            return look, app_menu.items(state)
        except Exception as e:
            self._failed("read_state", "Couldn't read the state for Nova's tray icon", e)
            return None

    def _refresh(self, backend) -> None:
        """Shows the icon for the state as it is now."""
        read = self._read(False)
        if read is not None:
            self._show(backend, read[0])

    def _show(self, backend, look: Tuple[str, str]) -> None:
        """
        Shows that icon and hover text, unless they are what is shown already.

        One which Windows refuses - at logon before the taskbar is there, say - is tried
        again with the next change, or when the taskbar is made.
        """
        if look == self._shown:
            return
        try:
            backend.show(*look)
            self._shown = look
        except Exception as e:
            self._shown = None
            self._failed("show", "Couldn't show Nova's tray icon; it's tried again with the next change", e)

    def _open_menu(self, backend) -> None:
        """Draws the menu from the state read afresh, and passes on what is chosen."""
        if self._menu_closed_at is not None and time.monotonic() - self._menu_closed_at < REOPEN_GAP:
            return
        read = self._read(True)
        if read is None:
            return
        look, menu_items = read
        # The state was just read, so the icon is brought up to date with the menu.
        self._show(backend, look)
        try:
            choice = backend.menu(menu_items)
        except Exception as e:
            choice = None
            self._failed("menu", "Couldn't draw Nova's tray menu", e)
        self._menu_closed_at = time.monotonic()
        if choice is not None:
            self._call("on_choice", f"What was chosen from Nova's tray menu ({choice}) failed", self._on_choice,
                       choice)

    def _set_shortcut(self, backend, shortcut: Optional[Shortcut]) -> None:
        """Registers the shortcut, letting the old one go first, and tells on_taken how it went."""
        if shortcut is not None and shortcut == self._held:
            return
        if self._held is not None:
            self._held = None
            try:
                backend.unregister()
            except Exception as e:
                self._failed("unregister", "Couldn't let Nova's shortcut go", e)
        if shortcut is None:
            self._call("on_taken", "Couldn't pass on whether Nova's shortcut is taken", self._on_taken, False)
            return
        try:
            held = bool(backend.register(shortcut.modifiers, shortcut.key))
        except Exception as e:
            # It doesn't work, whatever the reason, so it's reported as taken rather than
            # left showing beside Listen.
            held = False
            self._failed("register", "Couldn't register Nova's shortcut", e)
        if held:
            self._held = shortcut
        self._call("on_taken", "Couldn't pass on whether Nova's shortcut is taken", self._on_taken, not held)


# Windows' numbers for the modifiers a shortcut can hold (winuser.h, RegisterHotKey).
_MODIFIER_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
# Holding the keys down registers one press, not a press for each repeat.
_MOD_NOREPEAT = 0x4000


def _hotkey_modifiers(modifiers: Tuple[str, ...]) -> int:
    """
    RegisterHotKey's modifier flags for a shortcut's modifiers.

    Raises:
        ValueError: For a modifier which isn't one of shortcut.MODIFIERS.
    """
    flags = _MOD_NOREPEAT
    for name in modifiers:
        if name not in _MODIFIER_FLAGS:
            raise ValueError(f"no such modifier: {name!r}")
        flags |= _MODIFIER_FLAGS[name]
    return flags


def _virtual_key(key: str) -> int:
    """
    Windows' virtual-key code for one of shortcut.KEYS.

    Raises:
        ValueError: For a key which isn't one of them.
    """
    if len(key) == 1 and (key.isdigit() or "a" <= key <= "z"):
        # A letter's code is its capital's, and a digit's is its own character's.
        return ord(key.upper())
    if key == "space":
        return 0x20
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        # VK_F1 is 0x70, and F2 to F24 follow it in order.
        return 0x70 + int(key[1:]) - 1
    raise ValueError(f"no such key: {key!r}")


def _menu_text(item: Item) -> str:
    """A menu line's text: its label, and its shortcut after a tab, which Windows right-aligns."""
    return f"{item.label}\t{item.keys}" if item.keys else item.label


class _Win32Tray:
    """
    Nova's tray icon on Windows, through Win32 by ctypes. Made, used and destroyed only on
    Tray's own thread, as Windows gives a window's messages, a menu and a registered
    shortcut to the thread which made them - all but wake, which any thread may call.

    The icon belongs to a hidden window which never shows. It is a real top-level window,
    not a message-only one, because only top-level windows hear "TaskbarCreated", which
    Windows sends to every one of them when the taskbar is made again, as when Explorer
    restarts and the icons in the tray are lost.

    The icon is set to the version 4 behaviour (NOTIFYICON_VERSION_4). Then Windows tells
    the window of a left click once, on its release (NIN_SELECT), of a right click once,
    as WM_CONTEXTMENU, and of the icon chosen with the keyboard (NIN_KEYSELECT), rather
    than of each press and release of each button; so each click is one "click". If
    Windows turns the version down, the releases of either button are taken instead.

    Making it raises OSError if Windows refuses, and so do show, menu and wait; register
    returns False when Windows refuses the shortcut, as when another program has it.
    """

    # Windows' own numbers for what is asked of it (winuser.h, shellapi.h, winbase.h).
    WS_POPUP = 0x80000000
    WS_EX_TOOLWINDOW = 0x00000080
    WM_NULL = 0x0000
    WM_CLOSE = 0x0010
    WM_CONTEXTMENU = 0x007B
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_HOTKEY = 0x0312
    WM_USER = 0x0400
    WM_APP = 0x8000
    NIN_SELECT = WM_USER + 0
    NIN_KEYSELECT = WM_USER + 1
    NIM_ADD = 0x0
    NIM_MODIFY = 0x1
    NIM_DELETE = 0x2
    NIM_SETVERSION = 0x4
    NIF_MESSAGE = 0x01
    NIF_ICON = 0x02
    NIF_TIP = 0x04
    NIF_SHOWTIP = 0x80
    NOTIFYICON_VERSION_4 = 4
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SM_CXSMICON = 49
    SM_CYSMICON = 50
    MF_STRING = 0x0000
    MF_GRAYED = 0x0001
    MF_CHECKED = 0x0008
    MF_SEPARATOR = 0x0800
    TPM_RIGHTBUTTON = 0x0002
    TPM_NONOTIFY = 0x0080
    TPM_RETURNCMD = 0x0100
    QS_ALLINPUT = 0x04FF
    MWMO_INPUTAVAILABLE = 0x0004
    WAIT_FAILED = 0xFFFFFFFF
    PM_REMOVE = 0x0001
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    # The DPI Windows calls 100% scaling, and the small icon's side there.
    BASE_DPI = 96
    BASE_ICON = 16
    # The message the icon's clicks come to the window as, and the numbers the icon and
    # the shortcut are known by: there is one of each.
    CALLBACK_MESSAGE = WM_APP + 1
    ICON_ID = 1
    HOTKEY_ID = 1
    # NOTIFYICONDATAW's size with every field to Windows Vista's, which Windows checks it by.
    NOTIFYICONDATAW_SIZE = {8: 976, 4: 956}

    # Each window's class has a name of its own, so a window made again in the same run
    # never finds a class left registered with an earlier window's procedure.
    _numbers = itertools.count(1)

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        c_int, c_void_p, POINTER = ctypes.c_int, ctypes.c_void_p, ctypes.POINTER
        # Every handle is pointer-sized, so none is cut to 32 bits on 64-bit Windows.
        HANDLE, HWND = wintypes.HANDLE, wintypes.HWND
        BOOL, UINT, DWORD, LONG, WORD = wintypes.BOOL, wintypes.UINT, wintypes.DWORD, wintypes.LONG, wintypes.WORD
        BYTE, WCHAR = ctypes.c_ubyte, wintypes.WCHAR
        # WPARAM is pointer-sized and unsigned, as a menu item's UINT_PTR id is; LRESULT
        # is pointer-sized and signed, as LPARAM is.
        WPARAM, LPARAM, LPCWSTR = wintypes.WPARAM, wintypes.LPARAM, wintypes.LPCWSTR
        LRESULT = LPARAM

        class POINT(ctypes.Structure):
            _fields_ = [("x", LONG), ("y", LONG)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                        ("time", DWORD), ("pt", POINT), ("lPrivate", DWORD)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", BYTE * 8)]

        # The whole structure, as Windows Vista and later have it. uVersion shares its
        # place with uTimeout, a balloon's, which Nova never shows.
        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT), ("uFlags", UINT),
                        ("uCallbackMessage", UINT), ("hIcon", HANDLE), ("szTip", WCHAR * 128), ("dwState", DWORD),
                        ("dwStateMask", DWORD), ("szInfo", WCHAR * 256), ("uVersion", UINT),
                        ("szInfoTitle", WCHAR * 64), ("dwInfoFlags", DWORD), ("guidItem", GUID),
                        ("hBalloonIcon", HANDLE)]

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int),
                        ("cbWndExtra", c_int), ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                        ("hbrBackground", HANDLE), ("lpszMenuName", LPCWSTR), ("lpszClassName", LPCWSTR),
                        ("hIconSm", HANDLE)]

        expected = self.NOTIFYICONDATAW_SIZE.get(ctypes.sizeof(c_void_p))
        if ctypes.sizeof(NOTIFYICONDATAW) != expected:
            # Windows would refuse every call with it, with no word of why.
            raise OSError(f"NOTIFYICONDATAW is {ctypes.sizeof(NOTIFYICONDATAW)} bytes, not {expected}")

        # Libraries of its own, rather than ctypes.windll's shared ones, so the types set
        # here never clash with another module's for the same function.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        signatures = (
            (user32.RegisterClassExW, [POINTER(WNDCLASSEXW)], WORD),
            (user32.UnregisterClassW, [LPCWSTR, HANDLE], BOOL),
            (user32.CreateWindowExW, [DWORD, LPCWSTR, LPCWSTR, DWORD, c_int, c_int, c_int, c_int, HWND, HANDLE,
                                      HANDLE, c_void_p], HWND),
            (user32.DestroyWindow, [HWND], BOOL),
            (user32.DefWindowProcW, [HWND, UINT, WPARAM, LPARAM], LRESULT),
            (user32.RegisterWindowMessageW, [LPCWSTR], UINT),
            (user32.GetSystemMetrics, [c_int], c_int),
            (user32.LoadImageW, [HANDLE, LPCWSTR, UINT, c_int, c_int, UINT], HANDLE),
            (user32.DestroyIcon, [HANDLE], BOOL),
            (user32.CreatePopupMenu, [], HANDLE),
            (user32.AppendMenuW, [HANDLE, UINT, WPARAM, LPCWSTR], BOOL),
            (user32.DestroyMenu, [HANDLE], BOOL),
            (user32.GetCursorPos, [POINTER(POINT)], BOOL),
            (user32.SetForegroundWindow, [HWND], BOOL),
            # With TPM_RETURNCMD its BOOL is the id of the item chosen, or 0 for none.
            (user32.TrackPopupMenu, [HANDLE, UINT, c_int, c_int, c_int, HWND, c_void_p], BOOL),
            (user32.PostMessageW, [HWND, UINT, WPARAM, LPARAM], BOOL),
            (user32.RegisterHotKey, [HWND, c_int, UINT, UINT], BOOL),
            (user32.UnregisterHotKey, [HWND, c_int], BOOL),
            (user32.MsgWaitForMultipleObjectsEx, [DWORD, POINTER(HANDLE), DWORD, DWORD, DWORD], DWORD),
            (user32.PeekMessageW, [POINTER(MSG), HWND, UINT, UINT, UINT], BOOL),
            (user32.TranslateMessage, [POINTER(MSG)], BOOL),
            (user32.DispatchMessageW, [POINTER(MSG)], LRESULT),
            (kernel32.GetModuleHandleW, [LPCWSTR], HANDLE),
            (kernel32.CreateEventW, [c_void_p, BOOL, BOOL, LPCWSTR], HANDLE),
            (kernel32.SetEvent, [HANDLE], BOOL),
            (kernel32.CloseHandle, [HANDLE], BOOL),
            (shell32.Shell_NotifyIconW, [DWORD, POINTER(NOTIFYICONDATAW)], BOOL),
        )
        for function, arguments, result in signatures:
            function.argtypes = arguments
            function.restype = result
        # What is needed to follow each screen's own scaling, from Windows 10's 1607 on.
        # On an older one the icon is loaded at the size Windows gives a program unaware
        # of scaling, and Windows stretches it.
        try:
            dpi_signatures = (
                (user32.SetThreadDpiAwarenessContext, [HANDLE], HANDLE),
                (user32.GetDpiForWindow, [HWND], UINT),
                (user32.GetSystemMetricsForDpi, [c_int, UINT], c_int),
            )
        except AttributeError:
            dpi_signatures = ()
        for function, arguments, result in dpi_signatures:
            function.argtypes = arguments
            function.restype = result
        self._user32, self._kernel32, self._shell32 = user32, kernel32, shell32
        self._POINT, self._NOTIFYICONDATAW = POINT, NOTIFYICONDATAW
        self._message = MSG()

        # What destroy lets go of, as far as it has been made.
        self._hwnd = None
        self._registered = False
        self._icon = None
        # The file and size the icon was loaded from, so the same one isn't loaded again.
        self._icon_key: Optional[Tuple[str, int, int]] = None
        # Whether the icon is in the tray, as far as is known, and whether the shortcut is
        # registered.
        self._added = False
        self._hotkey = False
        # The event wake sets. Its lock keeps wake, from another thread, from setting it
        # once destroy has closed it, when its handle could be some other object's.
        self._wake_lock = threading.Lock()
        self._wake_event = None
        # What the window procedure has heard, for wait to hand on. Only this thread's
        # procedure adds to it, during wait or while the menu is up.
        self._events: List[str] = []
        # Which of the icon's notifications are a click: version 4's, until Windows turns
        # that version down.
        self._v4_clicks = frozenset((self.NIN_SELECT, self.NIN_KEYSELECT, self.WM_CONTEXTMENU))
        self._clicks = self._v4_clicks

        try:
            # This thread alone is made aware of each screen's own scaling, as the orb's
            # is, so the icon is loaded at the screen's real size, sharp at 150%, and the
            # menu is drawn for it rather than stretched. A window keeps its thread's
            # awareness from when it is made, so this comes first.
            self._aware = bool(dpi_signatures) and bool(
                user32.SetThreadDpiAwarenessContext(self.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            # Auto-reset, so a wake is used up by the wait it ends.
            self._wake_event = self._check(kernel32.CreateEventW(None, False, False, None), "CreateEventW")
            self._handles = (HANDLE * 1)(self._wake_event)
            self._procedure = WNDPROC(self._window_procedure)
            self._instance = kernel32.GetModuleHandleW(None)
            self._class_name = f"NovaTray{next(self._numbers)}"
            window_class = WNDCLASSEXW()
            window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
            window_class.lpfnWndProc = self._procedure
            window_class.hInstance = self._instance
            window_class.lpszClassName = self._class_name
            self._check(user32.RegisterClassExW(ctypes.byref(window_class)), "RegisterClassExW")
            self._registered = True
            # A popup with no size which is never shown, and a tool window, so even if
            # something showed it, it would have no taskbar button and no place in Alt-Tab.
            self._hwnd = self._check(user32.CreateWindowExW(self.WS_EX_TOOLWINDOW, self._class_name, "Nova",
                                                            self.WS_POPUP, 0, 0, 0, 0, None, None, self._instance,
                                                            None),
                                     "CreateWindowExW")
        except BaseException:
            self.destroy()
            raise

    def _window_procedure(self, hwnd, message, wparam, lparam):
        """
        Hears the window's messages, noting the icon's clicks, the shortcut and a new
        taskbar for wait. Called by Windows on this thread, while wait hands messages on
        or the menu is up; kept for as long as the class is registered. It does no more
        than note them, as an exception here would be lost.
        """
        if message == self.CALLBACK_MESSAGE:
            # Version 4 puts the notification in lParam's low word and the icon's id in
            # its high one; before it, lParam is the notification alone.
            if (lparam & 0xFFFF) in self._clicks:
                self._events.append("click")
            return 0
        if message == self.WM_HOTKEY:
            if wparam == self.HOTKEY_ID:
                self._events.append("shortcut")
            return 0
        if self._taskbar_created and message == self._taskbar_created:
            # The new taskbar has no icon of Nova's, so the next show adds it.
            self._added = False
            self._events.append("taskbar")
            return 0
        if message == self.WM_CLOSE:
            # Only the tray's own thread ever destroys the window, so a close asked for from
            # outside, by a tool which closes every window, say, is turned down.
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _check(self, result, what: str):
        """
        Returns a Win32 call's result, or raises OSError if it says it failed.

        Args:
            result: What the call returned: nothing (0 or None) for a failure.
            what: The call's name, for the error.
        """
        if not result:
            ctypes = self._ctypes
            raise ctypes.WinError(ctypes.get_last_error(), f"{what} failed")
        return result

    def _notify_data(self):
        """A NOTIFYICONDATAW naming Nova's icon, with nothing else in it yet."""
        data = self._NOTIFYICONDATAW()
        data.cbSize = self._ctypes.sizeof(self._NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = self.ICON_ID
        return data

    def _icon_size(self) -> Tuple[int, int]:
        """The side, across and down in pixels, of a small icon at the main screen's scaling."""
        user32 = self._user32
        if self._aware:
            # The window sits at the desktop's top-left corner, so its DPI is the main
            # screen's, which is where the taskbar's tray is.
            dpi = user32.GetDpiForWindow(self._hwnd) or self.BASE_DPI
            return (user32.GetSystemMetricsForDpi(self.SM_CXSMICON, dpi) or self.BASE_ICON,
                    user32.GetSystemMetricsForDpi(self.SM_CYSMICON, dpi) or self.BASE_ICON)
        return (user32.GetSystemMetrics(self.SM_CXSMICON) or self.BASE_ICON,
                user32.GetSystemMetrics(self.SM_CYSMICON) or self.BASE_ICON)

    def show(self, icon_path: str, hover: str) -> None:
        """
        Puts the icon in the tray, or changes it, with that text for when the pointer rests on it.

        Args:
            icon_path: The .ico file; the image for the screen's scaling is taken from it.
            hover: The text, of which Windows shows at most 127 characters.
        """
        ctypes = self._ctypes
        user32, byref = self._user32, ctypes.byref
        width, height = self._icon_size()
        key = (icon_path, width, height)
        if key == self._icon_key:
            icon = self._icon
        else:
            icon = self._check(user32.LoadImageW(None, icon_path, self.IMAGE_ICON, width, height,
                                                 self.LR_LOADFROMFILE), "LoadImageW")
        data = self._notify_data()
        data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP | self.NIF_SHOWTIP
        data.uCallbackMessage = self.CALLBACK_MESSAGE
        data.hIcon = icon
        data.szTip = hover[:127]
        added = self._added
        # Shell_NotifyIconW can say it failed when Explorer was only slow, having done it,
        # so whichever of add and change is tried first, the other is tried after it: a
        # change once the icon is lost (Explorer gone and not yet back) needs an add, and
        # an add which did go through needs a change.
        tries = (self.NIM_MODIFY, self.NIM_ADD) if added else (self.NIM_ADD, self.NIM_MODIFY)
        if not any(self._shell32.Shell_NotifyIconW(what, byref(data)) for what in tries):
            error = ctypes.get_last_error()
            self._added = False
            if icon != self._icon:
                user32.DestroyIcon(icon)
            raise ctypes.WinError(error, "Shell_NotifyIconW failed")
        self._added = True
        if not added:
            # The version is the icon's, so it is set again each time the icon is added.
            data.uVersion = self.NOTIFYICON_VERSION_4
            version_4 = self._shell32.Shell_NotifyIconW(self.NIM_SETVERSION, byref(data))
            self._clicks = self._v4_clicks if version_4 else frozenset((self.WM_LBUTTONUP, self.WM_RBUTTONUP))
        if icon != self._icon:
            # Windows keeps a copy of its own, so the one it replaces can go.
            if self._icon:
                user32.DestroyIcon(self._icon)
            self._icon, self._icon_key = icon, key

    def menu(self, items: List[Item]) -> Optional[str]:
        """
        Shows the menu at the pointer until something is chosen or it is closed. The tray's
        thread is in it all that time, but the window's messages are still heard.

        Args:
            items: Its lines, in order; one with no id is a separator.

        Returns:
            The id of the item chosen, or None if it was closed without one.
        """
        ctypes = self._ctypes
        user32, byref = self._user32, ctypes.byref
        handle = self._check(user32.CreatePopupMenu(), "CreatePopupMenu")
        try:
            # Windows gives back a number for the item chosen, 0 for none, so the items are
            # numbered from 1.
            ids = {}
            for number, item in enumerate(items, start=1):
                if not item.id:
                    self._check(user32.AppendMenuW(handle, self.MF_SEPARATOR, 0, None), "AppendMenuW")
                    continue
                flags = self.MF_STRING
                if item.ticked:
                    flags |= self.MF_CHECKED
                if not item.enabled:
                    flags |= self.MF_GRAYED
                self._check(user32.AppendMenuW(handle, flags, number, _menu_text(item)), "AppendMenuW")
                ids[number] = item.id
            point = self._POINT()
            self._check(user32.GetCursorPos(byref(point)), "GetCursorPos")
            # The window must be the foreground one while the menu is up, or a click
            # elsewhere doesn't close the menu; and the message after it makes Windows
            # switch tasks properly once the menu is gone (KB135788). Clicking the icon
            # lets this program take the foreground, so this is allowed.
            user32.SetForegroundWindow(self._hwnd)
            chosen = user32.TrackPopupMenu(handle, self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON | self.TPM_NONOTIFY,
                                           point.x, point.y, 0, self._hwnd, None)
            user32.PostMessageW(self._hwnd, self.WM_NULL, 0, 0)
            return ids.get(chosen)
        finally:
            user32.DestroyMenu(handle)

    def register(self, modifiers: Tuple[str, ...], key: str) -> bool:
        """
        Registers the shortcut, for the window, so a press of it comes to this thread as
        WM_HOTKEY even while the menu is up.

        Args:
            modifiers: Which of shortcut.MODIFIERS are held.
            key: One of shortcut.KEYS.

        Returns:
            True if it is registered; False if Windows refused it, as when another program
            has it.

        Raises:
            ValueError: For a modifier or key which isn't one of shortcut's.
        """
        flags, code = _hotkey_modifiers(modifiers), _virtual_key(key)
        # One at a time: the window's shortcut has one id, which Windows won't take twice.
        self.unregister()
        if self._user32.RegisterHotKey(self._hwnd, self.HOTKEY_ID, flags, code):
            self._hotkey = True
            return True
        logger.debug(f"Windows refused Nova's shortcut (error {self._ctypes.get_last_error()})")
        return False

    def unregister(self) -> None:
        """Lets the shortcut go, if one is registered."""
        if self._hotkey:
            self._user32.UnregisterHotKey(self._hwnd, self.HOTKEY_ID)
            self._hotkey = False

    def wait(self, seconds: float) -> List[str]:
        """
        Waits until Windows has something for the window or wake is called, at most that
        long, and hands every message waiting to the window.

        Args:
            seconds: The most to wait.

        Returns:
            What happened, in order: "click" for each click on the icon, "shortcut" for
            each press of the shortcut, and "taskbar" when the taskbar was made again. Any
            heard while the menu was up come first, without waiting.
        """
        ctypes = self._ctypes
        user32, byref = self._user32, ctypes.byref
        if not self._events:
            result = user32.MsgWaitForMultipleObjectsEx(1, self._handles, max(0, int(seconds * 1000)),
                                                        self.QS_ALLINPUT, self.MWMO_INPUTAVAILABLE)
            if result == self.WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error(), "MsgWaitForMultipleObjectsEx failed")
        # Whatever ended the wait, everything waiting is handed on, so nothing is left
        # unanswered and the window procedure notes what is Nova's.
        message = self._message
        while user32.PeekMessageW(byref(message), None, 0, 0, self.PM_REMOVE):
            user32.TranslateMessage(byref(message))
            user32.DispatchMessageW(byref(message))
        events = list(self._events)
        del self._events[:]
        return events

    def wake(self) -> None:
        """Ends a wait at once, or the next one if none is under way. Safe from any thread."""
        with self._wake_lock:
            if self._wake_event:
                self._kernel32.SetEvent(self._wake_event)

    def destroy(self) -> None:
        """Takes the icon out of the tray, lets the shortcut go and destroys the window, as far as they were made."""
        if self._hwnd:
            # Asked even if the icon isn't known to be there, as an add Windows said failed
            # may have gone through, and an icon left behind would stay until hovered over.
            data = self._notify_data()
            self._shell32.Shell_NotifyIconW(self.NIM_DELETE, self._ctypes.byref(data))
            self._added = False
            self.unregister()
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._icon:
            self._user32.DestroyIcon(self._icon)
            self._icon = self._icon_key = None
        with self._wake_lock:
            if self._wake_event:
                self._kernel32.CloseHandle(self._wake_event)
                self._wake_event = None
        # Only once the window is gone, as a class can't be unregistered while it has one.
        if self._registered:
            self._user32.UnregisterClassW(self._class_name, self._instance)
            self._registered = False
