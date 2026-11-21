#!/usr/bin/env python3
"""
Cut-in Scoring
Scores a recording made by measure_cut_in.py: how much of it the brain's speech detector
(Silero) takes for speech, and what the brain's speech-to-text (Nemotron) hears in it. Prints
one JSON object:

- speech_share: the share of the recording's frames Silero scores at 0.5 or above.
- longest_speech: the longest unbroken run of them, in seconds.
- first_speech_at: when the first of them was, in seconds in, or null if none.
- words: what Nemotron heard.
- own_words: with --said, the words the reply was saying, whether what Nemotron heard is a
  run of them - the reply's own voice - or null if it heard nothing or no --said was given.

Run it on the brain, where the models are, as the brain's user (the release checklist says
how). It plays no sound and uses no microphone. The recording holds room sound: delete it
once it's scored, and never commit it.
"""

import argparse
import json
import os
import re
import sys

import numpy as np
import soundfile as sf

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.recording import FRAME, RATE
from voice_assistant.vad import SileroDetector

# How sure Silero has to be that a frame is speech for it to count here - not the
# detector's own setting, so scores from different runs are always alike.
SPEECH_AT = 0.5


def parse_args():
    """CLI args: the recording, and what the reply was saying."""
    parser = argparse.ArgumentParser(description="Score a recording made by measure_cut_in.py")
    parser.add_argument("heard", help="heard.wav, 16kHz mono")
    parser.add_argument("--said", default=None, help="The words the reply was saying")
    return parser.parse_args()


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


def _words(text: str) -> list:
    """The words of a text, lower-cased and without punctuation."""
    return [word for word in (found.strip("'") for found in re.findall(r"[\w']+", text.lower())) if word]


# When voice_assistant/cut_in.py exists (Task 12), replace this function with
# `from voice_assistant.cut_in import own_words`, which allows one word in four wrong
# and is the check the satellite's cut-in really makes - this one is only a plain run.
def own_words(heard: str, said: str) -> bool:
    """
    Args:
        heard: What the speech-to-text heard.
        said: What the reply was saying.

    Returns:
        Whether the words heard are a run of consecutive words of what was said.
    """
    heard, said = _words(heard), _words(said)
    return any(said[start:start + len(heard)] == heard for start in range(len(said) - len(heard) + 1))


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


def score_cut_in():
    """Scores the recording with Silero, hears it with Nemotron, and prints the result."""
    args = parse_args()
    audio, rate = sf.read(args.heard, dtype="int16")
    if rate != RATE or audio.ndim != 1:
        raise SystemExit(f"{args.heard} is {rate}Hz with {audio.ndim} dimension(s) of samples: "
                         f"it needs to be {RATE}Hz mono, as measure_cut_in.py writes it")

    config = Config()
    folder = os.path.join(paths.PROJECT_DIRECTORY, config.section("backend").get("model"))
    if not os.path.isdir(folder):
        raise SystemExit(f"No speech model at {folder}: run this on the brain, where setup.sh --brain put it")
    # Loaded here, so importing this file needs neither model.
    from voice_assistant.nemotron import Nemotron

    result = score(audio, SileroDetector())
    words = hear(Nemotron(folder), audio)
    result["words"] = words
    result["own_words"] = own_words(words, args.said) if args.said is not None and words else None
    print(json.dumps(result))


if __name__ == "__main__":
    score_cut_in()
