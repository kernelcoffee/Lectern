// Metrics tab (M11) — full-page resource monitoring: on-disk size + CPU /
// memory / players history. Sampled server-side on a ~30s timer (see
// stats_sampler.py); this polls the history + size and redraws. Shown even when
// stopped, so past usage and disk size stay visible.
//
// Each chart is a single series titled by its metric, so color isn't
// distinguishing series *within* a chart — the three hues (blue/aqua/yellow)
// are the reference design-system's first categorical slots (dark steps),
// giving per-metric identity reinforced by the title (direct label).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getServerSize,
  getStatsHistory,
  ServerSize,
  StatSample,
} from "../../api/servers";

const SURFACE = "#0f172a"; // page background — the ring color on marks
const GRID = "#1e293b";
const COLORS = { cpu: "#3987e5", mem: "#199e70", players: "#c98500" };

const RANGES = [
  { label: "1h", minutes: 60 },
  { label: "6h", minutes: 360 },
  { label: "24h", minutes: 1440 },
];

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

function niceMax(v: number, integer = false): number {
  if (v <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / pow;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  const m = nice * pow;
  return integer ? Math.max(1, Math.ceil(m)) : m;
}

function useWidth<T extends HTMLElement>(ref: React.RefObject<T | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => setWidth(e[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

export default function MonitorPanel({ serverId }: { serverId: string }) {
  const [history, setHistory] = useState<StatSample[] | null>(null);
  const [size, setSize] = useState<ServerSize | null>(null);
  const [minutes, setMinutes] = useState(60);

  const refresh = useCallback(async () => {
    try {
      const [h, s] = await Promise.all([
        getStatsHistory(serverId, minutes),
        getServerSize(serverId),
      ]);
      setHistory(h);
      setSize(s);
    } catch {
      /* best-effort — the rest of the page still works */
    }
  }, [serverId, minutes]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const hasHistory = history !== null && history.length > 0;

  return (
    <section className="space-y-5">
      {/* Size tiles + time range */}
      <div className="flex flex-wrap items-center gap-3">
        <SizeTile label="World size" value={fmtBytes(size?.world_bytes ?? null)} />
        <SizeTile label="Server size" value={fmtBytes(size?.server_bytes ?? null)} />
        {size?.computed_at && (
          <span className="text-xs text-slate-600">
            measured {new Date(size.computed_at).toLocaleTimeString()}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1 rounded-md border border-slate-800 p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.minutes}
              onClick={() => setMinutes(r.minutes)}
              className={
                "rounded px-2.5 py-1 text-xs " +
                (minutes === r.minutes
                  ? "bg-slate-700 text-slate-100"
                  : "text-slate-400 hover:text-slate-200")
              }
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {!hasHistory ? (
        <p className="rounded-lg border border-slate-800 p-8 text-center text-sm text-slate-500">
          No history yet — resource samples are recorded every 30 seconds while
          the server runs.
        </p>
      ) : (
        <div className="space-y-4">
          <MetricChart
            title="CPU"
            color={COLORS.cpu}
            samples={history!}
            pick={(s) => s.cpu_percent}
            fixedMax={100}
            format={(v) => `${v.toFixed(0)}%`}
          />
          <MetricChart
            title="Memory"
            color={COLORS.mem}
            samples={history!}
            pick={(s) => s.memory_mb}
            format={fmtMem}
          />
          <MetricChart
            title="Players"
            color={COLORS.players}
            samples={history!}
            pick={(s) => s.players_online}
            integer
            format={(v) => `${Math.round(v)}`}
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
      <div className="text-sm font-medium tabular-nums text-slate-200">{value}</div>
    </div>
  );
}

function MetricChart({
  title,
  color,
  samples,
  pick,
  fixedMax,
  format,
  integer,
}: {
  title: string;
  color: string;
  samples: StatSample[];
  pick: (s: StatSample) => number;
  fixedMax?: number;
  format: (v: number) => string;
  integer?: boolean;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const width = useWidth(wrapRef);
  const height = 150;
  const padL = 46;
  const padR = 10;
  const padT = 10;
  const padB = 20;
  const [hover, setHover] = useState<number | null>(null);

  const values = samples.map(pick);
  const rawMax = Math.max(...values, integer ? 1 : 0.001);
  const yMax = fixedMax ?? niceMax(rawMax * 1.1, integer);
  const n = samples.length;
  const last = values[n - 1];
  const summary = useMemo(() => {
    const sum = values.reduce((a, b) => a + b, 0);
    return { avg: sum / n, max: Math.max(...values) };
  }, [values, n]);

  const plotW = Math.max(1, width - padL - padR);
  const plotH = height - padT - padB;
  const px = (i: number) => padL + (n <= 1 ? plotW : (i / (n - 1)) * plotW);
  const py = (v: number) => padT + plotH - (Math.min(v, yMax) / (yMax || 1)) * plotH;

  const line = samples.map((s, i) => `${i ? "L" : "M"}${px(i)},${py(pick(s))}`).join(" ");
  const area = n > 0 ? `${line} L${px(n - 1)},${padT + plotH} L${px(0)},${padT + plotH} Z` : "";

  const yTicks = [0, yMax / 2, yMax];
  const xTickCount = Math.min(6, n);
  const xTicks = Array.from({ length: xTickCount }, (_, k) =>
    Math.round((k / Math.max(1, xTickCount - 1)) * (n - 1)),
  );

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    if (n === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - rect.left - padL) / plotW;
    setHover(Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1)))));
  }

  const activeIdx = hover ?? n - 1;
  const active = samples[activeIdx];
  const gradId = `g-${title}`;

  return (
    <div className="rounded-lg border border-slate-800 p-3">
      <div className="mb-1 flex items-baseline gap-2">
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} />
        <span className="text-sm font-medium text-slate-200">{title}</span>
        <span className="ml-auto text-xs text-slate-500">
          now <span className="tabular-nums text-slate-300">{format(last)}</span>
          {"  ·  avg "}
          <span className="tabular-nums text-slate-400">{format(summary.avg)}</span>
          {"  ·  max "}
          <span className="tabular-nums text-slate-400">{format(summary.max)}</span>
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
                <stop offset="0%" stopColor={color} stopOpacity="0.26" />
                <stop offset="100%" stopColor={color} stopOpacity="0.02" />
              </linearGradient>
            </defs>

            {/* y gridlines + labels */}
            {yTicks.map((v, i) => (
              <g key={i}>
                <line x1={padL} y1={py(v)} x2={width - padR} y2={py(v)} stroke={GRID} strokeWidth="1" />
                <text
                  x={padL - 6}
                  y={py(v) + 3}
                  textAnchor="end"
                  className="fill-slate-600"
                  style={{ fontSize: 10 }}
                >
                  {format(v)}
                </text>
              </g>
            ))}

            {/* x time ticks */}
            {xTicks.map((idx, i) => (
              <text
                key={i}
                x={px(idx)}
                y={height - 6}
                textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"}
                className="fill-slate-600"
                style={{ fontSize: 10 }}
              >
                {new Date(samples[idx].created_at).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </text>
            ))}

            {area && <path d={area} fill={`url(#${gradId})`} />}
            {line && <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />}

            {hover !== null && (
              <>
                <line x1={px(hover)} y1={padT} x2={px(hover)} y2={padT + plotH} stroke="#475569" strokeWidth="1" />
                <circle cx={px(hover)} cy={py(pick(samples[hover]))} r="3.5" fill={color} stroke={SURFACE} strokeWidth="2" />
              </>
            )}
            {hover === null && n > 0 && (
              <circle cx={px(n - 1)} cy={py(last)} r="3" fill={color} stroke={SURFACE} strokeWidth="2" />
            )}
          </svg>
        )}
        {hover !== null && active && (
          <div
            className="pointer-events-none absolute z-10 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] text-slate-100 shadow"
            style={{
              left: Math.min(Math.max(px(hover), padL + 24), width - 24),
              top: 2,
              transform: "translateX(-50%)",
            }}
          >
            <div className="tabular-nums">{format(pick(active))}</div>
            <div className="text-slate-400">
              {new Date(active.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
