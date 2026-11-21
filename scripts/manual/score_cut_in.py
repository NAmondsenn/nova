#!/usr/bin/env python3
"""
Cut-in Scoring
Scores a recording made by measure_cut_in.py: how much of it the brain's speech detector
(Silero) takes for speech, and what the brain's speech-to-text (Nemotron) hears in it. Prints
one JSON object:

- speech_share: the share of the recording's frames Silero scores at 0.5 or above.
- longest_speech: the longest unbroken run of them, in seconds.
- first_speech_at: when the first of them was, in seconds into the recording, or null if none.
- words: what Nemotron heard.
- own_words: with --said, the words the reply was saying, whether what Nemotron heard is a
  run of them - the reply's own voice - or null if it heard nothing or no --said was given.

The start of a recording made without --warm-up was heard before the echo canceller was
ready, which isn't like the satellite once it has been running. So with a ready.json beside
the recording, the same numbers are printed again for the part after the canceller was ready
under after_ready (null if it never was). --from SECONDS scores only the recording from then
on instead, and prints just that.

Run it on the brain, where the models are. It plays no sound and uses no microphone. Through
the brain's read-only check command the script arrives on stdin, so it can't tell where the
repository is: pass --tree, as the release checklist shows. The recording holds room sound:
delete it once it's scored - and the copy on the brain - and never commit it.
"""

import argparse
import json
import os
import sys
from typing import Optional

import numpy as np
import soundfile as sf

# These are the recorder's, from voice_assistant/recording.py (a test checks they still
# are), written out here as the package can only be imported once the tree is on the path.
FRAME = 480
RATE = 16000

# How sure Silero has to be that a frame is speech for it to count here - not the
# detector's own setting, so scores from different runs are always alike.
SPEECH_AT = 0.5


def parse_args():
    """CLI args: the recording, and what the reply was saying."""
    parser = argparse.ArgumentParser(description="Score a recording made by measure_cut_in.py")
    parser.add_argument("heard", help="heard.wav, 16kHz mono")
    parser.add_argument("--said", default=None, help="The words the reply was saying")
    parser.add_argument("--from", dest="start", type=float, default=None,
                        help="Score only the recording from this many seconds in")
    parser.add_argument("--tree", default=None,
                        help="The repository to import voice_assistant and find the models in - needed when the "
                             "script is run from standard input")
    return parser.parse_args()


def tree_to_import(tree: Optional[str], file: str) -> str:
    """
    Args:
        tree: --tree, or None.
        file: This script's __file__, which is "<stdin>" when it's run from standard input.

    Returns:
        The repository to import from: --tree, else the one this script is in.

    Raises:
        SystemExit: If neither is known.
    """
    if tree:
        return os.path.abspath(os.path.expanduser(tree))
    if os.path.isfile(file):
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(file))))
    raise SystemExit("This script was run from standard input, so it can't tell where the repository is: "
                     "pass --tree <the repository's folder>.")


def score(audio: np.ndarray, detector) -> dict:
    """
    Args:
        audio: 16-bit samples at 16kHz.
        detector: A speech detector - see SileroDetector.

    Returns:
        speech_share, longest_speech and first_speech_at, as the top of this file says.
    """
    detector.reset()
    speech = [detector.speech_probability(audio[start:start + FRAME]) >= SPEECH_AT
              for start in range(0, len(audio) - FRAME + 1, FRAME)]
    longest = run = 0
    for frame in speech:
        run = run + 1 if frame else 0
        longest = max(longest, run)
    first = speech.index(True) if True in speech else None
    return {"speech_share": round(sum(speech) / len(speech), 4) if speech else 0.0,
            "longest_speech": round(longest * FRAME / RATE, 3),
            "first_speech_at": round(first * FRAME / RATE, 3) if first is not None else None}


def hear(nemotron, audio: np.ndarray) -> str:
    """
    Args:
        nemotron: The Nemotron, loaded.
        audio: 16-bit samples at 16kHz.

    Returns:
        The words Nemotron hears in all of it.
    """
    stream = nemotron.start()
    nemotron.add(stream, audio.astype(np.float32) / 32768)
    return str(nemotron.finish(stream)).strip()


def report(audio: np.ndarray, detector, nemotron, said: Optional[str], start: float = 0.0) -> dict:
    """
    Args:
        audio: The whole recording, 16-bit samples at 16kHz.
        detector: A speech detector - see SileroDetector.
        nemotron: The Nemotron, loaded.
        said: What the reply was saying, or None.
        start: Seconds into the recording to score from.

    Returns:
        The numbers for the recording from there on, as the top of this file says, with
        first_speech_at in seconds into the whole recording.
    """
    # Imported here, not at the top, as the package can only be imported once the tree it's
    # in is on the path - which it is by the time anything is reported.
    from voice_assistant.cut_in import own_words

    part = audio[round(start * RATE):]
    result = score(part, detector)
    if result["first_speech_at"] is not None:
        result["first_speech_at"] = round(result["first_speech_at"] + start, 3)
    words = hear(nemotron, part)
    result["words"] = words
    result["own_words"] = own_words(words, said) if said is not None and words else None
    return result


def ready_json(heard: str) -> Optional[dict]:
    """
    Args:
        heard: The path of the recording.

    Returns:
        What the ready.json beside it says, or None if there isn't a readable one.
    """
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(heard)), "ready.json")) as file:
            ready = json.load(file)
    except (OSError, ValueError):
        return None
    return ready if isinstance(ready, dict) else None


def ready_from(ready: dict) -> Optional[float]:
    """
    Args:
        ready: What measure_cut_in.py wrote in ready.json.

    Returns:
        The seconds into the recording from which the echo canceller was ready, or None if
        it never was.
    """
    if ready.get("ready_after") is None:
        return None
    return max(0.0, ready["ready_after"] - (ready.get("recorded_from") or 0.0))


def score_cut_in():
    """Scores the recording with Silero, hears it with Nemotron, and prints the result."""
    args = parse_args()
    tree = tree_to_import(args.tree, globals().get("__file__", "<stdin>"))
    audio, rate = sf.read(args.heard, dtype="int16")
    if rate != RATE or audio.ndim != 1:
        raise SystemExit(f"{args.heard} is {rate}Hz with {audio.ndim} dimension(s) of samples: "
                         f"it needs to be {RATE}Hz mono, as measure_cut_in.py writes it")

    # The package is imported only now, once the tree it's in is on the path, and so is
    # each model - importing this file needs neither.
    sys.path.insert(0, tree)
    from voice_assistant import paths
    from voice_assistant.config import Config
    from voice_assistant.nemotron import Nemotron
    from voice_assistant.vad import SileroDetector

    folder = os.path.join(paths.PROJECT_DIRECTORY, Config().section("backend").get("model"))
    if not os.path.isdir(folder):
        raise SystemExit(f"No speech model at {folder}: run this on the brain, where setup.sh --brain put it")
    detector, nemotron = SileroDetector(), Nemotron(folder)

    if args.start is not None:
        result = report(audio, detector, nemotron, args.said, args.start)
    else:
        result = report(audio, detector, nemotron, args.said)
        ready = ready_json(args.heard)
        if ready is not None:
            offset = ready_from(ready)
            result["after_ready"] = None if offset is None else report(audio, detector, nemotron, args.said, offset)
    print(json.dumps(result))


if __name__ == "__main__":
    score_cut_in()
