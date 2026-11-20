"""
PC Apps Module.

The things the PC satellite can open, found on the PC itself - see
docs/superpowers/specs/2026-11-19-pc-open-close-and-commands-design.md. Three places
are looked in, in this order: the Start menu, Epic's installed games and Steam's. Each
thing found is {"id", "name", "kind", "target"}: the id is the satellite's own, a short
hash, which is all the brain asks for it by, and the target is how it's launched, which
never leaves the PC - so nothing the brain sends is ever run, only looked up. An Epic or
Steam game also has its "folder", where it's installed, so its windows can be listed under
its name (see pc_windows.set_installed) - which never leaves the PC either.

Only the standard library is used, and Windows itself only in start_apps and
steam_dirs, so this imports anywhere and the tests run on the Pi.
"""

import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional

from voice_assistant.computers import plain

logger = logging.getLogger(__name__)

# Where Epic keeps a manifest for each game it has installed.
EPIC_MANIFESTS = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "EpicGamesLauncher",
                              "Data", "Manifests")

# How long the Start menu's list may take, in seconds. Get-StartApps takes a second or
# two; PowerShell starting can take longer on a busy PC.
START_APPS_WAIT = 20

# The start of a name which is never offered, as it would remove the thing it names.
UNINSTALL = "uninstall"

# The starts of Steam's names for what it installs to run games, not games themselves.
STEAM_TOOLS = ("Steamworks", "Proton", "Steam Linux Runtime")

# What an Epic manifest's AppCategories must hold one of, for a thing which can be played or run.
EPIC_CATEGORIES = ("games", "applications")

# The end of an Epic name for content added to a game, such as "LEGO Fortnite Content".
EPIC_CONTENT = " Content"

# A quoted value after a key in Steam's files, which escape backslashes and quotes.
_VDF_VALUE = r'"{key}"\s+"((?:[^"\\]|\\.)*)"'

# A backslash in JSON: one before a valid escape is kept, any other is doubled - see _lenient_json.
_BACKSLASH = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})?')


def build(start_apps: Callable[[], List[Dict]], epic_dir: str, steam_dirs: Callable[[], List[str]]) -> List[Dict]:
    """
    Builds the list of things the PC can open.

    Args:
        start_apps: Gives the Start menu's apps, each {"name", "target"} - start_apps,
            or a fake in the tests.
        epic_dir: The folder of Epic's manifests - EPIC_MANIFESTS.
        steam_dirs: Gives the folders Steam is installed in - steam_dirs, or a fake.

    Returns:
        Each thing, as {"id", "name", "kind", "target"}, with "folder" for an Epic or
        Steam game whose install folder is known: never an Uninstall one, and only the
        first of those with the same name - the Start menu's, as it launches directly,
        then Epic's, then Steam's. A place which can't be read adds nothing, and is logged.
    """
    found = []
    for kind, find in (("app", start_apps), ("epic", lambda: epic(epic_dir)), ("steam", lambda: steam(steam_dirs()))):
        try:
            found.extend((kind, item["name"], item["target"], item.get("folder")) for item in find())
        except Exception as e:
            # Only what kind of failure: it may carry a path with his name in it.
            logger.warning(f"Couldn't list the {kind} things on this PC ({type(e).__name__})")
    items, seen = [], set()
    for kind, name, target, folder in found:
        name = name.strip() if isinstance(name, str) else ""
        key = plain(name)
        if not key or key.startswith(UNINSTALL) or key in seen or not isinstance(target, str) or not target:
            continue
        seen.add(key)
        item = {"id": hashlib.sha1((kind + target).encode("utf-8")).hexdigest()[:12], "name": name,
                "kind": kind, "target": target}
        if isinstance(folder, str) and folder.strip():
            item["folder"] = folder
        items.append(item)
    return items


def catalogue() -> List[Dict]:
    """The things this PC can open, from its Start menu, Epic and Steam - see build."""
    return build(start_apps, EPIC_MANIFESTS, steam_dirs)


def start_apps() -> List[Dict]:
    """
    The Start menu's apps, as Windows' own Get-StartApps lists them.

    Returns:
        Each as {"name", "target"}, the target being shell:AppsFolder\\ and its AppID,
        which Explorer launches however it was installed.

    Raises:
        OSError, subprocess.SubprocessError or ValueError: PowerShell couldn't be run, or
            said something which isn't the list.
    """
    # UTF-8 out, so names such as "EA SPORTS FC™ 25" aren't mangled by the console's code page.
    command = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
               "Get-StartApps | ConvertTo-Json -Compress")
    # No console window flashes up from the satellite, which has none of its own.
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                            timeout=START_APPS_WAIT, check=True, creationflags=flags)
    listed = json.loads(result.stdout.decode("utf-8-sig") or "[]")
    # One app comes as itself, not a list of one.
    if isinstance(listed, dict):
        listed = [listed]
    return [{"name": app["Name"], "target": "shell:AppsFolder\\" + app["AppID"]} for app in listed
            if isinstance(app, dict) and isinstance(app.get("Name"), str) and isinstance(app.get("AppID"), str)]


