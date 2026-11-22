"""Host and Origin check (``lectern/request_guard.py``): the policy functions,
then the middleware on the real app (HTTP, the static web UI, the three
WebSockets), the allow list, and the refusal log."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from lectern import config
from lectern import main as main_module
from lectern.db import get_session
from lectern.main import app as main_app
from lectern.main import create_app
from lectern.request_guard import (
    REFUSAL_LOG_MEMORY,
    AllowPolicy,
    RequestGuard,
    evaluate,
    host_allowed,
    is_lan_safe,
    origin_allowed,
    parse_allowed_hosts,
    parse_host,
    parse_origin,
)
from lectern.scheduler import SchedulerService
from lectern.servers.manager import manager
from lectern.servers.stats_sampler import StatsSampler

DEFAULT = AllowPolicy()
EVIL_HOST = "lectern.evil.example"
EVIL_ORIGIN = "https://evil.example"
SOCKETS = ("/ws/status", "/ws/servers/guarded/console", "/ws/servers/guarded/install")


# --- policy functions --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.1.10", "192.168.1.10"),
        ("192.168.1.10:8000", "192.168.1.10"),
        ("[fd00::10]", "fd00::10"),
        ("[FD00:0::10]:8000", "fd00::10"),
        ("[::1]:8000", "::1"),
        ("localhost", "localhost"),
        ("LOCALHOST:8000", "localhost"),
        ("Pi:8000", "pi"),
        ("PI.Local.:8000", "pi.local"),
        ("  pi.lan  ", "pi.lan"),
        ("lectern_backend:8000", "lectern_backend"),
    ],
)
def test_parse_host_normalises(value, expected):
    assert parse_host(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "[::1", "[::1]x", "[::1]:", "[not-ip]:80", "fd00::10", "pi:", "pi:port",
     "pi:123456", "a..b", "pi.local..", "exa mple", ":8000", "pi/x", "é.local"],
)
def test_parse_host_rejects_garbage(value):
    assert parse_host(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://pi.local:8000", "pi.local"),
        ("HTTPS://PI.LOCAL", "pi.local"),
        ("http://[fd00::10]:8000", "fd00::10"),
        ("http://192.168.1.20:5173", "192.168.1.20"),
    ],
)
def test_parse_origin_normalises(value, expected):
    assert parse_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    ["null", "", "not a url", "pi.local", "http://", "http://pi.local/path",
     "http://user@pi.local", "chrome-extension://abcdef", "file://pi", "ws://pi.local",
     "http://[::1"],
)
def test_parse_origin_rejects_garbage(value):
    assert parse_origin(value) is None


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("localhost", True), ("pi", True), ("pi.local", True), ("nas.lan", True),
        ("box.home.arpa", True), ("srv.internal", True),
        ("lectern.evil.example", False), ("mc.example.org", False),
        ("local.evil.com", False), ("pilocal", True), ("evil.notlan", False),
        # An IP address is never LAN-safe by name, though IPv6 has no dot.
        ("2001:db8::1", False), ("192.168.1.10", False),
    ],
)
def test_is_lan_safe(name, safe):
    assert is_lan_safe(name) is safe


@pytest.mark.parametrize(
    "host",
    ["192.168.1.10", "192.168.1.10:8000", "[fd00::10]", "[fd00::10]:8000", "LOCALHOST",
     "localhost:8000", "pi", "PI:8000", "pi.local", "Pi.Local:8000", "nas.lan",
     "box.home.arpa:8000", "srv.INTERNAL", "203.0.113.7:8000", "testserver"],
)
def test_host_rule_accepts_lan_forms_without_configuration(host):
    assert evaluate([host], [], DEFAULT) is None


@pytest.mark.parametrize(
    "host", [EVIL_HOST, "mc.example.org", "mc.example.org:443", "[::1", "fd00::10", ""]
)
def test_host_rule_refuses_public_names_and_garbage(host):
    refusal = evaluate([host], [], DEFAULT)
    assert refusal is not None
    assert refusal.header == "Host"
    assert refusal.value == host


def test_host_rule_refuses_a_missing_or_repeated_host():
    missing = evaluate([], [], DEFAULT)
    assert missing is not None
    assert (missing.header, missing.value) == ("Host", None)
    assert "LECTERN_ALLOWED_HOSTS" in missing.message
    repeated = evaluate(["pi", "lectern.evil.example"], [], DEFAULT)
    assert repeated is not None
    assert repeated.header == "Host"


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://pi.local:8000", "pi.local:8000"),
        ("http://localhost:5173", "localhost:8000"),  # Vite proxy
        ("http://192.168.1.20:5173", "backend:8000"),  # compose dev, from a phone
        ("http://127.0.0.1:5173", "localhost:8000"),
        ("http://[::1]:5173", "localhost:8000"),
        ("http://10.1.2.3", "pi"),
        ("http://172.31.0.1", "pi"),
        ("http://169.254.3.4", "pi"),
        ("http://[fe80::1]", "pi"),
        ("http://[fd00::20]", "pi"),
        ("http://[::ffff:192.168.1.5]", "pi"),
        ("http://nas.lan", "pi"),
        # A public IP is fine as Origin when it is the request's own host.
        ("https://203.0.113.7", "203.0.113.7:443"),
    ],
)
def test_origin_rule_accepts_the_lan(origin, host):
    assert evaluate([host], [origin], DEFAULT) is None


@pytest.mark.parametrize(
    "origin",
    [
        EVIL_ORIGIN,
        "http://203.0.113.7",  # documentation range: ip.is_private says True
        "http://192.0.2.1",
        "http://100.64.0.1",
        "http://172.32.0.1",
        "http://8.8.8.8",
        "http://[2001:db8::1]",
        "http://[::ffff:203.0.113.7]",
        "null",
        "",
        "not a url",
        "chrome-extension://abcdef",
    ],
)
def test_origin_rule_refuses_outside_pages(origin):
    refusal = evaluate(["192.168.1.10:8000"], [origin], DEFAULT)
    assert refusal is not None
    assert (refusal.header, refusal.value) == ("Origin", origin)


def test_origin_rule_refuses_a_repeated_origin():
    refusal = evaluate(["pi"], ["http://pi", "http://pi"], DEFAULT)
    assert refusal is not None
    assert refusal.header == "Origin"


def test_parse_allowed_hosts_normalises_entries():
    policy = parse_allowed_hosts(" MC.Example.org , other.example.com:443,, [FD00::1] ")
    assert policy.listed == frozenset({"mc.example.org", "other.example.com", "fd00::1"})
    assert policy.disabled is False
    assert policy.rejected == ()


def test_parse_allowed_hosts_accepts_ip_addresses_with_or_without_brackets():
    policy = parse_allowed_hosts("2A01:DB8::5, [2a01:db8::6]:8000, 198.51.100.4, 2a01::5::6")
    assert policy.listed == frozenset({"2a01:db8::5", "2a01:db8::6", "198.51.100.4"})
    assert policy.rejected == ("2a01::5::6",)
    # A listed public address is accepted as a foreign Origin; unlisted it is not.
    assert evaluate(["192.168.1.10"], ["http://[2a01:db8::5]:5173"], policy) is None
    assert evaluate(["192.168.1.10"], ["http://[2a01:db8::7]:5173"], policy) is not None


def test_public_ipv4_origin_passes_only_when_listed():
    # Any IP address is a valid Host without an entry, but a public one as a
    # foreign Origin needs the list (the rule config.allowed_hosts documents).
    assert host_allowed("203.0.113.7", DEFAULT)
    assert evaluate(["192.168.1.10:8000"], ["http://203.0.113.7"], DEFAULT) is not None
    policy = parse_allowed_hosts("203.0.113.7")
    assert evaluate(["192.168.1.10:8000"], ["http://203.0.113.7"], policy) is None


def test_parse_allowed_hosts_keeps_bad_entries_out():
    policy = parse_allowed_hosts("mc.example.org, not a host, *.example.com")
    assert policy.listed == frozenset({"mc.example.org"})
    assert policy.rejected == ("not a host", "*.example.com")


@pytest.mark.parametrize("raw", ["*", " * ", "mc.example.org, *"])
def test_star_anywhere_disables_both_checks(raw):
    policy = parse_allowed_hosts(raw)
    assert policy.disabled is True
    assert evaluate([EVIL_HOST], [EVIL_ORIGIN], policy) is None
    assert evaluate([], ["null"], policy) is None


def test_listed_name_is_accepted_as_host_and_origin():
    policy = parse_allowed_hosts("mc.example.org")
    assert host_allowed("mc.example.org", policy)
    assert not host_allowed("mc.example.org", DEFAULT)
    assert origin_allowed("mc.example.org", "192.168.1.10", policy)
    assert not origin_allowed("mc.example.org", "192.168.1.10", DEFAULT)
    assert not host_allowed("other.example.org", policy)


# --- the middleware on the real app ----------------------------------------


@pytest.fixture(name="make_client")
def make_client_fixture(engine, monkeypatch) -> Iterator[Callable[..., TestClient]]:
    """A client on a fresh ``create_app()`` built under the given environment."""

    def override_get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    def build(**env: str) -> TestClient:
        # As shipped unless the test says otherwise, whatever the developer exported.
        env.setdefault("LECTERN_ALLOWED_HOSTS", "")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        config.get_settings.cache_clear()
        app = create_app()
        app.dependency_overrides[get_session] = override_get_session
        return TestClient(app)

    yield build
    config.get_settings.cache_clear()


def _create_server(client: TestClient, name: str = "Guarded") -> str:
    resp = client.post(
        "/api/servers", json={"name": name, "type": "vanilla", "mc_version": "1.21"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _assert_refused(resp, value: str) -> None:
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert value in detail
    assert "LECTERN_ALLOWED_HOSTS" in detail


@pytest.mark.parametrize("path", ["/api/servers", "/api/health"])
def test_refused_host_gets_403_on_the_api(client, path):
    assert client.get(path).status_code == 200
    _assert_refused(client.get(path, headers={"Host": EVIL_HOST}), EVIL_HOST)


def test_refused_host_gets_403_on_the_web_ui(make_client, tmp_path: Path):
    (tmp_path / "index.html").write_text("<h1>Lectern</h1>")
    client = make_client(LECTERN_STATIC_DIR=str(tmp_path))
    assert client.get("/", headers={"Host": "pi.local:8000"}).text == "<h1>Lectern</h1>"
    _assert_refused(client.get("/", headers={"Host": EVIL_HOST}), EVIL_HOST)
    _assert_refused(client.get("/index.html", headers={"Host": EVIL_HOST}), EVIL_HOST)


def test_unlisted_reverse_proxy_name_names_the_variable(client):
    _assert_refused(client.get("/api/servers", headers={"Host": "mc.example.org"}),
                    "mc.example.org")


def test_cross_site_post_creates_nothing(client):
    resp = client.post(
        "/api/servers",
        json={"name": "Planted", "type": "vanilla", "mc_version": "1.21"},
        headers={"Host": "192.168.1.10:8000", "Origin": EVIL_ORIGIN},
    )
    _assert_refused(resp, EVIL_ORIGIN)
    assert client.get("/api/servers").json() == []


def test_cross_site_patch_changes_nothing(client):
    server_id = _create_server(client)
    resp = client.patch(
        f"/api/servers/{server_id}", json={"name": "Renamed"}, headers={"Origin": EVIL_ORIGIN}
    )
    _assert_refused(resp, EVIL_ORIGIN)
    assert client.get(f"/api/servers/{server_id}").json()["name"] == "Guarded"
    # The same request from the LAN page goes through: the 403 was the guard.
    resp = client.patch(
        f"/api/servers/{server_id}", json={"name": "Renamed"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200, resp.text
    assert client.get(f"/api/servers/{server_id}").json()["name"] == "Renamed"


def test_cross_site_delete_deletes_nothing(client):
    server_id = _create_server(client)
    for origin in (EVIL_ORIGIN, "http://203.0.113.7", "null"):
        resp = client.delete(f"/api/servers/{server_id}", headers={"Origin": origin})
        _assert_refused(resp, origin)
    assert client.get(f"/api/servers/{server_id}").status_code == 200


def test_cross_site_get_is_refused(client):
    _create_server(client)
    _assert_refused(client.get("/api/servers", headers={"Origin": EVIL_ORIGIN}), EVIL_ORIGIN)


def test_lan_requests_pass(client):
    server_id = _create_server(client)
    cases = [
        {"Host": "192.168.1.10:8000"},  # script or curl: no Origin
        {"Host": "pi.local:8000", "Origin": "http://pi.local:8000"},
        {"Host": "[fd00::10]:8000"},
        {"Host": "localhost:8000", "Origin": "http://localhost:5173"},
        {"Host": "backend:8000", "Origin": "http://192.168.1.20:5173"},
    ]
    for headers in cases:
        resp = client.get(f"/api/servers/{server_id}", headers=headers)
        assert resp.status_code == 200, (headers, resp.text)


def test_cors_preflight_from_vite_still_answered(client):
    resp = client.options(
        "/api/servers",
        headers={
            "Host": "localhost:8000",
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_listed_name_passes_and_letter_case_and_spaces_are_ignored(make_client):
    client = make_client(LECTERN_ALLOWED_HOSTS="  MC.Example.ORG , other.example.net ")
    ok = {"Host": "mc.example.org", "Origin": "https://Mc.Example.Org"}
    assert client.get("/api/health", headers=ok).status_code == 200
    as_origin = {"Host": "192.168.1.10:8000", "Origin": "https://mc.example.org"}
    assert client.get("/api/health", headers=as_origin).status_code == 200
    _assert_refused(client.get("/api/health", headers={"Host": "third.example.org"}),
                    "third.example.org")


def test_suite_runs_with_the_checks_as_shipped(make_client):
    """conftest pins the variable, so a developer's own LECTERN_ALLOWED_HOSTS
    (exported or in .env) changes neither the shared app nor a fresh one."""
    assert config.get_settings().allowed_hosts == ""
    assert main_app.state.request_guard_policy == AllowPolicy()
    assert make_client().app.state.request_guard_policy == AllowPolicy()


class _NoServers:
    """Stands in for the server manager while the lifespan runs: no server
    is reconciled, auto-started or stopped, and the shared manager is not
    left shutting down for the tests that follow."""

    def reconcile(self) -> None:
        return None

    def autostart(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


@pytest.fixture(name="quiet_startup")
def quiet_startup_fixture(monkeypatch) -> None:
    """Let a test run the app's lifespan (``with client:``) without touching
    the shared manager, scheduler or sampler."""
    monkeypatch.setattr(main_module, "manager", _NoServers())
    monkeypatch.setattr(main_module, "scheduler_service", SchedulerService())
    monkeypatch.setattr(main_module, "stats_sampler", StatsSampler())


def _startup_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "lectern.main" and r.levelno == logging.WARNING]


def test_star_switches_the_checks_off_and_warns_at_startup(make_client, quiet_startup, caplog):
    with caplog.at_level(logging.WARNING, logger="lectern.main"):
        client = make_client(LECTERN_ALLOWED_HOSTS="*")
        # Building the app (importing lectern.main) says nothing; startup does.
        assert _startup_warnings(caplog) == []
        with client:
            warnings = _startup_warnings(caplog)
            assert len(warnings) == 1
            assert "LECTERN_ALLOWED_HOSTS" in warnings[0].getMessage()
            assert "off" in warnings[0].getMessage()
            headers = {"Host": EVIL_HOST, "Origin": EVIL_ORIGIN}
            assert client.get("/api/health", headers=headers).status_code == 200
            with client.websocket_connect("/ws/status", headers=headers) as ws:
                assert ws.receive_json()["type"] == "snapshot"


def test_off_switch_warning_reads_like_lectern_s_other_lines(
    make_client, quiet_startup, monkeypatch, capsys
):
    """Under plain uvicorn (no logging config) the warning reaches stderr with
    the ``WARNING:`` prefix and logger name of Lectern's other lines, so a grep
    of the container log finds it."""
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    lectern_logger = logging.getLogger("lectern")
    monkeypatch.setattr(lectern_logger, "handlers", [])
    monkeypatch.setattr(lectern_logger, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "level", logging.NOTSET)
    client = make_client(LECTERN_ALLOWED_HOSTS="*")
    assert capsys.readouterr().err == ""
    with client:
        pass
    lines = [line for line in capsys.readouterr().err.splitlines() if "contains '*'" in line]
    assert len(lines) == 1
    assert lines[0].startswith("WARNING:")
    assert "lectern.main: LECTERN_ALLOWED_HOSTS contains '*'" in lines[0]


def test_default_app_logs_no_startup_warning(make_client, quiet_startup, caplog):
    with caplog.at_level(logging.WARNING, logger="lectern.main"), make_client():
        pass
    assert _startup_warnings(caplog) == []


def test_bad_list_entry_is_logged_at_startup(make_client, quiet_startup, caplog):
    with caplog.at_level(logging.WARNING, logger="lectern.main"):
        client = make_client(LECTERN_ALLOWED_HOSTS="mc.example.org, not a host")
        assert _startup_warnings(caplog) == []
        with client:
            messages = [r.getMessage() for r in _startup_warnings(caplog)]
            assert len(messages) == 1
            assert "'not a host'" in messages[0]
            assert client.get("/api/health", headers={"Host": "mc.example.org"}).status_code == 200


# --- WebSockets ----------------------------------------------------------------


@pytest.mark.parametrize("path", SOCKETS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Host": EVIL_HOST},
        {"Host": "192.168.1.10:8000", "Origin": EVIL_ORIGIN},
        {"Origin": "http://203.0.113.7"},
        {"Origin": "null"},
    ],
)
def test_socket_handshake_refused(client, path, headers):
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(path, headers=headers),
    ):
        pass
    assert refused.value.code == 1008


@pytest.mark.parametrize("path", SOCKETS)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost:5173"},
        {"Host": "pi.local:8000", "Origin": "http://pi.local:8000"},
        {"Host": "backend:8000", "Origin": "http://192.168.1.20:5173"},
    ],
)
def test_socket_handshake_accepted(client, path, headers):
    from lectern.servers import install

    install._set("guarded", "download_jar", "Downloading server jar")
    try:
        with client.websocket_connect(path, headers=headers) as ws:
            if path == "/ws/status":
                assert ws.receive_json()["type"] == "snapshot"
            elif path.endswith("/install"):
                assert ws.receive_json()["step"] == "download_jar"
    finally:
        install._progress.pop("guarded", None)


class _RecordingProc:
    """Just what ``manager.send`` needs: a running process recording stdin."""

    def __init__(self) -> None:
        self.running = True
        self.sent: list[str] = []

    async def send(self, command: str) -> None:
        self.sent.append(command)


ATTACKER_FRAME: Message = {"type": "websocket.receive", "text": "op attacker"}


def _console_scope(origin: bytes) -> Scope:
    return {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/servers/guarded/console", "raw_path": b"/ws/servers/guarded/console",
        "root_path": "", "query_string": b"", "subprotocols": [],
        "headers": [(b"host", b"192.168.1.10:8000"), (b"origin", origin)],
        "server": ("192.168.1.10", 8000), "client": ("192.168.1.20", 50000),
    }


async def _console_session(asgi: ASGIApp, origin: bytes) -> tuple[list[Message], list[Message]]:
    """Open the console socket with a command already queued behind the
    handshake, as a page would send it. Returns what the app sent and the
    messages it never read."""
    queued: list[Message] = [
        {"type": "websocket.connect"}, dict(ATTACKER_FRAME), {"type": "websocket.disconnect",
                                                               "code": 1000},
    ]

    async def receive() -> Message:
        return queued.pop(0) if queued else {"type": "websocket.disconnect", "code": 1000}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await asyncio.wait_for(asgi(_console_scope(origin), receive, send), timeout=5)
    return sent, queued


async def test_queued_console_command_behind_a_refused_handshake_never_arrives(monkeypatch):
    proc = _RecordingProc()
    monkeypatch.setitem(manager._procs, "guarded", proc)
    manager.hub.clear("guarded")
    sent, unread = await _console_session(main_app, EVIL_ORIGIN.encode())
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert ATTACKER_FRAME in unread
    assert proc.sent == []
    # The same queued command from a LAN page is delivered: the guard made the difference.
    sent, unread = await _console_session(main_app, b"http://192.168.1.20:5173")
    assert sent[0]["type"] == "websocket.accept"
    assert unread == []
    assert proc.sent == ["op attacker"]


# --- raw ASGI: missing Host, refusal log ------------------------------------


class _Downstream:
    """The app behind the guard; records whether a request reached it."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _http(asgi, headers: list[tuple[bytes, bytes]]) -> list[Message]:
    scope: Scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/health", "raw_path": b"/api/health",
        "root_path": "", "query_string": b"", "headers": headers,
        "server": ("192.168.1.10", 8000), "client": ("192.168.1.20", 50000),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await asgi(scope, receive, send)
    return sent


