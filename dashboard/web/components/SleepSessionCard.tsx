'use client';

import { useEffect, useState } from 'react';
import { api, NapPlan, WakeReviewPayload } from '@/lib/api';
import WakeReviewSheet from './WakeReviewSheet';

interface Props {
  sessionMode: 'night' | 'induce' | 'nap';
  nap: NapPlan | null;
  napDeadline: string | null;
  /** The controller's own state. A night the sleeper never started with "help me fall
   *  asleep" still runs -- bed entry is detected from the armband -- and until this was
   *  wired in, such a night had no off switch anywhere in the app. */
  controllerState?: string | null;
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

const RUNNING_STATES = ['induction', 'maintenance', 'wake_recovery', 'wake_window',
  'calibration'];

export default function SleepSessionCard({
  sessionMode,
  nap,
  napDeadline,
  controllerState,
  onChanged,
  onToast,
}: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [napMin, setNapMin] = useState(20);
  const [preview, setPreview] = useState<NapPlan | null>(null);
  const [remaining, setRemaining] = useState<number | null>(null);
  const [review, setReview] = useState<WakeReviewPayload | null>(null);

  // "I'm awake": end the session AND open the morning review in one press. Until this
  // existed the only way to stop a night was the Stop button on the induce card, which
  // disappears the moment onset is confirmed -- so a running night had no off switch at all.
  const wakeUp = async () => {
    setBusy('wake');
    try {
      const payload = await api.wakeUp();
      onToast?.('Session ended');
      onChanged?.();
      setReview(payload);
    } catch {
      onToast?.('Command failed');
    } finally {
      setBusy(null);
    }
  };

  const sheet = review ? (
    <WakeReviewSheet payload={review} onClose={() => setReview(null)} onToast={onToast} />
  ) : null;

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
          onClick={wakeUp}
          disabled={!!busy}
          className="w-full py-3 rounded-xl bg-surface-raised border border-surface-border text-sm font-semibold text-gray-200 disabled:opacity-50"
        >
          ☀️ I&apos;m awake
        </button>
        {sheet}
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
          onClick={wakeUp}
          disabled={!!busy}
          className="w-full py-2.5 rounded-xl bg-surface-raised border border-surface-border text-sm font-medium text-gray-300 disabled:opacity-50"
        >
          ☀️ I&apos;m awake
        </button>
        {sheet}
      </div>
    );
  }

  // ---- a night is running without an induce session: the same button, the other way round ----
  const nightRunning = !!controllerState && RUNNING_STATES.includes(controllerState);
  if (nightRunning) {
    return (
      <div className="bg-surface-card rounded-2xl p-4 border border-brand/30 space-y-3">
        <div className="flex items-center gap-2">
          <span className="live-dot" />
          <p className="text-sm font-semibold text-white">Night in progress</p>
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          Holding your bed through the night. Press this when you get up and it will end the
          session and ask a few quick questions about how it went.
        </p>
        <button
          onClick={wakeUp}
          disabled={!!busy}
          className="w-full py-3 rounded-xl bg-brand text-surface font-semibold active:scale-[0.98] transition disabled:opacity-50"
        >
          ☀️ I&apos;m awake
        </button>
        {sheet}
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
      {sheet}

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
