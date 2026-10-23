"""Roster: online players parsed from console lines, bots flagged via [local]."""

from __future__ import annotations

from lectern.servers.roster import Roster

PREFIX = "[12:00:00] [Server thread/INFO]: "
AUTH = "[12:00:00] [User Authenticator #1/INFO]: "
NOTCH_DASHED = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


def _join_real(r: Roster, name="Notch", uuid=NOTCH_DASHED):
    r.feed(AUTH + f"UUID of player {name} is {uuid}")
    r.feed(PREFIX + f"{name}[/192.0.2.7:52717] logged in with entity id 123 at (0, 64, 0)")
    r.feed(PREFIX + f"{name} joined the game")


def _join_bot(r: Roster, name="bot_farm"):
    # Carpet fake players: no authenticator line, `[local]` connection.
    r.feed(PREFIX + f"{name}[local] logged in with entity id 321 at (0, 64, 0)")
    r.feed(PREFIX + f"{name} joined the game")


def test_real_player_join_leave():
    r = Roster()
    _join_real(r)
    [p] = r.online()
    assert (p.name, p.uuid, p.bot) == ("Notch", NOTCH_DASHED.replace("-", ""), False)
    r.feed(PREFIX + "Notch left the game")
    assert r.online() == []


def test_carpet_bot_flagged_no_uuid():
    r = Roster()
    _join_bot(r)
    [p] = r.online()
    assert (p.name, p.uuid, p.bot) == ("bot_farm", None, True)


def test_mixed_roster_sorted_by_join():
    r = Roster()
    _join_real(r)
    _join_bot(r)
    assert [(p.name, p.bot) for p in r.online()] == [("Notch", False), ("bot_farm", True)]


def test_lost_connection_removes():
    r = Roster()
    _join_real(r)
    r.feed(PREFIX + "Notch lost connection: Timed out")
    assert r.online() == []
    # The usual trailing "left the game" after a disconnect is a no-op.
    r.feed(PREFIX + "Notch left the game")
    assert r.online() == []


def test_join_without_login_line_defaults():
    # Defensive: an unrecognised login flow still yields a (non-bot) entry.
    r = Roster()
    r.feed(PREFIX + "Someone joined the game")
    [p] = r.online()
    assert (p.name, p.uuid, p.bot) == ("Someone", None, False)


def test_chat_cannot_spoof_membership():
    r = Roster()
    _join_real(r)
    r.feed(PREFIX + "<Notch> bob joined the game")  # player chat
    r.feed(PREFIX + "[Server] bob joined the game")  # /say broadcast
    r.feed(PREFIX + "<griefer> Notch left the game")
    assert [p.name for p in r.online()] == ["Notch"]


def test_rejoin_refreshes_entry():
    r = Roster()
    _join_real(r)
    r.feed(PREFIX + "Notch left the game")
    _join_real(r)
    assert [p.name for p in r.online()] == ["Notch"]


# --- Minecraft 26.3 log shape ------------------------------------------------


def _join_real_263(r: Roster, name="Notch", uuid=NOTCH_DASHED, both=False):
    r.feed(AUTH + f"UUID of player {name} is {uuid}")
    r.feed(
        PREFIX
        + f"{name} [/192.0.2.7:52717]: logged in with entity id 3795 at "
        "([minecraft:overworld]-29.0, 81.1, -17.4)"
    )
    if both:  # a build that logs the plain line and the broadcast
        r.feed(PREFIX + f"{name} joined the game")
    r.feed(PREFIX + f"System chat: {name} joined the game")


def test_263_join_and_leave():
    r = Roster()
    _join_real_263(r)
    [p] = r.online()
    assert (p.name, p.uuid, p.bot) == ("Notch", NOTCH_DASHED.replace("-", ""), False)
    r.feed(PREFIX + "System chat: Notch left the game")
    assert r.online() == []


def test_263_bot_login_shape():
    r = Roster()
    r.feed(PREFIX + "bot_farm [local]: logged in with entity id 321 at ([minecraft:overworld]0, 64, 0)")
    r.feed(PREFIX + "System chat: bot_farm joined the game")
    [p] = r.online()
    assert (p.name, p.uuid, p.bot) == ("bot_farm", None, True)


def test_263_double_join_counts_once_and_keeps_uuid():
    r = Roster()
    _join_real_263(r, both=True)
    [p] = r.online()
    assert p.uuid == NOTCH_DASHED.replace("-", "")
    assert p.bot is False


def test_263_lost_connection_then_broadcast_leave():
    r = Roster()
    _join_real_263(r)
    r.feed(PREFIX + "Notch lost connection: Disconnected")
    r.feed(PREFIX + "System chat: Notch left the game")
    assert r.online() == []


def test_263_system_chat_cannot_spoof():
    r = Roster()
    _join_real_263(r)
    r.feed(PREFIX + "System chat: [Server] bob joined the game")  # /say on 26.3
    r.feed(PREFIX + "System chat: <griefer> Notch left the game")
    r.feed(PREFIX + "System chat: Notch fell from a high place")  # death message
    assert [p.name for p in r.online()] == ["Notch"]


def test_263_rejoin_after_leave_refreshes():
    r = Roster()
    _join_real_263(r)
    r.feed(PREFIX + "System chat: Notch left the game")
    _join_real_263(r)
    [p] = r.online()
    assert p.uuid == NOTCH_DASHED.replace("-", "")


