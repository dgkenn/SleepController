'use client';

import Link from 'next/link';
import useSWR from 'swr';
import { fetcher, DiagnosticsReport, DiagVerdict } from '@/lib/api';

const VERDICT_STYLE: Record<DiagVerdict, { dot: string; text: string; label: string }> = {
  HEALTHY: { dot: 'bg-success', text: 'text-success', label: 'OK' },
  DEGRADED: { dot: 'bg-warning', text: 'text-warning', label: 'Degraded' },
  DOWN: { dot: 'bg-danger', text: 'text-danger', label: 'Down' },
};

const UNKNOWN_STYLE = { dot: 'bg-gray-600', text: 'text-gray-500', label: 'Unknown' };

/**
 * Persistent, always-visible health badge -- polls the auth-gated GET /diagnostics summary
 * and shows a color-coded dot + short verdict label. Tapping it opens the full /diagnostics
 * page. Degrades to a grey "Unknown" state on any fetch error/timeout/pre-login state rather
 * than blocking or erroring the page it's mounted in.
 */
export default function HealthBadge() {
  const { data, error } = useSWR<DiagnosticsReport>('/api/diagnostics', fetcher, {
    refreshInterval: 45000,
    revalidateOnFocus: false,
    shouldRetryOnError: true,
    errorRetryInterval: 30000,
  });

  const style = !data || error ? UNKNOWN_STYLE : VERDICT_STYLE[data.verdict] ?? UNKNOWN_STYLE;
  const label = style.label;

  return (
    <Link
      href="/diagnostics"
      aria-label={`System health: ${label}. Open diagnostics.`}
      className="fixed top-0 right-0 z-[60] flex items-center gap-1.5 pl-3 pr-4 py-3 pt-[calc(env(safe-area-inset-top,0px)+0.5rem)] min-h-[44px] min-w-[44px] active:opacity-70"
    >
      <span
        className={`w-2.5 h-2.5 rounded-full ${style.dot} ${
          !data || error ? '' : data.verdict !== 'HEALTHY' ? 'animate-pulseDot' : ''
        }`}
      />
      <span className={`text-[11px] font-medium ${style.text}`}>{label}</span>
    </Link>
  );
}
