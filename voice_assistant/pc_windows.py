"""
PC Windows Module.

The PC satellite's windows: which programs have one open, closing a program's windows
or the one in front, and opening something and bringing it to the front - see
docs/superpowers/specs/2026-11-19-pc-open-close-and-commands-design.md.

A window is closed only as its own X closes it, with WM_CLOSE, so the program can ask
about unsaved work: nothing is ever terminated. Nova's own process and Windows' shell
(Explorer, the Start menu, search, the desktop and the taskbar) are never closed, and
never count as open.

Windows itself is reached through _Win32, made by load() only on Windows; tests give
this module a fake of it instead, so it imports anywhere and the tests run on the Pi.
"""

import logging
import ntpath
import os
import re
import sys
import time
from typing import Dict, FrozenSet, List, Optional, Tuple

from voice_assistant.computers import plain

logger = logging.getLogger(__name__)

# The programs of Windows' shell, by exe name, lower-case: never closed, never listed.
SHELL = frozenset({"explorer.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                   "textinputhost.exe"})

# The desktop and the taskbar, by window class - Explorer's, so never closed anyway, but
# named too, as close_front could otherwise be handed them.
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})

# How long something just opened has to show a window to be brought to the front, and
# how often it's looked for meanwhile, in seconds.
FRONT_WAIT = 3.0
FRONT_EVERY = 0.1

# How long a browser which was already open has to open a window of its own, before its
# open one is taken to have the new tab, in seconds.
BROWSER_SETTLE = 0.5

# The Windows API, once load() has made it - None until then, and off Windows.
_api = None

# Each program's name, by the path of its exe, as reading it from the file each time the
# window list is checked would be wasted work.
_names: Dict[str, str] = {}

# The install folders of the Epic and Steam games in the list of things to open, each with
# its name there, longest first - so a game's window, whose exe is often named nothing like
# it ("FortniteClient-Win64-Shipping"), is listed and closed by that name. See set_installed.
_installed: List[Tuple[str, str]] = []


def load() -> bool:
    """
    Makes the Windows API, on Windows.

    Returns:
        Whether it could be - never on another system, nor where it fails, which is logged.
    """
    global _api
    if _api is not None:
        return True
    if sys.platform != "win32":
        return False
    try:
        _api = _Win32()
    except Exception as e:
        logger.warning(f"Couldn't reach Windows' windows, so this PC opens and closes nothing ({type(e).__name__})")
        return False
    return True


def _folder(path: str) -> str:
    """A Windows path as compared: lower-case, with backslashes and one at the end."""
    return ntpath.join(ntpath.normcase(ntpath.normpath(path)), "")


def set_installed(items: List[Dict]):
    """
    Keeps where the games in the list of things to open are installed, so a window whose
    exe lies in one of those folders is named as the list names it.

    Args:
        items: The list, from pc_apps - those with a "folder" count.
    """
    global _installed
    _installed = sorted(((_folder(item["folder"]), item["name"]) for item in items
                         if isinstance(item.get("folder"), str) and item["folder"].strip()),
                        key=lambda each: len(each[0]), reverse=True)


def _installed_name(path: str) -> Optional[str]:
    """The name in the list of things to open of the game whose folder an exe lies in, or None."""
    exe = ntpath.normcase(ntpath.normpath(path))
    return next((name for folder, name in _installed if exe.startswith(folder)), None)


def _name(path: str) -> str:
    """A program's name: its exe's description, as Task Manager shows it, else the exe's name without .exe."""
    if path not in _names:
        try:
            description = _api.description(path)
        except Exception:
            description = None
        _names[path] = (description or "").strip() or ntpath.splitext(ntpath.basename(path))[0]
    return _names[path]


