"""
Long Recordings Module
Keeps a small record of the push-to-talk recordings whose speech still ran over five
seconds, so it can be seen whether ending a recording when the voice drops away or the
words stop (see recording.py) works, and what is left.

One line of JSON per recording goes in long-recordings.jsonl in the logs folder: when it
was noted, how long the speech was, the numbers the recorder saw, and the words heard -
only if the caller gives them, which it does while logging.log_transcripts is on, and a
line without them takes them out of the lines kept before it too. Never audio. The file
is rewritten to keep only the newest MOST lines, so it can't grow, and there's no date
after which it stops: how long it's wanted for is a decision for later.
"""

import contextlib
import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

# Logger setup
logger = logging.getLogger(__name__)

# Speech longer than this, in seconds, is worth a line: most requests are said well
# within it, so what's over it is the recordings the ends may not have caught.
LONGEST_SPEECH = 5.0

# How many lines are kept - enough to see a pattern across a few days, and small enough
# for the file to be read whole.
MOST = 50

# Held while the file is read and rewritten. Each satellite's session saves from a thread
# of its own, and without it two could read the same lines and the second to write would
# lose the first's.
_save_lock = threading.Lock()

# Whether a failure has been logged since the file last worked, so a disk which stays
# full is one line in the log and not one for every long recording.
_warned = False


def note_long_recording(path: str, at: datetime, speech_seconds: float, report: Dict,
                        words: Optional[str] = None) -> bool:
    """
    Adds a recording to the record if its speech was long, dropping the oldest lines
    once there are more than MOST. Without the words - "Keep what's said in the logs" is
    off - it takes them out of the lines already kept as well, so they don't stay there
    once the switch is off until they drop off the end.

    Best-effort: a failure is only logged - the first time, until the file works again -
    since keeping this record must never be what stops a request being answered. That
    goes for what it's given too: nothing in making the line is ever raised.

    Args:
        path: The .jsonl file to keep the record in.
        at: When it's noted - once the words are known, a moment after the recording ended.
        speech_seconds: How long the speech in it lasted, from speech_started to
            speech_ended.
        report: What record_until_silence said about it - what ended it and the numbers
            it saw. Kept as it is, so it must hold nothing but numbers and strings.
        words: What was heard, or None to leave the words out - of every line kept.

    Returns:
        Whether a line was written.
    """
    global _warned
    try:
        if speech_seconds <= LONGEST_SPEECH:
            return False
        line = {"at": at.isoformat(timespec="seconds"), "speech_seconds": round(speech_seconds, 2), **report}
        if words is not None:
            line["words"] = words
        temporary = path + ".new"
        with _save_lock:
            kept = []
            if os.path.exists(path):
                # Read whatever is there, damaged or not, so one bad line can't stop the record
                # being kept - it drops off the end as newer lines come.
                with open(path, encoding="utf-8", errors="replace") as f:
                    kept = [kept_line for kept_line in f.read().splitlines() if kept_line.strip()]
            if words is None:
                kept = _without_words(kept)
            kept.append(json.dumps(line))
            # Written beside the file and moved over it, so a failure part way through
            # leaves the lines already kept as they were.
            try:
                with open(temporary, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept[-MOST:]) + "\n")
                os.replace(temporary, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(temporary)
                raise
        _warned = False
        return True
    except Exception as e:
        if not _warned:
            _warned = True
            logger.warning(f"Could not keep the note of a long recording in {path}: {e}")
        return False


def _without_words(kept: List[str]) -> List[str]:
    """
    The lines kept, each without its words. One which can't be read as a line of the record
    goes, as what is in it can't be told from words; one without words is kept just as it was.
    """
    stripped = []
    for kept_line in kept:
        try:
            line = json.loads(kept_line)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        if "words" in line:
            del line["words"]
            kept_line = json.dumps(line)
        stripped.append(kept_line)
    return stripped
