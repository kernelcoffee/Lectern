// Change a server's Minecraft version (M9.5, F-SM-9).
//
// Lives in the Properties tab. The server must be stopped. The user picks a
// target version (catalog is newest-first) and, for loader server types, an
// optional loader build ("auto" = newest for the target). A pre-change backup
// is offered by default because Minecraft upgrades the world in place and
// one-way at the next start. Selecting a version older than the current one is
// a downgrade: Minecraft can't do it, so we demand an explicit checkbox
// acknowledging the world may be unusable. On success we render the migration
// report (what content was updated / disabled / regenerated / kept) and hand
// the refreshed server up so the page header reflects the new version.

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../../api/client";
import { getFabricLoaders, getMinecraftVersions } from "../../api/catalog";
import {
  changeServerVersion,
  MigrationReport,
  previewVersionChange,
  ServerDetail,
} from "../../api/servers";
import { useToast } from "../../components/Toasts";

// Server types that carry a loader build (the backend registry only implements
// Fabric today; vanilla has none).
const LOADER_TYPES: ServerDetail["type"][] = ["fabric", "quilt"];

export default function ChangeVersionCard({
  server,
  onServerUpdate,
}: {
  server: ServerDetail;
  onServerUpdate: (s: ServerDetail) => void;
}) {
  const [versions, setVersions] = useState<string[] | null>(null);
  const [target, setTarget] = useState(server.mc_version);
  const [loaders, setLoaders] = useState<string[] | null>(null);
  const [loader, setLoader] = useState<string>(""); // "" = auto (newest)
  const [backupFirst, setBackupFirst] = useState(true);
  const [ackDowngrade, setAckDowngrade] = useState(false);
  const [busy, setBusy] = useState(false);
  // Catalog-load failures only — the change itself reports through toasts.
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  const [report, setReport] = useState<MigrationReport | null>(null);
  // Pre-flight compatibility check for the selected target (null while
  // loading or when the target is the current version).
  const [preview, setPreview] = useState<MigrationReport | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const hasLoader = LOADER_TYPES.includes(server.type);
  const stopped = !server.running && server.status !== "starting" &&
    server.status !== "stopping";

  // Load the target version list for this server type.
  useEffect(() => {
    let live = true;
    getMinecraftVersions(server.type)
      .then((vs) => live && setVersions(vs))
      .catch((e) => live && setError(errorMessage(e)));
    return () => {
      live = false;
    };
  }, [server.type]);

  // Load loader builds for the chosen target (loader types only).
  useEffect(() => {
    if (!hasLoader) return;
    let live = true;
    setLoaders(null);
    setLoader(""); // reset to auto when the version changes
    getFabricLoaders(target)
      .then((ls) => live && setLoaders(ls))
      .catch(() => live && setLoaders([]));
    return () => {
      live = false;
    };
  }, [hasLoader, target]);

  // Check installed-content compatibility as soon as a target is picked, so
  // "these mods have no build for X and will be disabled" is visible before
  // the user commits to the upgrade.
  useEffect(() => {
    setPreview(null);
    if (target === server.mc_version) return;
    let live = true;
    setPreviewLoading(true);
    previewVersionChange(server.id, target)
      .then((r) => live && setPreview(r))
      .catch(() => live && setPreview(null)) // advisory only — never blocks
      .finally(() => live && setPreviewLoading(false));
    return () => {
      live = false;
    };
  }, [server.id, server.mc_version, target]);

  // Downgrade = target sits below the current version in the newest-first list.
  const isDowngrade = useMemo(() => {
    if (!versions) return false;
    const cur = versions.indexOf(server.mc_version);
    const tgt = versions.indexOf(target);
    return cur !== -1 && tgt !== -1 && tgt > cur;
  }, [versions, server.mc_version, target]);

  const unchanged = target === server.mc_version;
  const blocked = busy || !stopped || unchanged || (isDowngrade && !ackDowngrade);

  async function submit() {
    setBusy(true);
    setReport(null);
    try {
      const res = await changeServerVersion(server.id, {
        mc_version: target,
        loader_version: hasLoader && loader ? loader : null,
        allow_downgrade: isDowngrade,
        backup_first: backupFirst,
      });
      setReport(res.report);
      onServerUpdate(res.server);
      setAckDowngrade(false);
      toast.success(`Version changed to ${res.server.mc_version}.`);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-slate-800 p-4">
      <div>
        <h3 className="text-sm font-semibold text-slate-200">Minecraft version</h3>
        <p className="text-xs text-slate-500">
          Currently {server.type} · MC {server.mc_version}
          {server.loader_version && ` · ${server.loader_version}`}. The world is
          upgraded in place and one-way at the next start.
        </p>
      </div>

      {!stopped && (
        <p className="text-xs text-amber-400">
          Stop the server before changing its version.
        </p>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-400">
          Target version
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            disabled={!versions || busy}
            className="mt-1 block w-48 rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100 disabled:opacity-50"
          >
            {versions === null ? (
              <option>Loading…</option>
            ) : (
              versions.map((v) => (
                <option key={v} value={v}>
                  {v}
                  {v === server.mc_version ? " (current)" : ""}
                </option>
              ))
            )}
          </select>
        </label>

        {hasLoader && (
          <label className="text-xs text-slate-400">
            Loader build
            <select
              value={loader}
              onChange={(e) => setLoader(e.target.value)}
              disabled={!loaders || busy}
              className="mt-1 block w-48 rounded bg-slate-800 px-2 py-1.5 text-sm text-slate-100 disabled:opacity-50"
            >
              <option value="">Newest (auto)</option>
              {(loaders ?? []).map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {/* Pre-flight compatibility check for the selected target. */}
      {!unchanged && previewLoading && (
        <p className="text-xs text-slate-500">
          Checking installed content against {target}…
        </p>
      )}
      {!unchanged && preview && <PreviewView target={target} preview={preview} />}

      <label className="flex items-center gap-2 text-xs text-slate-300">
        <input
          type="checkbox"
          checked={backupFirst}
          onChange={(e) => setBackupFirst(e.target.checked)}
          disabled={busy}
        />
        Back up the server first (recommended)
      </label>

      {isDowngrade && (
        <label className="flex items-start gap-2 rounded border border-red-800/70 bg-red-950/30 p-2 text-xs text-red-200">
          <input
            type="checkbox"
            checked={ackDowngrade}
            onChange={(e) => setAckDowngrade(e.target.checked)}
            disabled={busy}
            className="mt-0.5"
          />
          <span>
            <strong>Downgrade.</strong> Minecraft cannot move a world to an older
            version — after this the world may be unusable. The supported way
            back is restoring a backup. I understand.
          </span>
        </label>
      )}

      <button
        onClick={submit}
        disabled={blocked}
        className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {busy
          ? "Changing…"
          : unchanged
            ? "Pick a different version"
            : `Change to ${target}`}
      </button>

      {error && <p className="text-xs text-red-400">{error}</p>}

      {report && <ReportView report={report} />}
    </section>
  );
}

// Advisory summary of the dry-run check: what would update, what has no build
// for the target (and would be disabled), what regenerates / stays as-is.
function PreviewView({
  target,
  preview,
}: {
  target: string;
  preview: MigrationReport;
}) {
  const total =
    preview.updated.length +
    preview.incompatible.length +
    preview.regenerated.length +
    preview.kept.length;
  if (total === 0) return null; // nothing installed — no need for a verdict

  return (
    <div className="space-y-1.5 rounded border border-slate-800 bg-slate-900/60 p-3 text-xs">
      {preview.incompatible.length > 0 ? (
        <p className="font-medium text-amber-300">
          {preview.incompatible.length} of {total} installed item
          {total === 1 ? "" : "s"} ha{preview.incompatible.length === 1 ? "s" : "ve"}{" "}
          no build for {target} and will be <strong>disabled</strong>:
        </p>
      ) : (
        <p className="font-medium text-emerald-300">
          All installed content is available for {target}.
        </p>
      )}
      {preview.incompatible.length > 0 && (
        <p className="text-amber-200/80">{preview.incompatible.join(", ")}</p>
      )}
      {preview.updated.length > 0 && (
        <p className="text-slate-400">
          Will update ({preview.updated.length}): {preview.updated.join(", ")}
        </p>
      )}
      {preview.regenerated.length > 0 && (
        <p className="text-slate-400">
          Will regenerate: {preview.regenerated.join(", ")}
        </p>
      )}
      {preview.kept.length > 0 && (
        <p className="text-slate-500">
          Kept as-is ({preview.kept.length}): {preview.kept.join(", ")} — modpack
          files move by reimporting the pack.
        </p>
      )}
    </div>
  );
}

function ReportView({ report }: { report: MigrationReport }) {
  const groups: { label: string; items: string[]; className: string }[] = [
    { label: "Updated", items: report.updated, className: "text-emerald-300" },
    {
      label: "Disabled (no compatible build)",
      items: report.incompatible,
      className: "text-amber-300",
    },
    { label: "Regenerated", items: report.regenerated, className: "text-sky-300" },
    {
      label: "Kept (reimport the modpack to move these)",
      items: report.kept,
      className: "text-slate-400",
    },
  ];
  const any = groups.some((g) => g.items.length > 0);
  return (
    <div className="space-y-2 rounded border border-slate-800 bg-slate-900/60 p-3 text-xs">
      <p className="font-semibold text-slate-200">Version changed. Migration report:</p>
      {!any && <p className="text-slate-400">No installed content to migrate.</p>}
      {groups
        .filter((g) => g.items.length > 0)
        .map((g) => (
          <div key={g.label}>
            <p className={`font-medium ${g.className}`}>
              {g.label} ({g.items.length})
            </p>
            <p className="text-slate-400">{g.items.join(", ")}</p>
          </div>
        ))}
    </div>
  );
}
