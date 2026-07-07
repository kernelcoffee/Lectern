"""Live server stats (M5): psutil resource usage + Minecraft Server List Ping.

Pull-based only — everything here is computed on ``GET /servers/{id}/stats``
(driven by frontend polling); there is no background stats loop (see
docs/technical.md §5).

The Server List Ping follows the wiki.vg status protocol: varint-framed
handshake (next state = status) then a status request; the response is a JSON
document. MOTD ``description`` comes in several shapes in the wild — a plain
string, ``{"text": …}``, ``{"translate": …}``, or a ``{"extra": [...]}``
component tree (see docs/references/crafty-4.md) — ``motd_text`` flattens all
of them. The favicon is optional.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from dataclasses import dataclass, field

import psutil

_PING_TIMEOUT = 3.0  # seconds; local pings answer near-instantly
_MAX_STATUS_BYTES = 1 << 21  # 2 MiB guard against a garbage length prefix

# Formatting codes (§x) are stripped from flattened MOTD text.
_SECTION = "§"


# --- protocol helpers -------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for shift in range(5):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << (7 * shift)
        if not byte & 0x80:
            return result
    raise ValueError("varint too long")


def _handshake(host: str, port: int) -> bytes:
    """Handshake packet (id 0x00, next state 1 = status)."""
    host_bytes = host.encode()
    payload = (
        b"\x00"  # packet id
        + _encode_varint(0xFFFFFFFF)  # protocol version -1 (two's complement varint) = just pinging
        + _encode_varint(len(host_bytes))
        + host_bytes
        + struct.pack(">H", port)
        + b"\x01"  # next state: status
    )
    return _encode_varint(len(payload)) + payload


def motd_text(description: object) -> str:
    """Flatten any MOTD ``description`` shape to plain text."""
    if description is None:
        return ""
    if isinstance(description, str):
        text = description
    elif isinstance(description, list):
        text = "".join(motd_text(part) for part in description)
    elif isinstance(description, dict):
        text = str(description.get("text") or description.get("translate") or "")
        text += "".join(motd_text(part) for part in description.get("extra", ()))
    else:
        text = str(description)
    # Strip legacy §-formatting codes.
    while _SECTION in text:
        i = text.index(_SECTION)
        text = text[:i] + text[i + 2 :]
    return text


@dataclass
class PingResult:
    online: int = 0
    max: int = 0
    players: list[str] = field(default_factory=list)
    motd: str = ""
    version: str = ""
    favicon: str | None = None  # data: URI, straight from the server


def parse_status(payload: dict) -> PingResult:
    players = payload.get("players") or {}
    version = payload.get("version") or {}
    favicon = payload.get("favicon")
    return PingResult(
        online=int(players.get("online", 0)),
        max=int(players.get("max", 0)),
        players=[
            str(p.get("name", ""))
            for p in players.get("sample", ())
            if isinstance(p, dict)
        ],
        motd=motd_text(payload.get("description")),
        version=str(version.get("name", "")),
        favicon=favicon if isinstance(favicon, str) and favicon else None,
    )


async def server_list_ping(
    host: str, port: int, timeout: float = _PING_TIMEOUT
) -> PingResult | None:
    """Status-ping a Java server. Returns ``None`` when the server is not
    answering (still booting, port mismatch, …) — never raises."""
    try:
        return await asyncio.wait_for(_ping(host, port), timeout)
    except Exception:  # noqa: BLE001 — unreachable/garbled server is a normal outcome
        return None


async def _ping(host: str, port: int) -> PingResult:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(_handshake(host, port))
        writer.write(_encode_varint(1) + b"\x00")  # status request
        await writer.drain()

        length = await _read_varint(reader)
        if not 0 < length <= _MAX_STATUS_BYTES:
            raise ValueError(f"implausible status length {length}")
        packet_id = await _read_varint(reader)
        if packet_id != 0:
            raise ValueError(f"unexpected packet id {packet_id}")
        json_length = await _read_varint(reader)
        if not 0 < json_length <= _MAX_STATUS_BYTES:
            raise ValueError(f"implausible JSON length {json_length}")
        data = await reader.readexactly(json_length)
        return parse_status(json.loads(data))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# --- process resource usage --------------------------------------------------

# psutil computes cpu_percent against the *previous* call on the same Process
# object, so instances are cached per pid; a fresh object would always say 0.
_proc_cache: dict[int, psutil.Process] = {}


@dataclass
class ResourceUsage:
    cpu_percent: float
    memory_mb: float


def resource_usage(pid: int) -> ResourceUsage | None:
    """CPU/memory of the server process (children included — Java forks none,
    but modded launchers might). ``None`` if the process is gone."""
    try:
        proc = _proc_cache.get(pid)
        if proc is None or not proc.is_running():
            proc = psutil.Process(pid)
            _proc_cache.clear()  # one running server process is the norm; keep tidy
            _proc_cache[pid] = proc
        with proc.oneshot():
            cpu = proc.cpu_percent(interval=None)
            rss = proc.memory_info().rss
        for child in proc.children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                rss += child.memory_info().rss
        return ResourceUsage(cpu_percent=round(cpu, 1), memory_mb=round(rss / (1024 * 1024), 1))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
