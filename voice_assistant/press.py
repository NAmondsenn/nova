"""
Press.

How a key press reaches a push-to-talk satellite, such as the PC: a small web server on
this machine's loopback address, which the key's script (flow100.ahk's Numpad 3) posts
to. It answers one request only, and refuses anything which isn't certainly that - a web
page open in the user's browser can post to a local address too, so the request must
carry a header no page can add without the server's say-so (X-Nova-Press), and the Host
it was sent to must be this address, which a page reaching it through a name of its own
(DNS rebinding) won't have.
"""

import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Where it listens: this machine only, never the network.
ADDRESS = "127.0.0.1"

# The header a press must carry, and its value. A browser page can't send a custom
# header to another origin without asking first (an OPTIONS request, which is refused).
PRESS_HEADER = "X-Nova-Press"

# How soon after one press the next is taken for a bounce of the same key rather than
# another press, in seconds. A key held down repeats far quicker than anyone presses
# twice, and a deliberate second press - to end a recording - takes longer.
PRESS_DEBOUNCE = 0.4

# How long a connection may stay silent before it's dropped, in seconds, so one which
# says nothing can't hold a thread for good.
CLIENT_TIMEOUT = 5


def _on_windows() -> bool:
    """Whether this is Windows, where the PC's push-to-talk satellite runs."""
    return os.name == "nt"


class _Server(ThreadingHTTPServer):
    """A server which keeps its port to itself, which Windows needs to be asked for."""

    def server_bind(self):
        if _on_windows():
            # Where SO_REUSEADDR, which the standard server sets, lets a second socket
            # bind a port which is in use - a second satellite, or another program on
            # this machine - and presses then go to whichever of them Windows picks.
            # Left on elsewhere, where it only gets past connections still closing, so a
            # restarted satellite can bind again at once.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _Handler(BaseHTTPRequestHandler):
    """Answers POST /press, and refuses everything else."""

    timeout = CLIENT_TIMEOUT

    def do_POST(self):
        listener: "PressListener" = self.server.listener
        expected_host = f"{ADDRESS}:{self.server.server_address[1]}"
        if (self.path != "/press" or self.headers.get(PRESS_HEADER) != "1"
                or self.headers.get("Host") != expected_host):
            self._answer(403)
            return
        try:
            listener.pressed()
        except Exception:
            logger.exception("A press failed")
            self._answer(500)
            return
        self._answer(204)

    def _refuse(self):
        self._answer(403)

    # Whatever else is asked is refused the same way. A method not named here is
    # answered 501 by the standard handler, which does nothing either.
    do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

    def _answer(self, status: int):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        # To the satellite's log, not to the console.
        logger.debug("Press listener: " + format % args)


class PressListener:
    """Calls a function whenever a press is posted to it, from a thread of its own."""

    def __init__(self, port: int, on_press: Callable[[], None]):
        """
        Args:
            port: Where to listen on this machine, or 0 for any free port - see port.
            on_press: Called, on one of the listener's threads, for each press which isn't
                a bounce of the one before. It should return quickly.
        """
        self._port = port
        self._on_press = on_press
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        # Guards the time of the last press, as each request has a thread of its own.
        self._lock = threading.Lock()
        self._last_press: Optional[float] = None

    @property
    def port(self) -> int:
        """Where it listens - once started, the free port it found if it was given 0."""
        return self._port

    def start(self):
        """
        Starts listening.

        Raises:
            OSError: The port is already taken.
        """
        server = _Server((ADDRESS, self._port), _Handler)
        server.listener = self
        self._server = server
        # What it was given if that was a port, and the one found if it was 0.
        self._port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="press")
        self._thread.start()
        logger.info(f"Listening for key presses on {ADDRESS}:{self.port}")

    def close(self):
        """Stops listening. Safe to call if it never started, and more than once."""
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def pressed(self):
        """Counts one request which has been accepted as a press - unless it's a bounce."""
        with self._lock:
            now = time.monotonic()
            if self._last_press is not None and now - self._last_press < PRESS_DEBOUNCE:
                return
            self._last_press = now
        self._on_press()
