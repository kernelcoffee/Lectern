"""Live online-player roster, parsed from console output.

``ServerProcess`` feeds every (ANSI-stripped) stdout line into a ``Roster``;
membership is driven by the server's own join/leave messages, so it needs no
extra protocol and works the moment the console works. The roster dies with the
process — a new run starts empty.

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

Chat can't spoof these: player chat renders as ``<Name> text`` and ``/say`` as
``[Server] text``, neither of which matches the anchored patterns below.
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

_RE_UUID = re.compile(_PREFIX + rf"UUID of player ({_NAME}) is ([0-9a-fA-F-]{{32,36}})$")
_RE_LOGIN = re.compile(_PREFIX + rf"({_NAME})\[(local|/[^\]]+)\] logged in with entity id ")
_RE_JOINED = re.compile(_PREFIX + rf"({_NAME}) joined the game$")
_RE_LEFT = re.compile(_PREFIX + rf"({_NAME}) (?:left the game$|lost connection: )")


@dataclass
class OnlinePlayer:
    name: str
    uuid: str | None  # undashed; None for bots / offline-mode joins
    bot: bool
    joined_at: float  # wall-clock, for "online for …" display


@dataclass
class Roster:
    players: dict[str, OnlinePlayer] = field(default_factory=dict)
    # Seen before "joined the game" lands: authenticator UUIDs and login
    # connection kinds, keyed by name.
    _uuids: dict[str, str] = field(default_factory=dict)
    _local: dict[str, bool] = field(default_factory=dict)

    def feed(self, line: str) -> None:
        if m := _RE_UUID.match(line):
            self._uuids[m.group(1)] = m.group(2).replace("-", "").lower()
        elif m := _RE_LOGIN.match(line):
            self._local[m.group(1)] = m.group(2) == "local"
        elif m := _RE_JOINED.match(line):
            name = m.group(1)
            self.players[name] = OnlinePlayer(
                name=name,
                uuid=self._uuids.pop(name, None),
                bot=self._local.pop(name, False),
                joined_at=time.time(),
            )
        elif m := _RE_LEFT.match(line):
            name = m.group(1)
            self.players.pop(name, None)
            self._uuids.pop(name, None)
            self._local.pop(name, None)

    def online(self) -> list[OnlinePlayer]:
        return sorted(self.players.values(), key=lambda p: p.joined_at)
