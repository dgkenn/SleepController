'use client';

import { useEffect, useState } from 'react';
import { api, NapPlan } from '@/lib/api';

interface Props {
  sessionMode: 'night' | 'induce' | 'nap';
  nap: NapPlan | null;
  napDeadline: string | null;
  onChanged?: () => void;
  onToast?: (msg: string) => void;
}

const STRAT_STYLE: Record<string, string> = {
  power: 'text-success',
  cycle: 'text-cool',
  trap: 'text-warning',
};

function fmtClock(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export default function SleepSessionCard({
  sessionMode,
  nap,
  napDeadline,
  onChanged,
  onToast,
}: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [napMin, setNapMin] = useState(20);
  const [preview, setPreview] = useState<NapPlan | null>(null);
  const [remaining, setRemaining] = useState<number | null>(null);

  // Live preview of the chosen nap length's strategy (when idle).
  useEffect(() => {
    if (sessionMode !== 'night') return;
    let alive = true;
    api.napPreview(napMin).then((p) => alive && setPreview(p)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [napMin, sessionMode]);

  // Countdown for an active nap.
  useEffect(() => {
    if (!napDeadline) {
      setRemaining(null);
      return;
    }
    const tick = () =>
      setRemaining(Math.max(0, Math.round((new Date(napDeadline).getTime() - Date.now()) / 60000)));
    tick();
    const id = setInterval(tick, 15000);
    return () => clearInterval(id);
  }, [napDeadline]);

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

  // ---- active session ----
  if (sessionMode === 'induce') {
    return (
      <div className="bg-surface-card rounded-2xl p-4 border border-brand/30 space-y-3">
        <div className="flex items-center gap-2">
          <span className="live-dot" />
          <p className="text-sm font-semibold text-white">Helping you fall asleep…</p>
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          A gentle warm nudge to trigger sleep onset, then cooling as you drift off. Lie back and
          let go.
        </p>
        <button
          onClick={() => run('end', api.endSession, 'Stopped')}
          disabled={busy === 'end'}
          className="w-full py-2.5 rounded-xl bg-surface-raised border border-surface-border text-sm font-medium text-gray-300 disabled:opacity-50"
        >
          Stop
        </button>
      </div>
    );
  }

  if (sessionMode === 'nap' && nap) {
    return (
      <div className="bg-surface-card rounded-2xl p-4 border border-brand/30 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="live-dot" />
            <p className="text-sm font-semibold text-white">Napping</p>
          </div>
          <span className={`text-xs font-semibold ${STRAT_STYLE[nap.strategy]}`}>
            {nap.headline}
          </span>
        </div>
        {remaining != null && (
          <div className="text-center py-1">
            <span className="text-3xl font-bold text-white tabular-nums">{remaining}</span>
            <span className="text-sm text-gray-400"> min left</span>
            <p className="text-[11px] text-gray-600">wake by {fmtClock(napDeadline)}</p>
          </div>
        )}
        <p className="text-xs text-gray-400 leading-relaxed">{nap.advice}</p>
        <button
          onClick={() => run('end', api.endSession, 'Nap ended')}
          disabled={busy === 'end'}
          className="w-full py-2.5 rounded-xl bg-surface-raised border border-surface-border text-sm font-medium text-gray-300 disabled:opacity-50"
        >
          End nap now
        </button>
      </div>
    );
  }

  // ---- idle: offer induce + nap ----
  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">Fall asleep now</p>

      <button
        onClick={() => run('induce', api.induceSleep, 'Inducing sleep')}
        disabled={!!busy}
        className="w-full py-3 rounded-xl bg-brand text-surface font-semibold active:scale-[0.98] transition disabled:opacity-50"
      >
        😴 Help me fall asleep
      </button>
      <p className="text-[11px] text-gray-600 -mt-2 leading-relaxed">
        Runs a warm-then-cool onset program (cutaneous warming speeds sleep onset), then hands
        off to normal night control once you&apos;re asleep.
      </p>

      <div className="border-t border-surface-border pt-3">
        <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">Nap</p>
        <div className="grid grid-cols-3 gap-2 mb-2">
          {[20, 45, 90].map((m) => (
            <button
              key={m}
              onClick={() => setNapMin(m)}
              className={`py-2 rounded-xl text-sm font-medium transition min-h-[40px] ${
                napMin === m
                  ? 'bg-brand text-surface'
                  : 'bg-surface-raised border border-surface-border text-gray-400'
              }`}
            >
              {m} min
            </button>
          ))}
        </div>
        {preview && (
          <div className="bg-surface-raised rounded-xl p-3 mb-2">
            <p className={`text-xs font-semibold ${STRAT_STYLE[preview.strategy]}`}>
              {preview.headline}
            </p>
            <p className="text-[11px] text-gray-400 mt-1 leading-relaxed">{preview.advice}</p>
          </div>
        )}
        <button
          onClick={() => run('nap', () => api.startNap(napMin), `Nap started (${napMin} min)`)}
          disabled={!!busy}
          className="w-full py-2.5 rounded-xl bg-cool/20 border border-cool/30 text-cool font-semibold active:scale-[0.98] transition disabled:opacity-50"
        >
          Start {napMin}-min nap
        </button>
      </div>
    </div>
  );
}
