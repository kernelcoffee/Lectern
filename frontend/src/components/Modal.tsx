// Shared dialog shell — backdrop, panel, header (title + ✕), Escape-to-close.
//
// Grown out of three hand-rolled modals that had drifted apart: the backdrop
// dismisses on mouseDOWN (an onClick backdrop fires when a text selection
// started inside the panel ends outside it — accidental close), Escape always
// works, and the panel carries dialog semantics for screen readers.
//
// The panel is a flex column capped to the viewport; children own the body
// layout (add your own overflow-auto wrapper — or not, for split-pane bodies
// like the content browser). Size via `panelClassName` (width/height classes).

import { ReactNode, useEffect } from "react";

export default function Modal({
  title,
  onClose,
  panelClassName = "max-w-2xl",
  testId,
  children,
}: {
  /** Header content, next to the ✕ button. */
  title: ReactNode;
  onClose: () => void;
  /** Width/height classes for the panel (e.g. "max-w-4xl max-h-[85vh]"). */
  panelClassName?: string;
  testId?: string;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-2 sm:p-4"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        data-testid={testId}
        className={`flex max-h-[92dvh] w-full flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-xl ${panelClassName}`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
          <div className="min-w-0 flex-1">{title}</div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
