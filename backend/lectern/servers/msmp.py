"""Minecraft Server Management Protocol (MSMP) client and enablement.

MSMP is Mojang's management API for the Java dedicated server: JSON-RPC 2.0
over a WebSocket, built into the server since 1.21.9 (25w35a). Lectern uses it
as the primary source for the online roster and the ``running``/``stopping``
transitions, and as the kick channel; console parsing stays the fallback.

Facts this module relies on (probed on a Fabric 26.3 server):

* Enablement is ``server.properties``: ``management-server-enabled``,
  ``-host``, ``-port`` (0 = random), ``-secret`` (40 alphanumerics; generated
  and written back by the server when empty), ``-tls-enabled`` (default true).
  Lectern writes the five keys in ``MANAGED_KEYS`` at every start
  (:func:`enable`): localhost only, TLS off, a port of its choosing and a
  secret it generates once and then keeps.
* Connect to ``ws://127.0.0.1:<port>`` with ``Authorization: Bearer <secret>``.
  Params are **positional JSON arrays** (``[[{"name": "Steve"}]]`` for kick); an
  object is rejected with -32602.
* ``rpc.discover`` returns the schema (``result.info.version``);
  ``minecraft:players`` (no trailing slash) lists ``[{id, name}]``;
  ``minecraft:players/kick``; ``minecraft:server/status`` returns
  ``{started, version}``.
* Notifications arrive unsolicited on the same socket (no ``id``):
  ``minecraft:notification/players/joined`` and ``/left`` with
  ``params: [{id, name}]`` (Carpet bots included, with an offline UUID), and
  ``minecraft:notification/server/started``, ``/stopping``, ``/saving``,
  ``/saved``, ``/status`` (heartbeat), ``/activity``.
* Versions: introduced 1.21.9; auth + TLS default since 25w37a; schema 1.1.0
  (25w41a), 2.0.0 (25w44a), 3.0.0 (26.2: the management server starts *before*
  the game server), 3.1.0 (26.3). On 1.21.9–26.1 the port opens only once the
  game is up, so the client retries patiently and without a cap. Java Edition
  servers only; proxies (Velocity) have none — the manager checks
  ``is_proxy_type`` before :func:`supports_msmp`.

Fallback order: a connected client is authoritative for the roster and drives
``running``/``stopping``; console parsing (``roster.py``, the ``Done (``
marker) covers older versions, proxies and any time the socket is down. The
client never raises into the process lifecycle and logs at debug/info only;
the secret is never logged.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import secrets
import socket
import string
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .properties import properties_file, read_properties, write_properties
from .types import is_proxy_type

log = logging.getLogger(__name__)
# websockets logs every handshake header (the Bearer secret included) at DEBUG;
# its connection logger is pinned to INFO so no log configuration leaks it.
_wire_log = logging.getLogger(f"{__name__}.wire")
_wire_log.setLevel(logging.INFO)

MSMP_MIN_VERSION = (1, 21, 9)
MSMP_PORT_BASE = 25585
_PORT_SPAN = 200
_SECRET_LENGTH = 40
_BACKOFF_START = 0.5
_BACKOFF_MAX = 5.0

M_DISCOVER = "rpc.discover"
M_PLAYERS = "minecraft:players"  # no trailing slash: "minecraft:players/" is -32601
M_KICK = "minecraft:players/kick"
M_STATUS = "minecraft:server/status"
N_JOINED = "minecraft:notification/players/joined"
N_LEFT = "minecraft:notification/players/left"
N_STARTED = "minecraft:notification/server/started"
N_STOPPING = "minecraft:notification/server/stopping"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
# Everything the reconnect loop treats as "the socket is gone, try again".
_RETRYABLE = (OSError, WebSocketException, TimeoutError, json.JSONDecodeError)


# --- version gate ------------------------------------------------------------


def parse_mc_version(text: str) -> tuple[int, int, int] | None:
    """``1.21.9`` -> (1, 21, 9), ``26.1`` -> (26, 1, 0); snapshot ids and other
    shapes -> None. Tuple order covers both numbering schemes: (26, 1) sorts
    after (1, 21, 11)."""
    m = _VERSION_RE.match(text)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def supports_msmp(mc_version: str) -> bool:
    """True for Minecraft versions that ship the management server (1.21.9+).
    Callers exclude proxies first: Velocity stores its own version here."""
    parsed = parse_mc_version(mc_version)
    return parsed is not None and parsed >= MSMP_MIN_VERSION


def manages(server_type: str, mc_version: str) -> bool:
    """True when Lectern runs the management server for this server: a Java
    (non-proxy) type on 1.21.9+. Only then are the managed keys Lectern's."""
    return not is_proxy_type(server_type) and supports_msmp(mc_version)


