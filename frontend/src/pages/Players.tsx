// Players registry (the "friends" list). Add players by username or UUID —
// Lectern validates them against Mojang and stores their canonical UUID + name.
// From here they can be added to any server's whitelist / ops / banned lists
// (on the server's Players tab).

import { FormEvent, useEffect, useState } from "react";
import { errorMessage } from "../api/client";
import { addPlayer, listPlayers, Player, removePlayer } from "../api/players";
import Avatar from "../components/Avatar";
import { useToast } from "../components/Toasts";

type View = "cards" | "list";

export default function Players() {
  const [players, setPlayers] = useState<Player[] | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  // Load failures only — add/remove report through toasts.
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("cards");
  const toast = useToast();

  async function refresh() {
    try {
      setPlayers(await listPlayers());
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function add(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    try {
      await addPlayer(q);
      setQuery("");
      await refresh();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(player: Player) {
    if (!window.confirm(`Remove ${player.name} from the registry?`)) return;
    try {
      await removePlayer(player.uuid);
      await refresh();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  }

  return (
    <div className="space-y-5 p-4 sm:p-6">
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <h2 className="text-xl font-semibold">Players</h2>
          <p className="text-sm text-slate-500">
            Your known players. Add by username or UUID (validated against
            Mojang), then add them to a server's whitelist, ops, or ban list from
            its Players tab.
          </p>
        </div>
        {players && players.length > 0 && (
          <div className="flex items-center gap-1 rounded-md border border-slate-800 p-0.5">
            {(["cards", "list"] as View[]).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={
                  "rounded px-2.5 py-1 text-xs capitalize " +
                  (view === v
                    ? "bg-slate-700 text-slate-100"
                    : "text-slate-400 hover:text-slate-200")
                }
              >
                {v}
              </button>
            ))}
          </div>
        )}
      </div>

      <form onSubmit={add} className="flex max-w-lg gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Minecraft username or UUID"
          className="flex-1 rounded border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={busy || !query.trim()}
          className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy ? "Adding…" : "Add player"}
        </button>
      </form>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {players === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : players.length === 0 ? (
        <p className="text-sm text-slate-500">
          No players yet — add one above to get started.
        </p>
      ) : view === "cards" ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-3">
          {players.map((p) => (
            <div
              key={p.uuid}
              className="group relative flex flex-col items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/40 p-4"
            >
              <button
                onClick={() => remove(p)}
                title="Remove"
                aria-label={`Remove ${p.name}`}
                className="absolute right-1.5 top-1.5 rounded px-1 text-slate-600 opacity-0 transition hover:text-red-300 group-hover:opacity-100"
              >
                ✕
              </button>
              <Avatar uuid={p.uuid} name={p.name} size={80} />
              <div className="w-full truncate text-center text-sm font-medium text-slate-200">
                {p.name}
              </div>
              <div className="w-full truncate text-center font-mono text-[10px] text-slate-600">
                {p.uuid.slice(0, 12)}…
              </div>
            </div>
          ))}
        </div>
      ) : (
        <ul className="max-w-lg divide-y divide-slate-800 rounded-lg border border-slate-800">
          {players.map((p) => (
            <li key={p.uuid} className="flex items-center gap-3 p-3">
              <Avatar uuid={p.uuid} name={p.name} size={32} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-slate-200">{p.name}</div>
                <div className="truncate font-mono text-[11px] text-slate-600">
                  {p.uuid}
                </div>
              </div>
              <button
                onClick={() => remove(p)}
                className="text-xs text-red-400 hover:text-red-300"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
