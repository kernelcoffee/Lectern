// Global toast notifications — transient action feedback (saves, failures,
// background results) that used to be scattered inline <p> lines. Persistent
// state (form validation, the EULA gate, "unsaved changes" hints, the content
// browser's in-modal feedback) stays inline: toasts are for things that
// happen, not things that are.
//
// No dependency: a context exposes push functions and the provider renders
// the stack. Errors linger longer than successes; everything is manually
// dismissible. The viewport sits above modals (those are z-50) and adapts —
// bottom-center on phones, bottom-right column from sm up.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";

type Kind = "success" | "error" | "info";

interface Toast {
  id: number;
  kind: Kind;
  message: string;
  leaving?: boolean;
}

export interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast must be used inside <ToastProvider>");
  return api;
}

const TTL: Record<Kind, number> = { success: 4000, info: 5000, error: 8000 };

const STYLES: Record<Kind, { bar: string; dot: string }> = {
  success: { bar: "border-emerald-500", dot: "bg-emerald-400" },
  error: { bar: "border-red-500", dot: "bg-red-400" },
  info: { bar: "border-sky-500", dot: "bg-sky-400" },
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    // Two-phase: mark leaving (plays the exit transition), then drop.
    setToasts((ts) => ts.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    window.setTimeout(
      () => setToasts((ts) => ts.filter((t) => t.id !== id)),
      200,
    );
  }, []);

  const push = useCallback(
    (kind: Kind, message: string) => {
      const id = nextId.current++;
      // Replace an identical toast instead of stacking twins (repeat saves),
      // and cap the stack at 5.
      setToasts((ts) => [
        ...ts
          .filter((t) => t.kind !== kind || t.message !== message)
          .slice(-4),
        { id, kind, message },
      ]);
      window.setTimeout(() => dismiss(id), TTL[kind]);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (m) => push("success", m),
      error: (m) => push("error", m),
      info: (m) => push("info", m),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        aria-label="Notifications"
        className="pointer-events-none fixed inset-x-3 bottom-3 z-[60] flex flex-col items-stretch gap-2 sm:inset-x-auto sm:bottom-4 sm:right-4 sm:w-96 sm:items-end"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            className={
              `pointer-events-auto flex w-full items-start gap-2.5 rounded-md border border-slate-700 ` +
              `border-l-4 ${STYLES[t.kind].bar} bg-slate-800 px-3 py-2.5 shadow-lg shadow-black/40 ` +
              `transition-all duration-200 ` +
              (t.leaving
                ? "translate-y-1 opacity-0"
                : "animate-[toast-in_150ms_ease-out]")
            }
          >
            <span
              className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${STYLES[t.kind].dot}`}
              aria-hidden
            />
            <p className="min-w-0 flex-1 break-words text-sm text-slate-100">
              {t.message}
            </p>
            <button
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
              className="shrink-0 rounded px-1 text-slate-500 hover:text-slate-200"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
