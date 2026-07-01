'use client';

import useSWR from 'swr';
import { DataQualityResponse, GuardrailResponse, fetcher } from '@/lib/api';

/** Safety/quality backstop — live data-quality gate + decision guardrail state.
 *
 * Additive-only surfacing: reads whatever the daemon has published into runtime_state
 * (data_quality / guardrail). Until a daemon publishes those keys, the API reports the
 * neutral "nothing wrong" defaults and this card quietly shows a healthy state.
 */
export default function SafetyGuardrailCard() {
  const { data: dq } = useSWR<DataQualityResponse>('/api/safety/data-quality', fetcher, {
    refreshInterval: 20000,
  });
  const { data: gr } = useSWR<GuardrailResponse>('/api/safety/guardrail', fetcher, {
    refreshInterval: 20000,
  });

  if (!dq && !gr) return null;

  const scorePct = dq?.score != null ? Math.round(dq.score * 100) : null;
  const gating = Boolean(dq?.gating);
  const critical = Boolean(gr?.critical);
  const healthy = !gating && !critical;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Safety backstop</p>
        {healthy ? (
          <span className="text-xs font-semibold text-success uppercase">Nominal</span>
        ) : (
          <span className="flex items-center gap-1.5 text-xs font-semibold text-warning">
            <span className="live-dot" />
            Holding
          </span>
        )}
      </div>

      {scorePct != null && (
        <div>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-gray-500 uppercase tracking-wider">Data quality</span>
            <span className="text-white font-medium tabular-nums">{scorePct}%</span>
          </div>
          <div className="h-2 bg-surface-raised rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full ${
                scorePct < 50 ? 'bg-danger' : scorePct < 80 ? 'bg-warning' : 'bg-success'
              }`}
              style={{ width: `${Math.min(100, scorePct)}%` }}
            />
          </div>
        </div>
      )}

      {dq?.top_reason && (
        <p className="text-sm text-gray-300 leading-relaxed">
          {gating ? 'Holding: ' : 'Watching: '}
          {dq.top_reason.replace(/_/g, ' ')}
        </p>
      )}

      {gr && gr.findings.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {gr.findings.map((f, i) => (
            <span
              key={`${f.code}-${i}`}
              className={`text-[11px] px-2 py-1 rounded-lg border ${
                f.severity === 'critical'
                  ? 'bg-danger/15 border-danger/30 text-danger'
                  : 'bg-surface-raised border-surface-border text-gray-300'
              }`}
              title={f.message}
            >
              {f.code.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
