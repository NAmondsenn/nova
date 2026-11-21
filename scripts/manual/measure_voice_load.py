#!/usr/bin/env python3
"""
Voice Load Measurement
Measures whether the brain's processor copes with Nemotron listening while Piper makes a
long reply, as it would for a cut-in: synthesises about the number of seconds of speech
asked for with the brain's voice, twice - on its own, then with Nemotron working through a
spoken clip at the same moment, at the pace of speech - and prints, for each run, its real-time
factor (the time it took over the length of speech it made, so under 1 is faster than the
speech itself) and the longest gap between two pieces of it, which is how long a reply being
played would have to be held back.

It plays no sound and uses no microphone: the speech is only made, and thrown away. Run it
on the brain, where the models are, as the brain's user (the release checklist says how).
"""

import argparse
import itertools
import os
import sys
import threading
import time
from typing import List, Tuple

import numpy as np
import soxr

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.nemotron import RATE, Nemotron
from voice_assistant.text_to_speech import TextToSpeech

# What the reply says, a sentence at a time as a reply is made, said over again until
# there's enough. Plain sentences like Nova's own, none longer than it would speak at once.
SENTENCES = [
    "The weather today is dry and bright, with a light wind from the west.",
    "Your next meeting is at two o'clock, and nothing else is on the calendar this afternoon.",
    "I've turned the lights down in the living room and set a timer for ten minutes.",
    "The train leaves at a quarter past five, so you have about an hour to get ready.",
    "That album was released in nineteen ninety-seven, and most of it was written on tour.",
]

# How much of the spoken clip Nemotron is given at a time, in seconds - the size of a frame
# the recorder checks, which is how it is fed during a real reply.
CLIP_PIECE = 0.03


def parse_args():
    """CLI args: how much speech to make."""
    parser = argparse.ArgumentParser(description="Measure the brain's voice with and without Nemotron listening")
    parser.add_argument("--seconds", type=float, required=True, help="About how much speech to make")
    return parser.parse_args()


def longest_gap(arrivals: List[float]) -> float:
    """
    Args:
        arrivals: When each piece of speech arrived, in seconds, in order.

    Returns:
        The longest time between two of them - 0 if there are fewer than two.
    """
    return max((later - earlier for earlier, later in zip(arrivals, arrivals[1:])), default=0.0)


def real_time_factor(took: float, made: float) -> float:
    """
    Args:
        took: How long making the speech took, in seconds.
        made: How long the speech is, in seconds.

    Returns:
        Took over made, so under 1 is faster than the speech is spoken.
    """
    return took / made if made else 0.0


def synthesise(tts, seconds: float) -> Tuple[float, float, float]:
    """
    Makes speech with Piper, a sentence at a time, until there's this much of it.

    Args:
        tts: The brain's TextToSpeech.
        seconds: How much speech to make, at least.

    Returns:
        How long it took, how long the speech is, and the longest gap between its pieces,
        all in seconds.
    """
    arrivals = []
    made = 0.0
    started = time.monotonic()
    for sentence in itertools.cycle(SENTENCES):
        if made >= seconds:
            break
        for piece in tts.stream(sentence, remember=False):
            arrivals.append(time.monotonic())
            made += len(piece) / tts.sample_rate
    return time.monotonic() - started, made, longest_gap(arrivals)


def clip_pieces(tts) -> List[np.ndarray]:
    """
    Returns:
        A spoken clip, as float32 pieces at Nemotron's rate - the voice's own words, which are
        speech to Nemotron as much as anyone's.
    """
    audio = np.concatenate([piece for sentence in SENTENCES for piece in tts.stream(sentence, remember=False)])
    audio = soxr.resample(audio, tts.sample_rate, RATE)
    size = round(CLIP_PIECE * RATE)
    return [audio[start:start + size] for start in range(0, len(audio), size)]


def listen(nemotron, pieces: List[np.ndarray], stop: threading.Event):
    """
    Has Nemotron work through the clip, over and over, at the pace of speech, until told to stop.

    Args:
        nemotron: The Nemotron, loaded.
        pieces: The clip, from clip_pieces.
        stop: Set when the speech has been made.
    """
    while not stop.is_set():
        stream = nemotron.start()
        started = time.monotonic()
        for number, piece in enumerate(pieces):
            if stop.is_set():
                return
            nemotron.add(stream, piece)
            # Given at the pace it was spoken, as a microphone would give it.
            wait = started + (number + 1) * CLIP_PIECE - time.monotonic()
            if wait > 0:
                time.sleep(wait)


def measure_voice_load():
    """Makes the speech on its own and then with Nemotron listening, and prints how each went."""
    args = parse_args()
    config = Config()
    folder = os.path.join(paths.PROJECT_DIRECTORY, config.section("backend").get("model"))
    if not os.path.isdir(folder):
        raise SystemExit(f"No speech model at {folder}: run this on the brain, where setup.sh --brain put it")

    tts = TextToSpeech(config)
    try:
        nemotron = Nemotron(folder)
        pieces = clip_pieces(tts)
        for name, listening in (("on its own", False), ("with Nemotron listening", True)):
            stop = threading.Event()
            thread = threading.Thread(target=listen, args=(nemotron, pieces, stop), daemon=True)
            if listening:
                thread.start()
            try:
                took, made, gap = synthesise(tts, args.seconds)
            finally:
                stop.set()
                if listening:
                    thread.join()
            print(f"{name}: {made:.1f}s of speech in {took:.1f}s, real-time factor "
                  f"{real_time_factor(took, made):.2f}, longest gap between pieces {gap:.2f}s")
    finally:
        tts.close()


if __name__ == "__main__":
    measure_voice_load()
