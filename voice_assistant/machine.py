"""
Machine Module.

What kind of computer a satellite is running on, and where its Downloads folder is.

A satellite says which it is in its hello, so the brain can tell "the PC" from "the Pi"
when music is moved, and only a PC or a Mac, the machines with a Downloads folder to
save into, is offered the notes tool.

The Windows lookup asks Windows for the Downloads known folder rather than assuming
%USERPROFILE%\\Downloads, as the folder can be moved to another drive. ctypes is only
imported inside that one function, so nothing here touches Windows' libraries on a Pi
or a Mac.
"""

import os
import sys
import uuid
from typing import Optional

# The kinds of machine a satellite can say it is. "linux" is any Linux that isn't a Pi.
MACHINES = ("pi", "pc", "mac", "linux")

# The device tree's name for the board, which only a Pi-like board has. A module constant
# so a test can point it at a file of its own.
MODEL_FILE = "/proc/device-tree/model"

# Windows' name for the Downloads folder, whatever drive it's been moved to.
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def machine_kind() -> str:
    """
    Says what kind of machine this is.

    Returns:
        One of MACHINES: "pc" on Windows, "mac" on macOS, "pi" on a Linux that names its
        board as a Raspberry Pi, and "linux" on any other.
    """
    if sys.platform == "win32":
        return "pc"
    if sys.platform == "darwin":
        return "mac"
    try:
        with open(MODEL_FILE, "rb") as file:
            model = file.read()
    except OSError:
        # Not a board with a device tree, so an ordinary Linux computer.
        return "linux"
    return "pi" if b"Raspberry Pi" in model else "linux"


def downloads_folder() -> Optional[str]:
    """
    Finds the Downloads folder a note can be saved in.

    Returns:
        Its path on Windows or macOS, or None on any other machine (a Pi's notes
        aren't wanted) or when the folder isn't there.
    """
    kind = machine_kind()
    if kind == "pc":
        try:
            folder = _windows_downloads_folder()
        except (OSError, AttributeError):
            # Windows couldn't say (or ctypes has no windll to ask it with), so the
            # usual place: C:\Users\name\Downloads.
            profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            folder = os.path.join(profile, "Downloads")
    elif kind == "mac":
        folder = os.path.expanduser("~/Downloads")
    else:
        return None
    return folder if os.path.isdir(folder) else None


def _windows_downloads_folder() -> str:
    """
    Asks Windows where the Downloads folder is, through SHGetKnownFolderPath.

    Kept on its own, and importing ctypes only here, so a test can replace it and
    nothing reaches for ctypes.windll on a machine that hasn't got one.

    Returns:
        The folder's path.

    Raises:
        OSError if Windows fails the lookup, AttributeError if there's no ctypes.windll.
    """
    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                    ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]

    identifier = uuid.UUID(FOLDERID_DOWNLOADS)
    folder_id = Guid(identifier.time_low, identifier.time_mid, identifier.time_hi_version,
                     (ctypes.c_ubyte * 8).from_buffer_copy(identifier.bytes[8:]))

    get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
    # An HRESULT result makes ctypes raise OSError for a failed lookup.
    get_folder.restype = ctypes.HRESULT
    get_folder.argtypes = [ctypes.POINTER(Guid), wintypes.DWORD, wintypes.HANDLE,
                           ctypes.POINTER(ctypes.c_void_p)]
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]

    path = ctypes.c_void_p()
    try:
        get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(path))
        if not path.value:
            raise OSError("Windows gave no Downloads folder.")
        return ctypes.wstring_at(path.value)
    finally:
        # Windows allocates the string, and wants it freed even after a failure.
        free(path)
