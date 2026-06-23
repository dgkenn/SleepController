'use client';

import useSWR from 'swr';
import { MaintenanceSummary, fetcher } from '@/lib/api';

/** Sleep-maintenance: the learned/preset awakening pattern (prevent) + recent handling. */
export default function MaintenanceCard() {
  const { data } = useSWR<MaintenanceSummary>('/api/maintenance', fetcher, {
    refreshInterval: 60000,
  });
  if (!data) return null;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold text-white">Sleep Maintenance</p>
        <span className="text-[10px] text-gray-500 uppercase">prevent + handle</span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-center">
        <div className="bg-surface-raised rounded-xl py-2">
          <p className="text-[10px] text-gray-500 uppercase">Avg awakenings</p>
          <p className="text-lg font-bold text-white">{data.avg_wake_events ?? '—'}</p>
        </div>
        <div className="bg-surface-raised rounded-xl py-2">
          <p className="text-[10px] text-gray-500 uppercase">Avg WASO</p>
          <p className="text-lg font-bold text-white">
            {data.avg_waso_min != null ? `${Math.round(data.avg_waso_min)}m` : '—'}
          </p>
        </div>
      </div>

      <div>
        <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">
          Your wake-risk windows
        </p>
        {data.recurring_wake_times.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {data.recurring_wake_times.map((t) => (
              <span key={t} className="text-xs px-2 py-1 rounded-lg bg-warning/15 border border-warning/30 text-warning">
                ~{t}
              </span>
            ))}
            <span className="text-xs px-2 py-1 rounded-lg bg-surface-raised border border-surface-border text-gray-400">
              + cycle boundaries · 3:30–5:30 nadir
            </span>
          </div>
        ) : (
          <p className="text-xs text-gray-500">
            Using the evidence-based preset (cycle boundaries, lighter back-half, 3:30–5:30
            circadian nadir) until your personal pattern is learned.
          </p>
        )}
        {data.personal_warm_threshold_f != null && (
          <p className="text-[11px] text-gray-500 mt-1.5">
            Personal warm threshold: bed ≥ {data.personal_warm_threshold_f.toFixed(1)}°F
          </p>
        )}
      </div>

      <p className="text-xs text-gray-400 leading-relaxed">{data.strategy}</p>
    </div>
  );
}