# --- source-agnostic mutators (MSMP + console merge) -------------------------------


def test_joined_is_idempotent_and_fills_uuid():
    r = Roster()
    r.joined("Notch")
    first = r.players["Notch"].joined_at
    r.joined("Notch", NOTCH_DASHED)
    r.joined("Notch", "ffffffff-ffff-ffff-ffff-ffffffffffff")  # first uuid sticks
    [p] = r.online()
    assert (p.uuid, p.bot, p.joined_at) == (NOTCH_DASHED.replace("-", ""), False, first)
    # The bot verdict comes only from the console login line, and a later
    # join from either source does not clear it.
    r.feed(PREFIX + "Notch[local] logged in with entity id 321 at (0, 64, 0)")
    r.joined("Notch", NOTCH_DASHED)
    assert r.players["Notch"].bot is True


def test_joined_uses_pending_console_info():
    r = Roster()
    r.feed(AUTH + f"UUID of player Notch is {NOTCH_DASHED}")
    r.joined("Notch")
    assert r.players["Notch"].uuid == NOTCH_DASHED.replace("-", "")
    assert r._uuids == {}


def test_sync_authoritative():
    r = Roster()
    _join_bot(r)
    _join_real(r)
    r.feed(PREFIX + "Stale joined the game")
    joined_at = r.players["bot_farm"].joined_at
    r.sync([{"id": NOTCH_DASHED, "name": "Notch"}, {"id": None, "name": "bot_farm"}])
    assert sorted(r.players) == ["Notch", "bot_farm"]
    assert r.players["bot_farm"].bot is True
    assert r.players["bot_farm"].joined_at == joined_at
    r.sync([])
    assert r.online() == []


def test_left_clears_pending():
    r = Roster()
    r.feed(AUTH + f"UUID of player Notch is {NOTCH_DASHED}")
    r.feed(PREFIX + "Notch[local] logged in with entity id 1 at (0, 64, 0)")
    r.left("Notch")
    assert (r._uuids, r._logins) == ({}, set())
    r.feed(PREFIX + "Notch joined the game")
    [p] = r.online()
    # The pending uuid is gone; the login line's bot verdict is not (it
    # outlives departures, see the merge rules).
    assert (p.uuid, p.bot) == (None, True)


def test_bot_flag_survives_sync_racing_the_console():
    """A bot spawns while the connect snapshot is in flight: the console adds
    it, ``sync`` (snapshot taken earlier) drops it, the replayed protocol join
    re-adds it — still a bot."""
    r = Roster()
    r.feed(PREFIX + "Steve [local]: logged in with entity id 7 at (0, 64, 0)")
    r.feed(PREFIX + "System chat: Steve joined the game")
    r.sync([])
    assert r.online() == []
    r.joined("Steve", "5b6b3b8e-0000-3000-8000-000000000001")
    assert r.players["Steve"].bot is True
    r.feed(PREFIX + "System chat: Steve joined the game")  # 26.3 duplicate
    assert r.players["Steve"].bot is True


def _bot_respawn(r: Roster, *, protocol_first: bool) -> None:
    """Kill + respawn of bot Steve, reported by both sources in either order."""
    console = [
        PREFIX + "System chat: Steve left the game",
        PREFIX + "Steve [local]: logged in with entity id 8 at (0, 64, 0)",
        PREFIX + "System chat: Steve joined the game",
    ]

    def protocol() -> None:
        r.left("Steve")
        r.joined("Steve", "5b6b3b8e-0000-3000-8000-000000000001")

    if protocol_first:
        protocol()
    for line in console:
        r.feed(line)
    if not protocol_first:
        protocol()


def test_bot_flag_converges_whichever_source_reports_first():
    results = []
    for protocol_first in (True, False):
        r = Roster()
        r.feed(PREFIX + "Steve [local]: logged in with entity id 7 at (0, 64, 0)")
        r.feed(PREFIX + "System chat: Steve joined the game")
        _bot_respawn(r, protocol_first=protocol_first)
        [p] = r.online()
        results.append((p.name, p.bot))
    assert results == [("Steve", True), ("Steve", True)]


def test_network_login_clears_a_stale_bot_verdict():
    """A real player later taking a former bot's name is not a bot, even when
    the protocol reports the join before the console's login line."""
    r = Roster()
    r.feed(PREFIX + "Steve [local]: logged in with entity id 7 at (0, 64, 0)")
    r.feed(PREFIX + "System chat: Steve joined the game")
    r.feed(PREFIX + "System chat: Steve left the game")
    r.joined("Steve", NOTCH_DASHED)  # protocol first: provisional verdict
    r.feed(PREFIX + "Steve [/192.0.2.7:52717]: logged in with entity id 9 at (0, 64, 0)")
    r.feed(PREFIX + "System chat: Steve joined the game")
    [p] = r.online()
    assert (p.bot, p.uuid) == (False, NOTCH_DASHED.replace("-", ""))


def test_protocol_names_must_match_the_console_pattern():
    """An offline-mode name like ``@a`` is a selector in a console command;
    it never becomes a roster key, so kick can never send ``kick @a``."""
    r = Roster()
    r.joined("@a", NOTCH_DASHED)
    r.sync([{"id": NOTCH_DASHED, "name": "@a"}, {"name": "a b"}, {"name": ""}])
    assert r.online() == []
    r.joined("bot.farm-1")
    assert [p.name for p in r.online()] == ["bot.farm-1"]
