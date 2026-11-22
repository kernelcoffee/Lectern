"""Black-box cases for the Host and Origin check, derived from the acceptance
criteria and the expected-failure table of the plan rather than from the
implementation: state-changing routes that must not act, bypass attempts
(trailing dots, suffix look-alikes, proxy headers, patterns in the list), the
private-range boundaries of the Origin rule, the reverse-proxy shape, the off
switch on every socket, and the bounded refusal log under a flood of Origins."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.types import ASGIApp, Message, Scope
from starlette.websockets import WebSocketDisconnect

from lectern import config
from lectern.db import get_session
from lectern.main import app as main_app
from lectern.main import create_app
from lectern.models import Server
from lectern.request_guard import REFUSAL_LOG_MEMORY, AllowPolicy, RequestGuard
from lectern.servers.manager import manager

EVIL_HOST = "lectern.evil.example"
EVIL_ORIGIN = "https://evil.example"
VARIABLE = "LECTERN_ALLOWED_HOSTS"
SOCKETS = ("/ws/status", "/ws/servers/bb/console", "/ws/servers/bb/install")


# --- helpers -----------------------------------------------------------------


@pytest.fixture(name="app_with")
def app_with_fixture(engine, monkeypatch) -> Iterator[Callable[[str], TestClient]]:
    """A client on a fresh ``create_app()`` built with the given
    ``LECTERN_ALLOWED_HOSTS``; the lifespan is not run."""

    def override_get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    def build(allowed_hosts: str) -> TestClient:
        monkeypatch.setenv(VARIABLE, allowed_hosts)
        config.get_settings.cache_clear()
        app = create_app()
        app.dependency_overrides[get_session] = override_get_session
        return TestClient(app)

    yield build
    config.get_settings.cache_clear()


def _assert_refused(resp, value: str) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert value in detail
    assert VARIABLE in detail


def _installed(client: TestClient, engine, tmp_path: Path) -> tuple[str, Path]:
    """A server row pointing at a temp directory, so the file manager acts on it."""
    resp = client.post("/api/servers", json={"name": "F", "type": "vanilla", "mc_version": "1.21"})
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    sdir = tmp_path / "srv"
    (sdir / "config").mkdir(parents=True)
    (sdir / "config" / "a.toml").write_text("secret = 1\n")
    with Session(engine) as s:
        srv = s.get(Server, sid)
        assert srv is not None
        srv.path = str(sdir)
        srv.status = "stopped"
        s.add(srv)
        s.commit()
    return sid, sdir


async def _raw_http(asgi: ASGIApp, headers: list[tuple[bytes, bytes]]) -> list[Message]:
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

    await asyncio.wait_for(asgi(scope, receive, send), timeout=5)
    return sent


async def _raw_console(
    asgi: ASGIApp, headers: list[tuple[bytes, bytes]], command: str
) -> list[Message]:
    """Open the console socket with ``command`` queued right behind the
    handshake, the way a hostile page would send it."""
    queued: list[Message] = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": command},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive() -> Message:
        return queued.pop(0) if queued else {"type": "websocket.disconnect", "code": 1000}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/servers/bb/console", "raw_path": b"/ws/servers/bb/console",
        "root_path": "", "query_string": b"", "subprotocols": [], "headers": headers,
        "server": ("192.168.1.10", 8000), "client": ("192.168.1.20", 50000),
    }
    await asyncio.wait_for(asgi(scope, receive, send), timeout=5)
    return sent


class _RecordingProc:
    """What ``manager.send`` needs: a running process that records stdin."""

    def __init__(self) -> None:
        self.running = True
        self.sent: list[str] = []

    async def send(self, command: str) -> None:
        self.sent.append(command)


def _guard_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "lectern.request_guard" and r.levelno == logging.WARNING]


# --- nothing changes behind a refusal ---------------------------------------


def test_cross_site_file_write_and_delete_change_nothing(client, engine, tmp_path):
    sid, sdir = _installed(client, engine, tmp_path)
    target = sdir / "config" / "planted.txt"
    resp = client.put(
        f"/api/servers/{sid}/files/content", params={"path": "config/planted.txt"},
        json={"content": "pwned"}, headers={"Host": "192.168.1.10:8000", "Origin": EVIL_ORIGIN},
    )
    _assert_refused(resp, EVIL_ORIGIN)
    assert not target.exists()
    resp = client.request(
        "DELETE", f"/api/servers/{sid}/files", params={"path": "config/a.toml"},
        headers={"Host": "192.168.1.10:8000", "Origin": "http://203.0.113.7"},
    )
    _assert_refused(resp, "http://203.0.113.7")
    assert (sdir / "config" / "a.toml").read_text() == "secret = 1\n"
    # The same write from the LAN page lands: the 403 was the guard, not the route.
    resp = client.put(
        f"/api/servers/{sid}/files/content", params={"path": "config/planted.txt"},
        json={"content": "fine"},
        headers={"Host": "192.168.1.10:8000", "Origin": "http://192.168.1.10:8000"},
    )
    assert resp.status_code == 204, resp.text
    assert target.read_text() == "fine"


def test_rebound_name_can_neither_read_nor_write_files(client, engine, tmp_path):
    """DNS rebinding: the page is same-origin with its own public name, so the
    browser sends no Origin on a GET; only the Host check stands."""
    sid, sdir = _installed(client, engine, tmp_path)
    resp = client.get(
        f"/api/servers/{sid}/files/content", params={"path": "config/a.toml"},
        headers={"Host": EVIL_HOST},
    )
    _assert_refused(resp, EVIL_HOST)
    assert "secret" not in resp.text
    resp = client.put(
        f"/api/servers/{sid}/files/content", params={"path": "config/a.toml"},
        json={"content": "pwned"},
        headers={"Host": f"{EVIL_HOST}:8000", "Origin": f"http://{EVIL_HOST}:8000"},
    )
    _assert_refused(resp, EVIL_HOST)
    assert (sdir / "config" / "a.toml").read_text() == "secret = 1\n"


def test_cross_site_settings_patch_changes_nothing(client):
    before = client.get("/api/settings").json()
    assert before, "expected at least one tunable"
    first = before[0]
    new_value = first["min"] if first["value"] != first["min"] else first["max"]
    resp = client.patch(
        "/api/settings", json={first["key"]: new_value}, headers={"Origin": EVIL_ORIGIN}
    )
    _assert_refused(resp, EVIL_ORIGIN)
    assert client.get("/api/settings").json() == before


def test_cross_site_server_action_is_refused_and_status_unchanged(client):
    sid = client.post(
        "/api/servers", json={"name": "A", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    before = client.get(f"/api/servers/{sid}").json()["status"]
    for action in ("start", "stop", "restart", "kill"):
        resp = client.post(f"/api/servers/{sid}/action/{action}", headers={"Origin": "null"})
        _assert_refused(resp, "null")
    assert client.get(f"/api/servers/{sid}").json()["status"] == before


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_foreign_origin_refused_whatever_the_method(client, method):
    headers = {"Host": "192.168.1.10:8000", "Origin": EVIL_ORIGIN}
    if method == "OPTIONS":
        # A CORS preflight is refused too: the guard runs before CORSMiddleware.
        headers["Access-Control-Request-Method"] = "POST"
    resp = client.request(method, "/api/servers", headers=headers)
    assert resp.status_code == 403, (method, resp.status_code, resp.text)
    assert "access-control-allow-origin" not in resp.headers


# --- the console socket -------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", EVIL_HOST.encode())],  # rebinding: no Origin needed to fail
        [(b"host", f"{EVIL_HOST}:8000".encode()), (b"origin", f"http://{EVIL_HOST}:8000".encode())],
        [(b"host", b"192.168.1.10:8000"), (b"origin", b"null")],
        [(b"host", b"192.168.1.10:8000"), (b"origin", b"")],
        [(b"origin", b"http://192.168.1.20:5173")],  # no Host at all
    ],
)
async def test_console_command_behind_refused_handshake_never_reaches_stdin(
    monkeypatch, headers
):
    proc = _RecordingProc()
    monkeypatch.setitem(manager._procs, "bb", proc)
    manager.hub.clear("bb")
    sent = await _raw_console(main_app, headers, "stop")
    assert [m["type"] for m in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008
    assert proc.sent == []


@pytest.mark.parametrize("path", SOCKETS)
def test_socket_refuses_an_empty_origin_and_a_rebound_name_with_its_own_origin(client, path):
    for headers in (
        {"Host": "192.168.1.10:8000", "Origin": ""},
        {"Host": f"{EVIL_HOST}:8000", "Origin": f"http://{EVIL_HOST}:8000"},
    ):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(path, headers=headers),
        ):
            pass
        assert refused.value.code == 1008, headers


# --- bypass attempts on the Host and Origin rules ---------------------------


@pytest.mark.parametrize(
    "host",
    [
        "LECTERN.EVIL.EXAMPLE",
        "lectern.evil.example.",
        "lectern.evil.example.:8000",
        "pi.local.evil.example",
        "local.evil.example",
        "evil.local.example",
        "evil-local.example",
        "evil.lan.example.com",
        "home.arpa.evil.example",
        "srv.internal.evil.example",
        "evil.notlocal",
        "evil.example.localhost.com",
        "localhost.evil.example",
        "127.0.0.1.nip.io",
        "192.168.1.10.evil.example",
        "user@pi.local",
        "pi.local:8000:8000",
        "pi.local 8000",
    ],
)
def test_look_alike_hosts_are_refused(client, host):
    resp = client.get("/api/health", headers={"Host": host})
    assert resp.status_code == 403, (host, resp.text)
    assert VARIABLE in resp.json()["detail"]


@pytest.mark.parametrize(
    "origin",
    [
        "HTTPS://EVIL.EXAMPLE",
        "https://evil.example.",
        "https://evil.example:443",
        "http://pi.local.evil.example",
        "http://evil.lan.example.com",
        "http://127.0.0.1.nip.io",
        "http://192.168.1.10.evil.example",
        "http://lectern.evil.example",
    ],
)
def test_look_alike_origins_are_refused(client, origin):
    resp = client.get("/api/health", headers={"Host": "192.168.1.10:8000", "Origin": origin})
    _assert_refused(resp, origin)


@pytest.mark.parametrize(
    "host",
    ["pi.local.", "PI.LAN:8000", "box.HOME.ARPA.", "srv.internal:1", "NAS", "[FD00::10]",
     "[::1]", "127.0.0.1", "0.0.0.0:8000", "8.8.8.8"],
)
def test_lan_host_forms_pass_without_configuration(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200, host


def test_proxy_headers_are_not_trusted(client):
    """No proxy-header trust: a forwarded name neither rescues a refused Host
    nor replaces an accepted one."""
    rescue = {
        "Host": EVIL_HOST,
        "X-Forwarded-Host": "pi.local",
        "Forwarded": "host=pi.local;proto=http",
        "X-Forwarded-For": "192.168.1.20",
    }
    _assert_refused(client.get("/api/health", headers=rescue), EVIL_HOST)
    ignored = {"Host": "pi.local:8000", "X-Forwarded-Host": EVIL_HOST, "Forwarded": f"host={EVIL_HOST}"}
    assert client.get("/api/health", headers=ignored).status_code == 200


async def test_non_ascii_header_bytes_are_refused_not_a_crash():
    for headers in (
        [(b"host", b"\xe9vil.example")],
        [(b"host", b"pi.local\xff")],
        [(b"host", b"pi"), (b"origin", b"http://\xe9vil.example")],
    ):
        sent = await _raw_http(main_app, headers)
        assert sent[0]["status"] == 403, headers


# --- Origin rule: the private-range boundaries ------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.0", "10.255.255.255",
        "172.16.0.0", "172.31.255.255",
        "192.168.0.0", "192.168.255.255",
        "127.0.0.1", "127.255.255.254",
        "169.254.0.1", "169.254.255.254",
        "[::1]", "[fc00::1]", "[fdff:ffff::1]", "[fe80::1]", "[febf::1]",
        "[::ffff:10.0.0.1]", "[::ffff:127.0.0.1]",
    ],
)
def test_local_ip_origin_passes_on_a_different_host(client, ip):
    resp = client.get("/api/health", headers={"Host": "pi:8000", "Origin": f"http://{ip}:5173"})
    assert resp.status_code == 200, (ip, resp.text)


@pytest.mark.parametrize(
    "ip",
    [
        "9.255.255.255", "11.0.0.0",
        "172.15.255.255", "172.32.0.0",
        "192.167.255.255", "192.169.0.0",
        "169.253.255.255", "169.255.0.1",
        "100.64.0.1", "100.127.255.254",
        "198.18.0.1", "192.0.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.7",
        "240.0.0.1", "255.255.255.255", "0.0.0.0",
        "[::]", "[2001:db8::1]", "[fe00::1]", "[fec0::1]", "[2606:4700::1111]",
        "[::ffff:8.8.8.8]", "[64:ff9b::8.8.8.8]",
    ],
)
def test_public_or_special_ip_origin_refused_on_a_different_host(client, ip):
    origin = f"http://{ip}"
    _assert_refused(
        client.get("/api/health", headers={"Host": "pi:8000", "Origin": origin}), origin
    )


@pytest.mark.parametrize("ip", ["100.64.0.1", "203.0.113.7", "[2001:db8::1]"])
def test_any_ip_origin_passes_when_it_is_the_request_s_own_host(client, ip):
    """Host and Origin on the same address: the admin typed that address, the
    page came from Lectern itself (ports differ, which is ignored)."""
    resp = client.get("/api/health", headers={"Host": f"{ip}:8000", "Origin": f"http://{ip}:5173"})
    assert resp.status_code == 200, (ip, resp.text)


# --- the allow list -----------------------------------------------------------


def test_reverse_proxy_that_rewrites_host_needs_the_public_name_listed(app_with):
    """The proxy sends ``Host: backend:8000``; the browser's Origin still
    carries the public name, so it must be listed."""
    unlisted = app_with("")
    payload = {"name": "Proxied", "type": "vanilla", "mc_version": "1.21"}
    headers = {"Host": "backend:8000", "Origin": "https://mc.example.org"}
    _assert_refused(unlisted.post("/api/servers", json=payload, headers=headers),
                    "https://mc.example.org")
    assert unlisted.get("/api/servers").json() == []
    listed = app_with("mc.example.org")
    resp = listed.post("/api/servers", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    assert [s["name"] for s in listed.get("/api/servers").json()] == ["Proxied"]


def test_listed_name_covers_neither_subdomains_nor_parents(app_with):
    client = app_with("mc.example.org")
    assert client.get("/api/health", headers={"Host": "mc.example.org"}).status_code == 200
    for host in ("a.mc.example.org", "example.org", "mc.example.org.evil.example",
                 "xmc.example.org"):
        _assert_refused(client.get("/api/health", headers={"Host": host}), host)
        origin = f"https://{host}"
        _assert_refused(
            client.get("/api/health", headers={"Host": "pi", "Origin": origin}), origin
        )


@pytest.mark.parametrize("raw", ["*.example.com", "**", "*example.com", "mc.*"])
def test_a_pattern_is_not_a_wildcard_and_does_not_switch_the_checks_off(app_with, raw):
    client = app_with(raw)
    for host in ("a.example.com", "example.com", "mc.example.org", EVIL_HOST):
        _assert_refused(client.get("/api/health", headers={"Host": host}), host)
    _assert_refused(
        client.get("/api/health", headers={"Host": "pi", "Origin": EVIL_ORIGIN}), EVIL_ORIGIN
    )


def test_listed_entry_with_port_or_trailing_dot_matches_any_port(app_with):
    client = app_with("MC.Example.org.:443")
    for host in ("mc.example.org", "mc.example.org:8443", "MC.EXAMPLE.ORG."):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200, host


def test_empty_or_comma_only_list_is_the_default_policy(app_with):
    for raw in ("", " ", ",", " , ,"):
        client = app_with(raw)
        assert client.app.state.request_guard_policy == AllowPolicy()
        _assert_refused(client.get("/api/health", headers={"Host": EVIL_HOST}), EVIL_HOST)


# --- the off switch -------------------------------------------------------------


@pytest.mark.parametrize("path", SOCKETS)
def test_off_switch_opens_every_socket_to_any_host_and_origin(app_with, path):
    from lectern.servers import install

    client = app_with("*")
    install._set("bb", "download_jar", "Downloading server jar")
    try:
        for headers in (
            {"Host": EVIL_HOST, "Origin": EVIL_ORIGIN},
            {"Host": "192.168.1.10", "Origin": "null"},
        ):
            with client.websocket_connect(path, headers=headers) as ws:
                if path == "/ws/status":
                    assert ws.receive_json()["type"] == "snapshot"
                elif path.endswith("/install"):
                    assert ws.receive_json()["step"] == "download_jar"
    finally:
        install._progress.pop("bb", None)


async def test_off_switch_passes_a_request_with_no_host(app_with):
    client = app_with("mc.example.org , *")
    sent = await _raw_http(client.app, [(b"origin", b"null")])
    assert sent[0]["status"] == 200
    assert client.post(
        "/api/servers", json={"name": "Off", "type": "vanilla", "mc_version": "1.21"},
        headers={"Host": EVIL_HOST, "Origin": "http://203.0.113.7"},
    ).status_code == 201


# --- refusal log under a flood -------------------------------------------------


async def test_flood_of_made_up_origins_is_refused_and_the_log_memory_is_bounded(caplog):
    """Each refused Origin is refused, logged once, and exactly the newest
    REFUSAL_LOG_MEMORY values are remembered: no more (memory) and no fewer
    (an attacker cannot flush one value with a handful of others)."""
    reached = 0

    async def downstream(scope, receive, send) -> None:
        nonlocal reached
        reached += 1

    guard = RequestGuard(downstream, AllowPolicy())
    flood = [f"https://o{i}.evil.example".encode() for i in range(REFUSAL_LOG_MEMORY * 10)]
    with caplog.at_level(logging.WARNING, logger="lectern.request_guard"):
        for origin in flood:
            sent = await _raw_http(guard, [(b"host", b"192.168.1.10:8000"), (b"origin", origin)])
            assert sent[0]["status"] == 403
        assert reached == 0
        assert len(_guard_warnings(caplog)) == len(flood)
        caplog.clear()
        for origin in flood[-REFUSAL_LOG_MEMORY:]:
            sent = await _raw_http(guard, [(b"host", b"192.168.1.10:8000"), (b"origin", origin)])
            assert sent[0]["status"] == 403
        assert _guard_warnings(caplog) == []
        sent = await _raw_http(
            guard, [(b"host", b"192.168.1.10:8000"), (b"origin", flood[-REFUSAL_LOG_MEMORY - 1])]
        )
        assert sent[0]["status"] == 403
        assert len(_guard_warnings(caplog)) == 1


async def test_a_refused_host_and_the_same_text_as_origin_are_logged_separately(caplog):
    async def downstream(scope, receive, send) -> None:
        raise AssertionError("a refused request reached the app")

    guard = RequestGuard(downstream, AllowPolicy())
    with caplog.at_level(logging.WARNING, logger="lectern.request_guard"):
        for headers in (
            [(b"host", b"evil.example")],
            [(b"host", b"pi"), (b"origin", b"evil.example")],
            [(b"host", b"evil.example")],
            [(b"host", b"pi"), (b"origin", b"evil.example")],
        ):
            sent = await _raw_http(guard, headers)
            assert sent[0]["status"] == 403
    messages = _guard_warnings(caplog)
    assert len(messages) == 2
    assert "evil.example" in messages[0] and "Origin" not in messages[0]
    assert "evil.example" in messages[1] and "Origin" in messages[1]
