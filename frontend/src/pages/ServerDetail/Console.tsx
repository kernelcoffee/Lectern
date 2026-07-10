import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { fileDownloadUrl } from "../../api/files";
import { useConsoleSocket } from "../../hooks/useConsoleSocket";

export default function Console({ serverId }: { serverId: string }) {
  const { lines, connected, send } = useConsoleSocket(serverId);
  const [command, setCommand] = useState("");
  const [search, setSearch] = useState("");
  const [autoscroll, setAutoscroll] = useState(true);
  const logRef = useRef<HTMLDivElement | null>(null);

  // Sent-command history for ↑/↓ recall (newest last). `histIdx` is the
  // position being viewed, or null when typing a fresh command.
  const [history, setHistory] = useState<string[]>([]);
  const [histIdx, setHistIdx] = useState<number | null>(null);

  const filtered = useMemo(() => {
    if (!search.trim()) return lines;
    const q = search.toLowerCase();
    return lines.filter((l) => l.toLowerCase().includes(q));
  }, [lines, search]);

  // Pin to the newest line — but not while searching (matches read top-down)
  // or when the user has paused autoscroll.
  useEffect(() => {
    const el = logRef.current;
    if (el && autoscroll && !search) el.scrollTop = el.scrollHeight;
  }, [filtered, autoscroll, search]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const cmd = command.trim();
    if (!cmd) return;
    send(cmd);
    setHistory((h) => (h[h.length - 1] === cmd ? h : [...h, cmd]));
    setHistIdx(null);
    setCommand("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowUp") {
      if (history.length === 0) return;
      e.preventDefault();
      const next = histIdx === null ? history.length - 1 : Math.max(0, histIdx - 1);
      setHistIdx(next);
      setCommand(history[next]);
    } else if (e.key === "ArrowDown") {
      if (histIdx === null) return;
      e.preventDefault();
      const next = histIdx + 1;
      if (next >= history.length) {
        setHistIdx(null);
        setCommand("");
      } else {
        setHistIdx(next);
        setCommand(history[next]);
      }
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
        <span className="flex items-center gap-2">
          <span
            className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-slate-600"}`}
          />
          {connected ? "console connected" : "console disconnected"}
        </span>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search output…"
          className="ml-auto w-48 rounded bg-slate-800 px-2 py-1 text-slate-200 placeholder:text-slate-600"
        />
        {search && (
          <span className="text-slate-500">
            {filtered.length} match{filtered.length === 1 ? "" : "es"}
          </span>
        )}
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={autoscroll}
            onChange={(e) => setAutoscroll(e.target.checked)}
          />
          Autoscroll
        </label>
        <a
          href={fileDownloadUrl(serverId, "logs/latest.log")}
          download="latest.log"
          className="rounded bg-slate-800 px-2 py-1 hover:bg-slate-700 hover:text-slate-200"
        >
          Download log
        </a>
      </div>
      <div
        ref={logRef}
        className="h-[clamp(20rem,100dvh_-_24rem,64rem)] overflow-y-auto rounded-lg border border-slate-800 bg-black/40 p-3 font-mono text-xs leading-relaxed text-slate-200"
      >
        {lines.length === 0 ? (
          <p className="text-slate-600">No output yet.</p>
        ) : filtered.length === 0 ? (
          <p className="text-slate-600">No lines match “{search}”.</p>
        ) : (
          filtered.map((line, i) => (
            <div key={i} className="whitespace-pre-wrap break-words">
              {line}
            </div>
          ))
        )}
      </div>
      <form onSubmit={onSubmit} className="flex gap-2">
        <span className="self-center font-mono text-slate-500">&gt;</span>
        <input
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="type a server command (↑ for history) — e.g. say hello"
          className="flex-1 bg-slate-800 rounded px-2 py-1 font-mono text-sm"
        />
        <button
          type="submit"
          className="bg-slate-700 hover:bg-slate-600 rounded px-3 py-1.5 text-sm"
        >
          Send
        </button>
      </form>
    </div>
  );
}
