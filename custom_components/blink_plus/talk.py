"""Two-way talk: check, pace and send ADTS AAC audio to an open live view.

No Home Assistant imports, so the offline tests can load it directly. The
IMMIS packets themselves are written by ``immis.BlinkLiveViewServer``.

The camera wants AAC-LC, 16 kHz, mono, one ADTS frame per IMMI audio packet
(validated by zperkd/blink-plus, MIT). ADTS splitting, the format check and
real-time pacing are adapted from its ``live_http.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

TALK_MAX_SECONDS = 60  # one talk (HTTP request or action) ends after this
MAX_ADTS = 8192  # largest ADTS frame we accept
SAMPLE_RATE = 16000
SAMPLES_PER_FRAME = 1024
FRAME_SECONDS = SAMPLES_PER_FRAME / SAMPLE_RATE  # 64 ms
MAX_LAG = 0.5  # s; if we fall further behind real time, restart the clock
MAX_UPLOAD = 8 * 1024 * 1024  # bytes of non-ADTS audio buffered for ffmpeg


class TalkBusyError(Exception):
    """Someone is already talking to this camera."""


class AudioFormatError(ValueError):
    """The audio is not AAC-LC, 16 kHz, mono ADTS."""


def is_adts(head: bytes) -> bool:
    """Return True if the data starts with an ADTS sync word (0xFFF).

    The talk endpoint uses this to tell ready ADTS audio (sent as is) from any
    other format (WebM/Opus from a browser, MP3, WAV ...), which goes through
    ffmpeg first.
    """
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0


def split_adts(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a buffer into complete ADTS frames.

    Return (frames, rest): ``rest`` is an incomplete last frame to prepend to
    the next chunk. Anything that isn't an ADTS frame raises AudioFormatError
    (no resync: a talk stream is ADTS from its first byte to its last).
    """
    frames: list[bytes] = []
    i = 0
    n = len(buf)
    while n - i >= 7:
        if buf[i] != 0xFF or buf[i + 1] & 0xF6 != 0xF0:
            raise AudioFormatError("Expected ADTS AAC audio (no sync word)")
        length = ((buf[i + 3] & 0x03) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
        header = 7 if buf[i + 1] & 0x01 else 9
        if length <= header or length > MAX_ADTS:
            raise AudioFormatError(f"Invalid ADTS frame length {length}")
        if i + length > n:
            break
        frames.append(bytes(buf[i : i + length]))
        i += length
    if n - i >= 2 and (buf[i] != 0xFF or buf[i + 1] & 0xF6 != 0xF0):
        raise AudioFormatError("Expected ADTS AAC audio (no sync word)")
    return frames, bytes(buf[i:])


def check_frame(frame: bytes) -> None:
    """Raise AudioFormatError unless this is one AAC-LC, 16 kHz, mono frame."""
    profile = frame[2] >> 6  # audio object type - 1
    rate_index = (frame[2] >> 2) & 0x0F
    channels = ((frame[2] & 0x01) << 2) | (frame[3] >> 6)
    blocks = frame[6] & 0x03  # raw data blocks in frame - 1
    if profile != 1 or rate_index != 8 or channels != 1 or blocks != 0:
        raise AudioFormatError("Expected AAC-LC, 16 kHz, mono ADTS audio")


async def async_talk(
    server: Any,
    chunks: AsyncIterator[bytes],
    *,
    max_seconds: float = TALK_MAX_SECONDS,
) -> int:
    """Speak ADTS audio from ``chunks`` through the camera's open live view.

    ``server`` is the camera's ``BlinkLiveViewServer``. Return the number of
    frames sent. Raises TalkBusyError, AudioFormatError, TimeoutError (talk
    longer than ``max_seconds``) or ``immis.TalkError`` (no live view, no ack,
    live view ended). The speaker is always turned off again.
    """
    if server.talk_lock.locked():
        raise TalkBusyError("Someone is already talking to this camera")
    async with server.talk_lock:
        handle = server.talk_handle()
        loop = asyncio.get_running_loop()
        sent = 0
        buf = b""
        clock: float | None = None
        try:
            async with asyncio.timeout(max_seconds):
                await server.async_start_talk(handle)
                async for chunk in chunks:
                    frames, buf = split_adts(buf + chunk)
                    for frame in frames:
                        check_frame(frame)
                        # Real-time pacing: the camera plays what it gets, it
                        # has no jitter buffer worth mentioning.
                        now = loop.time()
                        if clock is None or clock < now - MAX_LAG:
                            clock = now
                        elif clock > now:
                            await asyncio.sleep(clock - now)
                        await server.async_send_audio(handle, frame)
                        sent += 1
                        clock += FRAME_SECONDS
                if buf or not sent:
                    raise AudioFormatError("Empty or incomplete ADTS audio")
                # Let the last frames play before the speaker goes off.
                if clock is not None and clock > loop.time():
                    await asyncio.sleep(clock - loop.time())
        finally:
            await server.async_stop_talk(handle)
        _LOGGER.debug("talk: sent %d frames (%.1f s)", sent, sent * FRAME_SECONDS)
        return sent


async def ffmpeg_adts(
    source: str,
    *,
    ffmpeg: str = "ffmpeg",
    max_seconds: float = TALK_MAX_SECONDS,
    chunk_size: int = 4096,
) -> AsyncIterator[bytes]:
    """Transcode an audio file to AAC-LC, 16 kHz, mono ADTS with ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", source, "-t", str(max_seconds), "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "aac", "-profile:a", "aac_low",
        "-b:a", "32k", "-f", "adts", "pipe:1",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    got = False
    try:
        while chunk := await proc.stdout.read(chunk_size):
            got = True
            yield chunk
        await proc.wait()
        if proc.returncode or not got:
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            raise AudioFormatError(
                f"ffmpeg could not read the audio: {err[-200:] or proc.returncode}"
            )
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
