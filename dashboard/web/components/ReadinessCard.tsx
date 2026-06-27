'use client';

import useSWR from 'swr';
import { ReadinessResponse, fetcher } from '@/lib/api';

const BAND_META: Record<string, { label: string; color: string }> = {
  impaired: { label: 'Impaired', color: 'text-danger' },
  compromised: { label: 'Compromised', color: 'text-warning' },
  adequate: { label: 'Adequate', color: 'text-cool' },
  prime: { label: 'Prime', color: 'text-success' },
};

function fmtMin(min: number | null | undefined): string {
  if (min == null) return '—';
  const sign = min < 0 ? '-' : '';
  const abs = Math.abs(min);
  const h = Math.floor(abs / 60);
  const m = Math.round(abs % 60);
  return h ? `${sign}${h}h${m ? ` ${m}m` : ''}` : `${sign}${m}m`;
}

/** Morning readiness — daytime performance forecast from last night's sleep. */
export default function ReadinessCard() {
  const { data } = useSWR<ReadinessResponse>('/api/morning/readiness', fetcher, {
    refreshInterval: 60000,
  });

  if (!data || data.available === false) return null;

  const meta = (data.band && BAND_META[data.band]) || BAND_META.adequate;
  const score = data.score ?? 0;
  const c = data.components;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Morning Readiness</p>
        <span className={`text-xs font-semibold uppercase tracking-wider ${meta.color}`}>
          {meta.label}
        </span>
      </div>

      <div className="flex items-end gap-3">
        <p className={`text-5xl font-bold tabular-nums ${meta.color}`}>
          {score.toFixed(0)}
          <span className="text-lg text-gray-600 font-normal">/100</span>
        </p>
      </div>

      {c && (
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="bg-surface-raised rounded-xl py-2">
            <p className="text-[10px] text-gray-500 uppercase">Quality</p>
            <p className="text-sm font-bold text-white">{Math.round(c.sleep_quality)}</p>
          </div>
          <div className="bg-surface-raised rounded-xl py-2">
            <p className="text-[10px] text-gray-500 uppercase">Recovery</p>
            <p className="text-sm font-bold text-white">{Math.round(c.recovery)}</p>
          </div>
          <div className="bg-surface-raised rounded-xl py-2">
            <p className="text-[10px] text-gray-500 uppercase">Continuity</p>
            <p className="text-sm font-bold text-white">{Math.round(c.continuity)}</p>
          </div>
        </div>
      )}

      {data.debt_min != null && (
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500 uppercase tracking-wider">Sleep debt</span>
          <span className="text-white font-medium tabular-nums">{fmtMin(data.debt_min)}</span>
        </div>
      )}

      {data.flags && data.flags.length > 0 && (
        <div className="space-y-1.5">
          {data.flags.map((f) => (
            <div
              key={f.flag}
              className={`rounded-xl px-3 py-2 text-xs border ${
                f.severity === 'high'
                  ? 'bg-danger/10 border-danger/30 text-danger'
                  : f.severity === 'medium'
                    ? 'bg-warning/10 border-warning/30 text-warning'
                    : 'bg-surface-raised border-surface-border text-gray-400'
              }`}
            >
              {f.message}
            </div>
          ))}
        </div>
      )}

      {data.recommendation && (
        <p className="text-xs text-gray-400 leading-relaxed">{data.recommendation}</p>
      )}
    </div>
  );
}
