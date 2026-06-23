'use client';

const modes = ['auto', 'manual', 'view'] as const;
type Mode = (typeof modes)[number];

interface ModeToggleProps {
  value: Mode;
  onChange: (mode: Mode) => void;
  disabled?: boolean;
}

const modeLabels: Record<Mode, string> = {
  auto: 'Auto',
  manual: 'Manual',
  view: 'View',
};

const modeDescriptions: Record<Mode, string> = {
  auto: 'AI controls temperature',
  manual: 'You control temperature',
  view: 'Monitor only',
};

export default function ModeToggle({ value, onChange, disabled }: ModeToggleProps) {
  return (
    <div className="space-y-2">
      <div className="flex bg-surface-raised rounded-xl p-1 gap-1">
        {modes.map((mode) => (
          <button
            key={mode}
            onClick={() => onChange(mode)}
            disabled={disabled}
            className={`
              flex-1 min-h-[44px] rounded-lg font-semibold text-sm
              transition-all duration-200
              disabled:opacity-40 disabled:cursor-not-allowed
              ${
                value === mode
                  ? 'bg-brand text-white shadow-md'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-surface-card'
              }
            `}
          >
            {modeLabels[mode]}
          </button>
        ))}
      </div>
      <p className="text-xs text-center text-gray-500">{modeDescriptions[value]}</p>
    </div>
  );
}
