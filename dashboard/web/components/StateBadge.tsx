interface StateBadgeProps {
  state: string;
  mode?: string;
  stale?: boolean;
}

const stateColors: Record<string, string> = {
  cooling: 'bg-cool/20 text-cool border-cool/30',
  warming: 'bg-warm/20 text-warm border-warm/30',
  sleeping: 'bg-brand/20 text-brand border-brand/30',
  idle: 'bg-gray-700/40 text-gray-400 border-gray-600/30',
  off: 'bg-gray-800/40 text-gray-500 border-gray-700/30',
  wake: 'bg-success/20 text-success border-success/30',
  paused: 'bg-warning/20 text-warning border-warning/30',
  default: 'bg-gray-700/40 text-gray-300 border-gray-600/30',
};

export default function StateBadge({ state, mode, stale }: StateBadgeProps) {
  const colorClass = stateColors[state.toLowerCase()] ?? stateColors.default;

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span
        className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-semibold border ${colorClass}`}
      >
        <span className="w-2 h-2 rounded-full bg-current mr-2 animate-pulse" />
        {state.charAt(0).toUpperCase() + state.slice(1)}
      </span>
      {mode && (
        <span className="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-gray-700/40 text-gray-400 border border-gray-600/30">
          {mode}
        </span>
      )}
      {stale && (
        <span className="inline-flex items-center px-2 py-1 rounded-full text-xs font-medium bg-warning/20 text-warning border border-warning/30">
          Stale
        </span>
      )}
    </div>
  );
}
