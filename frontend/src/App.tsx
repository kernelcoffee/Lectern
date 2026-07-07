import { useEffect, useState } from "react";
import { apiGet } from "./api/client";
import ServerList from "./pages/ServerList";
import ServerDetail from "./pages/ServerDetail";

type Health = { status: string; version: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);
  // Lightweight routing: which server's detail page is open (null = list).
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    apiGet<Health>("/api/health").then(setHealth).catch(() => setError(true));
  }, []);

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100">
      <header className="border-b border-slate-800">
        <div className="max-w-3xl mx-auto px-6 py-4 flex items-baseline justify-between">
          <div>
            <h1 className="text-xl font-semibold">Lectern</h1>
            <p className="text-xs text-slate-400">Modded Minecraft server manager</p>
          </div>
          <span className="text-xs">
            {health ? (
              <span className="text-emerald-400">backend v{health.version}</span>
            ) : error ? (
              <span className="text-red-400">backend offline</span>
            ) : (
              <span className="text-slate-500">connecting…</span>
            )}
          </span>
        </div>
      </header>
      <main>
        {selected ? (
          <ServerDetail serverId={selected} onBack={() => setSelected(null)} />
        ) : (
          <ServerList onOpen={setSelected} />
        )}
      </main>
    </div>
  );
}
