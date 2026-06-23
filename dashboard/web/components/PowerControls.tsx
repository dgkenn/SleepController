'use client';

import { useState } from 'react';
import { api } from '@/lib/api';

interface PowerControlsProps {
  powerOn: boolean;
  away: boolean;
  onChanged?: () => void;
  onToast?: (msg: string) => void;
}

/** Bed power, away mode, and prime — parity with the official Eight Sleep app. */
export default function PowerControls({
  powerOn,
  away,
  onChanged,
  onToast,
}: PowerControlsProps) {
  const [busy, setBusy] = useState<string | null>(null);

  const run = async (key: string, fn: () => Promise<unknown>, msg: string) => {
    setBusy(key);
    try {
      await fn();
      onToast?.(msg);
      onChanged?.();
    } catch {
      onToast?.('Command failed');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">Bed</p>

      {/* Power toggle */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-white">Power</p>
          <p className="text-xs text-gray-500">
            {powerOn ? 'Bed is heating/cooling' : 'Bed is off'}
          </p>
        </div>
        <button
          onClick={() =>
            powerOn
              ? run('power', api.powerOff, 'Bed turned off')
              : run('power', api.powerOn, 'Bed turned on')
          }
          disabled={busy === 'power'}
          className={`relative w-14 h-8 rounded-full transition-colors disabled:opacity-50 ${
            powerOn ? 'bg-success' : 'bg-surface-raised border border-surface-border'
          }`}
          aria-label="Toggle bed power"
        >
          <span
            className={`absolute top-1 w-6 h-6 rounded-full bg-white transition-transform ${
              powerOn ? 'translate-x-7' : 'translate-x-1'
            }`}
          />
        </button>
      </div>

      {/* Away mode */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-white">Away mode</p>
          <p className="text-xs text-gray-500">
            {away ? 'On — bed paused while traveling' : 'Off'}
          </p>
        </div>
        <button
          onClick={() =>
            away
              ? run('away', api.awayOff, 'Away mode off')
              : run('away', api.awayOn, 'Away mode on')
          }
          disabled={busy === 'away'}
          className={`relative w-14 h-8 rounded-full transition-colors disabled:opacity-50 ${
            away ? 'bg-warning' : 'bg-surface-raised border border-surface-border'
          }`}
          aria-label="Toggle away mode"
        >
          <span
            className={`absolute top-1 w-6 h-6 rounded-full bg-white transition-transform ${
              away ? 'translate-x-7' : 'translate-x-1'
            }`}
          />
        </button>
      </div>

      {/* Prime */}
      <button
        onClick={() => run('prime', api.prime, 'Priming started')}
        disabled={busy === 'prime'}
        className="w-full py-3 rounded-xl bg-surface-raised border border-surface-border text-sm font-medium text-gray-300 active:scale-[0.98] transition disabled:opacity-50"
      >
        {busy === 'prime' ? 'Priming…' : 'Prime water'}
      </button>
    </div>
  );
}
