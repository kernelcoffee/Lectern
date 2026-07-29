"""Stats (M5): MOTD flattening, status parsing, a full offline Server List Ping
against a fake asyncio server speaking the wiki.vg protocol, psutil resource
usage, and the stats endpoint's not-running shape.
"""

from __future__ import annotations

import asyncio
import json
import os

from lectern.servers.stats import (
    PingResult,
    _encode_varint,
    motd_text,
    parse_status,
    resource_usage,
    server_list_ping,
)

# --- MOTD description variants (see docs/references/crafty-4.md) -------------


def test_motd_plain_string():
    assert motd_text("A Lectern server") == "A Lectern server"


def test_motd_text_dict():
    assert motd_text({"text": "hello"}) == "hello"


def test_motd_translate_dict():
    assert motd_text({"translate": "chat.type.text"}) == "chat.type.text"


def test_motd_extra_tree():
    desc = {
        "text": "",
        "extra": [
            {"text": "Welcome ", "color": "gold", "bold": True},
            {"text": "to ", "extra": [{"text": "Lectern"}]},
            "!",
        ],
    }
    assert motd_text(desc) == "Welcome to Lectern!"


def test_motd_strips_legacy_formatting_codes():
    assert motd_text("§6Golden§r text") == "Golden text"


def test_motd_none_and_garbage():
    assert motd_text(None) == ""
    assert motd_text(12345) == "12345"


# --- status JSON parsing -------------------------------------------------------


def test_parse_status_full_payload():
    result = parse_status(
        {
            "description": {"text": "hi"},
            "players": {
                "online": 2,
                "max": 20,
                "sample": [{"id": "u1", "name": "alex"}, {"id": "u2", "name": "steve"}],
            },
            "version": {"name": "1.20.1", "protocol": 763},
            "favicon": "data:image/png;base64,AAAA",
        }
    )
    assert result == PingResult(
        online=2,
        max=20,
        players=["alex", "steve"],
        motd="hi",
        version="1.20.1",
        favicon="data:image/png;base64,AAAA",
    )


def test_parse_status_minimal_payload():
    # Servers may omit sample/favicon entirely; empty favicon → None.
    result = parse_status({"players": {"online": 0, "max": 20}, "version": {}, "favicon": ""})
    assert result.players == []
    assert result.favicon is None
    assert result.motd == ""


# --- end-to-end ping against a fake server -------------------------------------


def _status_response(payload: dict) -> bytes:
    """Frame a status-response packet exactly like a real server would."""
    data = json.dumps(payload).encode()
    body = b"\x00" + _encode_varint(len(data)) + data
    return _encode_varint(len(body)) + body


def test_server_list_ping_against_fake_server():
    payload = {
        "description": "§aFake§r MOTD",
        "players": {"online": 1, "max": 5, "sample": [{"id": "x", "name": "alex"}]},
        "version": {"name": "1.21"},
    }

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Consume the handshake + status-request frames (lengths are enough).
        for _ in range(2):
            length = 0
            for shift in range(5):
                byte = (await reader.readexactly(1))[0]
                length |= (byte & 0x7F) << (7 * shift)
                if not byte & 0x80:
                    break
            await reader.readexactly(length)
        writer.write(_status_response(payload))
        await writer.drain()
        writer.close()

    async def run():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await server_list_ping("127.0.0.1", port, timeout=3.0)
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(run())
    assert result is not None
    assert result.motd == "Fake MOTD"
    assert result.online == 1
    assert result.max == 5
    assert result.players == ["alex"]
    assert result.version == "1.21"


def test_server_list_ping_unreachable_returns_none():
    async def run():
        # Port 1 on localhost: connection refused ⇒ graceful None, no raise.
        return await server_list_ping("127.0.0.1", 1, timeout=0.5)

    assert asyncio.run(run()) is None


# --- resource usage --------------------------------------------------------------


def test_resource_usage_on_own_process():
    usage = resource_usage(os.getpid())
    assert usage is not None
    assert usage.memory_mb > 0
    assert usage.cpu_percent >= 0


def test_resource_usage_missing_pid():
    # PIDs are ints; 2**22 is above the default Linux pid_max.
    assert resource_usage(2**22 + 1) is None


# --- endpoint shape ---------------------------------------------------------------


def test_stats_endpoint_not_running(client):
    server_id = client.post(
        "/api/servers", json={"name": "S", "type": "vanilla", "mc_version": "1.21"}
    ).json()["id"]
    body = client.get(f"/api/servers/{server_id}/stats").json()
    assert body["running"] is False
    assert body["pid"] is None
    assert body["ping"] is None


def test_stats_endpoint_missing_server_404(client):
    assert client.get("/api/servers/nope/stats").status_code == 404
