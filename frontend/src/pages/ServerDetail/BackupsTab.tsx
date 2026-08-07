// Backups tab (M9) — list/create/restore/delete archives of this server.
//
// Restore is destructive (full directory replace) and needs a stopped
// server, so it double-confirms and surfaces the backend's 409s verbatim.
// Backup settings (retention, exclusions, compress, stop-before) live with
// the other Lectern settings on the Properties tab.

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  Backup,
  backupDownloadUrl,
  createBackup,
  deleteBackup,
  listBackups,
  restoreBackup,
} from "../../api/backups";
import { ServerDetail } from "../../api/servers";
import { useToast } from "../../components/Toasts";
import { formatBytes } from "../../lib/format";

export default function BackupsTab({
  serverId,
  server,
  onServerChanged,
}: {
  serverId: string;
  server: ServerDetail;
  /** Restore may change on-disk state the header shows (port etc.). */
  onServerChanged: () => void;
}) {
  const [backups, setBackups] = useState<Backup[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // backup id or "create"
  // Load failures only — action outcomes go through toasts.
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      setBackups(await listBackups(serverId));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key);
    try {
      await fn();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const create = () =>
    run("create", async () => {
      const backup = await createBackup(serverId);
      toast.success(`Backup created (${formatBytes(backup.size_bytes)}).`);
      await refresh();
    });

  const restore = (backup: Backup) => {
    if (
      !window.confirm(
        `Restore "${server.name}" from ${backup.filename}?\n\n` +
          "This REPLACES the entire server directory (world included) with " +
          "the backup's contents. Anything changed since then is lost.",
      )
    )
      return;
    run(backup.id, async () => {
      await restoreBackup(serverId, backup.id);
      toast.success("Backup restored — the server directory was replaced.");
      onServerChanged();
      await refresh();
    });
  };

  const remove = (backup: Backup) =>
    run(backup.id, async () => {
      await deleteBackup(serverId, backup.id);
      await refresh();
    });

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300 flex-1">
          Backups{" "}
          {backups && <span className="text-slate-500">({backups.length})</span>}
        </h3>
        <button
          onClick={create}
          disabled={busy !== null}
          className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          {busy === "create" ? "Backing up…" : "Create backup"}
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {!backups ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : backups.length === 0 ? (
        <p className="text-sm text-slate-500">
          No backups yet — create one before risky changes (version upgrades,
          big mod swaps).
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {backups.map((b) => (
            <li key={b.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
              <div className="w-full sm:w-auto sm:flex-1 min-w-0">
                <p className="text-sm truncate">
                  {new Date(b.created_at + "Z").toLocaleString()}{" "}
                  <span className="text-xs text-slate-500">
                    {formatBytes(b.size_bytes)} · {b.trigger}
                  </span>
                </p>
                <p className="text-xs text-slate-500 truncate">{b.filename}</p>
              </div>
              <a
                href={backupDownloadUrl(serverId, b.id)}
                download={b.filename}
                className="bg-slate-700 hover:bg-slate-600 rounded px-2.5 py-1 text-xs"
              >
                Download
              </a>
              <button
                onClick={() => restore(b)}
                disabled={busy !== null || server.running}
                title={server.running ? "Stop the server first" : undefined}
                className="bg-sky-700 hover:bg-sky-600 disabled:opacity-40 rounded px-2.5 py-1 text-xs"
              >
                {busy === b.id ? "Working…" : "Restore"}
              </button>
              <button
                onClick={() => remove(b)}
                disabled={busy !== null}
                className="bg-red-900/60 hover:bg-red-800 disabled:opacity-50 rounded px-2.5 py-1 text-xs text-red-200"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-slate-500">
        Archives live outside the server directory (in Lectern's backups
        folder). Retention and exclusions are configured under Properties →
        Lectern settings.
      </p>
    </section>
  );
}
