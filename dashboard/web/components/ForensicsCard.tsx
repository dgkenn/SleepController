'use client';

import useSWR from 'swr';
import { ForensicsResponse, fetcher } from '@/lib/api';

function fmtTemp(f: number | null | undefined): string {
  return f != null ? `${f.toFixed(1)}°F` : '—';
}

/** Awakening forensics — root-cause attribution for recent awakenings. */
export default function ForensicsCard() {
  const { data } = useSWR<ForensicsResponse>('/api/forensics/awakenings', fetcher, {
    refreshInterval: 120000,
  });

  if (!data) return null;

  const { events, summary } = data;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Awakening Forensics</p>
        <span className="text-[10px] text-gray-500 uppercase">{summary.n_awakenings} events</span>
      </div>

      {summary.top_factors.length > 0 && (
        <div>
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Top factors</p>
          <div className="flex flex-wrap gap-1.5">
            {summary.top_factors.map((f, i) => (
              <span
                key={f.factor}
                className={`text-[11px] px-2 py-1 rounded-lg border ${
                  i === 0
                    ? 'bg-warning/15 border-warning/30 text-warning'
                    : 'bg-surface-raised border-surface-border text-gray-300'
                }`}
              >
                {f.factor.replace(/_/g, ' ')}
                <span className="text-gray-500"> ×{f.count}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {events.length > 0 ? (
        <div className="divide-y divide-surface-border">
          {events.map((e, i) => {
            const cause = e.likely_causes[0];
            return (
              <div key={`${e.night_date}-${e.time}-${i}`} className="py-2.5 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-medium text-white">
                    {e.time ?? '—'}
                    <span className="text-gray-600 text-xs font-normal"> · {e.night_date}</span>
                  </p>
                  <span className="text-xs text-warning shrink-0">
                    {e.top_cause.replace(/_/g, ' ')}
                  </span>
                </div>
                {cause && <p className="text-xs text-gray-400 leading-relaxed">{cause.detail}</p>}
                <p className="text-[11px] text-gray-600">
                  bed {fmtTemp(e.bed_temp_f)} · room {fmtTemp(e.room_temp_f)}
                </p>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-gray-500">
          No awakenings recorded yet — nights have been clean.
        </p>
      )}
    </div>
  );
}
