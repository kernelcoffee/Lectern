// Server-status styling, shared by every surface that renders a status —
// chips (dashboard table, detail header) and dots (sidebar). One source so a
// new status can't end up colored differently in different places. State is
// never color-alone: chips spell the word, dots carry a title tooltip.

import { ServerStatus } from "../api/servers";

/** Filled pill (text on colored background). */
export const STATUS_CHIP: Record<ServerStatus, string> = {
  installing: "bg-sky-500 text-slate-900",
  install_failed: "bg-red-500 text-slate-100",
  stopped: "bg-slate-600 text-slate-100",
  starting: "bg-amber-500 text-slate-900",
  running: "bg-emerald-500 text-slate-900",
  stopping: "bg-amber-500 text-slate-900",
  crashed: "bg-red-500 text-slate-100",
};

/** Small round indicator (sidebar rows). */
export const STATUS_DOT: Record<ServerStatus, string> = {
  installing: "bg-sky-400",
  install_failed: "bg-red-500",
  stopped: "bg-slate-500",
  starting: "bg-amber-400",
  running: "bg-emerald-400",
  stopping: "bg-amber-400",
  crashed: "bg-red-500",
};
