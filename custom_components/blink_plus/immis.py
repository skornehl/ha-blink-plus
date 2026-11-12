"""Blink live view (IMMIS) streaming engine.

Reverse engineered from the Blink Android app 60.0 (WalnutSignalling /
BlinkWalnutLiveViewSessionManager / libwalnut.so IMMIStreamSource) and from
blinkpy's livestream.py.

How it works
------------
Every camera gets one persistent local TCP server (``tcp://<host>:<port>``).
Nothing talks to Blink while nobody is connected, so no battery is used.

When the first client connects (HA stream worker, HomeKit ffmpeg, go2rtc,
VLC...), we:

1. ``POST .../liveview`` (v6 cameras, v2 owls/doorbells) with
   ``{"intent": "liveview"}``. The response has ``server``
   (``immis://host:443/<conn_id>__IMDS_<serial>?client_id=N``),
   ``command_id``, ``polling_interval``, ``duration``, ``continue_interval``,
   ``extended_duration``, ``type`` (lv/elv) and ``liveview_token``.
2. Open TLS to that server and send the 122-byte IMMIS auth header. The app
   hands ``liveview_token`` to the native player (``Player.setAuthToken``),
   which fills the 64-byte token field. Older firmware accepts zeros there.
3. Read IMMIS frames: 9-byte header ``type(1) seq(4) len(4)``. Type 0x00
   carries MPEG-TS, which is copied to every connected client. We send a
   keepalive (0x0A) every 10 s and latency stats (0x12) every second.
4. Poll the command every ``polling_interval``. When Blink ends the session
   (time limit) and clients are still watching, start a new one with
   ``intent=extended_liveview``, as the app does for RESTART_IN_ELV.
5. When the last client leaves, wait a short linger time, then close the
   session and mark the command done.

Two-way talk
------------
Talk rides the open IMMIS connection, as in the app (talk only works during
live view). The sequence below follows zperkd/blink-plus (MIT), where it was
verified audibly on a Mini with the Blink app closed:

* the session must have been opened with the real ``liveview_token`` in the
  auth header, followed by the app's initial keepalive (0x0A, seq 0);
* speaker on is IMMI 0x17 with control ID 3, then wait for a fresh 0x18 ack;
* audio goes out as IMMI 0x05 packets, one ADTS AAC-LC 16 kHz mono frame each,
  with their own sequence counting from 0;
* speaker off is IMMI 0x17 with control ID 4.

``talk.py`` checks and paces the audio; this module only writes the packets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextvars
import inspect
from dataclasses import dataclass, field
import json
import logging
import ssl
import time
from typing import Any
import urllib.parse

from blinkpy import api as blink_api

_LOGGER = logging.getLogger(__name__)

# Closing a live view command is best effort. blinkpy logs every connection
# error as ERROR ("possibly down or throttled"); for these calls we retry and
# log ourselves instead. The flag is per task, so other requests still log.
_QUIET_BLINKPY = contextvars.ContextVar("blink_plus_quiet_blinkpy", default=False)


class _QuietBestEffort(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _QUIET_BLINKPY.get():
            return True
        # Keep what blinkpy knew, at debug level. Its message often ends in an
        # empty "Error: " (an exception without text), so log type and repr.
        args = record.args if isinstance(record.args, tuple) else ()
        err = next((a for a in args if isinstance(a, BaseException)), None)
        _LOGGER.debug(
            "blinkpy (muted, best effort): %s [%s]",
            record.getMessage(),
            _describe(err) if err else "-",
        )
        return False


def _describe(err: BaseException) -> str:
    """Type and repr of an exception and what caused it (aiohttp often wraps
    the real OS error in a bare ClientConnectionError)."""
    parts = []
    seen: set[int] = set()
    while err is not None and id(err) not in seen and len(parts) < 4:
        seen.add(id(err))
        parts.append(f"{type(err).__module__}.{type(err).__qualname__}: {err!r}")
        err = err.__cause__ or err.__context__
    return " <- ".join(parts)


logging.getLogger("blinkpy.auth").addFilter(_QuietBestEffort())

TS_PACKET = 188
IMMI_HEADER = 9
MSG_VIDEO = 0x00
MSG_KEEPALIVE = 0x0A
MSG_LATENCY = 0x12
# IMMI_DATA_FLAG enum from libwalnut.so DWARF: DATA=0 END=1 STATS=2 LOG=3 JPEG=4
# AUDIO=5 START=6 STOP=7 CLOSE=8 KEEPALIVE_CONFIG=9 KEEPALIVE=10 JOIN=11
# AUDIO_CONFIG=12 CLIENT_JOIN=13 REQUEST_SHUTDOWN=14 BUTTON_PRESS=15 ...
MSG_STOP = 0x07
MSG_AUDIO = 0x05
MSG_SPEAKER = 0x17  # speaker control, the control ID goes in the seq field
MSG_SPEAKER_ACK = 0x18
SPEAKER_ON = 3
SPEAKER_OFF = 4
TALK_ACK_TIMEOUT = 3  # s to wait for the camera to acknowledge speaker on
# The app sends its first keepalive (seq 0) together with the auth header.
INITIAL_KEEPALIVE = bytes([MSG_KEEPALIVE]) + bytes(8)
# IMMIStreamSource::setConnectionState(Disconnecting): stop keepalives, then
# sendMediaInfo(STOP, seq=1, empty payload), then close the socket.
STOP_PACKET = bytes([MSG_STOP]) + (1).to_bytes(4, "big") + (0).to_bytes(4, "big")

TOKEN_MODE_AUTO = "auto"  # use liveview_token, fall back to zeros if it fails
TOKEN_MODE_TOKEN = "token"
TOKEN_MODE_NULL = "null"

MAX_FRAME = 4 * 1024 * 1024  # bytes; a larger IMMIS frame means a broken stream
CLIENT_QUEUE_CHUNKS = 600  # about 10-20 s of video per client before we drop it
FIRST_DATA_TIMEOUT = 30  # seconds to wait for first video after auth


class LiveViewError(Exception):
    """Live view could not be started."""


class TalkError(Exception):
    """Two-way talk is not possible right now."""


def immi_packet(msgtype: int, seq: int, payload: bytes = b"") -> bytes:
    """Build one IMMI packet: type(1) seq(4) len(4) payload."""
    return (
        bytes([msgtype])
        + (seq & 0xFFFFFFFF).to_bytes(4, "big")
        + len(payload).to_bytes(4, "big")
        + payload
    )


@dataclass
class LiveStats:
    """Runtime info, shown as camera attributes."""

    state: str = "idle"  # idle | starting | streaming | error
    clients: int = 0
    sessions: int = 0
    bytes_streamed: int = 0
    last_error: str | None = None
    last_server: str | None = None
    session_type: str | None = None
    token_used: bool | None = None
    started_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return attributes."""
        return {
            "live_view_state": self.state,
            "live_view_clients": self.clients,
            "live_view_sessions": self.sessions,
            "live_view_bytes": self.bytes_streamed,
            "live_view_last_error": self.last_error,
            "live_view_type": self.session_type,
            "live_view_token_used": self.token_used,
            "live_view_viewers": self.extra.get("viewers", []),
            "live_view_cooldown_s": self.extra.get("cooldown", 0),
            "live_view_talking": self.extra.get("talking", False),
        }


