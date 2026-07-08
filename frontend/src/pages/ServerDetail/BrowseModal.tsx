// Modrinth browse/install modal, shared by the Mods and Resource Packs tabs.
// The caller scopes it with `projectType` (+ `loader` for mods); results are
// always compatible with the server. Install feedback (busy/error/success)
// renders inside the modal — the tab behind it isn't visible.

import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { searchContent, SearchHit } from "../../api/content";
import { ServerDetail } from "../../api/servers";

export default function BrowseModal({
  server,
  projectType,
  loader,
  installedProjects,
  onInstall,
  onClose,
  extraControls,
}: {
  server: ServerDetail;
  projectType: string; // "mod" | "resourcepack"
  loader?: string; // omit for loaderless kinds
  installedProjects: Set<string>;
  onInstall: (hit: SearchHit) => Promise<{ name: string }[]>;
  onClose: () => void;
  /** Optional row under the search box (e.g. the optional-deps checkbox). */
  extraControls?: React.ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null); // project_id
  const [installNotice, setInstallNotice] = useState<string | null>(null);

  async function install(hit: SearchHit) {
    setInstalling(hit.project_id);
    setError(null);
    setInstallNotice(null);
    try {
      const installed = await onInstall(hit);
      setInstallNotice(`Installed: ${installed.map((i) => i.name).join(", ")}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setInstalling(null);
    }
  }

  // Search as the user types (debounced); empty query = Modrinth's most
  // popular items for this scope, a sensible starting page.
  useEffect(() => {
    const t = window.setTimeout(async () => {
      setSearching(true);
      try {
        const res = await searchContent({
          query,
          project_type: projectType,
          loader,
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
  }, [query, projectType, loader, server.mc_version]);

  return (
    <div
      className="fixed inset-0 z-10 bg-black/60 flex items-start justify-center p-6 overflow-y-auto"
      onClick={onClose}
    >
      <div
        data-testid="browse-modal"
        className="bg-slate-900 border border-slate-700 rounded-lg w-full max-w-2xl p-4 space-y-3"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium flex-1">
            Browse Modrinth{" "}
            <span className="text-slate-500">
              — {projectType === "resourcepack" ? "resource packs" : server.type} · MC{" "}
              {server.mc_version}
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
          placeholder={
            projectType === "resourcepack" ? "Search resource packs…" : "Search mods…"
          }
          className="w-full bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm"
        />
        {extraControls}

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