def _program(hwnd: int) -> Optional[Dict[str, str]]:
    """
    Whose a window is, if it's one which may be listed and closed.

    Returns:
        {"name", "own", "exe"}: the program's name - its name in the list of things to
        open, if it's a game installed in a folder there, else its own - its own name (see
        _name) and the exe's, lower-case; or None for the desktop, the taskbar, Nova
        itself, Windows' shell, or a program which can't be told.
    """
    if _api.window_class(hwnd) in SHELL_CLASSES:
        return None
    pid = _api.pid(hwnd)
    if not pid or pid == os.getpid():
        return None
    path = _api.exe(pid)
    if not path:
        return None
    exe = ntpath.basename(path).lower()
    if exe in SHELL:
        return None
    own = _name(path)
    return {"name": _installed_name(path) or own, "own": own, "exe": exe}


def open_windows() -> List[str]:
    """The names of the programs with a window open, each once, in order of name."""
    names = set()
    for hwnd in _api.windows():
        program = _program(hwnd)
        if program is not None:
            names.add(program["name"])
    return sorted(names, key=str.lower)


def close(name: str) -> Optional[str]:
    """
    Closes every window of a program, as its X would.

    Args:
        name: The program, as open_windows names it - or its own name, or its exe's.

    Returns:
        The program's name, or None if none of its windows is open.
    """
    wanted, closed = plain(name), None
    for hwnd in _api.windows():
        program = _program(hwnd)
        if program is None or wanted not in (plain(program["name"]), plain(program["own"]),
                                             plain(ntpath.splitext(program["exe"])[0])):
            continue
        if _api.post_close(hwnd):
            closed = program["name"]
    return closed


def close_front() -> Optional[str]:
    """
    Closes the window in front, as its X would.

    Returns:
        Its program's name, or None if what's in front is nothing, the desktop, the
        taskbar, Nova or Windows' shell.
    """
    hwnd = _api.foreground()
    if not hwnd:
        return None
    program = _program(hwnd)
    if program is None or not _api.post_close(hwnd):
        return None
    return program["name"]


def ready_to_open() -> FrozenSet[int]:
    """
    Gets ready to open something: lets whatever starts take the front itself, and notes
    the windows open now, for bring_to_front to tell which is new. Never raises.

    Returns:
        The windows open now.
    """
    try:
        _api.allow_foreground()
    except Exception as e:
        logger.warning(f"Couldn't let what opens take the front ({type(e).__name__})")
    try:
        return frozenset(_api.windows())
    except Exception as e:
        logger.warning(f"Couldn't list the windows before opening something ({type(e).__name__})")
        return frozenset()


def start(target: str):
    """
    Opens something as double-clicking it would - an app's target from pc_apps, or a web
    address in the default browser.

    Raises:
        OSError: Windows couldn't open it.
    """
    _api.start(target)


def _raise(hwnd: int) -> bool:
    """Brings a window to the front, restored if it was minimised. Returns whether Windows let it."""
    if _api.is_iconic(hwnd):
        _api.restore(hwnd)
    if _api.set_foreground(hwnd):
        return True
    # Windows' foreground lock stops a background program giving the front away; joined
    # to the input of the thread in front for a moment, it can.
    front, own = _api.thread_of(_api.foreground()), _api.own_thread()
    joined = bool(front) and front != own and _api.attach(front, own, True)
    try:
        return bool(_api.set_foreground(hwnd))
    finally:
        if joined:
            _api.attach(front, own, False)


def bring_to_front(before: FrozenSet[int], browser: bool = False, wait: float = FRONT_WAIT) -> bool:
    """
    Brings what was just opened to the front: the satellite runs in the background, so
    Windows otherwise only flashes it in the taskbar. Best effort, and never raises: a
    failure is logged by its type.

    Not yet checked on the PC (the plan's Task 0, his named job): whether
    AllowSetForegroundWindow before the launch is enough, or the AttachThreadInput
    fallback is needed, and whether either gets past the foreground lock from pythonw.

    Args:
        before: The windows open before it was opened, from ready_to_open.
        browser: Whether a web address was opened - when the default browser, if it was
            already open, has the new tab in its open window rather than a new one, so
            that window is raised.
        wait: The most seconds to look for its window.

    Returns:
        Whether a window was brought to the front.
    """
    try:
        started = time.monotonic()
        browser_exe = _api.default_browser() if browser else None
        while True:
            windows = _api.windows()
            target = next((hwnd for hwnd in windows if hwnd not in before and _program(hwnd) is not None), None)
            if target is None and browser_exe and time.monotonic() - started >= BROWSER_SETTLE:
                target = next((hwnd for hwnd in windows
                               if (_program(hwnd) or {}).get("exe") == browser_exe), None)
            if target is not None:
                if _raise(target):
                    return True
                logger.info("Windows wouldn't let what was opened come to the front")
                return False
            if time.monotonic() - started >= wait:
                logger.info("Nothing which was opened showed a window to bring to the front")
                return False
            time.sleep(FRONT_EVERY)
    except Exception as e:
        logger.warning(f"Couldn't bring what was opened to the front ({type(e).__name__})")
        return False


