// Unified content browser (the "add content" popup shared by every content
// tab). Two orthogonal rails on the left pick WHAT and FROM WHERE:
//
//   PROVIDER  Modrinth | Vanilla Tweaks | Upload
//   TYPE      Mods (modded servers) | Resource packs | Datapacks
//             | Crafting tweaks (Vanilla Tweaks only)
//
// The right-hand pane adapts to the provider:
//   * Modrinth — search + sort + category chips (real Modrinth taxonomy) +
//     paginated results. Datapacks search project_type=datapack and install
//     with kind=datapack (Modrinth models them as mods with a "datapack"
//     pseudo-loader).
//   * Vanilla Tweaks — share-code input + category/checkbox picker for the
//     selected VT type; Generate replaces the server's previous VT set of
//     that type (unchanged selections are a backend fingerprint no-op).
//   * Upload — a zip drop for resource packs / datapacks (pack.mcmeta
//     validated server-side).
//
// The browser owns the full installed-items list (all kinds) so "Installed"
// states are correct whichever tab opened it; `onChanged` tells the opening
// tab to refresh its own list.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  ContentItem,
  getModrinthCategories,
  getVtCategories,
  installContent,
  installVanillaTweaks,
  listContent,
  searchContent,
  SearchHit,
  SortIndex,
  uploadResourcePack,
  VtCategory,
  VtPackType,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";

type Provider = "modrinth" | "vanillatweaks" | "upload";
export type BrowserType = "mod" | "resourcepack" | "datapack" | "craftingtweak";

const TYPE_LABEL: Record<BrowserType, string> = {
  mod: "Mods",
  resourcepack: "Resource packs",
  datapack: "Datapacks",
  craftingtweak: "Crafting tweaks",
};

const MODDED_TYPES = ["fabric", "quilt", "paper"];
const PAGE_SIZE = 20;

/** Which types each provider offers (crafting tweaks are VT-only; mods are
 * Modrinth-only and need a modded server). */
function typesFor(provider: Provider, server: ServerDetail): BrowserType[] {
  const modded = MODDED_TYPES.includes(server.type);
  switch (provider) {
    case "modrinth":
      return modded
        ? ["mod", "resourcepack", "datapack"]
        : ["resourcepack", "datapack"];
    case "vanillatweaks":
      return ["resourcepack", "datapack", "craftingtweak"];
    case "upload":
      return ["resourcepack", "datapack"];
  }
}

const VT_TYPE: Record<string, VtPackType> = {
  resourcepack: "resourcepacks",
  datapack: "datapacks",
  craftingtweak: "craftingtweaks",
};