# --- enablement ----------------------------------------------------------------


def generate_secret() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(_SECRET_LENGTH))


def _port_free(port: int) -> bool:
    # SO_REUSEADDR matches how the JVM binds its listener on Linux: a port left
    # in TIME_WAIT by the previous run's closed connection counts as free (so a
    # quick restart keeps its port), while a socket another process has bound
    # or is listening on still fails the bind.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_management_port(preferred: int | None, reserved: set[int]) -> int:
    """The previous port when still usable, else the first free localhost port
    in ``25585..25784`` outside ``reserved`` (game ports and other servers'
    management ports)."""
    candidates: list[int] = []
    if preferred is not None and 1 <= preferred <= 65535 and preferred not in reserved:
        candidates.append(preferred)
    candidates.extend(
        p for p in range(MSMP_PORT_BASE, MSMP_PORT_BASE + _PORT_SPAN) if p not in reserved
    )
    for port in candidates:
        if _port_free(port):
            return port
    raise RuntimeError("no free management port")


@dataclass(frozen=True)
class Endpoint:
    port: int
    secret: str


def enable(server_dir: Path, *, game_port: int, reserved: set[int]) -> Endpoint:
    """Write the managed keys into ``server.properties`` and return where the
    management server will listen. An existing non-empty secret is kept; every
    other key in the file is preserved. Raises ``PropertiesPathError`` (an
    ``OSError``) when ``server.properties`` is a symlink leading out of the
    server directory: nothing is read or written then."""
    properties_file(server_dir)
    props = read_properties(server_dir)
    secret = props.get("management-server-secret") or generate_secret()
    existing = props.get("management-server-port", "")
    # ASCII only: str.isdigit() also accepts "²", which int() refuses.
    preferred = int(existing) if existing.isascii() and existing.isdigit() else None
    port = pick_management_port(preferred, reserved | {game_port})
    props["management-server-enabled"] = "true"
    props["management-server-host"] = "127.0.0.1"
    props["management-server-port"] = str(port)
    props["management-server-tls-enabled"] = "false"
    props["management-server-secret"] = secret
    write_properties(server_dir, props)
    return Endpoint(port=port, secret=secret)


# --- client --------------------------------------------------------------------


