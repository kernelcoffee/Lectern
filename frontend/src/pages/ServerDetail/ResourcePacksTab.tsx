// Resource Packs tab — the installed list; all ingestion (Modrinth browse,
// Vanilla Tweaks generation, upload) lives in the shared ContentBrowser.
//
// "Use in game" points the server's resource-pack/resource-pack-sha1 at the
// pack (served by the backend) so clients get the download prompt; one pack
// at a time, applies at next start. The active one is derived from the
// server.properties URL (it embeds the item id).

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  ContentItem,
  listContent,
  removeContent,
  serveResourcePack,
  SOURCE_LABEL,
} from "../../api/content";
import { getProperties, ServerDetail } from "../../api/servers";
import { useToast } from "../../components/Toasts";
import ContentBrowser from "./ContentBrowser";

export default function ResourcePacksTab({
  serverId,
  server,
}: {
  serverId: string;
  server: ServerDetail;
}) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [servedId, setServedId] = useState<string | null>(null);
  // Load failures only — action outcomes go through toasts.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      const all = await listContent(serverId);
      setItems(all.filter((i) => i.kind === "resourcepack"));
      // Which pack is the in-game one? The resource-pack URL embeds the item id.
      try {
        const props = await getProperties(serverId);
        const url = props.properties["resource-pack"] ?? "";
        const match = url.match(/\/content\/([0-9a-f]{32})\/file/);
        setServedId(match ? match[1] : null);
      } catch {
        setServedId(null);
      }
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

  const remove = (item: ContentItem) =>
    run(item.id, async () => {
      await removeContent(serverId, item.id);
      await refresh();
    });

  const toggleServe = (item: ContentItem) =>
    run(item.id, async () => {
      const enable = servedId !== item.id;
      await serveResourcePack(serverId, item.id, enable);
      toast.success(
        enable
          ? `${item.name} will be offered to players (next start).`
          : "In-game resource-pack prompt removed (next start).",
      );
      await refresh();
    });

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300 flex-1">
          Installed packs{" "}
          {items && <span className="text-slate-500">({items.length})</span>}
        </h3>
        <button
          onClick={() => setBrowsing(true)}
          className="bg-emerald-600 hover:bg-emerald-500 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          Add packs
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {!items ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-500">
          No resource packs yet — add some from Modrinth, Vanilla Tweaks, or an
          upload.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
              <div className="w-full sm:w-auto sm:flex-1 min-w-0">
                <p className="text-sm truncate">
                  {item.name}{" "}
                  <span className="text-xs text-slate-500">
                    {SOURCE_LABEL[item.source] ?? item.source}
                    {item.version_number && ` · ${item.version_number}`}
                  </span>
                  {servedId === item.id && (
                    <span className="ml-2 text-xs text-emerald-400">
                      offered in game
                    </span>
                  )}
                </p>
                <p className="text-xs text-slate-500 truncate">{item.filename}</p>
              </div>
              <button
                onClick={() => toggleServe(item)}
                disabled={busy !== null}
                className={
                  "rounded px-2.5 py-1 text-xs disabled:opacity-50 " +
                  (servedId === item.id
                    ? "bg-slate-700 hover:bg-slate-600"
                    : "bg-sky-700 hover:bg-sky-600")
                }
              >
                {servedId === item.id ? "Stop offering" : "Use in game"}
              </button>
              <button
                onClick={() => remove(item)}
                disabled={busy !== null}
                className="bg-red-900/60 hover:bg-red-800 disabled:opacity-50 rounded px-2.5 py-1 text-xs text-red-200"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-slate-500">
        The in-game prompt takes effect the next time the server starts.
      </p>

      {browsing && (
        <ContentBrowser
          server={server}
          initialType="resourcepack"
          onChanged={refresh}
          onClose={() => setBrowsing(false)}
        />
      )}
    </section>
  );
}
