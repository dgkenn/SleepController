'use client';

interface WakeTimePickerProps {
  value: string; // "HH:MM"
  windowMin?: number;
  vibration?: number; // 0=off, 20 low, 50 med, 100 high
  nightType?: string; // auto | work | recovery
  onChange: (time: string, windowMin: number, vibration: number, nightType: string) => void;
  onClear?: () => void;
  disabled?: boolean;
}

const VIBE_OPTIONS = [
  { value: 0, label: 'Off' },
  { value: 20, label: 'Gentle' },
  { value: 50, label: 'Medium' },
  { value: 100, label: 'Strong' },
];

const NIGHT_TYPES = [
  { value: 'auto', label: 'Auto', hint: 'Infer from your wake time + sleep debt' },
  { value: 'work', label: 'Work', hint: 'Short night — maximise quality per hour' },
  { value: 'recovery', label: 'Recovery', hint: 'Off day — maximise recovery, repay debt' },
];

export default function WakeTimePicker({
  value,
  windowMin = 30,
  vibration = 50,
  nightType = 'auto',
  onChange,
  onClear,
  disabled = false,
}: WakeTimePickerProps) {
  return (
    <div className="bg-surface-raised rounded-2xl p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
          Smart Wake
        </h3>
        {onClear && (
          <button
            onClick={onClear}
            disabled={disabled}
            className="text-xs text-gray-500 hover:text-danger disabled:opacity-40"
          >
            Clear
          </button>
        )}
      </div>

      {/* Night type — drives the whole controller's objective for tonight */}
      <div>
        <label className="block text-xs text-gray-500 mb-1.5">Night type</label>
        <div className="grid grid-cols-3 gap-2">
          {NIGHT_TYPES.map((n) => (
            <button
              key={n.value}
              disabled={disabled}
              title={n.hint}
              onClick={() => onChange(value, windowMin, vibration, n.value)}
              className={`py-2 rounded-xl text-xs font-medium transition-all min-h-[40px] disabled:opacity-40 ${
                nightType === n.value
                  ? 'bg-brand text-surface'
                  : 'bg-surface-card border border-surface-border text-gray-400'
              }`}
            >
              {n.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-4">
        <div className="flex-1">
          <label className="block text-xs text-gray-500 mb-1">Wake by</label>
          <input
            type="time"
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value, windowMin, vibration, nightType)}
            className="
              w-full bg-surface-card border border-surface-border rounded-xl
              px-4 py-3 text-xl font-semibold text-white
              disabled:opacity-40 disabled:cursor-not-allowed
              focus:outline-none focus:border-brand
              min-h-[52px]
            "
          />
        </div>

        <div className="w-28">
          <label className="block text-xs text-gray-500 mb-1">Window</label>
          <select
            value={windowMin}
            disabled={disabled}
            onChange={(e) => onChange(value, parseInt(e.target.value), vibration, nightType)}
            className="
              w-full bg-surface-card border border-surface-border rounded-xl
              px-3 py-3 text-white text-sm font-medium
              disabled:opacity-40 disabled:cursor-not-allowed
              focus:outline-none focus:border-brand
              min-h-[52px]
            "
          >
            <option value={10}>10 min</option>
            <option value={15}>15 min</option>
            <option value={20}>20 min</option>
            <option value={30}>30 min</option>
            <option value={45}>45 min</option>
          </select>
        </div>
      </div>

      {/* Vibration intensity — heat is always part of the smart wake (silent, no audio) */}
      <div>
        <label className="block text-xs text-gray-500 mb-1.5">Vibration</label>
        <div className="grid grid-cols-4 gap-2">
          {VIBE_OPTIONS.map((o) => (
            <button
              key={o.value}
              disabled={disabled}
              onClick={() => onChange(value, windowMin, o.value, nightType)}
              className={`py-2 rounded-xl text-xs font-medium transition-all min-h-[40px] disabled:opacity-40 ${
                vibration === o.value
                  ? 'bg-brand text-surface'
                  : 'bg-surface-card border border-surface-border text-gray-400'
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      <p className="text-xs text-gray-600 leading-relaxed">
        Wakes you with warmth{vibration > 0 ? ' + gentle vibration' : ''} during your lightest
        sleep in the {windowMin}-minute window before this time. Audio stays off.
      </p>
    </div>
  );
}
