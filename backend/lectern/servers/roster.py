"""Live online-player roster: Server Management Protocol first, console fallback.

Two sources feed one ``Roster`` per running process, and it dies with the
process (a new run starts empty):

* **MSMP** (1.21.9+, see ``msmp.py``): on connect ``sync()`` seeds membership
  from ``minecraft:players``, then ``players/joined`` / ``players/left``
  notifications call ``joined()`` / ``left()``. It gives uuid + name only.
* **Console** (always on): ``ServerProcess`` feeds every (ANSI-stripped)
  stdout line into ``feed()``, which tracks the server's own join/leave
  messages. It is the whole roster on older versions, proxies and while the
  management socket is down, and it is the only source of the ``bot`` flag.

Merge rules (convergent whatever order the sources report in): entries are
keyed by name; adding is idempotent (the earliest ``joined_at`` wins, ``uuid``
fills in when first learned); a departure reported by either source removes;
only ``sync()`` drops names, and only names the server itself did not report
(stale console state after a reconnect). The ``bot`` flag is the verdict of
the latest login line for that name and outlives the entry: a departure or a
``sync()`` that races the console (a socket reporting a kill/respawn late, a
bot spawning while the connect snapshot is in flight) cannot turn a bot into
a player when the protocol re-adds it. Only a network login line for the name
clears it.

Names from the protocol are held to the same pattern as console names, so a
roster key never carries selector syntax (``@a``) into a console command.

Artificial players (Carpet's ``/player`` bots and the like) are first-class
here: they join over a non-network connection, which the login line exposes as
``Name[local] logged in`` instead of ``Name[/ip:port] logged in`` — that is the
``bot`` flag. Bots skip the authenticator (no "UUID of player" line) and often
have names no Mojang account owns, so ``uuid`` is optional and nothing below
requires the player registry.

Line shapes handled (vanilla + Fabric use the same format)::

    [12:00:00] [User Authenticator #1/INFO]: UUID of player Notch is 069a79f4-…
    [12:00:00] [Server thread/INFO]: Notch[/192.0.2.7:52717] logged in with entity id …
    [12:00:00] [Server thread/INFO]: bot_farm[local] logged in with entity id …
    [12:00:00] [Server thread/INFO]: Notch joined the game
    [12:00:00] [Server thread/INFO]: Notch lost connection: Disconnected
    [12:00:00] [Server thread/INFO]: Notch left the game

Minecraft 26.3 reshaped these ("some log lines that server software might
look out for have been removed"): the login line gained a space and a colon,
and join/leave are now logged as system-chat broadcasts::

    [12:00:00] [Server thread/INFO]: Notch [/192.0.2.7:52717]: logged in with entity id …
    [12:00:00] [Server thread/INFO]: System chat: Notch joined the game
    [12:00:00] [Server thread/INFO]: System chat: Notch left the game

Both generations are accepted, and a server that logs both the plain and the
``System chat:`` join for one login counts it once (the second join carries no
new login info, so it is ignored rather than resetting the entry).

Chat can't spoof these: player chat renders as ``<Name> text`` and ``/say`` as
``[Server] text`` (``System chat: [Server] text`` on 26.3), neither of which
matches the anchored patterns below.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Vanilla names are [A-Za-z0-9_]{3,16}; be a little laxer (dots, dashes, any
# length ≥1) for offline-mode servers and creatively-named bots.
_NAME = r"[\w.-]+"
# The `]: ` tail anchor skips the timestamp/thread prefix without trusting its
# exact shape (mod loaders occasionally restyle it).
_PREFIX = r"^.*\]:\s"

# 26.3 prefixes join/leave broadcasts with "System chat: " — optional here.
_CHAT = r"(?:System chat: )?"
_RE_UUID = re.compile(_PREFIX + rf"UUID of player ({_NAME}) is ([0-9a-fA-F-]{{32,36}})$")
# `Name[/ip:port] logged in` (≤26.2) or `Name [/ip:port]: logged in` (26.3).
_RE_LOGIN = re.compile(_PREFIX + rf"({_NAME}) ?\[(local|/[^\]]+)\]:? logged in with entity id ")
_RE_JOINED = re.compile(_PREFIX + _CHAT + rf"({_NAME}) joined the game$")
_RE_LEFT = re.compile(_PREFIX + rf"(?:{_CHAT}({_NAME}) left the game$|({_NAME}) lost connection: )")
_RE_VALID_NAME = re.compile(_NAME)


def _undash(uuid: str | None) -> str | None:
    return uuid.replace("-", "").lower() if uuid else None


@dataclass
class OnlinePlayer:
    name: str
    uuid: str | None  # undashed; None for bots / offline-mode joins
    bot: bool
    joined_at: float  # wall-clock, for "online for …" display


@dataclass
class Roster:
    players: dict[str, OnlinePlayer] = field(default_factory=dict)
    # Seen before "joined the game" lands: authenticator UUIDs by name, and
    # the names whose login line is waiting for its join line.
    _uuids: dict[str, str] = field(default_factory=dict)
    _logins: set[str] = field(default_factory=set)
    # Names whose latest login line was ``[local]``; outlives the entry (see
    # the merge rules above) and is cleared only by a network login line.
    _bots: set[str] = field(default_factory=set)

    # --- source-agnostic mutators ---------------------------------------------

    def joined(self, name: str, uuid: str | None = None) -> None:
        """Add ``name`` (or merge into its entry). ``uuid`` defaults to what the
        console's authenticator recorded; ``bot`` always comes from the login
        line verdict (``_bots``), never from the caller. A name the console
        could not have produced is ignored."""
        if not _RE_VALID_NAME.fullmatch(name):
            return
        uuid = _undash(uuid) or self._uuids.get(name)
        bot = name in self._bots
        entry = self.players.get(name)
        if entry is None:
            self.players[name] = OnlinePlayer(
                name=name, uuid=uuid, bot=bot, joined_at=time.time()
            )
        else:
            entry.uuid = entry.uuid or uuid
            entry.bot = entry.bot or bot
        self._uuids.pop(name, None)
        self._logins.discard(name)

    def left(self, name: str) -> None:
        self.players.pop(name, None)
        self._uuids.pop(name, None)
        self._logins.discard(name)

    def sync(self, players: list[dict]) -> None:
        """Authoritative snapshot (``minecraft:players``: ``[{id, name}]``):
        merge every reported player, drop every name it does not report."""
        names: set[str] = set()
        for player in players:
            if not isinstance(player, dict) or not isinstance(player.get("name"), str):
                continue
            uuid = player.get("id")
            self.joined(player["name"], uuid if isinstance(uuid, str) else None)
            names.add(player["name"])
        for name in [n for n in self.players if n not in names]:
            del self.players[name]

    # --- console -----------------------------------------------------------------

    def feed(self, line: str) -> None:
        if m := _RE_UUID.match(line):
            self._uuids[m.group(1)] = _undash(m.group(2))
        elif m := _RE_LOGIN.match(line):
            name, local = m.group(1), m.group(2) == "local"
            self._logins.add(name)
            if local:
                self._bots.add(name)
            else:
                self._bots.discard(name)
            if name in self.players:
                # MSMP reported the join before this line was read.
                self.players[name].bot = local
        elif m := _RE_JOINED.match(line):
            name = m.group(1)
            if name in self.players and name not in self._uuids and name not in self._logins:
                return  # duplicate join line for the same login (26.3 logs two)
            self.joined(name)
        elif m := _RE_LEFT.match(line):
            self.left(m.group(1) or m.group(2))

    def online(self) -> list[OnlinePlayer]:
        return sorted(self.players.values(), key=lambda p: p.joined_at)
