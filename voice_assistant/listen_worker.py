"""
Listen Worker Module.

The process songs are listened to in, to judge them clean or explicit (decision 0022,
section 2). Nova starts it through listener.Listener, at the lowest priority, with its own
copy of Nemotron: a thread inside the brain couldn't be given a lower priority than the
rest of it, and sharing the brain's own model would hold up what the user says to Nova.

    python -m voice_assistant.listen_worker <model folder> <threads>

Each song comes on stdin as a 4-byte little-endian length, then that many bytes of
float32 samples at 16kHz. Each is answered with one line of JSON on stdout - {"text": ...}
with what was heard, or {"error": ...} with a plain reason - and the worker ends when its
stdin does. Anything else it has to say goes to stderr, which Nova logs.
"""

import json
import os
import struct
import sys
import traceback
from typing import BinaryIO, Optional

import numpy as np

from voice_assistant.nemotron import RATE, Nemotron


def hear(model, samples: np.ndarray) -> str:
    """
    What's sung in a song, given to the model a second at a time from start to end - the
    way the spike behind decision 0022's thresholds listened, so they hold.

    Args:
        model: The Nemotron, or anything with its start, add and finish.
        samples: The whole song, float32 at 16kHz.

    Returns:
        The words, as the model gives them.
    """
    stream = model.start()
    for start in range(0, len(samples), RATE):
        model.add(stream, samples[start:start + RATE])
    return model.finish(stream)


def _read(reader: BinaryIO, size: int) -> Optional[bytes]:
    """Exactly size bytes, or None if the input ends first."""
    data = bytearray()
    while len(data) < size:
        piece = reader.read(size - len(data))
        if not piece:
            return None
        data += piece
    return bytes(data)


def serve(model, reader: BinaryIO, writer: BinaryIO):
    """
    Answers every song sent, one at a time, until the input ends.

    Args:
        model: The Nemotron.
        reader: Where songs come from.
        writer: Where answers go.
    """
    while True:
        header = _read(reader, 4)
        if header is None:
            return
        data = _read(reader, struct.unpack("<I", header)[0])
        # Cut short: Nova has gone, or given up on this song and is about to kill this.
        if data is None:
            return
        try:
            if len(data) % 4:
                raise ValueError(f"{len(data)} bytes isn't a whole number of samples")
            answer = {"text": hear(model, np.frombuffer(data, dtype="<f4"))}
        except Exception:
            # The detail is for the log; the reason goes back to Nova, which may say it.
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            answer = {"error": "couldn't hear the song"}
        writer.write(json.dumps(answer).encode() + b"\n")
        writer.flush()


def main(argv=None):
    folder, threads = (argv if argv is not None else sys.argv[1:])[:2]
    # Nova starts this under "nice -n 19" too, but it may not have nice to hand.
    try:
        os.nice(19)
    except OSError:
        pass
    # The answers have stdout to themselves: anything else printed there, by this or by
    # the libraries under the model, is moved to stderr, where it's only logged.
    answers = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    model = Nemotron(folder, int(threads))
    serve(model, sys.stdin.buffer, answers)


if __name__ == "__main__":
    main()
