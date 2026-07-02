'use client';

import { useState } from 'react';
import useSWR from 'swr';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import {
  fetcher,
  diagnosticsApi,
  DiagnosticsReport,
  DiagCheck,
  DiagEvent,
  DiagVerdict,
} from '@/lib/api';

const VERDICT_BANNER: Record<DiagVerdict, { bg: string; border: string; text: string; label: string }> = {
  HEALTHY: {
    bg: 'bg-success/10',
    border: 'border-success/30',
    text: 'text-success',
    label: 'Healthy',
  },
  DEGRADED: {
    bg: 'bg-warning/10',
    border: 'border-warning/30',
    text: 'text-warning',
    label: 'Degraded',
  },
  DOWN: {
    bg: 'bg-danger/10',
    border: 'border-danger/30',
    text: 'text-danger',
    label: 'Down',
  },
};

const STATUS_ORDER: Record<DiagCheck['status'], number> = { fail: 0, warn: 1, ok: 2, info: 3 };
const STATUS_STYLE: Record<DiagCheck['status'], { text: string; label: string; dot: string }> = {
  fail: { text: 'text-danger', label: 'FAIL', dot: 'bg-danger' },
  warn: { text: 'text-warning', label: 'WARN', dot: 'bg-warning' },
  ok: { text: 'text-success', label: 'OK', dot: 'bg-success' },
  info: { text: 'text-gray-500', label: 'INFO', dot: 'bg-gray-600' },
};

/** Mirrors app/diagnostics.py's render_diagnosis_text() so "Copy diagnostics" produces the
 * exact same plaintext block a maintainer would get from /diag -- pasteable straight to
 * Claude from a phone. */
function renderDiagnosisText(report: DiagnosticsReport): string {
  const lines = [`=== DIAGNOSIS: ${report.verdict ?? 'UNKNOWN'} ===`];
  lines.push(`! ${report.headline ?? 'unknown'}`);
  if (report.primary_remedy) lines.push(`-> ${report.primary_remedy}`);
  const sorted = [...(report.checks ?? [])].sort(
    (a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]
  );
  for (const c of sorted) {
    const label = STATUS_STYLE[c.status]?.label ?? String(c.status).toUpperCase();
    let line = `[${label.padEnd(4)}] ${c.id}: ${c.detail}`;
    if (c.remedy) line += `  (fix: ${c.remedy})`;
    lines.push(line);
  }
  return lines.join('\n');
}

function CheckRow({ check }: { check: DiagCheck }) {
  const style = STATUS_STYLE[check.status] ?? STATUS_STYLE.info;
  return (
    <div className="py-2.5 border-b border-surface-border last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 shrink-0">
          <span className={`w-2 h-2 rounded-full ${style.dot}`} />
          <span className={`text-[11px] font-semibold ${style.text}`}>{style.label}</span>
        </div>
        <span className="text-sm text-gray-300 text-right flex-1">{check.title}</span>
      </div>
      <p className="text-xs text-gray-500 mt-1 leading-relaxed">{check.detail}</p>
      {check.remedy && (
        <p className="text-xs text-brand mt-1 leading-relaxed">Fix: {check.remedy}</p>
      )}
    </div>
  );
}

function EventRow({ event }: { event: DiagEvent }) {
  const sevColor: Record<string, string> = {
    critical: 'text-danger',
    error: 'text-danger',
    warning: 'text-warning',
    warn: 'text-warning',
    info: 'text-gray-400',
  };
  return (
    <div className="flex gap-2 items-start py-1.5 border-b border-surface-border last:border-b-0 text-xs">
      <span className="text-gray-600 shrink-0 tabular-nums">
        {event.ts ? new Date(event.ts).toLocaleString() : '—'}
      </span>
      <span className={`shrink-0 uppercase ${sevColor[event.severity?.toLowerCase()] ?? 'text-gray-400'}`}>
        {event.severity}
      </span>
      <span className="text-gray-600 shrink-0">{event.category}</span>
      <span className="text-gray-300 break-all">
        {(event.message as string) ?? JSON.stringify(event.data ?? {})}
      </span>
    </div>
  );
}

