import { StatusResponse } from '@/lib/api';

interface StatusHeroProps {
  data: StatusResponse;
}

function fmt(value: number | null | undefined, digits = 1): string {
  return value == null ? '--' : value.toFixed(digits);
}

function TempDisplay({ label, value, unit = '°F' }: { label: string; value: number | null; unit?: string }) {
  return (
    <div className="flex flex-col items-center gap-1">
      <span className="text-xs text-gray-500 uppercase tracking-wider">{label}</span>
      <span className="text-4xl font-bold tabular-nums text-white">
        {fmt(value)}<span className="text-xl text-gray-400">{unit}</span>
      </span>
    </div>
  );
}

export default function StatusHero({ data }: StatusHeroProps) {
  const sleepOpMin = data.schedule?.sleep_opportunity_min;

  // Primary wake time = the actually-armed Smart Wake alarm for tonight.
  // The schedule's required_wake_time is an external (e.g. shift) constraint that
  // may not match what's armed -- surface it only as a secondary note when it differs.
  const armedWake = data.wake?.wake_time;
  const requiredWake = data.schedule?.required_wake_time;
  const primaryWake = armedWake ?? requiredWake ?? null;
  const showShiftNote = !!requiredWake && !!armedWake && requiredWake !== armedWake;

  return (
    <div className="bg-surface-card rounded-2xl p-5 space-y-4">
      {/* Temperatures */}
      <div className="flex items-center justify-around">
        <TempDisplay label="Bed" value={data.bed_temp_f} />
        <div className="flex flex-col items-center gap-1">
          <span className="text-xs text-gray-500 uppercase tracking-wider">Target</span>
          <span className="text-2xl font-semibold text-brand tabular-nums">
            {fmt(data.target_temp_f)}°F
          </span>
        </div>
        <TempDisplay label="Room" value={data.room_temp_f} />
      </div>

      {/* Divider */}
      <div className="border-t border-surface-border" />

      {/* Sleep info row */}
      <div className="flex items-center justify-between text-sm">
        {sleepOpMin != null && (
          <div className="flex flex-col">
            <span className="text-xs text-gray-500">Sleep Opportunity</span>
            <span className="text-white font-medium">
              {Math.floor(sleepOpMin / 60)}h {sleepOpMin % 60}m
            </span>
          </div>
        )}
        {primaryWake && (
          <div className="flex flex-col items-end">
            <span className="text-xs text-gray-500">{armedWake ? 'Alarm Set' : 'Wake Time'}</span>
            <span className="text-white font-medium">{primaryWake}</span>
          </div>
        )}
      </div>

      {showShiftNote && (
        <p className="text-xs text-warning bg-warning/10 rounded-lg px-3 py-2">
          Shift needs you up by {requiredWake} — alarm is set for {armedWake}
        </p>
      )}

      {data.schedule?.is_short_sleep_day && (
        <p className="text-xs text-warning bg-warning/10 rounded-lg px-3 py-2">
          Short sleep day — recovery mode active
        </p>
      )}
    </div>
  );
}
