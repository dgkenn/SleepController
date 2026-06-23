import { NightSummary } from '@/lib/api';

interface NightCardProps {
  night: NightSummary;
  compact?: boolean;
}

function fmtMin(min: number) {
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-sm font-semibold text-white">{value}</span>
    </div>
  );
}

export default function NightCard({ night, compact = false }: NightCardProps) {
  const score = night.outcome_score ?? 0;
  const scoreColor =
    score >= 80 ? 'text-success' : score >= 60 ? 'text-warning' : 'text-danger';

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-gray-300">{night.date}</span>
        <span className={`text-xl font-bold ${scoreColor}`}>
          {score.toFixed(0)}
          <span className="text-xs text-gray-500 ml-1">/ 100</span>
        </span>
      </div>

      {!compact && (
        <div className="grid grid-cols-3 gap-3">
          <Stat label="Total Sleep" value={fmtMin(night.total_sleep_min)} />
          <Stat label="Deep" value={fmtMin(night.deep_min)} />
          <Stat label="REM" value={fmtMin(night.rem_min)} />
          <Stat label="Efficiency" value={`${(night.sleep_efficiency * 100).toFixed(0)}%`} />
          <Stat label="Wake Events" value={String(night.wake_events)} />
          <Stat label="Avg HRV" value={`${night.avg_hrv.toFixed(0)} ms`} />
        </div>
      )}
      {compact && (
        <div className="flex gap-4 text-xs text-gray-400">
          <span>{fmtMin(night.total_sleep_min)}</span>
          <span>Eff {(night.sleep_efficiency * 100).toFixed(0)}%</span>
          <span>HRV {night.avg_hrv.toFixed(0)}ms</span>
        </div>
      )}
    </div>
  );
}
