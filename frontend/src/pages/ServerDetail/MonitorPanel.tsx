// Resource monitoring (M11): on-disk size tiles + CPU / memory / players
// history charts. Sampled server-side on a ~30s timer (see stats_sampler.py);
// this polls the history + size and redraws. Shown even when stopped, so past
// usage and disk size stay visible.
//
// Each chart is a single series titled by its metric, so color isn't
// distinguishing series *within* a chart — the three hues (blue/aqua/yellow)
// are the reference design-system's first categorical slots (dark steps),
// giving per-metric identity that's reinforced by the title (direct label).

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getServerSize,
  getStatsHistory,
  ServerSize,
  StatSample,
} from "../../api/servers";

const SURFACE = "#0f172a"; // page background — the ring/gap color on marks
const COLORS = { cpu: "#3987e5", mem: "#199e70", players: "#c98500" };

function fmtBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(2)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function fmtMem(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`;
}

function useWidth<T extends HTMLElement>(ref: React.RefObject<T | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      setWidth(entries[0].contentRect.width);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

export default function MonitorPanel({ serverId }: { serverId: string }) {
  const [history, setHistory] = useState<StatSample[] | null>(null);
  const [size, setSize] = useState<ServerSize | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, s] = await Promise.all([
        getStatsHistory(serverId, 60),
        getServerSize(serverId),
      ]);
      setHistory(h);
      setSize(s);
    } catch {
      // Monitoring is best-effort — the rest of the page still works.
    }
  }, [serverId]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const hasHistory = history !== null && history.length > 0;

  return (
    <section className="space-y-4 rounded-lg border border-slate-800 p-4">
      <div className="flex items-baseline gap-3">
        <h3 className="text-sm font-semibold text-slate-200">Monitoring</h3>
        <span className="text-xs text-slate-500">last hour · sampled every 30s</span>
      </div>

      {/* On-disk size */}
      <div className="flex flex-wrap gap-3">
        <SizeTile label="World size" value={fmtBytes(size?.world_bytes ?? null)} />
        <SizeTile label="Server size" value={fmtBytes(size?.server_bytes ?? null)} />
      </div>

      {/* History charts */}
      {!hasHistory ? (
        <p className="text-xs text-slate-500">
          No history yet — samples are recorded while the server runs.
        </p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <TimeChart
            title="CPU"
            color={COLORS.cpu}
            points={history!.map((s) => ({ t: s.created_at, v: s.cpu_percent }))}
            fixedMax={100}
            format={(v) => `${v.toFixed(0)}%`}
          />
          <TimeChart
            title="Memory"
            color={COLORS.mem}
            points={history!.map((s) => ({ t: s.created_at, v: s.memory_mb }))}
            format={fmtMem}
          />
          <TimeChart
            title="Players"
            color={COLORS.players}
            points={history!.map((s) => ({ t: s.created_at, v: s.players_online }))}
            format={(v) => `${Math.round(v)}`}
            integer
          />
        </div>
      )}
    </section>
  );
}

function SizeTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/40 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-sm font-medium text-slate-200 tabular-nums">{value}</div>
    </div>
  );
}

interface Pt {
  t: string;
  v: number;
}

function TimeChart({
  title,
  color,
  points,
  fixedMax,
  format,
  integer,
}: {
  title: string;
  color: string;
  points: Pt[];
  fixedMax?: number;
  format: (v: number) => string;
  integer?: boolean;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const width = useWidth(wrapRef);
  const height = 84;
  const padY = 10;
  const [hover, setHover] = useState<number | null>(null);

  const values = points.map((p) => p.v);
  const rawMax = fixedMax ?? Math.max(...values, integer ? 1 : 0.001);
  const yMax = fixedMax ?? Math.max(rawMax * 1.15, integer ? 1 : rawMax || 1);
  const n = points.length;
  const last = points[n - 1];

  const px = (i: number) => (n <= 1 ? width : (i / (n - 1)) * width);
  const py = (v: number) =>
    height - padY - (Math.min(v, yMax) / (yMax || 1)) * (height - 2 * padY);

  const line = points.map((p, i) => `${i ? "L" : "M"}${px(i)},${py(p.v)}`).join(" ");
  const area =
    n > 0
      ? `${line} L${px(n - 1)},${height} L${px(0)},${height} Z`
      : "";

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    if (n === 0 || width === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - rect.left) / rect.width;
    setHover(Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1)))));
  }

  const active = hover !== null ? points[hover] : last;
  const gradId = `g-${title}`;

  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2">
        <span className="h-2 w-2 rounded-full" style={{ background: color }} />
        <span className="text-xs text-slate-400">{title}</span>
        <span className="ml-auto text-sm font-medium tabular-nums text-slate-200">
          {active ? format(active.v) : "—"}
        </span>
      </div>
      <div ref={wrapRef} className="relative" style={{ height }}>
        {width > 0 && (
          <svg
            width={width}
            height={height}
            onMouseMove={onMove}
            onMouseLeave={() => setHover(null)}
          >
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity="0.28" />
                <stop offset="100%" stopColor={color} stopOpacity="0.02" />
              </linearGradient>
            </defs>
            {/* recessive baseline */}
            <line
              x1="0"
              y1={height - padY}
              x2={width}
              y2={height - padY}
              stroke="#1e293b"
              strokeWidth="1"
            />
            {area && <path d={area} fill={`url(#${gradId})`} />}
            {line && (
              <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
            )}
            {/* hover crosshair + point */}
            {hover !== null && (
              <>
                <line
                  x1={px(hover)}
                  y1={padY / 2}
                  x2={px(hover)}
                  y2={height - padY}
                  stroke="#475569"
                  strokeWidth="1"
                />
                <circle cx={px(hover)} cy={py(points[hover].v)} r="3.5" fill={color} stroke={SURFACE} strokeWidth="2" />
              </>
            )}
            {/* emphasized endpoint */}
            {hover === null && last && (
              <circle cx={px(n - 1)} cy={py(last.v)} r="3" fill={color} stroke={SURFACE} strokeWidth="2" />
            )}
          </svg>
        )}
        {hover !== null && active && (
          <div
            className="pointer-events-none absolute -top-1 z-10 -translate-y-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] text-slate-100 shadow"
            style={{ left: Math.min(Math.max(px(hover), 30), width - 30), transform: "translate(-50%,-100%)" }}
          >
            <div className="tabular-nums">{format(active.v)}</div>
            <div className="text-slate-400">
              {new Date(active.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
