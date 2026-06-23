interface ConfidenceMeterProps {
  value: number; // 0-1
  label?: string;
  size?: 'sm' | 'md' | 'lg';
}

export default function ConfidenceMeter({
  value,
  label = 'Confidence',
  size = 'md',
}: ConfidenceMeterProps) {
  const pct = Math.min(100, Math.max(0, Math.round(value * 100)));

  const barColor =
    pct >= 70
      ? 'bg-success'
      : pct >= 40
      ? 'bg-warning'
      : 'bg-danger';

  const heightClass = size === 'lg' ? 'h-3' : size === 'sm' ? 'h-1.5' : 'h-2';
  const textClass = size === 'lg' ? 'text-base' : size === 'sm' ? 'text-xs' : 'text-sm';

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className={`${textClass} text-gray-400`}>{label}</span>
        <span className={`${textClass} font-semibold text-white tabular-nums`}>{pct}%</span>
      </div>
      <div className={`w-full bg-surface-border rounded-full overflow-hidden ${heightClass}`}>
        <div
          className={`${heightClass} ${barColor} rounded-full transition-all duration-500`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
