'use client';

import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import ConfidenceMeter from '@/components/ConfidenceMeter';
import RecommendationCard from '@/components/RecommendationCard';
import MaintenanceCard from '@/components/MaintenanceCard';
import ForensicsCard from '@/components/ForensicsCard';
import ExperimentsCard from '@/components/ExperimentsCard';
import EfficacyCard from '@/components/EfficacyCard';
import TargetsCard from '@/components/TargetsCard';
import LearningPhasesCard from '@/components/LearningPhasesCard';
import useSWR from 'swr';
import { LearningLedgerResponse, MLOverview, fetcher } from '@/lib/api';

const SOURCE_BADGE: Record<string, string> = {
  learned: 'bg-success/15 text-success border-success/30',
  measured: 'bg-brand/15 text-brand border-brand/30',
  preset: 'bg-gray-700/40 text-gray-400 border-gray-600/50',
};

function LedgerConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const color = pct >= 70 ? 'bg-success' : pct >= 40 ? 'bg-warning' : 'bg-danger';
  return (
    <div className="flex items-center gap-2 min-w-[72px]">
      <div className="flex-1 h-1.5 bg-surface-border rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[10px] text-gray-500 tabular-nums w-7 text-right">{pct}%</span>
    </div>
  );
}

/** "What the system has learned" — a unified ledger across EVERY independent learner (onset,
 *  settle, lead time, wake ramp/tuning, deepening, setpoints, thermal calibration, comfort
 *  profile, resting baseline, baselines), each with its current value, data source, maturity
 *  and a heuristic confidence — plus any advisory contradiction warnings (two learners quietly
 *  pulling the same phase's temperature opposite ways). Read-model only; nothing here changes
 *  controller behavior. */
