#!/usr/bin/env python3
"""
Cut-in Measurement
Measures how much of Nova's own voice, and of the music under it, the echo-cancelled
microphone still hears: plays a WAV (and music under it, if given) through the same
Speaker the satellite uses, records the same Microphone with the same echo canceller for
the number of seconds asked, and writes the result for score_cut_in.py to score on the
brain, where the speech detector and speech-to-text are.

It writes only into the folder given with --out, which must be outside the repository:
heard.wav, the echo-cancelled microphone as 16kHz mono, and ready.json, which says how
long the echo canceller took to be ready once the sound began (ready_after) and where in
the sound the recording began (recorded_from). The recording holds room sound, and anyone
talking in the room, so it is to be deleted once it's scored and never committed.

A freshly made echo canceller passes the microphone through as it is until it has measured
the gap between speaker and microphone, which takes some seconds of sound, so the start of
a recording isn't like the satellite when it has been running a while. Normally the whole
sound is recorded, and score_cut_in.py scores the part after ready_after on its own too.
With --warm-up the sound plays and nothing is recorded until the canceller is ready, and
the --seconds are recorded after that, so the sound has to be long enough for both.

This makes sound in the room and uses the microphone, so run it only when you mean to
- the release checklist says where it's for. Run it on a satellite, from the clone with
venv/bin/python, as the satellite's user. It reads the satellite's own settings and saved
volumes, from NOVA_CONFIG and NOVA_DATA or else ~/.config/nova and ~/.local/share/nova:
on the PC, where only Nova's launcher sets them, set them to the install's folders
(%LOCALAPPDATA%\\nova\\config and %LOCALAPPDATA%\\nova\\data) before running it by hand. It
says which folders, layout and volumes it has found before anything plays, and plays
nothing if there are no saved volumes, unless --volume is given.
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, Optional, Tuple

import numpy as np
import soundfile as sf

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.echo import make_echo_canceller
from voice_assistant.fallback import StateCopy
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.speaker import Speaker

# How much of the recording is waited for at a time, so the moment the echo canceller
# became ready is found to the tenth of a second.
READ_SECONDS = 0.1


def volume_between_0_and_1(text: str) -> float:
    """An argparse type: a volume from 0 to 1."""
    volume = float(text)
    if not 0.0 <= volume <= 1.0:
        raise argparse.ArgumentTypeError(f"{text} isn't between 0 and 1")
    return volume


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
    parser.add_argument("--volume", type=volume_between_0_and_1, default=None,
                        help="Play the voice at this volume, from 0 to 1, and the music at this times --music-share, "
                             "instead of the volumes the brain last set - needed where none are saved")
    parser.add_argument("--warm-up", action="store_true",
                        help="Play the sound but record nothing until the echo canceller is ready, then record "
                             "--seconds. The --play WAV (and --music) must be long enough for the warm-up and "
                             "then --seconds, or the recording stops where the sound ends")
    parser.add_argument("--warm-up-most", type=float, default=30.0,
                        help="With --warm-up, give up if the echo canceller isn't ready after this many seconds "
                             "(default 30)")
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


def saved_volumes(path: str) -> Dict[str, float]:
    """
    Args:
        path: The satellite's saved state - see fallback.StateCopy.

    Returns:
        The volumes the brain last sent, or none if the file is missing or unreadable.
    """
    try:
        with open(path) as file:
            volumes = json.load(file).get("volumes", {})
        return {category: float(volume) for category, volume in volumes.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def chosen_volumes(saved: Dict[str, float], volume: Optional[float], music: bool,
                   music_share: float, path: str = "the saved state") -> Tuple[float, float]:
    """
    Args:
        saved: The volumes the brain last sent - see saved_volumes.
        volume: The voice's volume given on the command line, or None to use the saved one.
        music: Whether there's music to play.
        music_share: How loud the music is, as a share of the music volume.
        path: Where the volumes were looked for, for the message.

    Returns:
        The voice's volume, and the music's.

    Raises:
        SystemExit: If a volume is needed and there's none saved - a copy of the satellite
            run by hand which can't see the install's folders would play at full volume.
    """
    if volume is not None:
        return volume, volume * music_share
    if "general" not in saved or (music and "music" not in saved):
        raise SystemExit(f"No saved volumes to play at in {path}, so nothing is played - this is likely not the "
                         f"install's data folder (set NOVA_DATA to it). Give --volume to choose the volume.")
    return saved["general"], saved.get("music", 1.0) * music_share


def describe(config_dir: str, data_dir: str, push_to_talk: bool, echo: bool, voice: float, music: float) -> str:
    """
    Returns:
        What this run found, to be said before anything plays.
    """
    return (f"Config folder: {config_dir}\nData folder: {data_dir}\n"
            f"Trigger: {'push to talk' if push_to_talk else 'wake word'}\n"
            f"Echo canceller: {'yes' if echo else 'no'}\n"
            f"Voice volume: {voice:.2f}, music volume: {music:.2f}")


def measure(speaker, microphone, echo, voice: str, seconds: float, volume: float,
            music: Optional[str] = None, music_volume: float = 0.0,
            warm_up: bool = False, warm_up_most: float = 30.0):
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
        warm_up: Record nothing until the echo canceller is ready, then record `seconds`.
        warm_up_most: With warm_up, the seconds to wait for it before giving up.

    Returns:
        The recording as 16-bit samples at 16kHz, and what ready.json says: ready_after
        (how long after the sound began the canceller was ready, or None if there's none
        or it never was), recorded_from (how long after the sound began the recording
        did) and, with warm_up, warmed_up - False if it gave up, with nothing recorded.

    Raises:
        SystemExit: If a sound couldn't be played, or was over as soon as it was started.
    """
    # Held for a satellite which only opens its microphone while it's in use, as the PC does.
    microphone.hold()
    try:
        reader = microphone.listen()
        started = time.monotonic()
        playing = [(voice, speaker.play_file(voice, volume=volume, kind="reply", cache=False))]
        if music is not None:
            # Played for as long as is recorded, and waited for, from the start again if it's shorter.
            playing.append((music, speaker.play_file(music, volume=music_volume, kind="music", cache=False,
                                                     length=seconds + (warm_up_most if warm_up else 0.0))))
        for path, playback in playing:
            if playback is None:
                raise SystemExit(f"Couldn't play {path}")
            # A speaker which couldn't be opened gives a sound already finished, and a silent
            # recording would score as clean.
            if playback.done:
                raise SystemExit(f"Nothing is playing: {path} was over as soon as it was started, so the "
                                 f"speaker may not have opened. Check the speaker and try again.")
        speech = playing[0][1]

        ready = {"ready_after": None, "recorded_from": 0.0}
        if warm_up:
            while echo is None or not echo.ready:
                if echo is None or time.monotonic() - started >= warm_up_most or speech.done:
                    print(f"The echo canceller wasn't ready after {time.monotonic() - started:.0f}s, "
                          f"so nothing was recorded.", file=sys.stderr)
                    return np.zeros(0, dtype=np.int16), {"ready_after": None, "recorded_from": None,
                                                         "warmed_up": False}
                # What's heard is waited for and thrown away, which also paces the loop.
                reader.read(round(READ_SECONDS * RATE))
            ready = {"ready_after": round(time.monotonic() - started, 1), "warmed_up": True}
            ready["recorded_from"] = ready["ready_after"]
            reader = microphone.listen()

        pieces = []
        for _ in range(round(seconds / READ_SECONDS)):
            if warm_up and speech.done:
                print(f"The sound ended after {len(pieces) * READ_SECONDS:.1f}s of the {seconds:g}s asked for, "
                      f"so the recording stops there. Give --play a longer sound.", file=sys.stderr)
                break
            pieces.append(reader.read(round(READ_SECONDS * RATE)))
            if not warm_up and ready["ready_after"] is None and echo is not None and echo.ready:
                ready["ready_after"] = round(time.monotonic() - started, 1)
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16), ready
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
    state_path = StateCopy().path
    voice_volume, music_volume = chosen_volumes(saved_volumes(state_path), args.volume, bool(args.music),
                                                args.music_share, state_path)

    # The one speaker and the one microphone, with what's played taken back out of what's
    # heard, built as the satellite builds them.
    echo = make_echo_canceller(config, sample_rate)
    print(describe(paths.CONFIG_DIR, paths.DATA_DIR, push_to_talk, echo is not None, voice_volume, music_volume))
    if echo is None:
        if args.warm_up:
            raise SystemExit("There's no echo canceller (switched off, or speexdsp isn't installed), so there "
                             "is nothing to warm up.")
        print("There's no echo canceller (switched off, or speexdsp isn't installed), so this records "
              "the microphone as it is.", file=sys.stderr)
    speaker = Speaker(sample_rate=sample_rate, echo_reference=echo.played if echo else None,
                      always_open=not push_to_talk)
    microphone = Microphone(sample_rate=sample_rate, echo=echo, always_open=not push_to_talk)
    try:
        heard, ready = measure(speaker, microphone, echo, args.play, args.seconds, volume=voice_volume,
                               music=args.music, music_volume=music_volume,
                               warm_up=args.warm_up, warm_up_most=args.warm_up_most)
    finally:
        speaker.close()
        microphone.close()

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ready.json"), "w") as file:
        json.dump(ready, file)
    if ready.get("warmed_up") is False:
        raise SystemExit(1)
    sf.write(os.path.join(out, "heard.wav"), heard, RATE, subtype="PCM_16")
    print(f"Recorded {len(heard) / RATE:.1f}s into {out}; the echo canceller was "
          f"{'ready after ' + str(ready['ready_after']) + 's' if ready['ready_after'] is not None else 'never ready'}.")
    print("The recording holds room sound: score it, then delete it, and never commit it.")


if __name__ == "__main__":
    measure_cut_in()
