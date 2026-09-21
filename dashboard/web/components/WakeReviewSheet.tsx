'use client';

import { useState } from 'react';
import {
  api,
  SuspectedAwakening,
  WakeReviewPayload,
  WakeVerdictValue,
} from '@/lib/api';

/**
 * The morning review. Three taps and a verdict on each awakening the detector thinks it found.
 *
 * Deliberately short. A survey nobody finishes at 6 a.m. measures nothing, so this asks only
 * what changes a decision:
 *   - how rested: the outcome every learner is otherwise scoring itself against;
 *   - the temperature: the open question the n-of-1 trial is running right now;
 *   - how long onset felt: the detector confirmed 96 minutes late on 2026-09-20 and nothing
 *     but the sleeper can say what actually happened;
 *   - each awakening, confirmed or denied. A denial is the only false-alarm evidence the
 *     system has ever had.
 */
interface Props {
  payload: WakeReviewPayload;
  onClose: () => void;
  onToast?: (msg: string) => void;
}

const RESTED = [
  { v: 1, label: 'Wrecked' },
  { v: 2, label: 'Poor' },
  { v: 3, label: 'OK' },
  { v: 4, label: 'Good' },
  { v: 5, label: 'Great' },
];

const TEMPERATURE = [
  { v: 'too_cold', label: 'Too cold' },
  { v: 'bit_cold', label: 'A bit cold' },
  { v: 'right', label: 'Right' },
  { v: 'bit_warm', label: 'A bit warm' },
  { v: 'too_warm', label: 'Too warm' },
];

const ONSET = [
  { v: 'fast', label: 'Fast' },
  { v: 'normal', label: 'Normal' },
  { v: 'slow', label: 'Slow' },
];

const VERDICT_LABEL: Record<WakeVerdictValue, string> = {
  yes: 'Yes',
  no: 'No',
  unsure: 'Not sure',
};

function clock(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function Choice<T extends string | number>({
  options,
  value,
  onPick,
}: {
  options: { v: T; label: string }[];
  value: T | null;
  onPick: (v: T) => void;
}) {
  return (
    <div className="flex gap-1.5">
      {options.map((o) => (
        <button
          key={String(o.v)}
          onClick={() => onPick(o.v)}
          className={`flex-1 min-h-[44px] rounded-xl text-xs font-medium px-1 transition ${
            value === o.v
              ? 'bg-brand text-surface'
              : 'bg-surface-raised border border-surface-border text-gray-400'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export default function WakeReviewSheet({ payload, onClose, onToast }: Props) {
  const prior = payload.review;
  const [rested, setRested] = useState<number | null>(prior?.rested ?? null);
  const [temperature, setTemperature] = useState<string | null>(prior?.temperature ?? null);
  const [onsetFeel, setOnsetFeel] = useState<string | null>(prior?.onset_feel ?? null);
  const [note, setNote] = useState(prior?.note ?? '');
  const [verdicts, setVerdicts] = useState<Record<string, WakeVerdictValue>>(() => {
    const seed: Record<string, WakeVerdictValue> = {};
    (prior?.verdicts ?? []).forEach((v) => {
      seed[v.ts] = v.verdict;
    });
    return seed;
  });
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await api.submitWakeReview({
        night_date: payload.night_date,
        rested,
        temperature,
        onset_feel: onsetFeel,
        note: note.trim() || null,
        verdicts: Object.entries(verdicts).map(([ts, verdict]) => ({ ts, verdict })),
      });
      onToast?.('Thanks — that tunes tonight');
      onClose();
    } catch {
      onToast?.('Could not save the review');
    } finally {
      setSaving(false);
    }
  };

  const awakenings: SuspectedAwakening[] = payload.awakenings ?? [];

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-end sm:items-center justify-center">
      <div className="w-full sm:max-w-md bg-surface-card rounded-t-2xl sm:rounded-2xl border border-surface-border max-h-[92vh] overflow-y-auto">
        <div className="sticky top-0 bg-surface-card px-4 pt-4 pb-3 border-b border-surface-border flex items-center justify-between">
          <div>
            <p className="text-sm font-semibold text-white">Good morning</p>
            <p className="text-[11px] text-gray-500">{payload.night_date}</p>
          </div>
          <button
            onClick={onClose}
            className="text-xs text-gray-500 px-3 py-2 min-h-[40px]"
          >
            Skip
          </button>
        </div>

        <div className="p-4 space-y-5">
          <div className="space-y-2">
            <p className="text-xs text-gray-500 uppercase tracking-wider">How rested?</p>
            <Choice options={RESTED} value={rested} onPick={setRested} />
          </div>

          <div className="space-y-2">
            <p className="text-xs text-gray-500 uppercase tracking-wider">The bed was</p>
            <Choice options={TEMPERATURE} value={temperature} onPick={setTemperature} />
          </div>

          <div className="space-y-2">
            <p className="text-xs text-gray-500 uppercase tracking-wider">Falling asleep felt</p>
            <Choice options={ONSET} value={onsetFeel} onPick={setOnsetFeel} />
          </div>

          <div className="space-y-2">
            <p className="text-xs text-gray-500 uppercase tracking-wider">
              {awakenings.length > 0 ? 'Were you awake at these times?' : 'Awakenings'}
            </p>
            {awakenings.length === 0 && (
              <p className="text-[11px] text-gray-600 leading-relaxed">
                Nothing was flagged overnight. If you remember waking, say so in the note below
                with a time and it counts the same.
              </p>
            )}
            {awakenings.map((a) => (
              <div
                key={a.ts}
                className="bg-surface-raised rounded-xl p-2.5 border border-surface-border space-y-2"
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-sm font-semibold text-white tabular-nums">
                    {clock(a.ts)}
                  </span>
                  <span className="text-[11px] text-gray-600">
                    {a.minutes >= 1 ? `${a.minutes} min` : 'brief'}
                  </span>
                </div>
                <div className="flex gap-1.5">
                  {(['yes', 'no', 'unsure'] as WakeVerdictValue[]).map((v) => (
                    <button
                      key={v}
                      onClick={() => setVerdicts((prev) => ({ ...prev, [a.ts]: v }))}
                      className={`flex-1 min-h-[40px] rounded-lg text-xs font-medium transition ${
                        verdicts[a.ts] === v
                          ? 'bg-brand text-surface'
                          : 'bg-surface-card border border-surface-border text-gray-400'
                      }`}
                    >
                      {VERDICT_LABEL[v]}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <p className="text-xs text-gray-500 uppercase tracking-wider">
              Anything else (optional)
            </p>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              placeholder="e.g. woke cold around 1am"
              className="w-full bg-surface-raised rounded-xl px-3 py-2.5 text-sm text-white placeholder-gray-600 border border-surface-border focus:outline-none focus:border-brand resize-none"
            />
          </div>

          <button
            onClick={save}
            disabled={saving}
            className="w-full min-h-[48px] rounded-xl bg-brand text-surface font-semibold active:scale-[0.98] transition disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Done'}
          </button>
        </div>
      </div>
    </div>
  );
}
