// Files tab (M12) — browse and edit a server's files.
//
// The escape hatch for config Lectern doesn't model (mod config/, ops.json,
// reading logs). Every path is confined to the server directory server-side.
// Layout (Crafty-inspired): breadcrumb + toolbar, then a table with a
// select-all/per-row checkbox and Name · Type · Modified · Size · Permissions ·
// per-row actions menu. Selecting rows enables a bulk delete. Opening a text
// file shows it in a modal editor; binary/oversized files offer a download.
// Edits apply at the next start (like Properties), so no stop is required.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  deletePath,
  fileDownloadUrl,
  FileEntry,
  listFiles,
  makeDir,
  readFile,
  renamePath,
  uploadFile,
  writeFile,
} from "../../api/files";

function fmtSize(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function fmtDate(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}

function fileType(entry: FileEntry): string {
  if (entry.is_dir) return "Folder";
  const dot = entry.name.lastIndexOf(".");
  return dot > 0 ? `${entry.name.slice(dot + 1).toUpperCase()} file` : "File";
}

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);

interface OpenFile {
  path: string;
  content: string;
  binary: boolean;
  tooLarge: boolean;
  size: number;
}

export default function FilesTab({ serverId }: { serverId: string }) {
  const [cwd, setCwd] = useState("");
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [open, setOpen] = useState<OpenFile | null>(null);
  const [editContent, setEditContent] = useState("");
  const [dirty, setDirty] = useState(false);

  const fail = (e: unknown) =>
    setError(e instanceof ApiError ? e.message : String(e));

  const refresh = useCallback(async () => {
    try {
      setEntries((await listFiles(serverId, cwd)).entries);
      setSelected(new Set());
      setError(null);
    } catch (e) {
      fail(e);
    }
  }, [serverId, cwd]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setMenuFor(null);
    try {
      await fn();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  function navTo(path: string) {
    setCwd(path);
  }

  async function openFile(entry: FileEntry) {
    const path = join(cwd, entry.name);
    setBusy(true);
    setError(null);
    setMenuFor(null);
    try {
      const f = await readFile(serverId, path);
      setOpen({
        path,
        content: f.content ?? "",
        binary: f.binary,
        tooLarge: f.too_large,
        size: f.size,
      });
      setEditContent(f.content ?? "");
      setDirty(false);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      if (!open) return;
      await writeFile(serverId, open.path, editContent);
      setOpen({ ...open, content: editContent });
      setDirty(false);
    });

  const newFile = () =>
    run(async () => {
      const name = window.prompt("New file name:");
      if (!name) return;
      await writeFile(serverId, join(cwd, name), "");
      await refresh();
    });

  const newFolder = () =>
    run(async () => {
      const name = window.prompt("New folder name:");
      if (!name) return;
      await makeDir(serverId, join(cwd, name));
      await refresh();
    });

  const rename = (entry: FileEntry) =>
    run(async () => {
      const next = window.prompt(`Rename "${entry.name}" to:`, entry.name);
      if (!next || next === entry.name) return;
      await renamePath(serverId, join(cwd, entry.name), join(cwd, next));
      await refresh();
    });

  const removeOne = (entry: FileEntry) =>
    run(async () => {
      if (
        !window.confirm(
          `Delete "${entry.name}"${entry.is_dir ? " and everything in it" : ""}?`,
        )
      )
        return;
      await deletePath(serverId, join(cwd, entry.name));
      await refresh();
    });

  const removeSelected = () =>
    run(async () => {
      if (
        !window.confirm(
          `Delete ${selected.size} selected item${selected.size === 1 ? "" : "s"}?`,
        )
      )
        return;
      for (const name of selected) {
        await deletePath(serverId, join(cwd, name));
      }
      await refresh();
    });

  const onUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    run(async () => {
      await uploadFile(serverId, cwd, file);
      await refresh();
    });
  };

  function toggle(name: string) {
    setSelected((s) => {
      const next = new Set(s);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  function toggleAll() {
    setSelected((s) =>
      entries && s.size < entries.length
        ? new Set(entries.map((e) => e.name))
        : new Set(),
    );
  }

  const crumbs = cwd ? cwd.split("/") : [];
  const allSelected = !!entries && entries.length > 0 && selected.size === entries.length;

  return (
    <section className="space-y-3">
      {/* Breadcrumb + toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <nav className="flex flex-1 items-center gap-1 text-sm min-w-0">
          <button onClick={() => navTo("")} className="text-slate-300 hover:text-slate-100">
            server
          </button>
          {crumbs.map((seg, i) => {
            const path = crumbs.slice(0, i + 1).join("/");
            return (
              <span key={path} className="flex min-w-0 items-center gap-1">
                <span className="text-slate-600">/</span>
                <button
                  onClick={() => navTo(path)}
                  className="truncate text-slate-300 hover:text-slate-100"
                >
                  {seg}
                </button>
              </span>
            );
          })}
        </nav>
        <div className="flex items-center gap-1.5">
          {selected.size > 0 && (
            <button
              onClick={removeSelected}
              disabled={busy}
              className="rounded bg-red-900/70 px-2.5 py-1 text-xs text-red-200 hover:bg-red-800 disabled:opacity-50"
            >
              Delete {selected.size} selected
            </button>
          )}
          <ToolbarButton label="New file" onClick={newFile} disabled={busy} />
          <ToolbarButton label="New folder" onClick={newFolder} disabled={busy} />
          <label className="cursor-pointer rounded bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700">
            Upload
            <input type="file" onChange={onUpload} className="hidden" disabled={busy} />
          </label>
        </div>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      {/* Table */}
      {entries === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="w-8 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="Select all"
                  />
                </th>
                <th className="px-2 py-2">Name</th>
                <th className="px-2 py-2">Type</th>
                <th className="hidden px-2 py-2 md:table-cell">Modified</th>
                <th className="px-2 py-2 text-right">Size</th>
                <th className="hidden px-2 py-2 font-mono sm:table-cell">Perms</th>
                <th className="w-10 px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-center text-slate-500">
                    This folder is empty.
                  </td>
                </tr>
              )}
              {entries.map((entry) => (
                <tr
                  key={entry.name}
                  className="border-b border-slate-800/60 last:border-0 hover:bg-slate-800/40"
                >
                  <td className="px-3 py-1.5">
                    <input
                      type="checkbox"
                      checked={selected.has(entry.name)}
                      onChange={() => toggle(entry.name)}
                      aria-label={`Select ${entry.name}`}
                    />
                  </td>
                  <td className="px-2 py-1.5">
                    <button
                      onClick={() =>
                        entry.is_dir ? navTo(join(cwd, entry.name)) : openFile(entry)
                      }
                      className="flex items-center gap-2 text-left"
                    >
                      <span className="text-slate-500">{entry.is_dir ? "📁" : "📄"}</span>
                      <span className={entry.is_dir ? "text-slate-200" : "text-slate-300"}>
                        {entry.name}
                      </span>
                    </button>
                  </td>
                  <td className="px-2 py-1.5 text-slate-400">{fileType(entry)}</td>
                  <td className="hidden px-2 py-1.5 text-slate-400 md:table-cell">
                    {fmtDate(entry.mtime)}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                    {entry.is_dir ? "—" : fmtSize(entry.size)}
                  </td>
                  <td className="hidden px-2 py-1.5 font-mono text-xs text-slate-500 sm:table-cell">
                    {entry.mode}
                  </td>
                  <td className="relative px-2 py-1.5 text-right">
                    <RowMenu
                      open={menuFor === entry.name}
                      onToggle={() =>
                        setMenuFor((m) => (m === entry.name ? null : entry.name))
                      }
                      entry={entry}
                      cwd={cwd}
                      serverId={serverId}
                      onOpen={() => openFile(entry)}
                      onRename={() => rename(entry)}
                      onDelete={() => removeOne(entry)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <EditorModal
          serverId={serverId}
          file={open}
          content={editContent}
          dirty={dirty}
          busy={busy}
          onChange={(v) => {
            setEditContent(v);
            setDirty(v !== open.content);
          }}
          onSave={save}
          onClose={() => setOpen(null)}
        />
      )}
    </section>
  );
}

function RowMenu({
  open,
  onToggle,
  entry,
  cwd,
  serverId,
  onOpen,
  onRename,
  onDelete,
}: {
  open: boolean;
  onToggle: () => void;
  entry: FileEntry;
  cwd: string;
  serverId: string;
  onOpen: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onToggle();
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open, onToggle]);

  return (
    <div ref={ref} className="inline-block text-left">
      <button
        onClick={onToggle}
        aria-label="Actions"
        className="rounded px-2 py-0.5 text-slate-400 hover:bg-slate-700 hover:text-slate-100"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-2 z-10 mt-1 w-36 overflow-hidden rounded-md border border-slate-700 bg-slate-800 py-1 text-sm shadow-lg">
          {!entry.is_dir && (
            <>
              <MenuItem label="Edit" onClick={onOpen} />
              <a
                href={fileDownloadUrl(serverId, join(cwd, entry.name))}
                download={entry.name}
                onClick={onToggle}
                className="block px-3 py-1.5 text-slate-200 hover:bg-slate-700"
              >
                Download
              </a>
            </>
          )}
          <MenuItem label="Rename" onClick={onRename} />
          <MenuItem label="Delete" onClick={onDelete} danger />
        </div>
      )}
    </div>
  );
}

function MenuItem({
  label,
  onClick,
  danger,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "block w-full px-3 py-1.5 text-left hover:bg-slate-700 " +
        (danger ? "text-red-300" : "text-slate-200")
      }
    >
      {label}
    </button>
  );
}

function EditorModal({
  serverId,
  file,
  content,
  dirty,
  busy,
  onChange,
  onSave,
  onClose,
}: {
  serverId: string;
  file: OpenFile;
  content: string;
  dirty: boolean;
  busy: boolean;
  onChange: (v: string) => void;
  onSave: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const editable = !file.binary && !file.tooLarge;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-4xl flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
          <h3 className="flex-1 truncate font-mono text-sm text-slate-200">{file.path}</h3>
          {dirty && <span className="text-xs text-amber-400">unsaved</span>}
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {editable ? (
            <textarea
              value={content}
              onChange={(e) => onChange(e.target.value)}
              spellCheck={false}
              autoFocus
              className="h-[55vh] w-full resize-none rounded border border-slate-700 bg-slate-950 p-3 font-mono text-xs text-slate-100 outline-none focus:border-emerald-600"
            />
          ) : (
            <div className="space-y-3 py-6 text-center text-sm text-slate-400">
              <p>
                {file.binary
                  ? "This looks like a binary file — it can't be edited here."
                  : `This file is ${fmtSize(file.size)}, too large to edit here.`}
              </p>
              <a
                href={fileDownloadUrl(serverId, file.path)}
                download
                className="inline-block rounded bg-slate-700 px-3 py-1.5 text-xs text-slate-100 hover:bg-slate-600"
              >
                Download instead
              </a>
            </div>
          )}
        </div>

        {editable && (
          <div className="flex items-center gap-2 border-t border-slate-800 px-4 py-3">
            <button
              onClick={onSave}
              disabled={busy || !dirty}
              className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-slate-900 hover:bg-emerald-500 disabled:opacity-40"
            >
              {busy ? "Saving…" : "Save"}
            </button>
            <span className="text-xs text-slate-500">
              Changes apply the next time the server starts.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function ToolbarButton({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="rounded bg-slate-800 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
    >
      {label}
    </button>
  );
}
