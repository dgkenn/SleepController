'use client';

import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import useSWR from 'swr';
import {
  InsightDecision,
  InsightParameter,
  InsightsDecisionsResponse,
  InsightsParametersResponse,
  fetcher,
} from '@/lib/api';

function fmtValue(v: InsightParameter['value']): string {
  if (v === null || v === undefined) return '—';
  if (Array.isArray(v)) {
    return v
      .map((x) => (typeof x === 'number' ? x.toFixed(2) : String(x)))
      .join(' / ');
  }
  if (typeof v === 'number') return v.toFixed(2);
  return String(v);
}

function fmtTs(ts: string | null): string {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function sourceBadge(source: string | null) {
  const label = source ?? 'unknown';
  const learned = label === 'learned' || label === 'ml' || label === 'self_test' || label === 'comfort_cal';
  return (
    <span
      className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${
        learned
          ? 'bg-success/10 border-success/30 text-success'
          : 'bg-surface-raised border-surface-border text-gray-400'
      }`}
    >
      {label}
    </span>
  );
}

function DecisionRow({ d }: { d: InsightDecision }) {
  return (
    <div className="border-b border-surface-border last:border-0 py-2.5 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-gray-500 shrink-0">{fmtTs(d.ts)}</span>
        <span
          className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border shrink-0 ${
            d.moved
              ? 'bg-brand/10 border-brand/30 text-brand'
              : 'bg-surface-raised border-surface-border text-gray-500'
          }`}
        >
          {d.moved ? 'moved' : 'held'}
        </span>
      </div>
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="text-gray-300 font-medium">{d.state ?? 'unknown'}</span>
        {d.intent && <span className="text-gray-500">· {d.intent.replace(/_/g, ' ')}</span>}
        {d.action && <span className="text-gray-500">· {d.action}</span>}
        {d.target_temp_f != null && (
          <span className="text-white font-semibold ml-auto">{d.target_temp_f.toFixed(1)}°F</span>
        )}
      </div>
      {d.reason && <p className="text-[11px] text-gray-400 leading-snug">{d.reason}</p>}
      <div className="flex items-center gap-3 text-[10px] text-gray-600">
        {d.confidence != null && <span>confidence {(d.confidence * 100).toFixed(0)}%</span>}
        {d.magnitude_f != null && <span>Δ {d.magnitude_f.toFixed(1)}°F</span>}
      </div>
    </div>
  );
}

function DecisionsCard() {
  const { data, error } = useSWR<InsightsDecisionsResponse>(
    '/api/insights/decisions?limit=50',
    fetcher,
    { refreshInterval: 15000 }
  );

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
      <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Why it did that</p>
      <p className="text-[11px] text-gray-500 mb-3">
        Recent controller decisions — state, intent, target, and the reason behind each tick.
      </p>
      {error && (
        <p className="text-sm text-danger py-2">Failed to load decisions.</p>
      )}
      {!data && !error && (
        <div className="flex items-center justify-center py-6">
          <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
        </div>
      )}
      {data && data.decisions.length === 0 && (
        <p className="text-sm text-gray-600 text-center py-4">
          No decisions logged yet — the controller records one every tick once it&apos;s running.
        </p>
      )}
      {data && data.decisions.length > 0 && (
        <div className="max-h-[420px] overflow-y-auto">
          {[...data.decisions].reverse().map((d, i) => (
            <DecisionRow key={`${d.ts}-${i}`} d={d} />
          ))}
        </div>
      )}
    </div>
  );
}

function ParametersCard() {
  const { data, error } = useSWR<InsightsParametersResponse>(
    '/api/insights/parameters',
    fetcher,
    { refreshInterval: 30000 }
  );

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
      <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">What it&apos;s learned</p>
      <p className="text-[11px] text-gray-500 mb-3">
        The currently-active learned parameters, where they came from, and what each one does.
      </p>
      {error && <p className="text-sm text-danger py-2">Failed to load parameters.</p>}
      {!data && !error && (
        <div className="flex items-center justify-center py-6">
          <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
        </div>
      )}
      {data && (
        <div className="space-y-3">
          {data.parameters.map((p) => (
            <div
              key={p.name}
              className="flex items-start justify-between gap-3 border-b border-surface-border last:border-0 pb-3 last:pb-0"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm text-gray-200 font-medium">
                    {p.name.replace(/_/g, ' ')}
                  </span>
                  {sourceBadge(p.source)}
                  {p.version != null && (
                    <span className="text-[10px] text-gray-600">v{p.version}</span>
                  )}
                </div>
                <p className="text-[11px] text-gray-500 leading-snug mt-0.5">{p.what}</p>
              </div>
              <span className="text-sm font-semibold text-white shrink-0">
                {fmtValue(p.value)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function InsightsContent() {
  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Insights</h1>
          <p className="text-sm text-gray-500">Why the controller acted, and what it has learned</p>
        </div>

        <div className="px-4 space-y-4">
          <DecisionsCard />
          <ParametersCard />
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function InsightsPage() {
  return (
    <AuthGuard>
      <InsightsContent />
    </AuthGuard>
  );
}
