'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { EfficacyAnalysis, EfficacyMetricComparison, api, fetcher } from '@/lib/api';

const METRIC_LABELS: Record<string, string> = {
  wake_events: 'Awakenings / night',
  deep_pct: 'Deep sleep %',
  efficiency: 'Sleep efficiency',
};

function fmt(v: number | null, pct = false): string {
  if (v == null) return '—';
  return pct ? `${(v * 100).toFixed(1)}%` : v.toFixed(2);
}

function MetricRow({ name, m }: { name: string; m: EfficacyMetricComparison }) {
  const pct = name !== 'wake_events';
  const significant = m.p_value != null && m.p_value < 0.05;
  return (
    <div className="py-2 border-b border-surface-border last:border-0">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400">{METRIC_LABELS[name] ?? name}</span>
        {significant && (
          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-success/15 border border-success/30 text-success">
            significant
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-4 mt-1 text-xs text-gray-500">
        <span>
          Controlled: {fmt(m.controlled.mean, pct)} (n={m.controlled.n})
        </span>
        <span>
          Held: {fmt(m.held.mean, pct)} (n={m.held.n})
        </span>
      </div>
      {m.diff_held_minus_controlled != null && (
        <p className="text-[11px] text-gray-600 mt-0.5">
          Δ (held − controlled): {fmt(m.diff_held_minus_controlled, pct)}
          {m.ci ? ` · 95% CI [${fmt(m.ci[0], pct)}, ${fmt(m.ci[1], pct)}]` : ''}
          {m.p_value != null ? ` · p=${m.p_value.toFixed(3)}` : ''}
        </p>
      )}
    </div>
  );
}

function AnalysisView({ analysis }: { analysis: EfficacyAnalysis }) {
  return (
    <div className="bg-surface-raised rounded-xl px-3 py-2 space-y-1">
      <p className="text-xs text-gray-300 leading-relaxed">{analysis.verdict}</p>
      <div className="divide-y divide-surface-border">
        {Object.entries(analysis.metrics).map(([name, m]) => (
          <MetricRow key={name} name={name} m={m} />
        ))}
      </div>
    </div>
  );
}

/** "Is the controller helping?" — the standing CONTROLLED vs HELD (do-no-harm neutral baseline)
 * efficacy trial. Opt-in (default OFF): every night is auto-assigned an arm and compared over
 * time with significance, so the answer to "does the closed loop actually help?" is measured,
 * not assumed. */
export default function EfficacyCard() {
  const { data, mutate } = useSWR('/api/efficacy', fetcher, { refreshInterval: 60000 });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const config = data?.config;
  const analysis: EfficacyAnalysis | undefined = data?.analysis;

  const toggle = async () => {
    if (!config) return;
    setBusy(true);
    setErr('');
    try {
      await api.updateEfficacyConfig({ enabled: !config.enabled });
      await mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to update');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Is the controller helping?</p>
        <button
          onClick={toggle}
          disabled={busy || !config}
          className={`text-xs px-3 py-1.5 rounded-lg border disabled:opacity-50 ${
            config?.enabled
              ? 'bg-danger/10 border-danger/30 text-danger'
              : 'bg-brand/15 border-brand/30 text-brand'
          }`}
        >
          {busy ? 'Saving…' : config?.enabled ? 'Disable trial' : 'Enable trial'}
        </button>
      </div>

      <p className="text-[11px] text-gray-500 leading-relaxed">
        A standing background trial: each night is auto-assigned either the normal closed loop
        (CONTROLLED) or a do-no-harm fixed-neutral baseline (HELD — steering and pre-emption off,
        still clamped, still smart-wake). Compared over time with significance. Off by default.
      </p>

      {err && <p className="text-xs text-danger">{err}</p>}

      {config?.enabled && analysis && <AnalysisView analysis={analysis} />}

      {!config?.enabled && (
        <p className="text-xs text-gray-500">Trial is disabled — enable it to start comparing nights.</p>
      )}
    </div>
  );
}
