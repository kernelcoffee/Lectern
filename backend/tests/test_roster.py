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
