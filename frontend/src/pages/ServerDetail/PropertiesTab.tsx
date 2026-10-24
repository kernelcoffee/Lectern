// Properties tab: edits both kinds of server configuration.
//
// There are two distinct stores, each with its own form + save button:
//
//   1. "Lectern settings" — fields stored on the Server DB row (memory, JVM
//      args, port, auto-start, crash-restart, stop command/timeout). Saved via
//      PATCH /api/servers/{id}; only the fields the user changed are sent
//      (JSON-merge semantics). The parent passes the current ServerDetail in
//      and gets the updated one back through onServerUpdate so the whole page
//      stays consistent.
//
//   2. "server.properties" — Minecraft's own config file, fetched as a raw
//      key→value string map plus a `definitions` metadata map. For keys the
//      backend knows (definitions), we render a proper widget: toggle-style
//      select for booleans, dropdown for enums, bounded number input for
//      integers. Any other key in the file (mods add their own) gets a plain
//      text input, and new keys can be added free-form. Saved via
//      PATCH /api/servers/{id}/properties with only the changed keys; the
//      backend validates typed keys and 422s with a message we surface.
//      Managed keys (management-server-*) are reserved for Lectern's
//      management-protocol client (the backend 422s edits), so they render
//      read-only and only once present in the file.
//
// Edit-tracking pattern used by both forms: the fetched data is the source of
// truth; user changes go into a separate `edits` overlay keyed by field. Save
// sends the overlay, then replaces the source of truth with the response and
// clears the overlay. "Dirty" = overlay non-empty.
//
// Lectern settings and most server.properties keys apply at the NEXT server
// start (Minecraft reads the file and Lectern rebuilds the launch command on
// boot). The exception: on a running server whose management-protocol client
// is connected, the keys the backend marks `live` (motd, difficulty, …) are
// applied immediately through the protocol and the server rewrites the file
// itself; the PATCH answer lists them in `applied_live`. Each edited or just
// saved row says which of the two applies (see applyHint in lib/properties).

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, errorMessage } from "../../api/client";
import {
  getProperties,
  patchProperties,
  PropertiesResponse,
  PropertyDefinition,
  ServerDetail,
  ServerSettingsUpdate,
  ServerStatus,
  updateServerSettings,
} from "../../api/servers";
import { useToast } from "../../components/Toasts";
import {
  applyHint,
  pendingEdits,
  propertyRows,
  SaveOutcome,
  saveOutcome,
  savedMessage,
} from "../../lib/properties";

