'use client';

import useSWR from 'swr';
import { LearningPhases, fetcher } from '@/lib/api';

/** The whole point of the project, made visible: what the controller has learned across all three
 *  sleep phases — going to sleep, staying asleep, waking up — and whether each is personalized to
 *  you yet or still gathering nights. The thermal phases are shown for the "normal" night; short
 *  and recovery nights learn their own optima too (constraint-aware). */
export default function LearningPhasesCard() {
  const { data } = useSWR<LearningPhases>('/api/learning/phases', fetcher, {
    refreshInterval: 60000,
  });
  if (!data) return null;

  const onsetNormal = data.onset.per_mode?.normal;
  const wakeWin = data.wake.window_per_mode?.normal;
  const wakeTherm = data.wake.thermal_per_mode?.normal;

  const Row = ({
    label,
    knob,
    personalized,
    n,
    detail,
  }: {
    label: string;
    knob: string;
    personalized: boolean;
    n: number;
    detail: string;
  }) => (
    <div className="flex items-start gap-2.5">
      <span
        className={`mt-0.5 w-2 h-2 rounded-full shrink-0 ${
          personalized ? 'bg-success' : 'bg-gray-600'
        }`}
      />
      <div className="min-w-0">
        <p className="text-xs text-gray-200">
          {label} <span className="text-gray-500">· {knob}</span>
        </p>
        <p className="text-[11px] text-gray-400 leading-snug">{detail}</p>
        <p className="text-[10px] text-gray-600">
          {personalized ? 'personalized' : 'learning'} · {n} night{n === 1 ? '' : 's'} of data
        </p>
      </div>
    </div>
  );

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Learning — all phases</p>
        <span className="text-[10px] text-gray-600">per night-type</span>
      </div>

      <Row
        label="Going to sleep"
        knob="onset warmth"
        personalized={!!onsetNormal?.is_personalized}
        n={data.onset.n}
        detail={onsetNormal?.rationale ?? 'gathering onset-latency data'}
      />
      <Row
        label="Staying asleep"
        knob="settle nudge"
        personalized={data.maintenance.is_personalized}
        n={data.maintenance.precool_events}
        detail={`Settle nudge is ${data.maintenance.settle_direction} (${data.maintenance.settle_nudge_f.toFixed(
          1
        )} °F); ${data.maintenance.precool_events} pre-cool events logged.`}
      />
      <Row
        label="Waking up"
        knob="window + wake ramp"
        personalized={!!wakeWin?.is_personalized || !!wakeTherm?.is_personalized}
        n={data.wake.n}
        detail={[wakeWin?.rationale, wakeTherm?.rationale]
          .filter((r) => r && !r.includes('already fits'))
          .join(' · ') || 'gathering grogginess check-ins'}
      />

      <p className="text-[10px] text-gray-600 leading-snug pt-1 border-t border-surface-border/60">
        Green = personalized to you. Each phase keeps refining every night, and short / recovery
        nights learn their own optimum separately from full nights.
      </p>
    </div>
  );
}