class _Win32:
    """The few parts of the Windows API this module uses, through ctypes - made only on Windows."""

    # Windows' own numbers for what's asked of it.
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x80
    WS_EX_APPWINDOW = 0x40000
    GW_OWNER = 4
    DWMWA_CLOAKED = 14
    WM_CLOSE = 0x0010
    SW_RESTORE = 9
    ASFW_ANY = 0xFFFFFFFF
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    # A Store app's window belongs to this host; the app's own process owns the window inside it.
    FRAME_HOST = "applicationframehost.exe"
    CORE_WINDOW = "Windows.UI.Core.CoreWindow"

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        version = ctypes.WinDLL("version", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        HWND, DWORD, BOOL, UINT = wintypes.HWND, wintypes.DWORD, wintypes.BOOL, wintypes.UINT
        self._each = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
        signatures = (
            (user32.EnumWindows, [self._each, wintypes.LPARAM], BOOL),
            (user32.EnumChildWindows, [HWND, self._each, wintypes.LPARAM], BOOL),
            (user32.IsWindowVisible, [HWND], BOOL),
            (user32.GetWindowTextLengthW, [HWND], ctypes.c_int),
            (user32.GetWindowLongW, [HWND, ctypes.c_int], wintypes.LONG),
            (user32.GetWindow, [HWND, UINT], HWND),
            (user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(DWORD)], DWORD),
            (user32.GetClassNameW, [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user32.GetForegroundWindow, [], HWND),
            (user32.PostMessageW, [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM], BOOL),
            (user32.AllowSetForegroundWindow, [DWORD], BOOL),
            (user32.IsIconic, [HWND], BOOL),
            (user32.ShowWindow, [HWND, ctypes.c_int], BOOL),
            (user32.SetForegroundWindow, [HWND], BOOL),
            (user32.AttachThreadInput, [DWORD, DWORD, BOOL], BOOL),
            (kernel32.GetCurrentThreadId, [], DWORD),
            (kernel32.OpenProcess, [DWORD, BOOL, DWORD], wintypes.HANDLE),
            (kernel32.QueryFullProcessImageNameW, [wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)],
             BOOL),
            (kernel32.CloseHandle, [wintypes.HANDLE], BOOL),
            (version.GetFileVersionInfoSizeW, [wintypes.LPCWSTR, ctypes.POINTER(DWORD)], DWORD),
            (version.GetFileVersionInfoW, [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p], BOOL),
            (version.VerQueryValueW, [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(UINT)], BOOL),
            (dwmapi.DwmGetWindowAttribute, [HWND, DWORD, ctypes.c_void_p, DWORD], ctypes.c_long),
        )
        for function, arguments, result in signatures:
            function.argtypes = arguments
            function.restype = result
        self._user32, self._kernel32, self._version, self._dwmapi = user32, kernel32, version, dwmapi

    def _counts(self, hwnd) -> bool:
        """
        Whether a top-level window is one a person sees and can switch to: shown, titled,
        not a tool's, not cloaked by Windows, and not a dialog of another window.
        """
        user32 = self._user32
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
            return False
        style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
        if style & self.WS_EX_TOOLWINDOW:
            return False
        if user32.GetWindow(hwnd, self.GW_OWNER) and not style & self.WS_EX_APPWINDOW:
            return False
        # Store apps which are suspended keep a window which is "visible" but cloaked.
        cloaked = self._wintypes.DWORD()
        if self._dwmapi.DwmGetWindowAttribute(hwnd, self.DWMWA_CLOAKED, self._ctypes.byref(cloaked),
                                              self._ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return False
        return True

    def windows(self) -> List[int]:
        """The windows which count, top-level, front first."""
        found = []

        def each(hwnd, _):
            try:
                if hwnd and self._counts(hwnd):
                    found.append(hwnd)
            except Exception:
                pass
            return True

        self._user32.EnumWindows(self._each(each), 0)
        return found

    def window_class(self, hwnd) -> str:
        buffer = self._ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def _pid_of(self, hwnd) -> int:
        pid = self._wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return pid.value

    def pid(self, hwnd) -> int:
        """The process a window belongs to - for a Store app's, the app's, not its frame host's."""
        pid = self._pid_of(hwnd)
        path = self.exe(pid)
        if not path or ntpath.basename(path).lower() != self.FRAME_HOST:
            return pid
        inner = []

        def each(child, _):
            try:
                if self.window_class(child) == self.CORE_WINDOW and self._pid_of(child) != pid:
                    inner.append(self._pid_of(child))
                    return False
            except Exception:
                pass
            return True

        self._user32.EnumChildWindows(hwnd, self._each(each), 0)
        return inner[0] if inner else pid

    def thread_of(self, hwnd) -> int:
        return self._user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0

    def own_thread(self) -> int:
        return self._kernel32.GetCurrentThreadId()

    def exe(self, pid: int) -> Optional[str]:
        """The full path of a process's exe, or None if Windows won't say."""
        handle = self._kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = self._wintypes.DWORD(32768)
            buffer = self._ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self._ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            self._kernel32.CloseHandle(handle)

    def description(self, path: str) -> Optional[str]:
        """An exe's FileDescription, as its version information has it, or None."""
        ctypes, version = self._ctypes, self._version
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return None
        pointer, length = ctypes.c_void_p(), self._wintypes.UINT()
        languages = []
        if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
            languages = [f"{words[i]:04x}{words[i + 1]:04x}" for i in range(0, length.value // 2 - 1, 2)]
        # English with Unicode, and with Windows' Western code page, which most exes have when they say none.
        for language in languages + ["040904b0", "040904e4"]:
            if (version.VerQueryValueW(data, f"\\StringFileInfo\\{language}\\FileDescription", ctypes.byref(pointer),
                                       ctypes.byref(length)) and length.value and pointer.value):
                return ctypes.wstring_at(pointer.value)
        return None

    def foreground(self) -> int:
        return self._user32.GetForegroundWindow() or 0

    def post_close(self, hwnd) -> bool:
        return bool(self._user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    def allow_foreground(self):
        self._user32.AllowSetForegroundWindow(self.ASFW_ANY)

    def is_iconic(self, hwnd) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def restore(self, hwnd):
        self._user32.ShowWindow(hwnd, self.SW_RESTORE)

    def set_foreground(self, hwnd) -> bool:
        return bool(self._user32.SetForegroundWindow(hwnd))

    def attach(self, attach: int, to: int, on: bool) -> bool:
        return bool(self._user32.AttachThreadInput(attach, to, on))

    @staticmethod
    def start(target: str):
        os.startfile(target)

    @staticmethod
    def default_browser() -> Optional[str]:
        """The exe of the default browser, lower-case, as Windows' choice for https says - or None."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                r"\UrlAssociations\https\UserChoice") as key:
                program = winreg.QueryValueEx(key, "ProgId")[0]
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, program + r"\shell\open\command") as key:
                command = winreg.QueryValueEx(key, "")[0]
        except OSError:
            return None
        found = re.match(r'\s*(?:"([^"]+)"|(\S+))', command)
        if not found:
            return None
        return ntpath.basename(found.group(1) or found.group(2)).lower()
