'use client';

import useSWR from 'swr';
import { api, fetcher, GymAdvice, GymConfig, WakePlan } from '@/lib/api';

const LEANS: Array<GymConfig['lean']> = ['protect', 'balanced', 'push'];

/** Togglable "gym vs. sleep" morning call: should you get up early to train, or do you need
 *  the sleep? Resident-framed — the workout is your only window, weighed against being too
 *  short-slept to be safe on shift. */
export default function GymCard() {
  const { data: cfgWrap, mutate: mutateCfg } = useSWR<{ config: GymConfig }>(
    '/api/gym/config',
    fetcher,
    { refreshInterval: 60000 }
  );
  const { data: advice, mutate: mutateAdvice } = useSWR<GymAdvice>('/api/gym/advice', fetcher, {
    refreshInterval: 60000,
  });
  const { data: plan, mutate: mutatePlan } = useSWR<WakePlan>('/api/wake/plan', fetcher, {
    refreshInterval: 60000,
  });
  const cfg = cfgWrap?.config;
  if (!cfg) return null;

  const update = async (values: Partial<GymConfig>) => {
    await api.gymConfigUpdate(values);
    mutateCfg();
    mutateAdvice();
    mutatePlan();
  };

  const go = advice?.recommend === 'go';
  const sleepIn = advice?.recommend === 'sleep_in';

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Gym vs. Sleep</p>
        <button
          onClick={() => update({ enabled: !cfg.enabled })}
          className={`relative w-11 h-6 rounded-full transition-colors ${
            cfg.enabled ? 'bg-success' : 'bg-surface-raised border border-surface-border'
          }`}
          aria-label="Toggle gym advisor"
        >
          <span
            className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
              cfg.enabled ? 'translate-x-5' : 'translate-x-0.5'
            }`}
          />
        </button>
      </div>

      {!cfg.enabled ? (
        <p className="text-sm text-gray-500">
          Off. Turn on to get a morning call on whether to wake early for the gym or protect your
          sleep.
        </p>
      ) : (
        <>
          {advice && advice.recommend !== 'off' && (
            <div
              className={`rounded-xl p-3 ${
                go
                  ? 'bg-success/10 border border-success/30'
                  : sleepIn
                  ? 'bg-brand/10 border border-brand/30'
                  : 'bg-surface-raised border border-surface-border'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <span className="text-lg">{go ? '💪' : sleepIn ? '😴' : '🗓️'}</span>
                <span
                  className={`text-sm font-bold ${
                    go ? 'text-success' : sleepIn ? 'text-brand' : 'text-gray-300'
                  }`}
                >
                  {go ? 'GO TRAIN' : sleepIn ? 'SLEEP IN' : 'REST DAY'}
                </span>
                {advice.early_wake_time && advice.recommend !== 'rest_day' && (
                  <span className="text-xs text-gray-500 ml-auto">
                    {go ? `alarm ${advice.early_wake_time}` : `sleep to ${advice.normal_wake_time}`}
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-300 leading-snug">{advice.headline}</p>
              {advice.projected_gym_sleep_h != null && (
                <p className="text-[11px] text-gray-500 mt-1">
                  ~{advice.projected_gym_sleep_h}h sleep if you train
                  {advice.projected_sleepin_sleep_h != null &&
                    ` · ~${advice.projected_sleepin_sleep_h}h if you sleep in`}
                </p>
              )}
              {advice.reasons.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {advice.reasons.slice(0, 3).map((r, i) => (
                    <li key={i} className="text-[11px] text-gray-400 leading-snug flex gap-1.5">
                      <span className="text-gray-600">•</span>
                      <span>{r}</span>
                    </li>
                  ))}
                </ul>
              )}

              {/* Smart alarm wiring: the effective alarm + how it wakes you */}
              {plan && plan.effective_wake && advice.recommend !== 'rest_day' && (
                <div className="mt-2 pt-2 border-t border-surface-border/60">
                  <p className="text-[11px] text-gray-300">
                    ⏰ Smart alarm <span className="font-semibold text-white">{plan.effective_wake}</span>
                    {plan.moved_earlier && (
                      <span className="text-success"> · moved earlier for the gym</span>
                    )}
                  </p>
                  <p className="text-[10px] text-gray-500 mt-0.5">
                    Catches light sleep in a {plan.smart_window_min}-min window ·{' '}
                    {plan.silent_only ? 'silent (warmth + vibration)' : 'with sound'} · escalates only
                    if needed, guaranteed by {plan.normal_wake && plan.recommend === 'sleep_in' ? plan.normal_wake : plan.effective_wake}
                  </p>
                  {plan.live && plan.live.phase !== 'idle' && plan.live.phase !== 'hold' && (
                    <p className="text-[10px] text-brand mt-0.5 capitalize">
                      ● {plan.live.phase}: {plan.live.reason}
                    </p>
                  )}
                  {plan.learned && (
                    <p className="text-[10px] mt-0.5">
                      <span
                        className={
                          plan.learned.is_personalized ? 'text-success' : 'text-gray-500'
                        }
                      >
                        {plan.learned.is_personalized
                          ? `tuned to you: ${plan.learned.rationale}`
                          : plan.learned.rationale}
                      </span>
                    </p>
                  )}
                  {plan.learned?.thermal?.is_personalized && (
                    <p className="text-[10px] mt-0.5 text-success">
                      🌡 {plan.learned.thermal.rationale}
                    </p>
                  )}
                  {plan.dawn_light?.enabled && (
                    <p className="text-[10px] mt-0.5 text-amber-400">
                      ☀️ {plan.dawn_light.sunrise ? 'sunrise ramp' : 'therapy lamp'}
                      {plan.dawn_light.sunrise && plan.dawn_light.therapy && ' + therapy lamp'} ·
                      held bright {plan.dawn_light.post_wake_hold_min} min past wake for the
                      circadian kick
                    </p>
                  )}
                  {plan.readiness && (
                    <p className="text-[10px] mt-0.5 text-gray-400">
                      🧠 {plan.readiness.note}
                      {plan.readiness.caffeine.recommend && (
                        <span className="text-gray-300">
                          {' '}☕ {plan.readiness.caffeine.note}
                        </span>
                      )}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* lean selector */}
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">When it&apos;s close</span>
            <div className="flex gap-1">
              {LEANS.map((l) => (
                <button
                  key={l}
                  onClick={() => update({ lean: l })}
                  className={`text-[11px] px-2 py-1 rounded-lg capitalize ${
                    cfg.lean === l
                      ? 'bg-brand text-white'
                      : 'bg-surface-raised text-gray-400 border border-surface-border'
                  }`}
                >
                  {l === 'protect' ? 'protect sleep' : l === 'push' ? 'push gym' : 'balanced'}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Gym alarm is earlier by</span>
            <span className="text-xs text-white font-medium">{cfg.early_offset_min} min</span>
          </div>
        </>
      )}
    </div>
  );
}
