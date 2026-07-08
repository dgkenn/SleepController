import { NightSummary } from '@/lib/api';

interface NightCardProps {
  night: NightSummary;
  compact?: boolean;
}

const NO_PHYSIOLOGY_MSG =
  'No HR/HRV/stage data — Pod subscription-gated; connect the iPhone motion sensor (Admin → Phone Sensor).';

function fmtMin(min: number | null | undefined) {
  if (min == null) return '—';
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

function fmtHrv(hrv: number | null | undefined) {
  return hrv == null ? '—' : `${hrv.toFixed(0)} ms`;
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

  // Physiology (deep/REM/HRV) is Pod-subscription-gated; when all three are absent, show an
  // explicit message instead of letting the individual stats read as measured zeros.
  const noPhysiology = night.deep_min == null && night.rem_min == null && night.avg_hrv == null;

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
        <>
          <div className="grid grid-cols-3 gap-3">
            <Stat label="Total Sleep" value={fmtMin(night.total_sleep_min)} />
            <Stat label="Deep" value={fmtMin(night.deep_min)} />
            <Stat label="REM" value={fmtMin(night.rem_min)} />
            <Stat label="Efficiency" value={`${(night.sleep_efficiency * 100).toFixed(0)}%`} />
            <Stat label="Wake Events" value={String(night.wake_events)} />
            <Stat label="Avg HRV" value={fmtHrv(night.avg_hrv)} />
          </div>
          {noPhysiology && (
            <p className="text-xs text-gray-600 leading-relaxed">{NO_PHYSIOLOGY_MSG}</p>
          )}
        </>
      )}
      {compact && (
        <div className="flex flex-col gap-1">
          <div className="flex gap-4 text-xs text-gray-400">
            <span>{fmtMin(night.total_sleep_min)}</span>
            <span>Eff {(night.sleep_efficiency * 100).toFixed(0)}%</span>
            <span>HRV {night.avg_hrv == null ? '—' : `${night.avg_hrv.toFixed(0)}ms`}</span>
          </div>
          {noPhysiology && <span className="text-xs text-gray-600">{NO_PHYSIOLOGY_MSG}</span>}
        </div>
      )}
    </div>
  );
}
