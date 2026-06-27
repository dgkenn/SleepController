'use client';

import useSWR from 'swr';
import { fetcher } from '@/lib/api';

interface ModeTargets {
  targets: {
    deep_pct: [number, number];
    rem_pct: [number, number];
    efficiency_min: number;
    sol_max_min: number;
    waso_max_min: number;
    awakenings_max: number;
    total_sleep_target_min: number;
  };
  prior: Record<string, number>;
  personalized: Record<string, number>;
  is_personalized: boolean;
  rationale: string;
}
interface PerfectWeights {
  active_mode: string;
  modes: Record<string, ModeTargets>;
}

const pct = (x: number) => `${Math.round(x * 100)}%`;
const hm = (m: number) => `${Math.floor(m / 60)}h${m % 60 ? ` ${Math.round(m % 60)}m` : ''}`;

/** Tonight's "perfect sleep" targets to hit, and which metrics matter most for THIS user
 *  (personalized by revealed preference vs the evidence-based default). */
export default function TargetsCard() {
  const { data } = useSWR<PerfectWeights>('/api/perfect-weights', fetcher, { refreshInterval: 60000 });
  if (!data) return null;
  const m = data.modes[data.active_mode];
  if (!m) return null;
  const t = m.targets;
  const rows: Array<[string, string]> = [
    ['Deep sleep', `${pct(t.deep_pct[0])}–${pct(t.deep_pct[1])}`],
    ['REM', `${pct(t.rem_pct[0])}–${pct(t.rem_pct[1])}`],
    ['Efficiency', `≥ ${pct(t.efficiency_min)}`],
    ['Onset', `≤ ${t.sol_max_min}m`],
    ['WASO', `≤ ${t.waso_max_min}m`],
    ['Awakenings', `≤ ${t.awakenings_max}`],
    ['Total sleep', hm(t.total_sleep_target_min)],
  ];
  const keys = Object.keys(m.personalized).sort((a, b) => m.personalized[b] - m.personalized[a]);
  const maxW = Math.max(...keys.map((k) => m.personalized[k]), 0.001);

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">
          Tonight&apos;s targets · {data.active_mode}
        </p>
        <span className={`text-[10px] uppercase tracking-wider ${m.is_personalized ? 'text-success' : 'text-gray-500'}`}>
          {m.is_personalized ? 'tuned to you' : 'evidence default'}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-2">
        {rows.map(([label, val]) => (
          <div key={label} className="flex items-center justify-between">
            <span className="text-xs text-gray-500">{label}</span>
            <span className="text-xs text-white font-medium">{val}</span>
          </div>
        ))}
      </div>

      <div>
        <p className="text-[11px] text-gray-500 uppercase tracking-wider mb-1.5">
          What matters most for you
        </p>
        <div className="space-y-1.5">
          {keys.slice(0, 5).map((k) => {
            const p = m.personalized[k];
            const pr = m.prior[k] ?? 0;
            const shifted = Math.abs(p - pr) > 0.005;
            return (
              <div key={k} className="flex items-center gap-2">
                <span className="text-xs text-gray-400 w-24 capitalize">{k}</span>
                <div className="flex-1 h-2 bg-surface-raised rounded">
                  <div className="h-2 bg-brand rounded" style={{ width: `${(p / maxW) * 100}%` }} />
                </div>
                {shifted && (
                  <span className={`text-[10px] w-3 ${p > pr ? 'text-success' : 'text-gray-500'}`}>
                    {p > pr ? '↑' : '↓'}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <p className="text-[11px] text-gray-600 leading-relaxed">{m.rationale}</p>
    </div>
  );
}
