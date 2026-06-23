'use client';

import { SleepPlan } from '@/lib/api';

const MODE_META: Record<string, { label: string; color: string; bg: string }> = {
  normal: { label: 'Balanced', color: 'text-cool', bg: 'bg-cool/10 border-cool/30' },
  constrained: { label: 'Short / Work', color: 'text-warning', bg: 'bg-warning/10 border-warning/30' },
  recovery: { label: 'Recovery', color: 'text-success', bg: 'bg-success/10 border-success/30' },
};

function fmtMin(min: number | null | undefined): string {
  if (!min) return '—';
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return h ? `${h}h${m ? ` ${m}m` : ''}` : `${m}m`;
}

export default function SleepPlanCard({ plan }: { plan: SleepPlan }) {
  const meta = MODE_META[plan.mode] ?? MODE_META.normal;
  const t = plan.targets;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Tonight's Plan</p>
        <span className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${meta.bg} ${meta.color}`}>
          {meta.label}
        </span>
      </div>

      {/* Strategy — the "why" */}
      <p className="text-sm text-gray-300 leading-relaxed">{plan.strategy}</p>

      {/* Key numbers */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="bg-surface-raised rounded-xl py-2">
          <p className="text-[10px] text-gray-500 uppercase">Est. sleep</p>
          <p className="text-sm font-bold text-white">{fmtMin(plan.est_sleep_min)}</p>
        </div>
        <div className="bg-surface-raised rounded-xl py-2">
          <p className="text-[10px] text-gray-500 uppercase">Cycles</p>
          <p className="text-sm font-bold text-white">{plan.est_cycles ?? '—'}</p>
        </div>
        <div className="bg-surface-raised rounded-xl py-2">
          <p className="text-[10px] text-gray-500 uppercase">Sleep debt</p>
          <p className="text-sm font-bold text-white">{fmtMin(plan.sleep_debt_min)}</p>
        </div>
      </div>
      <p className="text-[10px] text-gray-600 -mt-1">
        {fmtMin(plan.sleep_opportunity_min)} in bed · ~{Math.round(plan.est_onset_latency_min)} min
        to fall asleep — cycles counted from when you actually sleep, not lights-out.
      </p>

      {/* Literature-backed targets for this mode */}
      <div>
        <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">
          Targets for this mode
        </p>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          <Target label="Deep sleep" val={`${Math.round(t.deep_pct_min * 100)}–${Math.round(t.deep_pct_ideal * 100)}%`} />
          <Target label="REM" val={`${Math.round(t.rem_pct_min * 100)}–${Math.round(t.rem_pct_ideal * 100)}%`} />
          <Target label="Efficiency" val={`≥${Math.round(t.efficiency_min * 100)}%`} />
          <Target label="Onset" val={`≤${t.sol_max_min}m`} />
          <Target label="WASO" val={`≤${t.waso_max_min}m`} />
          <Target label="Awakenings" val={`≤${t.awakenings_max}`} />
          {plan.mode === 'recovery' && (
            <Target label="Sleep target" val={fmtMin(t.total_sleep_target_min)} />
          )}
          <Target label="Wake window" val={`${plan.smart_wake_window_min}m`} />
        </div>
      </div>

      {/* Thermal phasing */}
      <div>
        <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">
          Thermal strategy
        </p>
        <div className="flex flex-wrap gap-1.5">
          {plan.thermal_phases.map((p) => (
            <span
              key={p.name}
              title={p.note}
              className="text-[11px] px-2 py-1 rounded-lg bg-surface-raised border border-surface-border text-gray-300"
            >
              {p.intent.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      </div>

      <p className="text-[10px] text-gray-600 leading-relaxed">
        Benchmarks from Ohayon et&nbsp;al. (NSF Sleep Health 2017; Sleep 2004) and thermal
        sleep physiology; personalised to you by the on-device learner.
      </p>
    </div>
  );
}

function Target({ label, val }: { label: string; val: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-gray-500">{label}</span>
      <span className="text-white font-medium tabular-nums">{val}</span>
    </div>
  );
}
