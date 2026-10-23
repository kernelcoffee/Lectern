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