def live_key(camera: Any) -> str:
    """Unique key per camera: type + network + camera id (serials can be missing)."""
    return (
        f"{getattr(camera, 'camera_type', '') or 'cam'}-"
        f"{camera.network_id}-{camera.camera_id}"
    )


def liveview_url(blink, camera) -> str:
    """Return the live view endpoint for this camera type (from app 60.0)."""
    acct = blink.account_id
    net = camera.network_id
    cid = camera.camera_id
    base = blink.urls.base_url
    ctype = getattr(camera, "camera_type", "") or ""
    if ctype == "mini":
        return f"{base}/api/v2/accounts/{acct}/networks/{net}/owls/{cid}/liveview"
    if ctype == "doorbell":
        return f"{base}/api/v2/accounts/{acct}/networks/{net}/doorbells/{cid}/liveview"
    return f"{base}/api/v6/accounts/{acct}/networks/{net}/cameras/{cid}/liveview"


def _string_field(value: str, max_len: int) -> bytes:
    raw = value.encode("utf-8")[:max_len].ljust(max_len, b"\x00")
    return len(raw).to_bytes(4, "big") + raw


def build_auth_header(serial: str, server: str, token: str | None) -> bytes:
    """Build the 122-byte IMMIS auth header."""
    target = urllib.parse.urlparse(server)
    client_id = urllib.parse.parse_qs(target.query).get("client_id", ["0"])[0]
    conn_id = target.path.split("/")[-1].split("__")[0]
    out = bytearray(b"\x00\x00\x00\x28")
    out += _string_field(serial or "", 16)
    out += int(client_id).to_bytes(4, "big")
    out += b"\x01\x08"
    if token:
        out += _string_field(token, 64)
    else:
        out += (64).to_bytes(4, "big") + b"\x00" * 64
    out += _string_field(conn_id, 16)
    out += b"\x00\x00\x00\x01"
    return bytes(out)


