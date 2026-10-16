// Project details side panel — opened when a Modrinth item is clicked in the
// Mods / Datapacks / Resource packs tabs or in the content browser's results.
// It renders beside the list (the parent lays the two out; on phones it
// stacks above the list) rather than as a popup, so the list stays usable.
//
// Shows the project's icon, title, summary, stats, categories and links
// (Modrinth page, source, issues, wiki, Discord — all in a new tab), the
// versions compatible with this server (loader + MC version, newest first),
// and the full description (Modrinth markdown, rendered then sanitized).
//
// Versions are actionable:
//   * installed item  → "Switch" to any other build, newer or older (a
//     rollback). Choosing a build PINS the item: update checks and the
//     update_mods schedule skip it until it is unpinned here or via a plain
//     Update. The current build is marked.
//   * not installed   → "Install" at a chosen build (also pinned) — the
//     browser's plain Install button remains the "newest by channel" path.
//
// The parent owns the installed list: after any action it refreshes and the
// `installed` prop re-renders this panel with the new state.

import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../../api/client";
import {
  applyContentUpdate,
  ContentItem,
  getProjectDetails,
  getProjectVersions,
  installContent,
  patchContent,
  ProjectDetails,
  ProjectVersion,
} from "../../api/content";
import { ServerDetail } from "../../api/servers";
import { useToast } from "../../components/Toasts";

export type DetailsKind = "mod" | "resourcepack" | "datapack";

// Every link in the rendered description opens in a new tab and never gets
// a referrer/opener back to Lectern.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function renderMarkdown(body: string): string {
  const html = marked.parse(body, { async: false, gfm: true, breaks: false });
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
}

const VERSION_TYPE_CLASS: Record<ProjectVersion["version_type"], string> = {
  release: "text-emerald-300",
  beta: "text-amber-300",
  alpha: "text-red-300",
};