def epic_target(item: Dict) -> str:
    """How Epic's launcher is asked to start one of its games, quietly, from its manifest."""
    return (f"com.epicgames.launcher://apps/{item['CatalogNamespace']}%3A{item['CatalogItemId']}%3A"
            f"{item['AppName']}?action=launch&silent=true")


def steam_target(appid: str) -> str:
    """How Steam is asked to start one of its games."""
    return f"steam://rungameid/{appid}"


def _lenient_json(text: str):
    """
    JSON, read as Epic writes it: its manifests can hold paths such as D:\\Gaming with
    the backslashes unescaped, so if it won't read as it is, each backslash which
    doesn't start an escape is doubled and it's tried again.

    Raises:
        ValueError: It still isn't JSON.
    """
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(_BACKSLASH.sub(lambda found: found.group(0) if found.group(1) else "\\\\", text))


def epic(folder: str) -> List[Dict]:
    """
    Epic's installed games and apps, from its manifests.

    Args:
        folder: Where the manifests are - EPIC_MANIFESTS.

    Returns:
        Each as {"name", "target", "folder"}, the folder being where it's installed, or None
        if the manifest doesn't say: only applications in the games or applications
        category, never content added to a game. A manifest which can't be read is
        skipped, with a warning.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(folder, "*.item"))):
        try:
            with open(path, encoding="utf-8-sig") as file:
                item = _lenient_json(file.read())
            name = item.get("DisplayName")
            categories = item.get("AppCategories") or []
            if (item.get("bIsApplication") is not True or not isinstance(name, str) or name.endswith(EPIC_CONTENT)
                    or not any(category in categories for category in EPIC_CATEGORIES)):
                continue
            folder = item.get("InstallLocation")
            found.append({"name": name, "target": epic_target(item),
                          "folder": folder if isinstance(folder, str) else None})
        except Exception as e:
            logger.warning(f"Skipped an Epic manifest which couldn't be read ({type(e).__name__})")
    return found


def _vdf_value(text: str, key: str) -> Optional[str]:
    """The first value given a key in one of Steam's files, with its escapes undone - or None."""
    found = re.search(_VDF_VALUE.format(key=key), text)
    return re.sub(r"\\(.)", r"\1", found.group(1)) if found else None


def _steam_libraries(steam: str) -> List[str]:
    """The folders of every Steam library, from libraryfolders.vdf - or Steam's own folder, if that can't be read."""
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as file:
            text = file.read()
    except OSError:
        return [steam]
    libraries = [re.sub(r"\\(.)", r"\1", path) for path in re.findall(_VDF_VALUE.format(key="path"), text)]
    return libraries or [steam]


def steam(installs: List[str]) -> List[Dict]:
    """
    Steam's installed games, from each library's app manifests.

    Args:
        installs: The folders Steam is installed in - steam_dirs.

    Returns:
        Each as {"name", "target", "folder"}, the folder being where it's installed (its
        library's steamapps\\common and its installdir), or None if the manifest doesn't say;
        never one of Steam's own tools. A manifest which can't be read is skipped.
    """
    libraries, seen = [], set()
    for install in installs:
        for library in _steam_libraries(install):
            key = os.path.normcase(os.path.normpath(library))
            if key not in seen:
                seen.add(key)
                libraries.append(library)
    found = []
    for library in libraries:
        for path in sorted(glob.glob(os.path.join(library, "steamapps", "appmanifest_*.acf"))):
            try:
                with open(path, encoding="utf-8", errors="replace") as file:
                    text = file.read()
            except OSError as e:
                logger.warning(f"Skipped a Steam manifest which couldn't be read ({type(e).__name__})")
                continue
            name, appid = _vdf_value(text, "name"), _vdf_value(text, "appid")
            if not name or not appid or not appid.isdigit() or name.startswith(STEAM_TOOLS):
                continue
            installed = _vdf_value(text, "installdir")
            found.append({"name": name, "target": steam_target(appid),
                          "folder": os.path.join(library, "steamapps", "common", installed) if installed else None})
    return found


def steam_dirs() -> List[str]:
    """
    The folders Steam is installed in, as its own registry keys say - none off Windows,
    or where it isn't installed.
    """
    if sys.platform != "win32":
        return []
    import winreg

    found = []
    for root, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(root, key) as opened:
                folder = os.path.normpath(winreg.QueryValueEx(opened, value)[0])
        except OSError:
            continue
        if os.path.isdir(folder) and folder not in found:
            found.append(folder)
    return found
