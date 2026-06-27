'use client';

import useSWR from 'swr';
import { PreemptionResponse, fetcher } from '@/lib/api';

/** Predictive pre-emption — live view of whether we're heading off an awakening. */
export default function PreemptionCard() {
  const { data } = useSWR<PreemptionResponse>('/api/predictive/preemption', fetcher, {
    refreshInterval: 20000,
  });

  if (!data) return null;

  const risk = data.wake_risk;
  const riskPct = risk != null ? Math.round(risk * 100) : null;
  const chips = [...data.risk_reasons, ...data.precursor_reasons];

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Pre-emption</p>
        {data.preempting ? (
          <span className="flex items-center gap-1.5 text-xs font-semibold text-success">
            <span className="live-dot" />
            Active
          </span>
        ) : (
          <span className="text-xs font-semibold text-gray-500 uppercase">Monitoring</span>
        )}
      </div>

      <p className="text-sm text-gray-300 leading-relaxed">
        {data.preempting
          ? 'Actively pre-empting an awakening.'
          : 'Monitoring for awakening precursors.'}
      </p>

      {riskPct != null && (
        <div>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-gray-500 uppercase tracking-wider">Wake risk</span>
            <span className="text-white font-medium tabular-nums">{riskPct}%</span>
          </div>
          <div className="h-2 bg-surface-raised rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full ${
                riskPct >= 66 ? 'bg-danger' : riskPct >= 33 ? 'bg-warning' : 'bg-success'
              }`}
              style={{ width: `${Math.min(100, riskPct)}%` }}
            />
          </div>
        </div>
      )}

      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {chips.map((r, i) => (
            <span
              key={`${r}-${i}`}
              className="text-[11px] px-2 py-1 rounded-lg bg-surface-raised border border-surface-border text-gray-300"
            >
              {r}
            </span>
          ))}
        </div>
      )}

      {data.recurring_wake_times.length > 0 && (
        <div>
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">
            Wake-risk windows
          </p>
          <div className="flex flex-wrap gap-1.5">
            {data.recurring_wake_times.map((t) => (
              <span
                key={t}
                className="text-xs px-2 py-1 rounded-lg bg-warning/15 border border-warning/30 text-warning"
              >
                ~{t}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
