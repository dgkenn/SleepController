'use client';

import { useState, useEffect } from 'react';
import { api } from '@/lib/api';

interface QuickTempProps {
  targetF: number | null;
  powerOn: boolean;
  step?: number;
}

/** Realtime bed-temperature control on the Home screen. The +/- buttons send a
 *  relative nudge the daemon applies within ~1s; the value reconciles with the
 *  live status stream. */
export default function QuickTemp({ targetF, powerOn, step = 1 }: QuickTempProps) {
  const [optimistic, setOptimistic] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  // Reconcile with live status whenever it moves.
  useEffect(() => {
    setOptimistic(null);
  }, [targetF]);

  const shown = optimistic ?? targetF;

  const nudge = async (delta: number) => {
    if (!powerOn) return;
    const base = shown ?? 70;
    const next = Math.max(55, Math.min(110, Math.round((base + delta) * 10) / 10));
    setOptimistic(next);
    setBusy(true);
    try {
      await api.nudgeTemp(delta);
    } catch {
      setOptimistic(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Adjust Temperature</p>
        <span className="text-[10px] text-gray-600">realtime</span>
      </div>
      <div className="flex items-center justify-between gap-4">
        <button
          onClick={() => nudge(-step)}
          disabled={!powerOn || busy}
          className="w-14 h-14 rounded-2xl bg-surface-raised border border-surface-border text-3xl font-light text-white flex items-center justify-center active:scale-90 transition disabled:opacity-30"
          aria-label="Cooler"
        >
          −
        </button>
        <div className="text-center">
          <span className="text-3xl font-bold text-white tabular-nums">
            {powerOn && shown != null ? shown.toFixed(1) : '--'}
          </span>
          <span className="text-base text-gray-400">°F</span>
          <p className="text-[11px] text-gray-600 mt-0.5">
            {powerOn ? (shown != null && shown > 80 ? 'warming' : 'cooling') : 'bed off'}
          </p>
        </div>
        <button
          onClick={() => nudge(step)}
          disabled={!powerOn || busy}
          className="w-14 h-14 rounded-2xl bg-surface-raised border border-surface-border text-3xl font-light text-white flex items-center justify-center active:scale-90 transition disabled:opacity-30"
          aria-label="Warmer"
        >
          +
        </button>
      </div>
    </div>
  );
}
