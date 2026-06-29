'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, ShiftPlan } from '@/lib/api';

const BAND_META: Record<string, { label: string; color: string }> = {
  none: { label: 'Rested', color: 'text-success' },
  mild: { label: 'Mild debt', color: 'text-cool' },
  moderate: { label: 'Moderate debt', color: 'text-warning' },
  severe: { label: 'Severe debt', color: 'text-danger' },
};

/** Cross-shift sleep strategy for a resident's erratic schedule: cumulative debt, tonight's
 *  target, proactive sleep banking before a night block, prophylactic/recovery/anchor naps, and
 *  safety warnings. Set an upcoming night shift to activate the shift-aware plan. */
export default function ShiftCard() {
  const { data: plan, mutate } = useSWR<ShiftPlan>('/api/shift/plan', fetcher, {
    refreshInterval: 60000,
  });
  const [busy, setBusy] = useState(false);
  if (!plan) return null;

  const meta = BAND_META[plan.debt_band] ?? BAND_META.none;

  const setShift = async (value: string) => {
    setBusy(true);
    // datetime-local gives "YYYY-MM-DDTHH:MM" (local) — send straight through as ISO-ish.
    await api.shiftConfigUpdate({ enabled: true, next_shift: value, kind: 'night' });
    await mutate();
    setBusy(false);
  };
  const clearShift = async () => {
    setBusy(true);
    await api.shiftConfigUpdate({ enabled: false, next_shift: null });
    await mutate();
    setBusy(false);
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Shift &amp; Sleep Debt</p>
        <span className={`text-xs font-semibold uppercase tracking-wider ${meta.color}`}>
          {meta.label}
        </span>
      </div>

      <div className="flex items-baseline gap-3">
        <div>
          <span className="text-2xl font-bold text-white">{plan.debt_h}</span>
          <span className="text-xs text-gray-500"> h debt</span>
        </div>
        <div className="text-gray-600">·</div>
        <div>
          <span className="text-2xl font-bold text-white">{plan.tonight_target_h}</span>
          <span className="text-xs text-gray-500"> h target tonight</span>
        </div>
      </div>

      {plan.strategy && <p className="text-xs text-gray-300 leading-snug">{plan.strategy}</p>}

      {plan.banking && (
        <div className="rounded-xl p-2.5 bg-brand/10 border border-brand/30">
          <p className="text-[11px] text-brand leading-snug">🏦 {plan.banking}</p>
        </div>
      )}

      {plan.naps.length > 0 && (
        <ul className="space-y-1.5">
          {plan.naps.map((n, i) => (
            <li key={i} className="text-[11px] text-gray-400 leading-snug flex gap-1.5">
              <span className="text-gray-600">😴</span>
              <span>
                <span className="text-gray-300 capitalize">{n.type}</span> · {n.duration_min} min ·{' '}
                {n.when} — {n.reason}
              </span>
            </li>
          ))}
        </ul>
      )}

      {plan.anchor_window && (
        <p className="text-[11px] text-gray-400 leading-snug">⚓ Anchor sleep: {plan.anchor_window}</p>
      )}

      {plan.warnings.map((w, i) => (
        <p key={i} className="text-[11px] text-warning leading-snug">⚠ {w}</p>
      ))}

      {/* Manual next-shift hint (until a calendar feed lands) */}
      <div className="pt-2 border-t border-surface-border/60 space-y-1.5">
        <p className="text-[10px] text-gray-500 uppercase tracking-wider">Next night shift</p>
        {plan.shift_enabled && plan.next_shift ? (
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-gray-300">
              {new Date(plan.next_shift).toLocaleString([], {
                weekday: 'short',
                hour: '2-digit',
                minute: '2-digit',
                month: 'short',
                day: 'numeric',
              })}
            </span>
            <button onClick={clearShift} disabled={busy} className="text-[11px] text-gray-500 disabled:opacity-50">
              clear
            </button>
          </div>
        ) : (
          <input
            type="datetime-local"
            disabled={busy}
            onChange={(e) => e.target.value && setShift(e.target.value)}
            className="w-full text-[12px] bg-surface-raised border border-surface-border rounded-lg px-2 py-1.5 text-gray-200"
          />
        )}
        <p className="text-[10px] text-gray-600 leading-snug">
          Set a coming night/call so the plan can bank sleep ahead and time a prophylactic nap.
        </p>
      </div>
    </div>
  );
}