class MsmpError(Exception):
    """A failed call: JSON-RPC error (``code`` set), timeout, or no connection."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MsmpClient:
    """One management connection per running server process.

    ``start()`` spawns a background task that connects (retrying with backoff
    for as long as the client lives), authenticates, reads ``rpc.discover``
    once per connection, then runs ``on_connect`` and dispatches notifications
    to ``on_notification``. Both callbacks run on the reader's side of the
    socket: ``on_connect`` may ``call()``, ``on_notification`` must not (the
    reader is what resolves calls)."""

    def __init__(self, server_id: str, port: int, secret: str) -> None:
        self.server_id = server_id
        self.port = port
        self.secret = secret
        self.connected = False
        self.schema_version: str | None = None
        self.on_connect: Callable[[], Coroutine[Any, Any, None]] | None = None
        self.on_notification: Callable[[str, list], Coroutine[Any, Any, None]] | None = None
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}

    async def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run())

    def abort(self) -> None:
        """The synchronous half of ``close()``: stop reconnecting and cancel
        the connection task without yielding, so the process can report its
        final state before the teardown finishes. Idempotent."""
        self._closed = True
        self.connected = False
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def close(self) -> None:
        """Stop reconnecting and drop the socket, waiting for the connection
        task to wind down. Idempotent; never raises."""
        self.abort()
        task, self._task = self._task, None
        if task is not None:
            await asyncio.wait([task])
        self._fail_pending("closed")

    async def call(self, method: str, params: list | None = None, timeout: float = 5.0) -> Any:
        ws = self._ws
        if not self.connected or ws is None:
            raise MsmpError("not connected")
        return await self._request(ws, method, params or [], timeout)

    # --- internals -------------------------------------------------------------

    async def _request(
        self, ws: ClientConnection, method: str, params: list, timeout: float
    ) -> Any:
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except ConnectionClosed:
            raise MsmpError("disconnected") from None
        except TimeoutError:
            raise MsmpError("timed out") from None
        finally:
            self._pending.pop(request_id, None)

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(MsmpError(reason))

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        failures = 0
        while not self._closed:
            try:
                async with connect(
                    f"ws://127.0.0.1:{self.port}",
                    additional_headers={"Authorization": f"Bearer {self.secret}"},
                    open_timeout=5,
                    ping_interval=None,
                    # Always loopback: never route the handshake (and its
                    # Bearer secret) through an HTTP(S)_PROXY from the env.
                    proxy=None,
                    logger=_wire_log,
                ) as ws:
                    log.info("msmp %s: connected on port %d", self.server_id, self.port)
                    backoff = _BACKOFF_START
                    failures = 0
                    await self._session(ws)
            except _RETRYABLE as exc:
                self._log_failure(exc, failures)
                failures += 1
            self.connected = False
            self._ws = None
            self._fail_pending("disconnected")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _log_failure(self, exc: BaseException, failures: int) -> None:
        if isinstance(exc, InvalidStatus):
            if failures == 0:
                log.info(
                    "msmp %s: handshake refused (HTTP %d)",
                    self.server_id, exc.response.status_code,
                )
            return
        if failures == 0:
            log.info("msmp %s: connection failed (%s)", self.server_id, type(exc).__name__)
        else:
            log.debug("msmp %s: retry %d failed: %s", self.server_id, failures, exc)

    async def _session(self, ws: ClientConnection) -> None:
        self._ws = ws
        reader = asyncio.create_task(self._reader(ws))
        try:
            await self._discover(ws)
            if not reader.done():
                self.connected = True
                if self.on_connect is not None:
                    await self._guard(self.on_connect())
            await reader
        finally:
            self.connected = False
            reader.cancel()
            # Let the cancellation finish (and the reader's own cleanup run)
            # before the session returns; never leave the task dangling.
            await asyncio.wait([reader])

    async def _discover(self, ws: ClientConnection) -> None:
        try:
            result = await self._request(ws, M_DISCOVER, [], 5.0)
        except MsmpError as exc:
            log.info("msmp %s: rpc.discover failed: %s", self.server_id, exc)
            return
        info = result.get("info") if isinstance(result, dict) else None
        version = info.get("version") if isinstance(info, dict) else None
        self.schema_version = version if isinstance(version, str) else None
        log.info("msmp %s: schema %s", self.server_id, self.schema_version)

    async def _guard(self, callback: Coroutine[Any, Any, None]) -> None:
        """Run a callback to completion in its own task so whatever it raises
        (a failed call, or e.g. a database error from the status writer) is
        logged here and never tears down the connection loop. Awaited in line,
        so notifications are still applied in order."""
        task = asyncio.create_task(callback)
        try:
            await asyncio.wait([task])
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait([task])
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, (MsmpError, ConnectionClosed)):
            log.info("msmp %s: callback failed: %s", self.server_id, exc)
        elif exc is not None:
            log.info(
                "msmp %s: callback raised %s", self.server_id, type(exc).__name__, exc_info=exc
            )

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                if isinstance(msg.get("id"), int):
                    self._resolve(msg)
                elif "method" in msg and self.on_notification is not None:
                    params = msg.get("params")
                    await self._guard(
                        self.on_notification(
                            msg["method"], params if isinstance(params, list) else []
                        )
                    )
        finally:
            self._fail_pending("disconnected")

    def _resolve(self, msg: dict) -> None:
        future = self._pending.pop(msg["id"], None)
        if future is None or future.done():
            return
        error = msg.get("error")
        if isinstance(error, dict):
            future.set_exception(MsmpError(str(error.get("message")), error.get("code")))
        else:
            future.set_result(msg.get("result"))
