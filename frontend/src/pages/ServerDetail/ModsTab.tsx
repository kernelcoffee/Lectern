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

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  applyContentUpdate,
  checkContentUpdates,
  ContentItem,
  ContentUpdate,
  installContent,
  listContent,
  patchContent,
  ReleaseChannel,
  removeContent,
  searchContent,
  SearchHit,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";

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
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // item id or "check"/"install"
  const [browsing, setBrowsing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setItems(await listContent(serverId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const checkUpdates = () =>
    run("check", async () => {
      const found = await checkContentUpdates(serverId);
      setUpdates(found);
      setNotice(
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

  // Runs inside the browse modal, which shows its own busy/error/success —
  // errors must NOT land in the tab's error slot (it's hidden behind the
  // modal). Throws so the modal can catch and display.
  const install = async (hit: SearchHit, includeOptional: boolean) => {
    const installed = await installContent(serverId, {
      project_id: hit.project_id,
      include_optional_deps: includeOptional,
    });
    setNotice(`Installed: ${installed.map((i) => i.name).join(", ")}`);
    await refresh();
    return installed;
  };

  const updateFor = (id: string) => updates?.find((u) => u.item_id === id);

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300 flex-1">
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
          onClick={() => setBrowsing(true)}
          className="bg-emerald-600 hover:bg-emerald-500 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          Browse Modrinth
        </button>
      </div>

      {notice && <p className="text-xs text-sky-400">{notice}</p>}
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
              <li key={item.id} className="flex items-center gap-3 p-3">
                <div className="flex-1 min-w-0">
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
        <BrowseModal
          server={server}
          installedProjects={new Set((items ?? []).map((i) => i.project_id ?? ""))}
          onInstall={install}
          onClose={() => setBrowsing(false)}
        />
      )}
    </section>
  );
}

// --- browse modal ------------------------------------------------------------

function BrowseModal({
  server,
  installedProjects,
  onInstall,
  onClose,
}: {
  server: ServerDetail;
  installedProjects: Set<string>;
  onInstall: (hit: SearchHit, includeOptional: boolean) => Promise<{ name: string }[]>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [includeOptional, setIncludeOptional] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null); // project_id
  const [installNotice, setInstallNotice] = useState<string | null>(null);

  async function install(hit: SearchHit) {
    setInstalling(hit.project_id);
    setError(null);
    setInstallNotice(null);
    try {
      const installed = await onInstall(hit, includeOptional);
      setInstallNotice(`Installed: ${installed.map((i) => i.name).join(", ")}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setInstalling(null);
    }
  }

  // Search as the user types (debounced); empty query = Modrinth's most
  // popular mods for this loader + MC version, a sensible starting page.
  useEffect(() => {
    const t = window.setTimeout(async () => {
      setSearching(true);
      try {
        const res = await searchContent({
          query,
          loader: server.type,
          mc_version: server.mc_version,
        });
        setHits(res.hits);
        setError(null);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => window.clearTimeout(t);
  }, [query, server.type, server.mc_version]);

  return (
    <div
      className="fixed inset-0 z-10 bg-black/60 flex items-start justify-center p-6 overflow-y-auto"
      onClick={onClose}
    >
      <div
        className="bg-slate-900 border border-slate-700 rounded-lg w-full max-w-2xl p-4 space-y-3"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium flex-1">
            Browse Modrinth <span className="text-slate-500">
              — {server.type} · MC {server.mc_version}
            </span>
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200 text-sm">
            ✕ Close
          </button>
        </div>

        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search mods…"
          className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm"
        />
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={includeOptional}
            onChange={(e) => setIncludeOptional(e.target.checked)}
          />
          Also install optional dependencies
        </label>

        {error && <p className="text-sm text-red-400">{error}</p>}
        {installNotice && <p className="text-xs text-sky-400">{installNotice}</p>}
        {searching && <p className="text-xs text-slate-500">Searching…</p>}

        <ul className="divide-y divide-slate-800">
          {(hits ?? []).map((hit) => {
            const installed = installedProjects.has(hit.project_id);
            return (
              <li key={hit.project_id} className="flex items-center gap-3 py-2.5">
                {hit.icon_url ? (
                  <img src={hit.icon_url} alt="" className="w-8 h-8 rounded shrink-0" />
                ) : (
                  <div className="w-8 h-8 rounded bg-slate-800 shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  <p className="text-sm truncate">
                    {hit.title}{" "}
                    <span className="text-xs text-slate-500">
                      {hit.downloads.toLocaleString()} downloads
                    </span>
                  </p>
                  <p className="text-xs text-slate-500 truncate">{hit.description}</p>
                </div>
                <button
                  onClick={() => install(hit)}
                  disabled={installing !== null || installed}
                  className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-2.5 py-1 text-xs font-medium text-slate-900 shrink-0"
                >
                  {installed
                    ? "Installed"
                    : installing === hit.project_id
                      ? "Installing…"
                      : "Install"}
                </button>
              </li>
            );
          })}
          {hits && hits.length === 0 && !searching && (
            <li className="py-3 text-sm text-slate-500">No results.</li>
          )}
        </ul>
      </div>
    </div>
  );
}
