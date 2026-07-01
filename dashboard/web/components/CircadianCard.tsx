'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, CircadianEstimate, CalendarConfig, ShiftPlan } from '@/lib/api';

/** "Tue 07:00" style clock for a next-shift/auto-wake ISO datetime, or null if unset. */
function _shiftClock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const day = d.toLocaleDateString(undefined, { weekday: 'short' });
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  return `${day} ${time}`;
}

/** Plain-language next-shift line for the card: day shift shows the auto-wake time, night
 *  shift points at the banking/anchor-sleep guidance instead of a morning alarm. */
function shiftSummary(plan: ShiftPlan | undefined): string | null {
  if (!plan || !plan.shift_enabled || !plan.next_shift) return null;
  const when = _shiftClock(plan.next_shift);
  if (!when) return null;
  if (plan.next_shift_kind === 'night') {
    return `Next: Night shift ${when} — bank sleep tonight, protect daytime sleep.`;
  }
  const wake = _shiftClock(plan.recommended_wake);
  const kindLabel = plan.next_shift_kind === 'call' ? 'Call shift' : 'Day shift';
  return wake
    ? `Next: ${kindLabel} ${when} → auto-wake ${wake}.`
    : `Next: ${kindLabel} ${when}.`;
}

/** Circadian phase estimate — the dominant variable on a rotating shift schedule. Shows your
 *  habitual sleep window/midpoint, how far your recent schedule has drifted from it, and the
 *  wake-maintenance zone (the hours before habitual sleep onset when your clock actively
 *  resists sleep). Also lets you paste a read-only secret ICS calendar URL (no OAuth) so the
 *  next shift can be picked up automatically instead of set by hand. */
export default function CircadianCard() {
  const { data: est } = useSWR<CircadianEstimate>('/api/circadian', fetcher, {
    refreshInterval: 5 * 60000,
  });
  const { data: cal, mutate: mutateCal } = useSWR<CalendarConfig>(
    '/api/calendar/config',
    fetcher,
    { refreshInterval: 5 * 60000 }
  );
  const { data: shiftPlan } = useSWR<ShiftPlan>('/api/shift/plan', fetcher, {
    refreshInterval: 5 * 60000,
  });
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  if (!est) return null;

  const hasEstimate = est.habitual_midpoint_clock !== null;

  const saveUrl = async () => {
    if (!url.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      await api.calendarConfigUpdate({ enabled: true, ics_url: url.trim() });
      const refreshed = await api.calendarRefresh();
      setMsg(refreshed.ok ? 'Calendar connected.' : refreshed.error || 'Could not fetch feed.');
      setUrl('');
      await mutateCal();
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    await api.calendarConfigUpdate({ enabled: false, ics_url: null });
    await mutateCal();
    setBusy(false);
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Circadian Phase</p>
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-400">
          {Math.round(est.confidence * 100)}% confidence
        </span>
      </div>

      {hasEstimate ? (
        <>
          <div className="flex items-baseline gap-3">
            <div>
              <span className="text-2xl font-bold text-white">{est.habitual_midpoint_clock}</span>
              <span className="text-xs text-gray-500"> habitual midpoint</span>
            </div>
          </div>
          <p className="text-[11px] text-gray-400 leading-snug">
            Usual sleep window ~{est.habitual_sleep_start_clock}–{est.habitual_sleep_end_clock}.
            {est.phase_shift_hours !== null && Math.abs(est.phase_shift_hours) >= 1 && (
              <>
                {' '}Recent nights are running ~{Math.abs(est.phase_shift_hours).toFixed(1)} h{' '}
                {est.phase_shift_hours > 0 ? 'later' : 'earlier'} than habit.
              </>
            )}
          </p>
          {est.wake_maintenance_zone && (
            <div className="rounded-xl p-2.5 bg-warning/10 border border-warning/30">
              <p className="text-[11px] text-warning leading-snug">
                Wake-maintenance zone: {est.wake_maintenance_zone.start_clock}–
                {est.wake_maintenance_zone.end_clock} — sleep is biologically resisted here.
              </p>
            </div>
          )}
        </>
      ) : (
        <p className="text-[11px] text-gray-400 leading-snug">{est.note}</p>
      )}

      {/* OAuth-free calendar ingest */}
      <div className="pt-2 border-t border-surface-border/60 space-y-1.5">
        <p className="text-[10px] text-gray-500 uppercase tracking-wider">Calendar auto-ingest</p>
        {cal?.configured ? (
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-gray-300">
              {cal.enabled ? `Connected (${cal.ics_url_masked})` : 'Saved but disabled'}
            </span>
            <button onClick={disconnect} disabled={busy} className="text-[11px] text-gray-500 disabled:opacity-50">
              disconnect
            </button>
          </div>
        ) : (
          <div className="flex gap-1.5">
            <input
              type="url"
              placeholder="Paste secret ICS URL"
              value={url}
              disabled={busy}
              onChange={(e) => setUrl(e.target.value)}
              className="flex-1 text-[12px] bg-surface-raised border border-surface-border rounded-lg px-2 py-1.5 text-gray-200"
            />
            <button
              onClick={saveUrl}
              disabled={busy || !url.trim()}
              className="text-[11px] px-2 py-1.5 rounded-lg bg-brand/20 text-brand disabled:opacity-50"
            >
              Save
            </button>
          </div>
        )}
        {msg && <p className="text-[10px] text-gray-500 leading-snug">{msg}</p>}
        <p className="text-[10px] text-gray-600 leading-snug">
          No sign-in required — Google Calendar (Settings → your calendar → &quot;Secret address in
          iCal format&quot;) gives a read-only .ics URL that auto-detects your next shift.
        </p>
      </div>

      {/* Next shift, auto-synced from the connected calendar (falls back to the manual hint) */}
      {shiftSummary(shiftPlan) && (
        <div className="pt-2 border-t border-surface-border/60">
          <p className="text-[11px] text-gray-300 leading-snug">{shiftSummary(shiftPlan)}</p>
          {shiftPlan?.next_shift_source && (
            <p className="text-[10px] text-gray-600 leading-snug">
              source: {shiftPlan.next_shift_source === 'calendar' ? 'work calendar' : 'manual'}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
