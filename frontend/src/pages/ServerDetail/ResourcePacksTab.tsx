// Resource Packs tab (M7) — installed packs + three ways in:
//   1. Browse Modrinth (project_type=resourcepack, loaderless — works on
//      every server type, vanilla included).
//   2. Vanilla Tweaks: paste a share code, or open the category picker and
//      tick packs; Generate builds the zip server-side. A server keeps one
//      generated VT pack — regenerating replaces it, an unchanged selection
//      is a no-op (backend fingerprint).
//   3. Upload a pack zip (validated via its pack.mcmeta).
//
// "Use in game" points the server's resource-pack/resource-pack-sha1 at the
// pack (served by the backend) so clients get the download prompt; one pack
// at a time, applies at next start. The active one is derived from the
// server.properties URL (it embeds the item id).

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  ContentItem,
  getVtCategories,
  installContent,
  installVanillaTweaks,
  listContent,
  removeContent,
  SearchHit,
  serveResourcePack,
  uploadResourcePack,
  VtCategory,
} from "../../api/content";
import { getProperties, ServerDetail } from "../../api/servers";
import BrowseModal from "./BrowseModal";

const SOURCE_LABEL: Record<string, string> = {
  modrinth: "Modrinth",
  vanillatweaks: "Vanilla Tweaks",
  upload: "Uploaded",
};