async def test_missing_host_is_refused_by_the_real_app():
    sent = await _http(main_app, [])
    assert sent[0]["status"] == 403
    assert b"no Host header" in sent[1]["body"]


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"[::1")],
        [(b"host", b"pi"), (b"origin", b"not a url")],
        [(b"host", b"pi"), (b"host", b"pi")],
    ],
)
async def test_refused_request_never_reaches_the_app(headers):
    downstream = _Downstream()
    sent = await _http(RequestGuard(downstream, DEFAULT), headers)
    assert sent[0]["status"] == 403
    assert downstream.calls == 0


async def test_refused_websocket_never_reaches_the_app_even_with_frames_queued():
    downstream = _Downstream()
    sent, unread = await _console_session(
        RequestGuard(downstream, DEFAULT), EVIL_ORIGIN.encode()
    )
    assert downstream.calls == 0
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert ATTACKER_FRAME in unread


async def test_accepted_request_reaches_the_app():
    downstream = _Downstream()
    sent = await _http(RequestGuard(downstream, DEFAULT), [(b"host", b"pi.local:8000")])
    assert sent[0]["status"] == 204
    assert downstream.calls == 1


async def test_lifespan_passes_through():
    seen: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await RequestGuard(downstream, DEFAULT)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


def _guard_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "lectern.request_guard" and r.levelno == logging.WARNING]


