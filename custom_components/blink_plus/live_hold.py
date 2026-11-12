"""Keep a camera's live view open from inside Home Assistant.

``blink_plus.start_live_view`` uses this. A hold is a local client of the
camera's live view server, exactly like a player: it connects to
``server.url``, reads the MPEG-TS and throws it away. While it is connected,
the server keeps the Blink session running (and extends it at Blink's time
limit, up to the configured maximum), so a player that attaches later finds a
warm stream instead of waiting 5-16 s for a battery camera to wake up.

One hold per camera. Starting it again only moves its end further out. It
ends after its time, on ``stop_hold`` (``blink_plus.stop_live_view``), or when
the server closes the connection (cool-down, time limit, preemption, unload).

No Home Assistant imports, so the offline tests can run it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
import time
from typing import TYPE_CHECKING, Any
import urllib.parse

if TYPE_CHECKING:
    from .immis import BlinkLiveViewServer

_LOGGER = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5
SETTLE_SECONDS = 1.0  # how long start waits for the server to accept or refuse

TaskFactory = Callable[[Coroutine[Any, Any, None]], asyncio.Task]


class LiveViewHold:
    """One local client that keeps a camera's live view open for a while."""

    def __init__(self, server: BlinkLiveViewServer, seconds: float) -> None:
        """Initialize."""
        self.server = server
        self.deadline = time.monotonic() + seconds
        self.bytes = 0
        self.task: asyncio.Task | None = None

    @property
    def remaining(self) -> int:
        """Seconds left."""
        return max(0, round(self.deadline - time.monotonic()))

    @property
    def running(self) -> bool:
        """Return True while the hold is connected (or connecting)."""
        return self.task is not None and not self.task.done()

    def extend(self, seconds: float) -> None:
        """Keep the hold for at least ``seconds`` from now."""
        self.deadline = max(self.deadline, time.monotonic() + seconds)

    async def run(self) -> None:
        """Connect like a player and read until the time is up."""
        target = urllib.parse.urlparse(self.server.url)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.hostname, target.port), CONNECT_TIMEOUT
        )
        try:
            writer.write(
                b"GET /stream.ts HTTP/1.1\r\nHost: blink-plus-hold\r\n"
                b"User-Agent: blink_plus.start_live_view\r\n\r\n"
            )
            await writer.drain()
            while (left := self.deadline - time.monotonic()) > 0:
                try:
                    chunk = await asyncio.wait_for(reader.read(READ_CHUNK), min(left, 1))
                except TimeoutError:
                    continue
                if not chunk:
                    _LOGGER.debug("%s: hold ended, the server closed it", self.server.name)
                    return
                self.bytes += len(chunk)
            _LOGGER.debug("%s: hold time is up", self.server.name)
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("%s: hold connection lost: %s", self.server.name, err)
        finally:
            writer.close()


_HOLDS: dict[BlinkLiveViewServer, LiveViewHold] = {}


def get_hold(server: BlinkLiveViewServer) -> LiveViewHold | None:
    """Return the running hold of a camera, if any."""
    hold = _HOLDS.get(server)
    return hold if hold is not None and hold.running else None


async def async_start_hold(
    server: BlinkLiveViewServer,
    seconds: float,
    create_task: TaskFactory | None = None,
) -> dict[str, Any]:
    """Start (or extend) the hold of one camera and report how it went.

    A camera in cool-down refuses every client, so we don't connect then; the
    answer says how long to wait instead.
    """
    if server.max_seconds:
        seconds = min(seconds, server.max_seconds)
    hold = get_hold(server)
    if hold is not None:
        hold.extend(seconds)
    elif server.active or not server.blocked_for:
        hold = LiveViewHold(server, seconds)
        hold.task = (create_task or asyncio.ensure_future)(_run(hold))
        _HOLDS[server] = hold
        # Give the server a moment to accept (start a session) or refuse.
        end = time.monotonic() + SETTLE_SECONDS
        while hold.running and not server.active and time.monotonic() < end:
            await asyncio.sleep(0.05)
    return hold_status(server)


async def _run(hold: LiveViewHold) -> None:
    try:
        await hold.run()
    except (TimeoutError, ConnectionError, OSError) as err:
        _LOGGER.warning("%s: could not open the live view: %s", hold.server.name, err)
    finally:
        if _HOLDS.get(hold.server) is hold:
            del _HOLDS[hold.server]


async def async_stop_hold(server: BlinkLiveViewServer) -> bool:
    """End a camera's hold now. Return True if one was running."""
    hold = _HOLDS.pop(server, None)
    if hold is None or hold.task is None or hold.task.done():
        return False
    hold.task.cancel()
    try:
        await hold.task
    except asyncio.CancelledError:
        pass
    return True


def hold_status(server: BlinkLiveViewServer) -> dict[str, Any]:
    """What a caller (the talk card, an automation) needs to know next."""
    hold = get_hold(server)
    stats = server.stats
    return {
        "state": stats.state,
        "holding": hold is not None,
        "seconds": hold.remaining if hold else 0,
        # A running session takes new clients even if a cool-down is set.
        "cooldown_s": 0 if server.active else server.blocked_for,
        "last_error": stats.last_error,
    }
