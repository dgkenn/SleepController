'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Experiment, Analysis, api, fetcher } from '@/lib/api';

const METRICS = [
  'wake_events',
  'waso_min',
  'sleep_efficiency',
  'deep_min',
  'rem_min',
  'total_sleep_min',
  'sleep_onset_latency_min',
  'avg_hrv',
  'outcome_score',
] as const;

const STATUS_META: Record<string, { color: string; label: string }> = {
  active: { color: 'bg-cool/15 border-cool/30 text-cool', label: 'Active' },
  complete: { color: 'bg-success/15 border-success/30 text-success', label: 'Complete' },
  stopped: { color: 'bg-surface-raised border-surface-border text-gray-400', label: 'Stopped' },
};

function armCounts(assignments: Record<string, 'a' | 'b'>): { a: number; b: number } {
  let a = 0;
  let b = 0;
  for (const v of Object.values(assignments)) {
    if (v === 'a') a += 1;
    else if (v === 'b') b += 1;
  }
  return { a, b };
}

function AnalysisView({ analysis }: { analysis: Analysis }) {
  return (
    <div className="bg-surface-raised rounded-xl px-3 py-2 space-y-1 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-gray-500 uppercase tracking-wider">Result</span>
        {analysis.winner ? (
          <span className="text-success font-medium">Winner: {analysis.winner}</span>
        ) : (
          <span className="text-gray-500">No clear winner</span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-gray-400">
        <span>
          Control: {analysis.control.mean != null ? analysis.control.mean.toFixed(2) : '—'} (n=
          {analysis.control.n})
        </span>
        <span>
          Treatment: {analysis.treatment.mean != null ? analysis.treatment.mean.toFixed(2) : '—'} (n=
          {analysis.treatment.n})
        </span>
        {analysis.diff != null && <span>Diff: {analysis.diff.toFixed(2)}</span>}
        {analysis.effect_size != null && <span>Effect: {analysis.effect_size.toFixed(2)}</span>}
      </div>
      <p className="text-gray-400 leading-relaxed">{analysis.recommendation}</p>
    </div>
  );
}

function ExperimentRow({
  exp,
  onChanged,
}: {
  exp: Experiment;
  onChanged: () => void;
}) {
  const [analysis, setAnalysis] = useState<Analysis | null>(exp.result);
  const [busy, setBusy] = useState<string | null>(null);
  const counts = armCounts(exp.assignments);
  const meta = STATUS_META[exp.status] ?? STATUS_META.stopped;

  const handleAnalyze = async () => {
    setBusy('analyze');
    try {
      const res = await api.analyzeExperiment(exp.id);
      setAnalysis(res.analysis);
    } catch {
      /* ignore */
    } finally {
      setBusy(null);
    }
  };

  const handleStop = async () => {
    setBusy('stop');
    try {
      await api.stopExperiment(exp.id);
      onChanged();
    } catch {
      /* ignore */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="py-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-white truncate">{exp.name}</p>
        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border shrink-0 ${meta.color}`}>
          {meta.label}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
        <span>metric: {exp.metric}</span>
        <span>·</span>
        <span>
          {exp.arm_a.label}: {counts.a}
        </span>
        <span>
          {exp.arm_b.label}: {counts.b}
        </span>
        <span className="text-gray-600">(min {exp.min_nights_per_arm}/arm)</span>
      </div>

      {analysis && <AnalysisView analysis={analysis} />}

      {exp.status === 'active' && (
        <div className="flex gap-2">
          <button
            onClick={handleAnalyze}
            disabled={busy != null}
            className="text-xs px-3 py-1.5 rounded-lg bg-surface-raised border border-surface-border text-gray-300 disabled:opacity-50"
          >
            {busy === 'analyze' ? 'Analyzing…' : 'Analyze'}
          </button>
          <button
            onClick={handleStop}
            disabled={busy != null}
            className="text-xs px-3 py-1.5 rounded-lg bg-danger/10 border border-danger/30 text-danger disabled:opacity-50"
          >
            {busy === 'stop' ? 'Stopping…' : 'Stop'}
          </button>
        </div>
      )}
    </div>
  );
}

/** A/B experiments — define, run, and analyze self-experiments on sleep levers. */
export default function ExperimentsCard() {
  const { data, mutate } = useSWR<{ experiments: Experiment[] }>('/api/experiments', fetcher, {
    refreshInterval: 60000,
  });

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [metric, setMetric] = useState<string>('outcome_score');
  const [armA, setArmA] = useState('Control');
  const [armB, setArmB] = useState('Treatment');
  const [minNights, setMinNights] = useState(7);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState('');

  const handleCreate = async () => {
    if (!name.trim()) {
      setErr('Name is required');
      return;
    }
    setSubmitting(true);
    setErr('');
    try {
      await api.createExperiment({
        name: name.trim(),
        hypothesis: '',
        variable: metric,
        metric,
        min_nights_per_arm: minNights,
        arm_a: { label: armA.trim() || 'Control', params: {} },
        arm_b: { label: armB.trim() || 'Treatment', params: {} },
      });
      setName('');
      setArmA('Control');
      setArmB('Treatment');
      setMinNights(7);
      setShowForm(false);
      await mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to create experiment');
    } finally {
      setSubmitting(false);
    }
  };

  const experiments = data?.experiments ?? [];

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Experiments</p>
        <button
          onClick={() => setShowForm((s) => !s)}
          className="text-xs px-3 py-1.5 rounded-lg bg-brand/15 border border-brand/30 text-brand"
        >
          {showForm ? 'Cancel' : 'New experiment'}
        </button>
      </div>

      {showForm && (
        <div className="bg-surface-raised rounded-xl p-3 space-y-2.5">
          <div>
            <label className="text-[10px] text-gray-500 uppercase tracking-wider">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Cooler deep-sleep bias"
              className="mt-1 w-full bg-surface-card border border-surface-border rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand"
            />
          </div>
          <div>
            <label className="text-[10px] text-gray-500 uppercase tracking-wider">Metric</label>
            <select
              value={metric}
              onChange={(e) => setMetric(e.target.value)}
              className="mt-1 w-full bg-surface-card border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand"
            >
              {METRICS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-[10px] text-gray-500 uppercase tracking-wider">Arm A</label>
              <input
                value={armA}
                onChange={(e) => setArmA(e.target.value)}
                className="mt-1 w-full bg-surface-card border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand"
              />
            </div>
            <div>
              <label className="text-[10px] text-gray-500 uppercase tracking-wider">Arm B</label>
              <input
                value={armB}
                onChange={(e) => setArmB(e.target.value)}
                className="mt-1 w-full bg-surface-card border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand"
              />
            </div>
          </div>
          <div>
            <label className="text-[10px] text-gray-500 uppercase tracking-wider">
              Min nights per arm
            </label>
            <input
              type="number"
              min={1}
              value={minNights}
              onChange={(e) => setMinNights(Math.max(1, Number(e.target.value) || 1))}
              className="mt-1 w-full bg-surface-card border border-surface-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand"
            />
          </div>
          {err && <p className="text-xs text-danger">{err}</p>}
          <button
            onClick={handleCreate}
            disabled={submitting}
            className="w-full text-sm font-medium px-3 py-2 rounded-lg bg-brand text-white disabled:opacity-50"
          >
            {submitting ? 'Creating…' : 'Create experiment'}
          </button>
        </div>
      )}

      {experiments.length > 0 ? (
        <div className="divide-y divide-surface-border">
          {experiments.map((exp) => (
            <ExperimentRow key={exp.id} exp={exp} onChanged={() => mutate()} />
          ))}
        </div>
      ) : (
        !showForm && (
          <p className="text-xs text-gray-500">
            No experiments yet. Create one to A/B test a sleep lever against your metrics.
          </p>
        )
      )}
    </div>
  );
}