async def test_each_refused_value_is_logged_once(caplog):
    guard = RequestGuard(_Downstream(), DEFAULT)
    with caplog.at_level(logging.WARNING, logger="lectern.request_guard"):
        for _ in range(3):
            await _http(guard, [(b"host", b"a.evil.example")])
        await _http(guard, [(b"host", b"b.evil.example")])
        await _http(guard, [(b"host", b"pi"), (b"origin", b"https://a.evil.example")])
    messages = _guard_warnings(caplog)
    assert len(messages) == 3
    assert "a.evil.example" in messages[0]
    assert "LECTERN_ALLOWED_HOSTS" in messages[0]
    assert "b.evil.example" in messages[1]
    assert "Origin" in messages[2]


async def test_flood_of_made_up_hosts_stays_bounded(caplog):
    downstream = _Downstream()
    guard = RequestGuard(downstream, DEFAULT)
    flood = [f"h{i}.evil.example".encode() for i in range(REFUSAL_LOG_MEMORY * 8)]
    with caplog.at_level(logging.WARNING, logger="lectern.request_guard"):
        for host in flood:
            sent = await _http(guard, [(b"host", host)])
            assert sent[0]["status"] == 403
        assert downstream.calls == 0
        assert len(_guard_warnings(caplog)) == len(flood)
        caplog.clear()
        # The newest values are still remembered; the oldest were forgotten,
        # so memory holds at most REFUSAL_LOG_MEMORY values.
        await _http(guard, [(b"host", flood[-1])])
        assert _guard_warnings(caplog) == []
        await _http(guard, [(b"host", flood[-REFUSAL_LOG_MEMORY - 1])])
        assert len(_guard_warnings(caplog)) == 1


async def test_logged_and_returned_values_are_truncated(caplog):
    guard = RequestGuard(_Downstream(), DEFAULT)
    huge = "x" * 5000 + ".evil.example"
    with caplog.at_level(logging.WARNING, logger="lectern.request_guard"):
        sent = await _http(guard, [(b"host", huge.encode())])
    assert sent[0]["status"] == 403
    assert len(sent[1]["body"]) < 500
    assert len(_guard_warnings(caplog)[0]) < 500
