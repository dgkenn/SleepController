'use client';

import { useEffect, useState } from 'react';

interface DisclosureProps {
  title: string;
  /** One line shown next to the title while collapsed -- the thing you would want to know
   *  WITHOUT opening it (a status, a count, a verdict). */
  summary?: React.ReactNode;
  /** Persist open/closed per section so the app remembers what you care about. */
  storageKey?: string;
  defaultOpen?: boolean;
  tone?: 'neutral' | 'warning' | 'danger';
  children: React.ReactNode;
}

const TONE: Record<NonNullable<DisclosureProps['tone']>, string> = {
  neutral: 'text-gray-400',
  warning: 'text-warning',
  danger: 'text-danger',
};

/**
 * A collapsible group of cards. The phone screens were 3-7 viewports tall because every card was
 * always expanded; the half-asleep reading of a page needs the first screen to be the whole
 * answer and everything else to be one tap away, not one scroll away.
 */
export default function Disclosure({
  title, summary, storageKey, defaultOpen = false, tone = 'neutral', children,
}: DisclosureProps) {
  const [open, setOpen] = useState(defaultOpen);

  useEffect(() => {
    if (!storageKey) return;
    try {
      const v = window.localStorage.getItem(`disclosure:${storageKey}`);
      if (v === '1') setOpen(true);
      if (v === '0') setOpen(false);
    } catch { /* storage unavailable (private mode) -- keep the default */ }
  }, [storageKey]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (storageKey) {
      try { window.localStorage.setItem(`disclosure:${storageKey}`, next ? '1' : '0'); } catch { /* ignore */ }
    }
  };

  return (
    <section className="space-y-3">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-center justify-between gap-3 px-1 min-h-[44px] text-left"
      >
        <span className="text-xs text-gray-500 uppercase tracking-wider shrink-0 whitespace-nowrap">{title}</span>
        <span className="flex items-center gap-2 min-w-0">
          {summary && !open && (
            <span className={`text-xs truncate ${TONE[tone]}`}>{summary}</span>
          )}
          <svg
            viewBox="0 0 24 24" fill="currentColor"
            className={`w-4 h-4 text-gray-600 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
          >
            <path d="M8.59 16.59L13.17 12 8.59 7.41 10 6l6 6-6 6z" />
          </svg>
        </span>
      </button>
      {open && <div className="space-y-4">{children}</div>}
    </section>
  );
}
