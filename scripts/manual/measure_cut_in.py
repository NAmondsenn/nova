#!/usr/bin/env python3
"""
Cut-in Measurement
Measures how much of Nova's own voice, and of the music under it, the echo-cancelled
microphone still hears: plays a WAV (and music under it, if given) through the same
Speaker the satellite uses, records the same Microphone with the same echo canceller for
the number of seconds asked, and writes the result for score_cut_in.py to score on the
brain, where the speech detector and speech-to-text are.

It writes only into the folder given with --out, which must be outside the repository:
heard.wav, the echo-cancelled microphone as 16kHz mono, and ready.json, how long the echo
canceller took to be ready once the sound began. The recording holds room sound, and
anyone talking in the room, so it is to be deleted once it's scored and never committed.

This makes sound in the room and uses the microphone, so run it only when you mean to
- the release checklist says where it's for. Run it on a satellite, from the clone with
venv/bin/python, as the satellite's user.
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

import numpy as np
import soundfile as sf

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from voice_assistant.config import Config
from voice_assistant.echo import make_echo_canceller
from voice_assistant.fallback import StateCopy
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.speaker import Speaker

# How much of the recording is waited for at a time, so the moment the echo canceller
# became ready is found to the tenth of a second.
READ_SECONDS = 0.1


def parse_args():
    """CLI args: what to play, for how long, and where to put the recording."""
    parser = argparse.ArgumentParser(description="Measure what the echo-cancelled microphone hears of Nova's own voice")
    parser.add_argument("--play", required=True, help="WAV of Nova's voice to play")
    parser.add_argument("--seconds", type=float, required=True, help="How long to record")
    parser.add_argument("--out", required=True,
                        help="Folder for heard.wav and ready.json, outside the repository")
    parser.add_argument("--music", default=None, help="WAV of music to play under the voice")
    parser.add_argument("--music-share", type=float, default=0.3,
                        help="How loud the music is, as a share of the music volume the brain last set (default 0.3)")
    return parser.parse_args()


def checked_out_folder(out: str) -> str:
    """
    Args:
        out: The folder the recording is to be written to.

    Returns:
        Its real path, with any symlinks followed - so a link outside the repository
        which leads into it doesn't get a recording of the room kept there.

    Raises:
        SystemExit: If it's inside the repository, where it could be committed.
    """
    folder = os.path.normcase(os.path.realpath(out))
    repository = os.path.normcase(os.path.realpath(ROOT))
    try:
        inside = os.path.commonpath([folder, repository]) == repository
    except ValueError:
        # Another drive, on Windows.
        inside = False
    if inside:
        raise SystemExit(f"Not writing the recording into {out}: it's inside the repository, and the "
                         f"recording holds room sound which must never be committed. Give --out a folder "
                         f"outside it, such as one in the temporary folder.")
    return os.path.realpath(out)


def measure(speaker, microphone, echo, voice: str, seconds: float, volume: float,
            music: Optional[str] = None, music_volume: float = 0.0):
    """
    Plays the voice, and the music under it, and records the microphone meanwhile.

    Args:
        speaker: The satellite's Speaker.
        microphone: The satellite's Microphone, with the echo canceller on it.
        echo: The EchoCanceller, or None if there isn't one.
        voice: Path of the WAV of Nova's voice.
        seconds: How long to record.
        volume: How loud the voice plays, from 0 to 1.
        music: Path of the WAV of music to play under it, or None.
        music_volume: How loud the music plays, from 0 to 1.

    Returns:
        The recording as 16-bit samples at 16kHz, and how long after the sound began the
        echo canceller was ready - or None if there's none, or it never was.

    Raises:
        SystemExit: If a sound couldn't be played.
    """
    # Held for a satellite which only opens its microphone while it's in use, as the PC does.
    microphone.hold()
    try:
        reader = microphone.listen()
        started = time.monotonic()
        playing = [(voice, speaker.play_file(voice, volume=volume, kind="reply", cache=False))]
        if music is not None:
            # Played for as long as is recorded, from the start again if it's shorter.
            playing.append((music, speaker.play_file(music, volume=music_volume, kind="music", cache=False,
                                                     length=seconds)))
        for path, playback in playing:
            if playback is None:
                raise SystemExit(f"Couldn't play {path}")

        pieces = []
        ready_after = None
        for _ in range(round(seconds / READ_SECONDS)):
            pieces.append(reader.read(round(READ_SECONDS * RATE)))
            if ready_after is None and echo is not None and echo.ready:
                ready_after = round(time.monotonic() - started, 1)
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16), ready_after
    finally:
        speaker.stop()
        microphone.release()


def measure_cut_in():
    """Opens the satellite's speaker and microphone, plays and records, and writes what was heard."""
    args = parse_args()
    out = checked_out_folder(args.out)
    for path in [args.play] + ([args.music] if args.music else []):
        if not os.path.isfile(path):
            raise SystemExit(f"No such file: {path}")

    config = Config()
    audio_config = config.section("audio")
    sample_rate = audio_config.get("sample_rate", 48000)
    # A push-to-talk satellite opens each only when needed - see satellite.py.
    push_to_talk = config.section("satellite").get("trigger", "wake_word") == "push_to_talk"
    state = StateCopy()

    # The one speaker and the one microphone, with what's played taken back out of what's
    # heard, built as the satellite builds them.
    echo = make_echo_canceller(config, sample_rate)
    if echo is None:
        print("There's no echo canceller (switched off, or speexdsp isn't installed), so this records "
              "the microphone as it is.", file=sys.stderr)
    speaker = Speaker(sample_rate=sample_rate, echo_reference=echo.played if echo else None,
                      always_open=not push_to_talk)
    microphone = Microphone(sample_rate=sample_rate, echo=echo, always_open=not push_to_talk)
    try:
        heard, ready_after = measure(speaker, microphone, echo, args.play, args.seconds,
                                     volume=state.volume("general"), music=args.music,
                                     music_volume=state.volume("music") * args.music_share)
    finally:
        speaker.close()
        microphone.close()

    os.makedirs(out, exist_ok=True)
    sf.write(os.path.join(out, "heard.wav"), heard, RATE, subtype="PCM_16")
    with open(os.path.join(out, "ready.json"), "w") as file:
        json.dump({"ready_after": ready_after}, file)
    print(f"Recorded {len(heard) / RATE:.1f}s into {out}; the echo canceller was "
          f"{'ready after ' + str(ready_after) + 's' if ready_after is not None else 'never ready'}.")
    print("The recording holds room sound: score it, then delete it, and never commit it.")


if __name__ == "__main__":
    measure_cut_in()