export default function ContentBrowser({
  server,
  initialType,
  onChanged,
  onClose,
}: {
  server: ServerDetail;
  initialType: BrowserType;
  onChanged: () => Promise<void> | void;
  onClose: () => void;
}) {
  const [provider, setProvider] = useState<Provider>(
    initialType === "craftingtweak" ? "vanillatweaks" : "modrinth",
  );
  const [type, setType] = useState<BrowserType>(initialType);
  const [installed, setInstalled] = useState<ContentItem[]>([]);

  const refreshInstalled = useCallback(async () => {
    try {
      setInstalled(await listContent(server.id));
    } catch {
      // "Installed" badges degrade gracefully.
    }
  }, [server.id]);

  useEffect(() => {
    refreshInstalled();
  }, [refreshInstalled]);

  const changed = async () => {
    await refreshInstalled();
    await onChanged();
  };

  function pickProvider(p: Provider) {
    setProvider(p);
    const types = typesFor(p, server);
    if (!types.includes(type)) setType(types[0]);
  }

  return (
    <div
      className="fixed inset-0 z-10 bg-black/60 flex items-start justify-center p-6"
      onClick={onClose}
    >
      <div
        data-testid="content-browser"
        className="bg-slate-900 border border-slate-700 rounded-lg w-full max-w-5xl h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-800">
          <h3 className="text-sm font-medium flex-1">
            Add content{" "}
            <span className="text-slate-500">
              — {server.name} · {server.type} · MC {server.mc_version}
            </span>
          </h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200 text-sm">
            ✕ Close
          </button>
        </div>

        <div className="flex flex-1 min-h-0">
          {/* Left rails */}
          <aside className="w-44 shrink-0 border-r border-slate-800 py-3 space-y-5 overflow-y-auto">
            <RailGroup label="Provider">
              <RailItem
                label="Modrinth"
                active={provider === "modrinth"}
                onClick={() => pickProvider("modrinth")}
              />
              <RailItem
                label="Vanilla Tweaks"
                active={provider === "vanillatweaks"}
                onClick={() => pickProvider("vanillatweaks")}
              />
              <RailItem
                label="Upload"
                active={provider === "upload"}
                onClick={() => pickProvider("upload")}
              />
            </RailGroup>
            <RailGroup label="Type">
              {typesFor(provider, server).map((t) => (
                <RailItem
                  key={t}
                  label={TYPE_LABEL[t]}
                  active={type === t}
                  onClick={() => setType(t)}
                />
              ))}
            </RailGroup>
          </aside>

          {/* Provider pane */}
          <div className="flex-1 min-w-0 overflow-y-auto p-4">
            {provider === "modrinth" && (
              <ModrinthPane
                key={type}
                server={server}
                type={type}
                installed={installed}
                onChanged={changed}
              />
            )}
            {provider === "vanillatweaks" && (
              <VanillaTweaksPane
                key={type}
                server={server}
                vtType={VT_TYPE[type] ?? "resourcepacks"}
                onChanged={changed}
              />
            )}
            {provider === "upload" && (
              <UploadPane
                server={server}
                kind={type === "datapack" ? "datapack" : "resourcepack"}
                onChanged={changed}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function RailGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="px-4 pb-1 text-[11px] font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <ul>{children}</ul>
    </div>
  );
}

function RailItem({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <li>
      <button
        onClick={onClick}
        className={
          "w-full text-left px-4 py-1.5 text-sm border-l-2 " +
          (active
            ? "border-emerald-500 bg-slate-800/60 text-slate-100"
            : "border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/40")
        }
      >
        {label}
      </button>
    </li>
  );
}

// --- Modrinth ------------------------------------------------------------------

function ModrinthPane({
  server,
  type,
  installed,
  onChanged,
}: {
  server: ServerDetail;
  type: BrowserType;
  installed: ContentItem[];
  onChanged: () => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortIndex>("relevance");
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedCats, setSelectedCats] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [total, setTotal] = useState(0);
  const [searching, setSearching] = useState(false);
  const [includeOptional, setIncludeOptional] = useState(false);
  const [installing, setInstalling] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Modrinth's search project_type; the tag taxonomy files datapacks under
  // "mod" (they ARE mods with a pseudo-loader there).
  const projectType = type === "mod" ? "mod" : type;
  const tagType = type === "datapack" ? "mod" : projectType;
  const loader = type === "mod" ? server.type : undefined;

  useEffect(() => {
    getModrinthCategories(tagType)
      .then((tags) =>
        setCategories(
          tags.filter((t) => t.header === "categories").map((t) => t.name),
        ),
      )
      .catch(() => setCategories([]));
  }, [tagType]);

  // Search on any input change (debounced); page resets elsewhere.
  useEffect(() => {
    const t = window.setTimeout(async () => {
      setSearching(true);
      try {
        const res = await searchContent({
          query,
          project_type: projectType,
          loader,
          mc_version: server.mc_version,
          categories: selectedCats,
          index: sort,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        });
        setHits(res.hits);
        setTotal(res.total_hits);
        setError(null);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => window.clearTimeout(t);
  }, [query, projectType, loader, server.mc_version, selectedCats, sort, page]);

  const installedProjects = new Set(installed.map((i) => i.project_id ?? ""));
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  async function install(hit: SearchHit) {
    setInstalling(hit.project_id);
    setError(null);
    setNotice(null);
    try {
      const items = await installContent(server.id, {
        project_id: hit.project_id,
        include_optional_deps: includeOptional,
        kind: type === "datapack" ? "datapack" : undefined,
      });
      setNotice(`Installed: ${items.map((i) => i.name).join(", ")}`);
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setInstalling(null);
    }
  }

  function toggleCat(cat: string) {
    setPage(0);
    setSelectedCats((prev) =>
      prev.includes(cat) ? prev.filter((c) => c !== cat) : [...prev, cat],
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <input
          autoFocus
          value={query}
          onChange={(e) => {
            setPage(0);
            setQuery(e.target.value);
          }}
          placeholder={`Search ${TYPE_LABEL[type].toLowerCase()}…`}
          className="flex-1 bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm"
        />
        <select
          value={sort}
          onChange={(e) => {
            setPage(0);
            setSort(e.target.value as SortIndex);
          }}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm"
        >
          <option value="relevance">Relevance</option>
          <option value="downloads">Downloads</option>
          <option value="follows">Follows</option>
          <option value="newest">Newest</option>
          <option value="updated">Recently updated</option>
        </select>
      </div>

      {categories.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {categories.map((cat) => (
            <button
              key={cat}
              onClick={() => toggleCat(cat)}
              className={
                "px-2 py-0.5 rounded-full text-xs " +
                (selectedCats.includes(cat)
                  ? "bg-emerald-600 text-slate-900 font-medium"
                  : "bg-slate-800 text-slate-400 hover:text-slate-200")
              }
            >
              {cat}
            </button>
          ))}
        </div>
      )}

      {type === "mod" && (
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={includeOptional}
            onChange={(e) => setIncludeOptional(e.target.checked)}
          />
          Also install optional dependencies
        </label>
      )}

      {notice && <p className="text-xs text-sky-400">{notice}</p>}
      {error && <p className="text-sm text-red-400">{error}</p>}
      {searching && hits === null && (
        <p className="text-xs text-slate-500">Searching…</p>
      )}

      <ul className="divide-y divide-slate-800">
        {(hits ?? []).map((hit) => {
          const isInstalled = installedProjects.has(hit.project_id);
          return (
            <li key={hit.project_id} className="flex items-center gap-3 py-2.5">
              {hit.icon_url ? (
                <img src={hit.icon_url} alt="" className="w-9 h-9 rounded shrink-0" />
              ) : (
                <div className="w-9 h-9 rounded bg-slate-800 shrink-0" />
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
                disabled={installing !== null || isInstalled}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-2.5 py-1 text-xs font-medium text-slate-900 shrink-0"
              >
                {isInstalled
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

      {total > PAGE_SIZE && (
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="bg-slate-800 hover:bg-slate-700 disabled:opacity-40 rounded px-2.5 py-1"
          >
            ← Prev
          </button>
          <span>
            Page {page + 1} / {pages} · {total.toLocaleString()} results
          </span>
          <button
            onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
            disabled={page >= pages - 1}
            className="bg-slate-800 hover:bg-slate-700 disabled:opacity-40 rounded px-2.5 py-1"
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}

// --- Vanilla Tweaks --------------------------------------------------------------

function VanillaTweaksPane({
  server,
  vtType,
  onChanged,
}: {
  server: ServerDetail;
  vtType: VtPackType;
  onChanged: () => Promise<void>;
}) {
  const [shareCode, setShareCode] = useState("");
  const [categories, setCategories] = useState<VtCategory[] | null>(null);
  const [selection, setSelection] = useState<Record<string, Set<string>>>({});
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setCategories(null);
    setSelection({});
    setOpenCategory(null);
    getVtCategories(server.id, vtType)
      .then((d) => setCategories(d.categories))
      .catch((e) =>
        setError(e instanceof ApiError ? e.message : String(e)),
      );
  }, [server.id, vtType]);

  const selectedCount = Object.values(selection).reduce((n, s) => n + s.size, 0);

  async function generate(body: {
    share_code?: string;
    packs?: Record<string, string[]>;
  }) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const items = await installVanillaTweaks(server.id, {
        ...body,
        pack_type: vtType,
      });
      setNotice(
        items.length === 1
          ? `Generated: ${items[0].name}`
          : `Generated ${items.length} datapacks.`,
      );
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
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

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Generated by vanillatweaks.net — a server keeps one generated set per
        type; generating again replaces it.
        {vtType !== "resourcepacks" &&
          " Datapacks and crafting tweaks land in the world's datapacks folder."}
      </p>

      <div className="flex items-center gap-2">
        <input
          value={shareCode}
          onChange={(e) => setShareCode(e.target.value)}
          placeholder="Share code from vanillatweaks.net"
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

      {!categories ? (
        <p className="text-xs text-slate-500">Loading categories…</p>
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
            <div className="max-h-64 overflow-y-auto rounded border border-slate-800 p-3 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
              {flatPacks(categories.find((c) => c.category === openCategory)).map(
                (pack) => (
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
                ),
              )}
            </div>
          )}
          {selectedCount > 0 && (
            <button
              onClick={() =>
                generate({
                  packs: Object.fromEntries(
                    Object.entries(selection).map(([c, s]) => [
                      c.toLowerCase(),
                      [...s],
                    ]),
                  ),
                })
              }
              disabled={busy}
              className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded px-3 py-1.5 text-sm font-medium text-slate-900"
            >
              {busy ? "Generating…" : `Generate (${selectedCount} tweaks)`}
            </button>
          )}
        </div>
      )}

      {notice && <p className="text-xs text-sky-400">{notice}</p>}
      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
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

// --- Upload -----------------------------------------------------------------------

function UploadPane({
  server,
  kind,
  onChanged,
}: {
  server: ServerDetail;
  kind: "resourcepack" | "datapack";
  onChanged: () => Promise<void>;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function upload(file: File) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const item = await uploadResourcePack(server.id, file, kind);
      setNotice(`Uploaded: ${item.name}`);
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Upload a {kind === "datapack" ? "datapack" : "resource pack"} zip — it
        must contain a valid <code>pack.mcmeta</code>.
        {kind === "datapack" && " It lands in the world's datapacks folder."}
      </p>
      <button
        onClick={() => fileInput.current?.click()}
        disabled={busy}
        className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded px-4 py-2 text-sm font-medium text-slate-900"
      >
        {busy ? "Uploading…" : "Choose zip file…"}
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
      {notice && <p className="text-xs text-sky-400">{notice}</p>}
      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
