'use client';

import Link from 'next/link';
import useSWR from 'swr';
import { fetcher, DiagnosticsReport, DiagVerdict } from '@/lib/api';

const VERDICT_STYLE: Record<DiagVerdict, { bg: string; border: string; text: string; dot: string; label: string }> = {
  HEALTHY: { bg: 'bg-success/10', border: 'border-success/30', text: 'text-success', dot: 'bg-success', label: 'System healthy' },
  DEGRADED: { bg: 'bg-warning/10', border: 'border-warning/30', text: 'text-warning', dot: 'bg-warning', label: 'System degraded' },
  DOWN: { bg: 'bg-danger/10', border: 'border-danger/30', text: 'text-danger', dot: 'bg-danger', label: 'System down' },
};

const UNKNOWN_STYLE = {
  bg: 'bg-gray-700/20',
  border: 'border-gray-600/30',
  text: 'text-gray-400',
  dot: 'bg-gray-600',
  label: 'Health unknown',
};

/**
 * Full-width, unmissable health banner for the top of Home. Reuses the same fused
 * verdict the /diagnostics page and the corner HealthBadge already show (daemon-alive +
 * stale + device-online -- not just SSE transport connectivity, which is all the old
 * header "Live/Polling" dot reflected). Tapping through goes to /diagnostics for detail.
 */
export default function HealthBanner() {
  const { data, error, isLoading } = useSWR<DiagnosticsReport>('/api/diagnostics', fetcher, {
    refreshInterval: 45000,
    revalidateOnFocus: false,
    shouldRetryOnError: true,
    errorRetryInterval: 30000,
  });

  const style = !data || error ? UNKNOWN_STYLE : VERDICT_STYLE[data.verdict] ?? UNKNOWN_STYLE;
  const headline = !data || error
    ? (isLoading ? 'Checking system health…' : "Couldn't reach diagnostics")
    : data.headline;

  return (
    <Link
      href="/diagnostics"
      className={`block rounded-2xl p-4 border ${style.bg} ${style.border} active:opacity-80 transition-opacity`}
      aria-label={`${style.label}: ${headline}. Tap for diagnostics.`}
    >
      <div className="flex items-center gap-3">
        <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${style.dot} ${data && data.verdict !== 'HEALTHY' ? 'animate-pulseDot' : ''}`} />
        <div className="min-w-0 flex-1">
          <p className={`text-sm font-bold ${style.text}`}>{style.label}</p>
          <p className="text-xs text-gray-400 truncate">{headline}</p>
        </div>
        <svg className="w-4 h-4 text-gray-600 shrink-0" viewBox="0 0 24 24" fill="currentColor">
          <path d="M8.59 16.59L13.17 12 8.59 7.41 10 6l6 6-6 6z" />
        </svg>
      </div>
    </Link>
  );
}
