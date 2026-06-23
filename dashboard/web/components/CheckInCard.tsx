'use client';

import { useState } from 'react';
import { api, CheckInResult } from '@/lib/api';

/** Wake-up exit survey. The answers are written into the night's context so the ML
 *  reward + confounder handling use them, and shown back compared to the measured
 *  benchmark score. */
export default function CheckInCard({
  date,
  onDone,
}: {
  date: string | null;
  onDone?: () => void;
}) {
  const [rested, setRested] = useState(6);
  const [grog, setGrog] = useState(3);
  const [energy, setEnergy] = useState(6);
  const [awak, setAwak] = useState(1);
  const [onset, setOnset] = useState('normal');
  const [factors, setFactors] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<CheckInResult | null>(null);

  const toggle = (k: string) => setFactors((f) => ({ ...f, [k]: !f[k] }));

  const submit = async () => {
    setSubmitting(true);
    try {
      const res = await api.submitCheckin({
        date: date ?? undefined,
        rested,
        grogginess: grog,
        daytime_energy: energy,
        awakenings_felt: awak,
        onset_feel: onset,
        factors,
      });
      setResult(res);
      onDone?.();
    } finally {
      setSubmitting(false);
    }
  };

  if (result) {
    const score = result.perfect_sleep?.score;
    return (
      <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-sm font-semibold text-white">Morning check-in saved</p>
          {score != null && (
            <span className="text-xs text-gray-400">
              Measured score{' '}
              <span className="text-brand font-bold">{score.toFixed(0)}/100</span>
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <Gauge label="You felt" value={rested * 10} />
          <Gauge label="Measured" value={score ?? 0} accent />
        </div>
        {result.insights?.map((t, i) => (
          <p key={i} className="text-xs text-gray-400 leading-relaxed">• {t}</p>
        ))}
        <p className="text-[10px] text-gray-600">
          Added to the reward your learner optimises — tailoring the benchmarks to you.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-brand/30 space-y-4">
      <div>
        <p className="text-sm font-semibold text-white">How did you sleep?</p>
        <p className="text-xs text-gray-500">
          A 20-second check-in — it trains the model to your felt experience.
        </p>
      </div>

      <Slider label="How rested do you feel?" value={rested} set={setRested} lowLabel="Wrecked" highLabel="Fully rested" />
      <Slider label="Grogginess on waking" value={grog} set={setGrog} lowLabel="Sharp" highLabel="Very foggy" />
      <Slider label="Expected daytime energy" value={energy} set={setEnergy} lowLabel="Drained" highLabel="Energised" />

      <div>
        <label className="block text-xs text-gray-500 mb-1.5">
          Awakenings you remember: <span className="text-white font-medium">{awak}</span>
        </label>
        <input type="range" min={0} max={6} step={1} value={awak}
          onChange={(e) => setAwak(parseInt(e.target.value))}
          className="w-full accent-brand" />
      </div>

      <div>
        <label className="block text-xs text-gray-500 mb-1.5">How fast did you fall asleep?</label>
        <div className="grid grid-cols-3 gap-2">
          {['quick', 'normal', 'slow'].map((o) => (
            <button key={o} onClick={() => setOnset(o)}
              className={`py-2 rounded-xl text-xs font-medium capitalize min-h-[40px] ${
                onset === o ? 'bg-brand text-surface' : 'bg-surface-raised border border-surface-border text-gray-400'
              }`}>
              {o}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="block text-xs text-gray-500 mb-1.5">Anything last night? (helps exclude off-nights)</label>
        <div className="flex flex-wrap gap-2">
          {[['caffeine', 'Caffeine late'], ['alcohol', 'Alcohol'], ['late_work', 'Late work'],
            ['stress', 'Stress'], ['illness', 'Illness'], ['travel', 'Travel']].map(([k, label]) => (
            <button key={k} onClick={() => toggle(k)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium ${
                factors[k] ? 'bg-warning/20 border border-warning/40 text-warning'
                  : 'bg-surface-raised border border-surface-border text-gray-400'
              }`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <button onClick={submit} disabled={submitting}
        className="w-full py-3 rounded-xl bg-brand text-surface font-semibold active:scale-[0.98] transition disabled:opacity-50">
        {submitting ? 'Saving…' : 'Submit check-in'}
      </button>
    </div>
  );
}

function Slider({ label, value, set, lowLabel, highLabel }: {
  label: string; value: number; set: (n: number) => void; lowLabel: string; highLabel: string;
}) {
  return (
    <div>
      <label className="block text-xs text-gray-500 mb-1.5">
        {label} <span className="text-white font-medium">{value}/10</span>
      </label>
      <input type="range" min={0} max={10} step={1} value={value}
        onChange={(e) => set(parseInt(e.target.value))} className="w-full accent-brand" />
      <div className="flex justify-between text-[10px] text-gray-600 mt-0.5">
        <span>{lowLabel}</span><span>{highLabel}</span>
      </div>
    </div>
  );
}

function Gauge({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className="flex-1 bg-surface-raised rounded-xl p-3 text-center">
      <p className="text-[10px] text-gray-500 uppercase">{label}</p>
      <p className={`text-2xl font-bold tabular-nums ${accent ? 'text-brand' : 'text-white'}`}>
        {Math.round(value)}
      </p>
    </div>
  );
}
