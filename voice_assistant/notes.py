"""
Notes Module.

Saves a note as a text file in the Downloads folder, for the satellite on the user's
computer ("make a note called Bins: green on Tuesday").

The title comes from speech, passed through Claude, so it's treated as hostile: it's
made into a plain file name that can only land in the Downloads folder itself, and a
file is created exclusively so a note never replaces one that's already there. When
the name is taken the next free " (2)" to " (99)" is used instead, as a browser does
for a download.
"""

import os
import re

# The longest note saved, in characters - far more than anyone dictates, and enough to
# stop a runaway reply filling the disk.
MOST_TEXT = 50_000

# What's said of a note over MOST_TEXT, by the satellite which refuses it and by the brain,
# which won't send it - so it's the same wherever it's refused.
TOO_LONG = "That note's too long to save."

# What's said of a note when no satellite saves them - by the brain choosing where one goes, and by
# the tool if Claude asks for it anyway.
NO_NOTES = "None of your computers can save notes."

# The longest title kept, before ".txt" - well inside every file system's limit.
MOST_TITLE = 80

# How many notes of one name there can be, counting the first: Bins.txt, Bins (2).txt ... Bins (99).txt.
MOST_NOTES = 99

# What Windows won't take in a file name, and the control characters no system wants in one.
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows' reserved device names: a file called con.txt is the console, not a file.
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)


class NoteRefused(Exception):
    """A note that couldn't be saved. The message is a sentence to say aloud."""


def safe_name(title: str) -> str:
    """
    Makes a title into a file name that's safe on Windows, macOS and Linux.

    Args:
        title: Whatever the title was said or asked as.

    Returns:
        The name without an extension: at most MOST_TITLE characters, with no path
        separators, control characters or characters Windows refuses, no leading dots or
        spaces (a leading dot hides the file) and no trailing ones (Windows drops them).
        "Note" if nothing is left, and a reserved name such as "con" becomes "con note".
    """
    name = UNSAFE.sub("", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Cut last, then strip again, as the cut can leave a space or dot at the end.
    name = name[:MOST_TITLE].strip(" .") or "Note"
    # Windows treats everything from the first dot as an extension, so "con.x" is the
    # console too: the " note" goes after the name itself.
    stem, dot, rest = name.partition(".")
    if RESERVED.fullmatch(stem.rstrip()):
        name = f"{stem} note{dot}{rest}"
    return name


def save_note(folder: str, title: str, text: str, windows: bool) -> str:
    """
    Saves a note as a new text file in a folder.

    Args:
        folder: The Downloads folder. The file is only ever made directly inside it.
        title: What to call the note; made safe first.
        text: What it says. Never logged.
        windows: Whether to end its lines with \\r\\n, as Notepad expects, not \\n.

    Returns:
        The name of the file written, e.g. "Bins.txt" or, if that was taken, "Bins (2).txt".

    Raises:
        NoteRefused with a sentence to say if the text is too long, the folder isn't
        there, all MOST_NOTES names are taken, or the file couldn't be written.
    """
    if len(text) > MOST_TEXT:
        raise NoteRefused(TOO_LONG)
    if not os.path.isdir(folder):
        raise NoteRefused("There's no Downloads folder to save it in.")
    try:
        # Checked before a file is made, so text that can't be written (a lone surrogate
        # from a broken reply) never leaves an empty file behind.
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise NoteRefused("The file couldn't be written.") from error

    name = safe_name(title)
    for number in range(1, MOST_NOTES + 1):
        file_name = f"{name}.txt" if number == 1 else f"{name} ({number}).txt"
        try:
            # "x" creates the file or fails, so another file is never overwritten, even
            # if one appears between looking and writing.
            with open(os.path.join(folder, file_name), "x", encoding="utf-8",
                      newline="\r\n" if windows else "\n") as file:
                file.write(text)
        except FileExistsError:
            continue
        except OSError as error:
            raise NoteRefused("The file couldn't be written.") from error
        return file_name
    raise NoteRefused(f"There are already {MOST_NOTES} notes called that.")
