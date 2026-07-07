import { FormEvent, useEffect, useRef, useState } from "react";
import { useConsoleSocket } from "../../hooks/useConsoleSocket";

export default function Console({ serverId }: { serverId: string }) {
  const { lines, connected, send } = useConsoleSocket(serverId);
  const [command, setCommand] = useState("");
  const logRef = useRef<HTMLDivElement | null>(null);

  // Keep the log pinned to the newest line.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const cmd = command.trim();
    if (!cmd) return;
    send(cmd);
    setCommand("");
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs text-slate-400">
        <span
          className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-slate-600"}`}
        />
        {connected ? "console connected" : "console disconnected"}
      </div>
      <div
        ref={logRef}
        className="h-96 overflow-y-auto rounded-lg border border-slate-800 bg-black/40 p-3 font-mono text-xs leading-relaxed text-slate-200"
      >
        {lines.length === 0 ? (
          <p className="text-slate-600">No output yet.</p>
        ) : (
          lines.map((line, i) => (
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
          placeholder="type a server command (e.g. say hello)"
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