export default function ResourcePacksTab({
  serverId,
  server,
}: {
  serverId: string;
  server: ServerDetail;
}) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [servedId, setServedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

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
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const installFromModrinth = async (hit: SearchHit) => {
    const installed = await installContent(serverId, { project_id: hit.project_id });
    await refresh();
    return installed;
  };

  const upload = (file: File) =>
    run("upload", async () => {
      const item = await uploadResourcePack(serverId, file);
      setNotice(`Uploaded: ${item.name}`);
      await refresh();
    });

  const remove = (item: ContentItem) =>
    run(item.id, async () => {
      await removeContent(serverId, item.id);
      await refresh();
    });

  const toggleServe = (item: ContentItem) =>
    run(item.id, async () => {
      const enable = servedId !== item.id;
      await serveResourcePack(serverId, item.id, enable);
      setNotice(
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
          onClick={() => fileInput.current?.click()}
          disabled={busy !== null}
          className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded px-3 py-1.5 text-sm"
        >
          {busy === "upload" ? "Uploading…" : "Upload zip"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".zip"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload(f);
            e.target.value = "";
          }}
        />
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
          No resource packs yet — browse Modrinth, generate a Vanilla Tweaks
          pack below, or upload a zip.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800">
          {items.map((item) => (
            <li key={item.id} className="flex items-center gap-3 p-3">
              <div className="flex-1 min-w-0">
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

      <VanillaTweaksSection
        serverId={serverId}
        onInstalled={async (item) => {
          setNotice(`Generated: ${item.name}`);
          await refresh();
        }}
      />

      <p className="text-xs text-slate-500">
        Server-side packs and the in-game prompt take effect the next time the
        server starts.
      </p>

      {browsing && (
        <BrowseModal
          server={server}
          projectType="resourcepack"
          installedProjects={new Set((items ?? []).map((i) => i.project_id ?? ""))}
          onInstall={installFromModrinth}
          onClose={() => setBrowsing(false)}
        />
      )}
    </section>
  );
}

// --- Vanilla Tweaks ----------------------------------------------------------

function VanillaTweaksSection({
  serverId,
  onInstalled,
}: {
  serverId: string;
  onInstalled: (item: ContentItem) => Promise<void>;
}) {
  const [shareCode, setShareCode] = useState("");
  const [categories, setCategories] = useState<VtCategory[] | null>(null);
  const [loadingCats, setLoadingCats] = useState(false);
  const [selection, setSelection] = useState<Record<string, Set<string>>>({});
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedCount = Object.values(selection).reduce((n, s) => n + s.size, 0);

  async function loadCategories() {
    setLoadingCats(true);
    setError(null);
    try {
      setCategories((await getVtCategories(serverId)).categories);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoadingCats(false);
    }
  }

  function togglePack(category: string, pack: string) {
    setSelection((prev) => {
      const next = { ...prev };
      const set = new Set(next[category] ?? []);
      if (set.has(pack)) set.delete(pack);
      else set.add(pack);
      next[category] = set;
      return next;
    });
  }

  async function generate(body: {
    share_code?: string;
    packs?: Record<string, string[]>;
  }) {
    setBusy(true);
    setError(null);
    try {
      const item = await installVanillaTweaks(serverId, body);
      await onInstalled(item);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const generateFromSelection = () =>
    generate({
      packs: Object.fromEntries(
        Object.entries(selection).map(([c, s]) => [c.toLowerCase(), [...s]]),
      ),
    });

  return (
    <section className="rounded-lg border border-slate-800 p-4 space-y-3">
      <h3 className="text-sm font-medium text-slate-300">
        Vanilla Tweaks{" "}
        <span className="text-xs font-normal text-slate-500">
          — generate a combined pack from vanillatweaks.net
        </span>
      </h3>

      {/* Share code — the fast path. */}
      <div className="flex items-center gap-2">
        <input
          value={shareCode}
          onChange={(e) => setShareCode(e.target.value)}
          placeholder="Share code (e.g. from a vanillatweaks.net selection)"
          className="flex-1 max-w-sm bg-slate-800 border border-slate-700 rounded px-2.5 py-1.5 text-sm"
        />
        <button
          onClick={() => generate({ share_code: shareCode.trim() })}
          disabled={busy || shareCode.trim() === ""}
          className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          {busy ? "Generating…" : "Generate"}
        </button>
      </div>

      {/* Category picker — browse and tick. */}
      {!categories ? (
        <button
          onClick={loadCategories}
          disabled={loadingCats}
          className="text-xs text-slate-400 hover:text-slate-200 underline disabled:opacity-50"
        >
          {loadingCats ? "Loading categories…" : "…or browse the pack categories"}
        </button>
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {categories.map((cat) => (
              <button
                key={cat.category}
                onClick={() =>
                  setOpenCategory(openCategory === cat.category ? null : cat.category)
                }
                className={
                  "px-2.5 py-1 rounded-full text-xs " +
                  (openCategory === cat.category
                    ? "bg-emerald-600 text-slate-900 font-medium"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700")
                }
              >
                {cat.category}
                {(selection[cat.category]?.size ?? 0) > 0 &&
                  ` (${selection[cat.category]!.size})`}
              </button>
            ))}
          </div>
          {openCategory && (
            <div className="max-h-56 overflow-y-auto rounded border border-slate-800 p-3 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
              {flatPacks(
                categories.find((c) => c.category === openCategory),
              ).map((pack) => (
                <label
                  key={pack.name}
                  className="flex items-center gap-2 text-xs text-slate-300"
                  title={pack.description}
                >
                  <input
                    type="checkbox"
                    checked={selection[openCategory]?.has(pack.name) ?? false}
                    onChange={() => togglePack(openCategory, pack.name)}
                  />
                  <span className="truncate">{pack.display}</span>
                </label>
              ))}
            </div>
          )}
          {selectedCount > 0 && (
            <button
              onClick={generateFromSelection}
              disabled={busy}
              className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
            >
              {busy ? "Generating…" : `Generate pack (${selectedCount} tweaks)`}
            </button>
          )}
        </div>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}
    </section>
  );
}

/** Flatten a category's packs including one level of subcategories. */
function flatPacks(category: VtCategory | undefined): {
  name: string;
  display: string;
  description?: string;
}[] {
  if (!category) return [];
  const own = category.packs ?? [];
  const nested = (category.categories ?? []).flatMap((sub) =>
    (sub.packs ?? []).map((p) => ({ ...p, display: `${sub.category}: ${p.display}` })),
  );
  return [...own, ...nested];
}