function DiagnosticsContent() {
  const {
    data: report,
    error: reportError,
    isLoading: reportLoading,
  } = useSWR<DiagnosticsReport>('/api/diagnostics', fetcher, {
    refreshInterval: 30000,
  });

  const {
    data: events,
    error: eventsError,
    isLoading: eventsLoading,
  } = useSWR<DiagEvent[]>('/api/diagnostics/events?limit=100', fetcher, {
    refreshInterval: 60000,
  });

  const [copied, setCopied] = useState(false);

  const copyDiagnostics = async () => {
    if (!report) return;
    const text = renderDiagnosisText(report);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard API unavailable/denied -- silently no-op, button just won't confirm */
    }
  };

  const banner = report ? VERDICT_BANNER[report.verdict] ?? VERDICT_BANNER.DOWN : null;
  const sortedChecks = report
    ? [...report.checks].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status])
    : [];

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Diagnostics</h1>
          <p className="text-sm text-gray-500">Self-diagnosis battery + recent events</p>
        </div>

        <div className="px-4 space-y-4">
          {/* Overall verdict banner */}
          {reportLoading && !report && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {reportError && !report && (
            <div className="bg-surface-card rounded-2xl p-4 border border-danger/30 text-center">
              <p className="text-sm text-danger font-medium">Couldn&apos;t reach diagnostics</p>
              <p className="text-xs text-gray-500 mt-1">
                The API may be down, or this session may need to log in again.
              </p>
            </div>
          )}

          {report && banner && (
            <div className={`rounded-2xl p-4 border ${banner.bg} ${banner.border} space-y-2`}>
              <div className="flex items-center justify-between">
                <span className={`text-sm font-bold uppercase tracking-wider ${banner.text}`}>
                  {banner.label}
                </span>
                <span className="text-[10px] text-gray-500">
                  {new Date(report.generated_at).toLocaleTimeString()}
                </span>
              </div>
              <p className="text-sm text-gray-200">{report.headline}</p>
              {report.primary_remedy && (
                <p className="text-xs text-brand">Fix: {report.primary_remedy}</p>
              )}
              {report.version?.sha && (
                <p className="text-[10px] text-gray-600">
                  commit {report.version.sha} on {report.version.branch ?? 'unknown'}
                </p>
              )}
            </div>
          )}

          {/* Copy diagnostics */}
          {report && (
            <button
              onClick={copyDiagnostics}
              className="w-full text-sm px-4 py-3 rounded-2xl bg-surface-card border border-surface-border text-brand font-medium min-h-[44px] active:bg-surface-raised"
            >
              {copied ? 'Copied ✓' : 'Copy diagnostics'}
            </button>
          )}

          {/* Per-check list */}
          {report && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">
                Checks ({report.checks.length})
              </p>
              <div>
                {sortedChecks.map((c) => (
                  <CheckRow key={c.id} check={c} />
                ))}
              </div>
            </div>
          )}

          {/* Recent structured events */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">Recent events</p>
            {eventsLoading && !events && (
              <p className="text-sm text-gray-600 text-center py-4">Loading…</p>
            )}
            {eventsError && !events && (
              <p className="text-sm text-gray-600 text-center py-4">
                Events unavailable right now
              </p>
            )}
            {events && events.length === 0 && (
              <p className="text-sm text-gray-600 text-center py-4">No recent events</p>
            )}
            {events && events.length > 0 && (
              <div className="max-h-96 overflow-y-auto">
                {events.map((e, i) => (
                  <EventRow key={e.id ?? i} event={e} />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function DiagnosticsPage() {
  return (
    <AuthGuard>
      <DiagnosticsContent />
    </AuthGuard>
  );
}