export default function PropertiesTab({
  serverId,
  server,
  onServerUpdate,
}: {
  serverId: string;
  server: ServerDetail;
  /** Called with the fresh detail after a settings save so the parent header
   *  (port/memory line, controls) reflects the change immediately. */
  onServerUpdate: (s: ServerDetail) => void;
}) {
  // Live keys apply immediately only on a fully running server whose
  // protocol client is connected — the backend's own gate. While starting or
  // stopping the client can still be connected, but the PATCH writes the file.
  const connected = server.status === "running" && server.msmp?.connected === true;
  return (
    <div className="space-y-6">
      <p className="text-xs text-slate-500">
        {connected
          ? "Changes apply at the next start. While the server runs with its management protocol connected, motd, difficulty, max players, view distance, the whitelist switches and gamemode take effect immediately; an edited row says which applies."
          : "Changes apply the next time the server starts."}
      </p>
      <LecternSettingsForm server={server} onServerUpdate={onServerUpdate} />
      {/* Proxies have no server.properties — velocity.toml lives in the
          Files tab, links in the Proxy tab. */}
      {server.type !== "velocity" && (
        <ServerPropertiesForm
          serverId={serverId}
          status={server.status}
          connected={connected}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section 1 — Lectern-owned settings (DB row)
// ---------------------------------------------------------------------------

// Field list drives rendering; `parse` converts the <input> string back to the
// JSON type the API expects.
const SETTINGS_FIELDS: {
  key: keyof ServerSettingsUpdate;
  label: string;
  type: "number" | "text" | "boolean";
  hint?: string;
  parse?: (raw: string) => number | string;
}[] = [
  { key: "memory_mb", label: "Memory (MB)", type: "number", parse: Number },
  { key: "port", label: "Game port", type: "number", parse: Number, hint: "also written to server.properties" },
  { key: "jvm_args", label: "Extra JVM args", type: "text", hint: "space-separated, e.g. -XX:+UseG1GC" },
  { key: "stop_command", label: "Stop command", type: "text" },
  { key: "shutdown_timeout", label: "Stop timeout (s)", type: "number", parse: Number },
  { key: "log_retention_days", label: "Log retention (days)", type: "number", parse: Number, hint: "delete rotated logs older than this at start; 0 = keep" },
  { key: "auto_start", label: "Start with Lectern", type: "boolean" },
  { key: "auto_start_delay", label: "Auto-start delay (s)", type: "number", parse: Number, hint: "staggers boot when several auto-start" },
  { key: "crash_restart", label: "Restart after crash", type: "boolean", hint: "gives up after 3 crashes in a row" },
  { key: "backup_max", label: "Backups to keep", type: "number", parse: Number, hint: "oldest pruned past this" },
  { key: "backup_excluded", label: "Backup exclusions", type: "text", hint: "comma-separated paths, e.g. logs,cache" },
  { key: "backup_compress", label: "Compress backups", type: "boolean" },
  { key: "backup_stop_server", label: "Stop server for backup", type: "boolean", hint: "restarted afterwards" },
];

function LecternSettingsForm({
  server,
  onServerUpdate,
}: {
  server: ServerDetail;
  onServerUpdate: (s: ServerDetail) => void;
}) {
  // Overlay of pending changes; empty object = form is pristine.
  const [edits, setEdits] = useState<ServerSettingsUpdate>({});
  const [saving, setSaving] = useState(false);
  // Validation errors stay inline next to Save (they refer to the fields);
  // success is transient → toast.
  const [error, setError] = useState<string | null>(null);
  const dirty = Object.keys(edits).length > 0;
  const toast = useToast();

  // Current display value = pending edit if any, else the saved server value.
  function valueOf(key: keyof ServerSettingsUpdate): string | boolean {
    const pending = edits[key];
    const current = pending !== undefined ? pending : server[key as keyof ServerDetail];
    return typeof current === "boolean" ? current : String(current ?? "");
  }

  function setField(key: keyof ServerSettingsUpdate, value: number | string | boolean) {
    setEdits((prev) => ({ ...prev, [key]: value }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      // Only the overlay is sent — untouched fields keep their values.
      onServerUpdate(await updateServerSettings(server.id, edits));
      setEdits({});
      toast.success("Server settings saved.");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-3">
      <h3 className="text-sm font-medium text-slate-300">Lectern settings</h3>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {SETTINGS_FIELDS.map((f) => (
          <label key={f.key} className="block text-xs text-slate-400 space-y-1">
            <span>
              {f.label}
              {f.hint && <span className="text-slate-600"> — {f.hint}</span>}
            </span>
            {f.type === "boolean" ? (
              <BoolSelect
                value={valueOf(f.key) === true || valueOf(f.key) === "true"}
                onChange={(v) => setField(f.key, v)}
              />
            ) : (
              <input
                type={f.type}
                value={String(valueOf(f.key))}
                onChange={(e) =>
                  setField(f.key, f.parse ? f.parse(e.target.value) : e.target.value)
                }
                className="w-full rounded-sm bg-slate-800 border border-slate-700 px-2 py-1.5 text-sm text-slate-200"
              />
            )}
          </label>
        ))}
      </div>
      <SaveRow dirty={dirty} saving={saving} error={error} onSave={save} />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Section 2 — server.properties (file on disk)
// ---------------------------------------------------------------------------

function ServerPropertiesForm({
  serverId,
  status,
  connected,
}: {
  serverId: string;
  /** Lifecycle status; any change makes the post-save hints stale. */
  status: ServerStatus;
  /** The server is running with its management-protocol client connected
   *  (live keys apply immediately on save). */
  connected: boolean;
}) {
  // `data` mirrors the backend response (file content + widget metadata).
  const [data, setData] = useState<PropertiesResponse | null>(null);
  // Overlay of changed keys → new string value.
  const [edits, setEdits] = useState<Record<string, string>>({});
  // How each key saved in this visit took effect (live vs at next start);
  // dropped for a key as soon as it is edited again, and for every key when
  // the server's status changes: after a stop or a (re)start "applied now" and
  // "applies at next start" no longer describe the running server.
  const [lastSave, setLastSave] = useState<Record<string, SaveOutcome>>({});
  // Keys this visit wrote to the file since the status last changed, including
  // the part a failed save still wrote. For a non-live key such as
  // force-gamemode the running server keeps the value it started with, so the
  // file no longer says what is in effect.
  const [writtenThisRun, setWrittenThisRun] = useState<ReadonlySet<string>>(new Set());
  const [hintStatus, setHintStatus] = useState(status);
  if (hintStatus !== status) {
    // Adjusting state on a prop change during render (no effect, so no frame
    // with stale hints); React re-renders before committing.
    setHintStatus(status);
    setLastSave({});
    setWrittenThisRun(new Set());
  }
  // Free-form "add a property" row.
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [saving, setSaving] = useState(false);
  // Validation errors (422 lists offending keys) stay inline; success → toast.
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setData(await getProperties(serverId));
      setError(null);
    } catch (e) {
      // 409 while still installing: show the message instead of a form.
      setError(errorMessage(e));
    }
  }, [serverId]);

  useEffect(() => {
    load();
  }, [load]);

  // Whether force-gamemode is on in the running server: the saved file value,
  // trusted only while this visit has neither a pending edit of it nor written
  // it since the status changed. Anything else gets the cautious gamemode
  // wording ("to new players").
  const forceGamemodeInEffect =
    data?.properties["force-gamemode"] === "true" &&
    edits["force-gamemode"] === undefined &&
    !writtenThisRun.has("force-gamemode");

  // Rows shown: file keys + known definitions (see propertyRows).
  const rows = useMemo(() => (data ? propertyRows(data) : []), [data]);

  const dirty = Object.keys(edits).length > 0 || newKey.trim() !== "";

  function valueOf(key: string): string {
    return edits[key] !== undefined ? edits[key] : (data?.properties[key] ?? "");
  }

  async function save() {
    if (!data) return;
    setSaving(true);
    setError(null);
    const added = newKey.trim();
    const updates: Record<string, string> = { ...edits };
    if (added) updates[added] = newValue;
    try {
      // Backend validates typed keys (enum/int/bool), applies live keys
      // through the management protocol when it can, and answers with the
      // full re-read file, which becomes the new source of truth.
      const saved = await patchProperties(serverId, updates);
      setData(saved);
      setLastSave((prev) => ({
        ...prev,
        ...saveOutcome(Object.keys(updates), saved.applied_live),
      }));
      setWrittenThisRun((prev) => new Set([...prev, ...Object.keys(updates)]));
      setEdits({});
      setNewKey("");
      setNewValue("");
      toast.success(savedMessage(saved.applied_live));
    } catch (e) {
      // 422 carries the list of invalid keys/messages from the backend and
      // means nothing was written. Any other failure (the 504 for a live set
      // the server did not confirm) may come after the backend already wrote
      // the other keys, so re-read the file and keep only the edits it lacks.
      setError(errorMessage(e));
      if (!(e instanceof ApiError && e.status === 422)) {
        await reconcileAfterFailure(updates, added);
      }
    } finally {
      setSaving(false);
    }
  }

  /** After a failed save: adopt the file as it is now and drop every edit
   *  (and the free-form row) it already holds. The error message stays. */
  async function reconcileAfterFailure(updates: Record<string, string>, added: string) {
    let fresh: PropertiesResponse;
    try {
      fresh = await getProperties(serverId);
    } catch {
      // The re-read failed too: keep every edit, so nothing looks saved
      // that may not be; the save error is already shown.
      return;
    }
    const pending = pendingEdits(updates, fresh.properties);
    const settled = Object.keys(updates).filter((key) => !(key in pending));
    setData(fresh);
    setEdits((prev) =>
      Object.fromEntries(Object.entries(prev).filter(([key]) => key in pending)),
    );
    if (added && !(added in pending)) {
      setNewKey("");
      setNewValue("");
    }
    setWrittenThisRun((prev) => new Set([...prev, ...settled]));
  }

  if (!data) {
    return (
      <section className="space-y-2">
        <h3 className="text-sm font-medium text-slate-300">server.properties</h3>
        <p className="text-sm text-slate-500">{error ?? "Loading…"}</p>
      </section>
    );
  }

  return (
    <section className="space-y-3">
      <h3 className="text-sm font-medium text-slate-300">server.properties</h3>
      <p className="text-xs text-slate-500">
        Blank fields aren't in the file yet — Minecraft writes its full
        defaults on first start; only fields you change here are saved.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {rows.map((key) => (
          <PropertyField
            key={key}
            name={key}
            definition={data.definitions[key]}
            value={valueOf(key)}
            edited={edits[key] !== undefined}
            hint={applyHint({
              edited: edits[key] !== undefined,
              live: data.definitions[key]?.live === true,
              connected,
              lastSave: lastSave[key] ?? null,
              key,
              forceGamemode: forceGamemodeInEffect,
            })}
            onChange={(v) => {
              setEdits((prev) => ({ ...prev, [key]: v }));
              setLastSave((prev) => {
                if (!(key in prev)) return prev;
                const next = { ...prev };
                delete next[key];
                return next;
              });
            }}
          />
        ))}
      </div>

      {/* Free-form row for keys we don't know about (mod settings etc.). */}
      <div className="flex items-end gap-2">
        <label className="block text-xs text-slate-400 space-y-1">
          <span>Add property</span>
          <input
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            placeholder="key"
            className="w-28 sm:w-44 rounded-sm bg-slate-800 border border-slate-700 px-2 py-1.5 text-sm text-slate-200"
          />
        </label>
        <input
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder="value"
          className="flex-1 min-w-0 rounded-sm bg-slate-800 border border-slate-700 px-2 py-1.5 text-sm text-slate-200"
        />
      </div>

      <SaveRow dirty={dirty} saving={saving} error={error} onSave={save} />
    </section>
  );
}

/** One server.properties row; the definition (if any) picks the widget. */
function PropertyField({
  name,
  definition,
  value,
  edited,
  hint,
  onChange,
}: {
  name: string;
  definition: PropertyDefinition | undefined;
  value: string;
  edited: boolean;
  /** When the edit or the last save takes effect; ignored for managed keys. */
  hint: string | null;
  onChange: (value: string) => void;
}) {
  const base =
    "w-full rounded-sm bg-slate-800 border px-2 py-1.5 text-sm text-slate-200 " +
    // Amber border marks unsaved edits so users see what the save will send.
    (edited ? "border-amber-600" : "border-slate-700");

  let input;
  if (definition?.managed) {
    // Reserved for Lectern's management-protocol client; PATCH rejects them.
    // The secret is a localhost-only credential, shown as-is.
    input = (
      <input
        value={value}
        disabled
        readOnly
        className="w-full rounded-sm bg-slate-900 border border-slate-800 px-2 py-1.5 text-sm text-slate-500 cursor-not-allowed"
      />
    );
  } else if (definition?.type === "boolean") {
    // Not the shared BoolSelect: a key absent from the file must show as
    // unset ("—"), not silently as false. Unset fields name the server's
    // built-in default so "—" isn't a mystery.
    input = (
      <select value={value} onChange={(e) => onChange(e.target.value)} className={base}>
        {value === "" && (
          <option value="">{unsetLabel(definition)}</option>
        )}
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  } else if (definition?.type === "enum" && definition.values) {
    input = (
      <select value={value} onChange={(e) => onChange(e.target.value)} className={base}>
        {/* Empty option when the file doesn't have this key yet. */}
        {value === "" && (
          <option value="">{unsetLabel(definition)}</option>
        )}
        {definition.values.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    );
  } else if (definition?.type === "integer") {
    input = (
      <input
        type="number"
        value={value}
        min={definition.min ?? undefined}
        max={definition.max ?? undefined}
        placeholder={definition.default ? `default: ${definition.default}` : undefined}
        onChange={(e) => onChange(e.target.value)}
        className={base}
      />
    );
  } else {
    input = (
      <input
        value={value}
        placeholder={
          definition?.default ? `default: ${definition.default}` : undefined
        }
        onChange={(e) => onChange(e.target.value)}
        className={base}
      />
    );
  }

  return (
    <label className="block text-xs text-slate-400 space-y-1" title={definition?.description}>
      <span>
        {name}
        {definition?.managed ? (
          <span className="text-slate-600"> — reserved for Lectern (management protocol)</span>
        ) : (
          hint && <span className="text-slate-600"> — {hint}</span>
        )}
      </span>
      {input}
    </label>
  );
}

/** Label for the unset option of a select: "—" plus the server's default. */
function unsetLabel(definition: PropertyDefinition | undefined): string {
  return definition?.default ? `— (default: ${definition.default})` : "—";
}

/** true/false dropdown used for both settings toggles and boolean properties. */
function BoolSelect({
  value,
  onChange,
  highlight,
}: {
  value: boolean;
  onChange: (v: boolean) => void;
  highlight?: boolean;
}) {
  return (
    <select
      value={value ? "true" : "false"}
      onChange={(e) => onChange(e.target.value === "true")}
      className={
        "w-full rounded-sm bg-slate-800 border px-2 py-1.5 text-sm text-slate-200 " +
        (highlight ? "border-amber-600" : "border-slate-700")
      }
    >
      <option value="true">true</option>
      <option value="false">false</option>
    </select>
  );
}

/** Save button + dirty indicator + error line, shared by both sections. */
function SaveRow({
  dirty,
  saving,
  error,
  onSave,
}: {
  dirty: boolean;
  saving: boolean;
  error: string | null;
  onSave: () => void;
}) {
  return (
    <div className="flex items-center gap-3">
      <button
        onClick={onSave}
        disabled={!dirty || saving}
        className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed rounded-sm px-4 py-1.5 text-sm font-medium"
      >
        {saving ? "Saving…" : "Save"}
      </button>
      {dirty && !saving && <span className="text-xs text-amber-400">unsaved changes</span>}
      {error && <span className="text-xs text-red-400">{error}</span>}
    </div>
  );
}