export default function ProjectDetailsPanel({
  server,
  projectId,
  kind,
  installed,
  onChanged,
  onClose,
  className = "",
}: {
  server: ServerDetail;
  projectId: string;
  kind: DetailsKind;
  /** The installed item for this project on this server, if any. */
  installed: ContentItem | null;
  onChanged: () => Promise<void> | void;
  onClose: () => void;
  /** Sizing/placement classes from the parent (width, sticky, max height). */
  className?: string;
}) {
  const [details, setDetails] = useState<ProjectDetails | null>(null);
  const [versions, setVersions] = useState<ProjectVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // version id or "pin"
  const toast = useToast();

  // Which loader facet the versions must satisfy: the server type for mods
  // (expanded to its compatibility chain server-side), the "datapack"
  // pseudo-loader for datapack builds, none for resource packs.
  const loader =
    kind === "mod" ? server.type : kind === "datapack" ? "datapack" : undefined;

  useEffect(() => {
    let live = true;
    setDetails(null);
    setVersions(null);
    setError(null);
    getProjectDetails(projectId)
      .then((d) => live && setDetails(d))
      .catch((e) => live && setError(errorMessage(e)));
    getProjectVersions(projectId, { loader, mc_version: server.mc_version })
      .then((vs) => live && setVersions(vs))
      .catch(() => live && setVersions([]));
    return () => {
      live = false;
    };
  }, [projectId, loader, server.mc_version]);

  const html = useMemo(
    () => (details?.body ? renderMarkdown(details.body) : ""),
    [details],
  );

  async function act(key: string, fn: () => Promise<void>) {
    setBusy(key);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const switchTo = (v: ProjectVersion) =>
    act(v.id, async () => {
      if (!installed) return;
      await applyContentUpdate(server.id, installed.id, v.id);
      toast.success(
        `${installed.name} switched to ${v.version_number} and pinned. Applies at next start.`,
      );
    });

  const installAt = (v: ProjectVersion) =>
    act(v.id, async () => {
      const items = await installContent(server.id, {
        project_id: projectId,
        version_id: v.id,
        kind: kind === "datapack" ? "datapack" : undefined,
      });
      toast.success(`Installed: ${items.map((i) => i.name).join(", ")}`);
    });

  const setPinned = (pinned: boolean) =>
    act("pin", async () => {
      if (!installed) return;
      await patchContent(server.id, installed.id, { pinned });
      toast.info(
        pinned
          ? `${installed.name} pinned — updates will skip it.`
          : `${installed.name} unpinned — updates apply again.`,
      );
    });

  const links: { label: string; url: string | null | undefined }[] = [
    { label: "Modrinth page", url: details?.page_url },
    { label: "Source", url: details?.source_url },
    { label: "Issues", url: details?.issues_url },
    { label: "Wiki", url: details?.wiki_url },
    { label: "Discord", url: details?.discord_url },
  ];

  return (
    <aside
      data-testid="project-details"
      aria-label="Project details"
      className={`flex min-h-0 flex-col rounded-lg border border-slate-700 bg-slate-900 ${className}`}
    >
      <div className="flex items-center gap-3 border-b border-slate-800 px-3 py-2.5">
        {details?.icon_url ? (
          <img src={details.icon_url} alt="" className="w-8 h-8 rounded-sm shrink-0" />
        ) : (
          <div className="w-8 h-8 rounded-sm bg-slate-800 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-medium truncate">
            {details?.title ?? "Loading…"}
          </h3>
          {installed ? (
            <p className="text-xs text-slate-500 truncate">
              installed · {installed.version_number ?? installed.version_id}
              {installed.pinned && (
                <span className="ml-1 text-amber-300" title="Update checks skip this item">
                  · pinned
                </span>
              )}
            </p>
          ) : (
            details && (
              <p className="text-xs text-slate-500 truncate">{details.description}</p>
            )
          )}
        </div>
        <button
          onClick={onClose}
          aria-label="Close details"
          title="Close details"
          className="rounded-sm px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
        >
          ✕
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 space-y-4">
        {error && <p className="text-sm text-red-400">{error}</p>}

        {details && installed && (
          <p className="text-xs text-slate-400">{details.description}</p>
        )}

        {details && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
            <span>{details.downloads.toLocaleString()} downloads</span>
            <span>{details.followers.toLocaleString()} followers</span>
            {details.license?.name && <span>{details.license.name}</span>}
            <span>Updated {new Date(details.updated).toLocaleDateString()}</span>
            {details.categories.length > 0 && (
              <span className="text-slate-500">{details.categories.join(" · ")}</span>
            )}
          </div>
        )}

        <div className="flex flex-wrap gap-1.5">
          {links
            .filter((l) => l.url)
            .map((l) => (
              <a
                key={l.label}
                href={l.url!}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:border-slate-500"
              >
                {l.label} ↗
              </a>
            ))}
          {installed && (
            <button
              onClick={() => setPinned(!installed.pinned)}
              disabled={busy !== null}
              className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:border-slate-500 disabled:opacity-50"
            >
              {installed.pinned ? "Unpin (follow updates)" : "Pin current version"}
            </button>
          )}
        </div>

        <section>
          <h4 className="text-xs font-medium uppercase tracking-wider text-slate-500 mb-2">
            Versions for {server.type} · MC {server.mc_version}
          </h4>
          {versions === null ? (
            <p className="text-xs text-slate-500">Loading versions…</p>
          ) : versions.length === 0 ? (
            <p className="text-xs text-slate-500">No compatible versions.</p>
          ) : (
            <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800 max-h-56 overflow-y-auto">
              {versions.map((v) => {
                const current = installed?.version_id === v.id;
                return (
                  <li key={v.id} className="flex items-center gap-2 px-2.5 py-1.5">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm truncate">
                        {v.version_number}
                        <span className={`ml-2 text-xs ${VERSION_TYPE_CLASS[v.version_type] ?? ""}`}>
                          {v.version_type}
                        </span>
                        {current && (
                          <span className="ml-2 text-xs text-sky-300">current</span>
                        )}
                      </p>
                      <p className="text-xs text-slate-500 truncate">
                        {new Date(v.date_published).toLocaleDateString()} ·{" "}
                        {v.downloads.toLocaleString()} downloads
                      </p>
                    </div>
                    {installed ? (
                      <button
                        onClick={() => switchTo(v)}
                        disabled={busy !== null || current}
                        className="bg-slate-700 hover:bg-slate-600 disabled:opacity-40 rounded-sm px-2.5 py-1 text-xs shrink-0"
                      >
                        {busy === v.id ? "…" : current ? "Current" : "Switch"}
                      </button>
                    ) : (
                      <button
                        onClick={() => installAt(v)}
                        disabled={busy !== null}
                        className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 rounded-sm px-2.5 py-1 text-xs font-medium text-slate-900 shrink-0"
                      >
                        {busy === v.id ? "Installing…" : "Install"}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {installed && (
            <p className="mt-1.5 text-xs text-slate-500">
              Switching to a build pins the item so updates leave it alone;
              a plain Update from the list moves to the newest and unpins.
            </p>
          )}
        </section>

        {details && (
          <section>
            <h4 className="text-xs font-medium uppercase tracking-wider text-slate-500 mb-2">
              Description
            </h4>
            {html ? (
              <div
                className="markdown text-sm text-slate-300"
                dangerouslySetInnerHTML={{ __html: html }}
              />
            ) : (
              <p className="text-xs text-slate-500">No description.</p>
            )}
          </section>
        )}
      </div>
    </aside>
  );
}