function LearningLedgerSection() {
  const { data } = useSWR<LearningLedgerResponse>('/api/learning/ledger', fetcher, {
    refreshInterval: 60000,
  });
  if (!data) return null;

  const { entries, contradictions } = data;
  if (!entries.length) return null;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">
          What the system has learned
        </p>
        <span className="text-[10px] text-gray-600">{entries.length} learners</span>
      </div>

      {contradictions.length > 0 && (
        <div className="bg-warning/10 border border-warning/30 rounded-xl px-3 py-2 space-y-1.5">
          <p className="text-warning text-xs font-medium">
            {contradictions.length} advisory contradiction{contradictions.length !== 1 ? 's' : ''}
          </p>
          {contradictions.map((w, i) => (
            <p key={i} className="text-[11px] text-warning/90 leading-snug">
              {w.message}
            </p>
          ))}
        </div>
      )}

      <div className="divide-y divide-surface-border">
        {entries.map((e) => (
          <div key={e.name} className="py-2.5 flex items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <p className="text-sm font-medium text-white truncate">{e.name}</p>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded border ${
                    SOURCE_BADGE[e.source] ?? SOURCE_BADGE.preset
                  }`}
                >
                  {e.source}
                </span>
              </div>
              <p className="text-[11px] text-gray-500 leading-snug truncate">{e.note}</p>
              <p className="text-[10px] text-gray-600">
                {e.value != null ? `${e.value.toFixed(2)} ${e.unit}` : 'n/a'} · {e.maturity}{' '}
                {e.maturity === 1 ? 'sample' : 'samples'}
              </p>
            </div>
            <LedgerConfidenceBar value={e.confidence} />
          </div>
        ))}
      </div>

      <p className="text-[10px] text-gray-600 leading-snug pt-1 border-t border-surface-border/60">
        Read-only view of every learner's current state — nothing here changes the controller.
        Contradictions are advisory only and are never auto-resolved.
      </p>
    </div>
  );
}

function LearningContent() {
  const { data, error } = useSWR<MLOverview>('/api/ml/overview', fetcher, {
    refreshInterval: 30000,
  });

  if (error) {
    return (
      <div className="flex flex-col min-h-screen">
        <div className="flex-1 flex items-center justify-center text-danger text-sm px-6 text-center">
          Failed to load ML data. Check that the backend is running.
        </div>
        <BottomNav />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col min-h-screen">
        <div className="flex-1 flex items-center justify-center">
          <div className="w-8 h-8 border-2 border-brand border-t-transparent rounded-full animate-spin" />
        </div>
        <BottomNav />
      </div>
    );
  }

  const nightsNeeded = Math.max(0, data.min_nights - data.clean_nights);

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Learning</h1>
          <p className="text-sm text-gray-500">ML model status and insights</p>
        </div>

        <div className="px-4 space-y-4">
          {/* What "perfect sleep" means tonight — targets to hit + personalized weights */}
          <TargetsCard />

          {/* What's learned across all three phases (onset / maintenance / wake), per night-type */}
          <LearningPhasesCard />

          {/* Meta-learning ledger: every learner's current value/source/maturity/confidence,
              plus advisory contradiction warnings */}
          <LearningLedgerSection />

          {/* Sleep maintenance: prevent + handle awakenings */}
          <MaintenanceCard />

          {/* Awakening forensics: root-cause attribution */}
          <ForensicsCard />

          {/* Self-experiments: A/B testing sleep levers */}
          <ExperimentsCard />

          {/* Standing efficacy trial: does the closed loop actually help? */}
          <EfficacyCard />

          {/* Model confidence */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider">Model Confidence</p>
            <ConfidenceMeter value={data.model_confidence} size="lg" />
            <div className="flex items-center justify-between text-sm">
              <div>
                <p className="text-gray-500 text-xs">Clean nights</p>
                <p className="text-white font-bold text-xl">{data.clean_nights}</p>
              </div>
              <div className="text-right">
                <p className="text-gray-500 text-xs">Minimum needed</p>
                <p className="text-white font-bold text-xl">{data.min_nights}</p>
              </div>
            </div>
            {nightsNeeded > 0 && (
              <div className="bg-warning/10 border border-warning/30 rounded-xl px-3 py-2">
                <p className="text-warning text-sm">
                  {nightsNeeded} more clean night{nightsNeeded !== 1 ? 's' : ''} needed for full confidence
                </p>
              </div>
            )}
          </div>

          {/* Recommendation */}
          <RecommendationCard recommendation={data.recommendation} />

          {/* Setpoint */}
          {data.setpoint && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                Learned Setpoint
              </p>
              <div className="grid grid-cols-2 gap-3">
                {[
                  ['Neutral', `${data.setpoint.neutral_f.toFixed(1)}°F`],
                  ['Deep Sleep Bias', `${data.setpoint.deep_bias_f > 0 ? '+' : ''}${data.setpoint.deep_bias_f.toFixed(1)}°F`],
                  ['REM Warm Offset', `${data.setpoint.rem_warm_offset_f > 0 ? '+' : ''}${data.setpoint.rem_warm_offset_f.toFixed(1)}°F`],
                  ['Wake Ramp', `${data.setpoint.wake_ramp_f > 0 ? '+' : ''}${data.setpoint.wake_ramp_f.toFixed(1)}°F`],
                  ['Bed Weight', data.setpoint.composite_bed_weight.toFixed(2)],
                  ['Source', data.setpoint.source],
                ].map(([label, val]) => (
                  <div key={label}>
                    <p className="text-xs text-gray-500">{label}</p>
                    <p className="text-white font-semibold">{val}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Recent actions */}
          {data.actions && data.actions.length > 0 && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                Recent Actions
              </p>
              <div className="divide-y divide-surface-border">
                {data.actions.slice(0, 10).map((a, i) => (
                  <div key={i} className="py-2.5 flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-white truncate">{a.action}</p>
                      <p className="text-xs text-gray-500">
                        {a.date} · {a.source}
                      </p>
                    </div>
                    <div className="text-right shrink-0">
                      <p className="text-xs text-gray-400">
                        {((a.confidence ?? 0) * 100).toFixed(0)}%
                      </p>
                      {a.reward == null ? (
                        <p className="text-xs font-medium text-gray-500">pending</p>
                      ) : (
                        <p
                          className={`text-xs font-medium ${
                            a.reward >= 0 ? 'text-success' : 'text-danger'
                          }`}
                        >
                          {a.reward >= 0 ? '+' : ''}{a.reward.toFixed(2)}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Phenotype */}
          {data.phenotype && data.phenotype.length > 0 && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                Sleep Phenotype
              </p>
              <div className="space-y-2">
                {data.phenotype.map((p) => (
                  <div key={p.feature} className="flex items-center gap-3">
                    <div className="flex-1">
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-gray-300">{p.feature}</span>
                        <span className="text-gray-500">r={p.r.toFixed(2)} n={p.n}</span>
                      </div>
                      <div className="h-1.5 bg-surface-border rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            p.r >= 0 ? 'bg-success' : 'bg-danger'
                          }`}
                          style={{ width: `${Math.abs(p.r) * 100}%`, marginLeft: p.r < 0 ? 'auto' : undefined }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Baselines */}
          {Object.keys(data.baselines ?? {}).length > 0 && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">Baselines</p>
              <div className="grid grid-cols-2 gap-3">
                {Object.entries(data.baselines).map(([k, v]) => (
                  <div key={k}>
                    <p className="text-xs text-gray-500">{k}</p>
                    <p className="text-white font-medium text-sm">
                      {typeof v === 'number' ? v.toFixed(2) : String(v)}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function LearningPage() {
  return (
    <AuthGuard>
      <LearningContent />
    </AuthGuard>
  );
}
