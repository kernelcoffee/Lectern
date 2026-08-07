// Mods tab (M6) — installed list + Modrinth browse/install.
//
// Workflow:
//   1. On mount, fetch the installed items (GET /content). Everything below
//      operates on that list and refreshes it after each mutation.
//   2. "Check updates" hits GET /content/updates; results attach an "Update →
//      x.y.z" action to the matching rows (and an "Update all" button).
//   3. "Browse" opens a modal that searches Modrinth scoped to this server's
//      loader + MC version, so every result is installable as-is. Installing
//      returns the full set of items that came along (required deps — optional
//      ones via the checkbox), which we surface in a one-line notice.
//   4. Per row: enable/disable (renames the file server-side so the loader
//      skips it), release-channel select (governs which versions qualify as
//      updates), remove.
//
// Mod changes apply at the next server start; the tab shows a static hint
// rather than tracking dirtiness — the backend is always the source of truth.

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  applyContentUpdate,
  checkContentUpdates,
  ContentItem,
  ContentUpdate,
  importModpack,
  listContent,
  patchContent,
  ReleaseChannel,
  removeContent,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";
import { useToast } from "../../components/Toasts";
import ContentBrowser from "./ContentBrowser";

const CHANNELS: ReleaseChannel[] = ["release", "beta", "alpha"];

export default function ModsTab({
  serverId,
  server,
}: {
  serverId: string;
  server: ServerDetail;
}) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [updates, setUpdates] = useState<ContentUpdate[] | null>(null);
  // Load failures only — action outcomes go through toasts.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // item id or "check"/"install"
  const [browsing, setBrowsing] = useState(false);
  const mrpackInput = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      // The content list is shared with the Resource Packs tab — only
      // mod-like kinds belong here.
      const all = await listContent(serverId);
      setItems(all.filter((i) => i.kind === "mod" || i.kind === "plugin"));
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

  const checkUpdates = () =>
    run("check", async () => {
      const found = await checkContentUpdates(serverId);
      setUpdates(found);
      toast.info(
        found.length === 0
          ? "Everything is up to date."
          : `${found.length} update${found.length > 1 ? "s" : ""} available.`,
      );
    });

  const updateOne = (itemId: string) =>
    run(itemId, async () => {
      await applyContentUpdate(serverId, itemId);
      setUpdates((u) => u?.filter((x) => x.item_id !== itemId) ?? null);
      await refresh();
    });

  const updateAll = () =>
    run("check", async () => {
      for (const u of updates ?? []) await applyContentUpdate(serverId, u.item_id);
      setUpdates([]);
      await refresh();
    });

  const toggle = (item: ContentItem) =>
    run(item.id, async () => {
      await patchContent(serverId, item.id, { enabled: !item.enabled });
      await refresh();
    });

  const setChannel = (item: ContentItem, channel: ReleaseChannel) =>
    run(item.id, async () => {
      await patchContent(serverId, item.id, { channel });
      await refresh();
    });

  const remove = (item: ContentItem) =>
    run(item.id, async () => {
      await removeContent(serverId, item.id);
      setUpdates((u) => u?.filter((x) => x.item_id !== item.id) ?? null);
      await refresh();
    });

  const importPack = (file: File) =>
    run("mrpack", async () => {
      const s = await importModpack(serverId, file);
      const parts = [
        `Imported ${s.pack_name} ${s.pack_version}: ${s.installed} files`,
      ];
      if (s.skipped_client_only.length > 0)
        parts.push(`${s.skipped_client_only.length} client-only skipped`);
      if (s.loader_changed) parts.push(`loader pinned to ${s.loader_version}`);
      toast.success(parts.join(" · ") + ". Applies at next start.");
      await refresh();
    });

  const updateFor = (id: string) => updates?.find((u) => u.item_id === id);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300 flex-1 whitespace-nowrap">
          Installed mods {items && <span className="text-slate-500">({items.length})</span>}
        </h3>
        {updates && updates.length > 0 && (
          <button
            onClick={updateAll}
            disabled={busy !== null}
            className="bg-sky-600 hover:bg-sky-500 disabled:opacity-50 rounded px-3 py-1.5 text-sm font-medium"
          >
            Update all ({updates.length})
          </button>
        )}
        <button
          onClick={checkUpdates}
          disabled={busy !== null || !items || items.length === 0}
          className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded px-3 py-1.5 text-sm"
        >
          {busy === "check" ? "Checking…" : "Check updates"}
        </button>
        <button
          onClick={() => mrpackInput.current?.click()}
          disabled={busy !== null}
          className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded px-3 py-1.5 text-sm"
        >
          {busy === "mrpack" ? "Importing…" : "Import .mrpack"}
        </button>
        <input
          ref={mrpackInput}
          type="file"
          accept=".mrpack,.zip"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) importPack(f);
            e.target.value = "";
          }}
        />
        <button
          onClick={() => setBrowsing(true)}
          className="bg-emerald-600 hover:bg-emerald-500 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          Add mods
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {!items ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-500">
          No mods installed yet — browse Modrinth to add some.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {items.map((item) => {
            const upd = updateFor(item.id);
            return (
              <li key={item.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
                <div className="w-full sm:w-auto sm:flex-1 min-w-0">
                  <p className="text-sm truncate">
                    <span className={item.enabled ? "" : "text-slate-500 line-through"}>
                      {item.name}
                    </span>{" "}
                    <span className="text-slate-500">{item.version_number}</span>
                    {upd && (
                      <span className="ml-2 text-xs text-sky-400">
                        → {upd.new_version_number} available
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-slate-500 truncate">{item.filename}</p>
                </div>
                <select
                  value={item.channel}
                  onChange={(e) => setChannel(item, e.target.value as ReleaseChannel)}
                  disabled={busy !== null}
                  title="Least-stable release type allowed for updates"
                  className="bg-slate-800 border border-slate-700 rounded px-1.5 py-1 text-xs"
                >
                  {CHANNELS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
                {upd && (
                  <button
                    onClick={() => updateOne(item.id)}
                    disabled={busy !== null}
                    className="bg-sky-600 hover:bg-sky-500 disabled:opacity-50 rounded px-2.5 py-1 text-xs font-medium"
                  >
                    {busy === item.id ? "…" : "Update"}
                  </button>
                )}
                <button
                  onClick={() => toggle(item)}
                  disabled={busy !== null}
                  className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded px-2.5 py-1 text-xs"
                >
                  {item.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  onClick={() => remove(item)}
                  disabled={busy !== null}
                  className="bg-red-900/60 hover:bg-red-800 disabled:opacity-50 rounded px-2.5 py-1 text-xs text-red-200"
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-xs text-slate-500">
        Mod changes take effect the next time the server starts.
      </p>

      {browsing && (
        <ContentBrowser
          server={server}
          initialType="mod"
          onChanged={refresh}
          onClose={() => setBrowsing(false)}
        />
      )}
    </section>
  );
}
