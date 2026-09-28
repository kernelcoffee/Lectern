// Datapacks tab — server-side gameplay content living in the world's
// datapacks folder ({level-name}/datapacks). Sources: Modrinth datapacks,
// Vanilla Tweaks datapacks + crafting tweaks (both land here — a crafting-
// tweaks zip IS a datapack), and direct uploads; all via the shared
// ContentBrowser.
//
// Enable/disable renames the zip (Minecraft only loads *.zip in the
// datapacks dir); changes apply the next time the server starts.

import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  ContentItem,
  listContent,
  patchContent,
  removeContent,
  SOURCE_LABEL,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";
import { useToast } from "../../components/Toasts";
import ContentBrowser from "./ContentBrowser";
import ProjectDetailsPanel from "./ProjectDetailsPanel";

export default function DatapacksTab({
  serverId,
  server,
}: {
  serverId: string;
  server: ServerDetail;
}) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  // Load failures only — action outcomes go through toasts.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  // Item whose project details popup is open (Modrinth datapacks only).
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const toast = useToast();
  const detailsItem = items?.find((i) => i.id === detailsId) ?? null;

  const refresh = useCallback(async () => {
    try {
      const all = await listContent(serverId);
      setItems(all.filter((i) => i.kind === "datapack"));
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
          className="bg-emerald-600 hover:bg-emerald-500 rounded-sm px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          Add datapacks
        </button>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* List + details panel side by side from lg up; stacked (panel first)
          on phones. */}
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
      <div className="min-w-0 flex-1 space-y-4">
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
            <li
              key={item.id}
              // The whole row opens details (Modrinth items only); the
              // controls inside stop the click from bubbling.
              onClick={
                item.source === "modrinth" && item.project_id
                  ? () => setDetailsId(item.id)
                  : undefined
              }
              title={item.source === "modrinth" && item.project_id ? "Show details" : undefined}
              className={
                "flex flex-wrap items-center gap-x-3 gap-y-2 p-3 " +
                (item.source === "modrinth" && item.project_id
                  ? "cursor-pointer hover:bg-slate-800/40 "
                  : "") +
                (detailsId === item.id ? "bg-slate-800/60" : "")
              }
            >
              <div className="w-full sm:w-auto sm:flex-1 min-w-0">
                {/* The name truncates; source and badges never do. */}
                <p className="flex min-w-0 items-baseline gap-2 text-sm">
                  <span
                    className={
                      "truncate " + (item.enabled ? "" : "text-slate-500 line-through")
                    }
                  >
                    {item.name}
                  </span>
                  <span className="shrink-0 text-xs text-slate-500">
                    {SOURCE_LABEL[item.source] ?? item.source}
                    {item.version_number && ` · ${item.version_number}`}
                  </span>
                  {item.pinned && (
                    <span
                      className="shrink-0 text-xs text-amber-300"
                      title="Pinned to this version — update checks skip it"
                    >
                      pinned
                    </span>
                  )}
                </p>
                <p className="text-xs text-slate-500 truncate">{item.filename}</p>
              </div>
              {item.source === "modrinth" && item.project_id && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setDetailsId(item.id);
                  }}
                  disabled={busy !== null}
                  className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded-sm px-2.5 py-1 text-xs"
                >
                  Details
                </button>
              )}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  toggle(item);
                }}
                disabled={busy !== null}
                className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded-sm px-2.5 py-1 text-xs"
              >
                {item.enabled ? "Disable" : "Enable"}
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  remove(item);
                }}
                disabled={busy !== null}
                className="bg-red-900/60 hover:bg-red-800 disabled:opacity-50 rounded-sm px-2.5 py-1 text-xs text-red-200"
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
      </div>

      {detailsItem?.project_id && (
        <ProjectDetailsPanel
          className="order-first lg:order-none lg:sticky lg:top-4 lg:w-[32.5rem] xl:w-[37.5rem] lg:shrink-0 lg:max-h-[calc(100dvh-2rem)]"
          server={server}
          projectId={detailsItem.project_id}
          kind="datapack"
          installed={detailsItem}
          onChanged={refresh}
          onClose={() => setDetailsId(null)}
        />
      )}
      </div>

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