def serial_for(server: str, camera: Any) -> str:
    """Return the serial the relay expects: the one in the URL (``__IMDS_<serial>``).

    blinkpy sometimes has no (or a different) serial for doorbells/minis, and a
    wrong serial makes the relay drop the connection without sending video.
    """
    path = urllib.parse.urlparse(server).path
    if "__IMDS_" in path:
        found = path.split("__IMDS_", 1)[1].split("/")[0].split("?")[0]
        if found:
            return found
    return str(getattr(camera, "serial", "") or "")


def _latency_packet() -> bytes:
    return bytes([0x12, 0x00, 0x00, 0x03, 0xE8, 0x00, 0x00, 0x00, 0x18]) + b"\x00" * 24


def _find_pat(data: bytes) -> int:
    """Offset of the first TS packet that starts a PAT (PID 0, PUSI), or -1."""
    for off in range(0, len(data) - 3, TS_PACKET):
        if data[off] == 0x47 and data[off + 1] & 0x40 and (
            ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        ) == 0:
            return off
    return -1


class _Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(CLIENT_QUEUE_CHUNKS)
        self.synced = False
        self.peer = writer.get_extra_info("peername")
        # A still grab (camera snapshot from a running live view) gets the
        # video too, but doesn't keep the session alive or wake the camera.
        self.still = False

    def close(self) -> None:
        """Tell the writer loop to stop and close the socket."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        if not self.writer.is_closing():
            self.writer.close()

    def offer(self, data: bytes) -> bool:
        """Queue data for this client, return False if it is too slow."""
        if not self.synced:
            off = _find_pat(data)
            if off < 0:
                return True
            data = data[off:]
            self.synced = True
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            return False
        return True


# One live view per Blink network (sync module) at a time, like the app.
# The sync module can only do one job at once; overlapping sessions make it
# answer "system busy" to everyone, including the Blink app.
_ACTIVE_BY_NETWORK: dict[str, "BlinkLiveViewServer"] = {}
PREEMPT_COOLDOWN = 10  # s a preempted camera refuses to restart on its own
REST_AFTER_LIMIT = 60  # s a camera rests after hitting the max-minutes limit
FAIL_COOLDOWN = 120  # s a camera refuses to retry after a failed live view
# Still grabs (``async_camera_image`` while streaming) connect with this query.
# Dashboards fetch a still every few seconds; counted as viewers they would
# keep a live view running until the time limit after everyone had left.
STILL_QUERY = "still=1"


class BlinkLiveViewServer:
    """Local TCP server that turns a Blink live view into a plain MPEG-TS feed."""

    def __init__(
        self,
        blink,
        camera,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        linger: float = 5.0,
        max_minutes: float = 10.0,
        token_mode: str = TOKEN_MODE_AUTO,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialize."""
        self.blink = blink
        self.camera = camera
        self.host = host
        self.port = port
        self.linger = linger
        self.max_seconds = max_minutes * 60 if max_minutes else 0
        self.token_mode = token_mode
        self.on_change = on_change
        self.stats = LiveStats()
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[_Client] = set()
        self._session_task: asyncio.Task | None = None
        self._stop_requested = False
        self._null_token_works: bool | None = None
        self._blocked_until: float = 0.0
        self._relay_hung_up = False
        self.on_preempt: Callable[[], None] | None = None
        # Returns True while something else (clip archive) has the sync module busy
        self.sync_busy: Callable[[], bool] | None = None
        # Camera entity id, set by the entity; the talk endpoint looks it up.
        self.entity_id: str | None = None
        # Two-way talk: the IMMIS connection of the running session (only while
        # video is flowing), whether it was opened with the real token, and the
        # speaker state. One talker per camera at a time (talk_lock).
        self._immis_writer: asyncio.StreamWriter | None = None
        self._immis_has_token = False
        self._talk_ack = asyncio.Event()
        self._audio_seq = 0
        self.talk_lock = asyncio.Lock()

    # ------------------------------------------------------------------ server
    @property
    def name(self) -> str:
        """Camera name for logs."""
        return getattr(self.camera, "name", "?")

    @property
    def url(self) -> str:
        """Stream URL for HA / ffmpeg."""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "", "::") else self.host
        return f"http://{host}:{self.port}/stream.ts"

    @property
    def still_url(self) -> str:
        """URL for one-frame grabs; such clients never keep a session alive."""
        return f"{self.url}?{STILL_QUERY}"

    @property
    def active(self) -> bool:
        """Return True while a Blink session is running."""
        return self._session_task is not None and not self._session_task.done()

    async def async_start(self) -> None:
        """Start listening locally."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.debug("%s: live view server on %s", self.name, self.url)

    async def async_stop(self) -> None:
        """Stop everything."""
        await self.async_stop_session()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def async_stop_session(self) -> None:
        """Disconnect everyone and end the Blink session."""
        self._stop_requested = True
        for client in list(self._clients):
            client.close()
        if self._session_task:
            self._session_task.cancel()
            try:
                await self._session_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._session_task = None
        self._stop_requested = False

    @property
    def _viewers(self) -> list[_Client]:
        """Clients that keep the session alive (everyone but still grabs)."""
        return [c for c in self._clients if not c.still]

    def _changed(self) -> None:
        viewers = self._viewers
        self.stats.clients = len(viewers)
        self.stats.extra["cooldown"] = self.blocked_for
        self.stats.extra["viewers"] = sorted(
            {str(c.peer[0]) if c.peer else "?" for c in viewers}
        )
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("on_change failed", exc_info=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Serve plain HTTP (works best with go2rtc / ffmpeg / HomeKit), but keep
        # accepting raw TCP clients that just connect and read.
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1.0)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            head = b""
        request_line = head.split(b"\r\n", 1)[0]
        still = STILL_QUERY.encode() in request_line
        if still and not self.active:
            # Never wake a camera for a still; the caller uses the thumbnail.
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        if head.startswith((b"GET", b"HEAD")):
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
                b"Cache-Control: no-cache, no-store\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            if head.startswith(b"HEAD"):
                writer.close()
                return
        client = _Client(writer)
        client.still = still
        self._clients.add(client)
        _LOGGER.debug(
            "%s: %s %s connected", self.name, "still grab" if still else "client", client.peer
        )
        if not still:
            self._changed()
        if not self.active:
            if still:
                # The session ended while we answered: no new one for a still.
                self._clients.discard(client)
                client.close()
                return
            if time.monotonic() < self._blocked_until:
                # Cool-down (handed to another camera, hit the time limit, or the
                # last attempt failed): don't send Blink anything, so a player
                # that keeps reconnecting can't keep the sync module busy.
                _LOGGER.debug(
                    "%s: in cool-down for %.0fs, not starting",
                    self.name,
                    self._blocked_until - time.monotonic(),
                )
                self._clients.discard(client)
                client.close()
                self._changed()
                return
            self._session_task = asyncio.create_task(self._session_loop())

        async def _watch_close() -> None:
            # Clients never send anything useful. EOF means they left.
            try:
                while await reader.read(1024):
                    pass
            except (ConnectionError, OSError):
                pass
            client.close()

        watcher = asyncio.create_task(_watch_close())
        try:
            while True:
                data = await client.queue.get()
                if data is None:
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            watcher.cancel()
            self._clients.discard(client)
            if not writer.is_closing():
                writer.close()
            _LOGGER.debug(
                "%s: %s %s left", self.name, "still grab" if still else "client", client.peer
            )
            if not still:
                self._changed()

    def _broadcast(self, data: bytes) -> None:
        self.stats.bytes_streamed += len(data)
        for client in list(self._clients):
            if not client.offer(data):
                _LOGGER.warning("%s: dropping slow client %s", self.name, client.peer)
                self._clients.discard(client)
                client.close()

    # ----------------------------------------------------------------- session
    def _block(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    @property
    def blocked_for(self) -> int:
        """Seconds left in the current cool-down (0 = can start)."""
        return max(0, int(self._blocked_until - time.monotonic()))

    @property
    def network_key(self) -> str:
        """Blink network (sync module) this camera belongs to."""
        return str(getattr(self.camera, "network_id", "") or "")

    async def async_preempt(self) -> None:
        """Another camera on the same network needs the sync module."""
        _LOGGER.info("%s: stopping live view, another camera on this network started", self.name)
        self._block(PREEMPT_COOLDOWN)
        if self.on_preempt:
            try:
                self.on_preempt()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("on_preempt failed", exc_info=True)
        await self.async_stop_session()

    async def _claim_network(self) -> None:
        key = self.network_key
        other = _ACTIVE_BY_NETWORK.get(key)
        if other is not None and other is not self and other.active:
            await other.async_preempt()
            await asyncio.sleep(1)  # let the sync module close that session
        _ACTIVE_BY_NETWORK[key] = self

    def _release_network(self) -> None:
        if _ACTIVE_BY_NETWORK.get(self.network_key) is self:
            del _ACTIVE_BY_NETWORK[self.network_key]

    async def _session_loop(self) -> None:
        """Keep Blink sessions running while anyone is connected."""
        # Let a clip upload that's already running finish first (max 45 s),
        # instead of colliding with it and getting "system busy".
        waited = 0
        while self.sync_busy and self.sync_busy() and waited < 45 and self._viewers:
            if waited == 0:
                self.stats.state = "starting"
                self._changed()
                _LOGGER.debug("%s: waiting for clip archive to free the sync module", self.name)
            await asyncio.sleep(1)
            waited += 1
        await self._claim_network()
        intent = "liveview"
        started = time.monotonic()
        self.stats.started_at = time.time()
        try:
            while self._viewers and not self._stop_requested:
                if self.max_seconds and time.monotonic() - started > self.max_seconds:
                    _LOGGER.info(
                        "%s: live view hit the %d min limit, stopping",
                        self.name,
                        self.max_seconds // 60,
                    )
                    # Don't let a player that reconnects at once restart it.
                    self._block(REST_AFTER_LIMIT)
                    break
                self.stats.state = "starting"
                self._changed()
                try:
                    got_video = await self._run_one_session(intent)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    got_video = False
                    self.stats.last_error = str(err) or type(err).__name__
                    _LOGGER.warning("%s: live view session failed: %s", self.name, err)
                if got_video:
                    intent = "extended_liveview"
                else:
                    # Never retry on our own: every retry is a new command to
                    # the sync module. Stop, and cool down so reconnecting
                    # players can't start it again straight away.
                    self.stats.state = "error"
                    self._block(FAIL_COOLDOWN)
                    self._changed()
                    _LOGGER.warning(
                        "%s: live view failed (%s); not retrying for %ds",
                        self.name,
                        self.stats.last_error,
                        FAIL_COOLDOWN,
                    )
                    break
                if self._viewers:
                    await asyncio.sleep(0.5)
        finally:
            self._release_network()
            for client in list(self._clients):
                client.close()
            if self.stats.state != "error":
                self.stats.state = "idle"
            self._changed()

    async def _request_liveview(self, intent: str) -> dict[str, Any]:
        url = liveview_url(self.blink, self.camera)
        body = json.dumps({"intent": intent, "motion_event_start_time": None})
        resp = await blink_api.http_post(self.blink, url, data=body)
        _LOGGER.debug("%s: liveview response %s", self.name, _redact(resp))
        if not isinstance(resp, dict) or not resp.get("server"):
            raise LiveViewError(f"Blink refused live view: {_redact(resp)}")
        return resp

    def _tokens_to_try(self, token: str | None) -> list[str | None]:
        if self.token_mode == TOKEN_MODE_NULL or not token:
            return [None]
        if self.token_mode == TOKEN_MODE_TOKEN:
            return [token]
        if self._null_token_works:
            return [None, token]
        return [token, None]

    async def _run_one_session(self, intent: str) -> bool:
        """Run one Blink session. Return True if any video was streamed."""
        self.stats.last_error = None
        resp = await self._request_liveview(intent)
        server = resp["server"]
        self.stats.last_server = server.split("?")[0]
        self.stats.session_type = resp.get("type")
        self.stats.sessions += 1
        command_id = resp.get("command_id") or resp.get("id")
        poll_interval = max(int(resp.get("polling_interval") or 15), 2)
        network_id = resp.get("network_id") or self.camera.network_id

        if server.startswith(("rtsps://", "rtsp://")):
            try:
                got = await self._stream_rtsp(server, network_id, command_id, poll_interval)
                self.stats.token_used = None
                return got
            finally:
                await self._command_done(network_id, command_id)

        if not server.startswith("immis://"):
            await self._command_done(network_id, command_id)
            raise LiveViewError(
                f"Unsupported live view transport {server.split(':')[0]} "
                "(please report this with debug logs)"
            )

        got_video = False
        try:
            for token in self._tokens_to_try(resp.get("liveview_token")):
                got_video = await self._stream_from(
                    server, token, network_id, command_id, poll_interval
                )
                if got_video:
                    self.stats.token_used = token is not None
                    if token is None:
                        self._null_token_works = True
                    break
                if not self._viewers or not self._relay_hung_up:
                    # Relay kept the connection open but the camera sent
                    # nothing: the token was fine, the camera is the problem.
                    break
                _LOGGER.debug(
                    "%s: no video with %s token, trying the other mode",
                    self.name,
                    "real" if token else "null",
                )
        finally:
            await self._command_done(network_id, command_id)
        if not got_video and not self.stats.last_error:
            self.stats.last_error = (
                "camera sent no video (asleep, low battery or weak Wi-Fi?)"
            )
        return got_video

    async def _stream_from(
        self,
        server: str,
        token: str | None,
        network_id: Any,
        command_id: Any,
        poll_interval: int,
    ) -> bool:
        target = urllib.parse.urlparse(server)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Blink IMMIS servers use private certs
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.hostname, target.port or 443, ssl=ctx),
            timeout=15,
        )
        header = build_auth_header(serial_for(server, self.camera), server, token)
        if token:
            # Like the app: first keepalive right after authenticating. Talk
            # stays silent without it (zperkd/blink-plus, verified on a Mini).
            header += INITIAL_KEEPALIVE
        writer.write(header)
        await writer.drain()

        first_video = asyncio.Event()
        tasks = [
            asyncio.create_task(self._recv(reader, first_video)),
            asyncio.create_task(self._keepalive(writer)),
            asyncio.create_task(self._poll(network_id, command_id, poll_interval)),
            asyncio.create_task(self._watch_clients()),
        ]
        try:
            if not await _wait_first(first_video, tasks[0]):
                self._relay_hung_up = tasks[0].done()
                return False
            self.stats.state = "streaming"
            self.stats.last_error = None
            self._immis_writer = writer
            self._immis_has_token = token is not None
            self.stats.token_used = token is not None
            self._changed()
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return True
        finally:
            if self._immis_writer is writer:
                self._immis_writer = None
                self._set_talking(False)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _send_stop_and_close(writer, self.name)

    async def _stream_rtsp(
        self, server: str, network_id: Any, command_id: Any, poll_interval: int
    ) -> bool:
        """Older/other firmware hands out an rtsps:// URL: remux it with ffmpeg."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", server,
            "-c", "copy", "-f", "mpegts", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        first = asyncio.Event()

        async def pump() -> None:
            chunk = TS_PACKET * 64
            try:
                while True:
                    try:
                        data = await proc.stdout.readexactly(chunk)
                    except asyncio.IncompleteReadError as err:
                        data = err.partial[: len(err.partial) // TS_PACKET * TS_PACKET]
                        if data:
                            self._broadcast(data)
                        return
                    first.set()
                    self._broadcast(data)
            except (ConnectionError, OSError):
                return

        tasks = [
            asyncio.create_task(pump()),
            asyncio.create_task(self._poll(network_id, command_id, poll_interval)),
            asyncio.create_task(self._watch_clients()),
        ]
        try:
            if not await _wait_first(first, tasks[0]):
                return False
            self.stats.state = "streaming"
            self.stats.last_error = None
            self._changed()
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return True
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            if err and not first.is_set():
                self.stats.last_error = f"rtsps: {err[-200:]}"

    async def _recv(self, reader: asyncio.StreamReader, first: asyncio.Event) -> None:
        try:
            while True:
                header = await reader.readexactly(IMMI_HEADER)
                msgtype = header[0]
                length = int.from_bytes(header[5:9], "big")
                if msgtype == MSG_SPEAKER_ACK:
                    self._talk_ack.set()
                if length <= 0:
                    continue
                if length > MAX_FRAME:
                    # Out of sync or garbage: don't try to buffer it, end the session.
                    _LOGGER.warning(
                        "%s: IMMIS frame of %d bytes, stream out of sync, closing",
                        self.name, length,
                    )
                    return
                payload = await reader.readexactly(length)
                if msgtype != MSG_VIDEO or payload[0] != 0x47:
                    continue
                if not first.is_set():
                    first.set()
                self._broadcast(payload)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError) as err:
            _LOGGER.debug("%s: IMMIS connection closed: %s", self.name, err)

    # -------------------------------------------------------------------- talk
    @property
    def talk_ready(self) -> bool:
        """Return True while a live view with video (and the token) is open."""
        writer = self._immis_writer
        return (
            writer is not None
            and not writer.is_closing()
            and self._immis_has_token
            and self.stats.state == "streaming"
        )

    def talk_handle(self) -> asyncio.StreamWriter:
        """Return the IMMIS connection to talk on, or raise TalkError.

        The handle ties a talk to one Blink session: if the live view ends or
        is replaced (extended session, preemption), the talk stops instead of
        speaking into the new session.
        """
        writer = self._immis_writer
        if writer is None or writer.is_closing() or self.stats.state != "streaming":
            raise TalkError("Open the live view before talking")
        if not self._immis_has_token:
            raise TalkError(
                "This live view runs without the live view token; two-way talk "
                "needs it (live view token mode auto or token)"
            )
        return writer

    def _writer_for(self, handle: asyncio.StreamWriter) -> asyncio.StreamWriter:
        if self._immis_writer is not handle or handle.is_closing():
            raise TalkError("The live view ended")
        return handle

    def _set_talking(self, talking: bool) -> None:
        if self.stats.extra.get("talking", False) != talking:
            self.stats.extra["talking"] = talking
            self._changed()

    async def async_start_talk(self, handle: asyncio.StreamWriter) -> None:
        """Turn the camera speaker on (0x17/3) and wait for a fresh 0x18 ack."""
        writer = self._writer_for(handle)
        self._audio_seq = 0
        self._talk_ack.clear()
        writer.write(immi_packet(MSG_SPEAKER, SPEAKER_ON))
        await writer.drain()
        try:
            await asyncio.wait_for(self._talk_ack.wait(), TALK_ACK_TIMEOUT)
        except TimeoutError:
            raise TalkError("The camera did not acknowledge talk") from None
        self._writer_for(handle)
        self._set_talking(True)
        _LOGGER.debug("%s: speaker on", self.name)

    async def async_send_audio(self, handle: asyncio.StreamWriter, frame: bytes) -> None:
        """Send one ADTS AAC frame as an IMMI audio packet (0x05)."""
        writer = self._writer_for(handle)
        writer.write(immi_packet(MSG_AUDIO, self._audio_seq, frame))
        self._audio_seq = (self._audio_seq + 1) & 0xFFFFFFFF
        try:
            await writer.drain()
        except (ConnectionError, OSError, ssl.SSLError) as err:
            raise TalkError("The live view ended") from err

    async def async_stop_talk(self, handle: asyncio.StreamWriter) -> None:
        """Turn the camera speaker off (0x17/4), only on the session it was
        turned on in: an old talk must never touch a newer session."""
        self._set_talking(False)
        if self._immis_writer is not handle or handle.is_closing():
            return
        try:
            handle.write(immi_packet(MSG_SPEAKER, SPEAKER_OFF))
            await asyncio.wait_for(handle.drain(), 3)
            _LOGGER.debug("%s: speaker off", self.name)
        except (ConnectionError, OSError, TimeoutError, ssl.SSLError) as err:
            _LOGGER.debug("%s: could not turn the speaker off: %s", self.name, err)

    async def _keepalive(self, writer: asyncio.StreamWriter) -> None:
        seq = 0
        tick = 0
        latency = _latency_packet()
        while not writer.is_closing():
            if tick % 10 == 0:
                seq += 1
                writer.write(bytes([MSG_KEEPALIVE]) + seq.to_bytes(4, "big") + b"\x00" * 4)
            writer.write(latency)
            await writer.drain()
            tick += 1
            await asyncio.sleep(1)

    async def _poll(self, network_id: Any, command_id: Any, interval: int) -> None:
        """Return when Blink says the live view command is finished."""
        if not command_id:
            await asyncio.Event().wait()
        while True:
            await asyncio.sleep(interval)
            try:
                resp = await blink_api.request_command_status(
                    self.blink, network_id, command_id
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: command poll error %s", self.name, err)
                continue
            if not isinstance(resp, dict):
                continue
            if resp.get("status_code", 908) != 908:
                _LOGGER.debug("%s: command poll ended: %s", self.name, _redact(resp))
                return
            for cmd in resp.get("commands", []):
                if str(cmd.get("id")) == str(command_id):
                    if cmd.get("state_condition") not in ("new", "running"):
                        _LOGGER.debug("%s: session ended by Blink (%s)", self.name, cmd)
                        return
            if resp.get("complete") is True and not resp.get("commands"):
                return

    async def _watch_clients(self) -> None:
        """Return when nobody has been watching for `linger` seconds.

        Still grabs don't count: they only borrow a frame from a running view.
        """
        idle_since: float | None = None
        while True:
            await asyncio.sleep(1)
            if self._viewers:
                idle_since = None
                continue
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= self.linger:
                return

    async def _command_done(self, network_id: Any, command_id: Any) -> None:
        if not command_id:
            return
        # The app's endpoint (CommandApi .../commands/{id}/done), then blinkpy's
        # legacy one only if the first got no usable answer. After an IMMIS
        # STOP Blink has usually ended the command already and answers 404;
        # that counts as closed. We ask for the raw response (json=False)
        # because blinkpy turns a 404 into a bare ClientConnectionError.
        base = self.blink.urls.base_url
        endpoints = (
            (
                "command done (v1)",
                f"{base}/api/v1/accounts/{self.blink.account_id}"
                f"/networks/{network_id}/commands/{command_id}/done",
                "{}",
            ),
            ("command done", f"{base}/network/{network_id}/command/{command_id}/done/", None),
        )
        for what, url, data in endpoints:
            status = await self._best_effort(
                what, lambda url=url, data=data: self._post_status(url, data)
            )
            if status is not None and (status < 300 or status == 404):
                _LOGGER.debug(
                    "%s: %s -> %s%s",
                    self.name,
                    what,
                    status,
                    " (Blink had already ended it)" if status == 404 else "",
                )
                return
            if status is not None:
                _LOGGER.debug("%s: %s -> HTTP %s", self.name, what, status)
        _LOGGER.info(
            "%s: Blink did not answer when closing the live view command; "
            "it ends on its own after the session time limit",
            self.name,
        )

    async def _post_status(self, url: str, data: str | None) -> int | None:
        """POST and return the HTTP status (None: no response at all)."""
        resp = await blink_api.http_post(self.blink, url, data=data, json=False)
        if resp is None:
            return None
        status = getattr(resp, "status", None)
        release = getattr(resp, "release", None)
        if callable(release):
            result = release()
            if inspect.isawaitable(result):
                await result
        # Something that answered without a status (e.g. already parsed JSON)
        # still means Blink took the request.
        return 200 if status is None else int(status)

    async def _best_effort(
        self, what: str, call: Callable[[], Awaitable[Any]], attempts: int = 2
    ) -> Any:
        """Run a Blink request quietly; retry once if there was no answer at
        all (network error). Return None if it never answered."""
        for attempt in range(1, attempts + 1):
            token = _QUIET_BLINKPY.set(True)
            try:
                resp = await call()
            except Exception as err:  # noqa: BLE001
                resp = None
                _LOGGER.debug("%s: %s failed: %s", self.name, what, err)
            finally:
                _QUIET_BLINKPY.reset(token)
            if resp is not None:
                return resp
            _LOGGER.debug("%s: %s got no answer (attempt %d)", self.name, what, attempt)
            if attempt < attempts:
                await asyncio.sleep(1)
        return None


async def _send_stop_and_close(writer: asyncio.StreamWriter, name: str) -> None:
    """End the IMMIS session the way the app does, so the camera and sync module
    are released right away instead of when Blink's session times out."""
    try:
        if not writer.is_closing():
            writer.write(STOP_PACKET)
            await asyncio.wait_for(writer.drain(), 3)
            await asyncio.sleep(0.3)
            _LOGGER.debug("%s: sent IMMIS STOP", name)
    except (ConnectionError, OSError, TimeoutError, ssl.SSLError) as err:
        _LOGGER.debug("%s: could not send STOP: %s", name, err)
    finally:
        writer.close()


async def _wait_first(first: asyncio.Event, reader_task: asyncio.Task) -> bool:
    """Wait for the first video bytes, but give up at once if the relay hangs up."""
    waiter = asyncio.ensure_future(first.wait())
    try:
        await asyncio.wait(
            {waiter, reader_task},
            timeout=FIRST_DATA_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        waiter.cancel()
    return first.is_set()


def _redact(resp: Any) -> Any:
    if isinstance(resp, dict) and "liveview_token" in resp:
        return {**resp, "liveview_token": "***"}
    return resp
