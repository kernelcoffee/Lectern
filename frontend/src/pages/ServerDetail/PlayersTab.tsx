// Players tab (per server): manage the whitelist / ops / banned lists, backed
// by the server's own whitelist.json / ops.json / banned-players.json. You add
// from the global registry (the sidebar Players page). On a running server the
// backend applies changes immediately via console commands; otherwise it edits
// the files, which Minecraft reads at the next start.

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  addToList,
  getOnlinePlayers,
  getPlayerLists,
  kickPlayer,
  listPlayers,
  ListKind,
  OnlinePlayer,
  Player,
  PlayerEntry,
  PlayerLists,
  removeFromList,
} from "../../api/players";
import Avatar from "../../components/Avatar";
import { useToast } from "../../components/Toasts";

const ONLINE_POLL_MS = 5000;

const SECTIONS: { kind: ListKind; title: string; help: string }[] = [
  { kind: "whitelist", title: "Whitelist", help: "allowed to join (if whitelisting is on)" },
  { kind: "ops", title: "Operators", help: "server admins (op level 4)" },
  { kind: "banned", title: "Banned", help: "blocked from joining" },
];

export default function PlayersTab({ serverId }: { serverId: string }) {
  const [lists, setLists] = useState<PlayerLists | null>(null);
  const [registry, setRegistry] = useState<Player[]>([]);
  const [busy, setBusy] = useState(false);
  // Load failures only — list mutations report through toasts.
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      const [l, r] = await Promise.all([getPlayerLists(serverId), listPlayers()]);
      setLists(l);
      setRegistry(r);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(fn: () => Promise<PlayerLists>) {
    setBusy(true);
    try {
      setLists(await fn());
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-5">
      <p className="text-xs text-slate-500">
        Changes apply immediately while the server is running, otherwise at the
        next start. Add players in the{" "}
        <span className="text-slate-400">Players</span> page first.
      </p>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <OnlineSection serverId={serverId} />

      {lists === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-3">
          {SECTIONS.map((s) => (
            <ListSection
              key={s.kind}
              title={s.title}
              help={s.help}
              members={lists[s.kind]}
              registry={registry}
              busy={busy}
              onAdd={(uuid) => run(() => addToList(serverId, s.kind, uuid))}
              onRemove={(uuid) => run(() => removeFromList(serverId, s.kind, uuid))}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function OnlineSection({ serverId }: { serverId: string }) {
  const [online, setOnline] = useState<OnlinePlayer[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const players = await getOnlinePlayers(serverId);
        if (live) {
          setOnline(players);
          setError(null);
        }
      } catch (e) {
        if (live) setError(e instanceof ApiError ? e.message : String(e));
      }
    };
    tick();
    const id = setInterval(tick, ONLINE_POLL_MS);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [serverId]);

  async function kick(name: string) {
    setBusy(true);
    try {
      setOnline(await kickPlayer(serverId, name));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-slate-800 p-3">
      <div>
        <h3 className="text-sm font-semibold text-slate-200">
          Online now{" "}
          <span className="text-slate-500">({online?.length ?? 0})</span>
        </h3>
        <p className="text-[11px] text-slate-500">
          live from the console — includes Carpet-style bots
        </p>
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}

      {online === null || online.length === 0 ? (
        <p className="text-xs text-slate-600">
          Nobody online (or the server isn't running).
        </p>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-3">
          {online.map((p) => (
            <div
              key={p.name}
              className="group relative flex flex-col items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/40 p-4"
            >
              <button
                onClick={() => kick(p.name)}
                disabled={busy}
                title={p.bot ? "Kill bot" : "Kick"}
                aria-label={`${p.bot ? "Kill bot" : "Kick"} ${p.name}`}
                className="absolute right-1.5 top-1.5 rounded px-1 text-slate-600 opacity-0 transition hover:text-red-300 group-hover:opacity-100 disabled:opacity-50"
              >
                ✕
              </button>
              <Avatar uuid={p.uuid} name={p.name} size={80} />
              <div className="w-full truncate text-center text-sm font-medium text-slate-200">
                {p.name}
              </div>
              {p.bot ? (
                <span className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-400">
                  bot
                </span>
              ) : (
                <div className="w-full truncate text-center font-mono text-[10px] text-slate-600">
                  {p.uuid ? `${p.uuid.slice(0, 12)}…` : " "}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ListSection({
  title,
  help,
  members,
  registry,
  busy,
  onAdd,
  onRemove,
}: {
  title: string;
  help: string;
  members: PlayerEntry[];
  registry: Player[];
  busy: boolean;
  onAdd: (uuid: string) => void;
  onRemove: (uuid: string) => void;
}) {
  const [pick, setPick] = useState("");
  const present = new Set(members.map((m) => m.uuid));
  const addable = registry.filter((p) => !present.has(p.uuid));

  return (
    <div className="space-y-3 rounded-lg border border-slate-800 p-3">
      <div>
        <h3 className="text-sm font-semibold text-slate-200">
          {title} <span className="text-slate-500">({members.length})</span>
        </h3>
        <p className="text-[11px] text-slate-500">{help}</p>
      </div>

      {members.length === 0 ? (
        <p className="text-xs text-slate-600">Nobody yet.</p>
      ) : (
        <ul className="space-y-1.5">
          {members.map((m) => (
            <li key={m.uuid} className="flex items-center gap-2">
              <Avatar uuid={m.uuid} name={m.name} size={24} />
              <span className="min-w-0 flex-1 truncate text-sm text-slate-300">
                {m.name}
              </span>
              <button
                onClick={() => onRemove(m.uuid)}
                disabled={busy}
                className="text-xs text-slate-500 hover:text-red-300 disabled:opacity-50"
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex gap-1.5 pt-1">
        <select
          value={pick}
          onChange={(e) => setPick(e.target.value)}
          disabled={busy || addable.length === 0}
          className="min-w-0 flex-1 rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 disabled:opacity-50"
        >
          <option value="">
            {addable.length === 0 ? "no more players to add" : "add a player…"}
          </option>
          {addable.map((p) => (
            <option key={p.uuid} value={p.uuid}>
              {p.name}
            </option>
          ))}
        </select>
        <button
          onClick={() => {
            if (pick) {
              onAdd(pick);
              setPick("");
            }
          }}
          disabled={busy || !pick}
          className="rounded bg-slate-700 px-2.5 py-1 text-xs hover:bg-slate-600 disabled:opacity-50"
        >
          Add
        </button>
      </div>
    </div>
  );
}
