// Datapacks tab — server-side gameplay content living in the world's
// datapacks folder ({level-name}/datapacks). Sources: Modrinth datapacks,
// Vanilla Tweaks datapacks + crafting tweaks (both land here — a crafting-
// tweaks zip IS a datapack), and direct uploads; all via the shared
// ContentBrowser.
//
// Enable/disable renames the zip (Minecraft only loads *.zip in the
// datapacks dir); changes apply the next time the server starts.

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import {
  ContentItem,
  listContent,
  patchContent,
  removeContent,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";
import ContentBrowser from "./ContentBrowser";

const SOURCE_LABEL: Record<string, string> = {
  modrinth: "Modrinth",
  vanillatweaks: "Vanilla Tweaks",
  upload: "Uploaded",
};

export default function DatapacksTab({
  serverId,
  server,
}: {
  serverId: string;
  server: ServerDetail;
}) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const all = await listContent(serverId);
      setItems(all.filter((i) => i.kind === "datapack"));
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

  const toggle = (item: ContentItem) =>
    run(item.id, async () => {
      await patchContent(serverId, item.id, { enabled: !item.enabled });
      await refresh();
    });

  const remove = (item: ContentItem) =>
    run(item.id, async () => {
      await removeContent(serverId, item.id);
      await refresh();
    });

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300 flex-1">
          Installed datapacks{" "}
          {items && <span className="text-slate-500">({items.length})</span>}
        </h3>
        <button
          onClick={() => setBrowsing(true)}
          className="bg-emerald-600 hover:bg-emerald-500 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          Add datapacks
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {!items ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-500">
          No datapacks yet — add gameplay tweaks from Modrinth or Vanilla
          Tweaks (datapacks & crafting tweaks), or upload a zip.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 p-3">
              <div className="w-full sm:w-auto sm:flex-1 min-w-0">
                <p className="text-sm truncate">
                  <span className={item.enabled ? "" : "text-slate-500 line-through"}>
                    {item.name}
                  </span>{" "}
                  <span className="text-xs text-slate-500">
                    {SOURCE_LABEL[item.source] ?? item.source}
                    {item.version_number && ` · ${item.version_number}`}
                  </span>
                </p>
                <p className="text-xs text-slate-500 truncate">{item.filename}</p>
              </div>
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
          ))}
        </ul>
      )}

      <p className="text-xs text-slate-500">
        Datapacks live in the world's datapacks folder and take effect the
        next time the server starts.
      </p>

      {browsing && (
        <ContentBrowser
          server={server}
          initialType="datapack"
          onChanged={refresh}
          onClose={() => setBrowsing(false)}
        />
      )}
    </section>
  );
}
