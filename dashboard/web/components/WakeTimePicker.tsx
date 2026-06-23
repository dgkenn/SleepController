'use client';

interface WakeTimePickerProps {
  value: string; // "HH:MM"
  windowMin?: number;
  onChange: (time: string, windowMin: number) => void;
  disabled?: boolean;
}

export default function WakeTimePicker({
  value,
  windowMin = 5,
  onChange,
  disabled = false,
}: WakeTimePickerProps) {
  return (
    <div className="bg-surface-raised rounded-2xl p-4 space-y-4">
      <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
        Wake Time
      </h3>

      <div className="flex items-center gap-4">
        <div className="flex-1">
          <label className="block text-xs text-gray-500 mb-1">Time</label>
          <input
            type="time"
            value={value}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value, windowMin)}
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
            onChange={(e) => onChange(value, parseInt(e.target.value))}
            className="
              w-full bg-surface-card border border-surface-border rounded-xl
              px-3 py-3 text-white text-sm font-medium
              disabled:opacity-40 disabled:cursor-not-allowed
              focus:outline-none focus:border-brand
              min-h-[52px]
            "
          >
            <option value={5}>5 min</option>
            <option value={10}>10 min</option>
            <option value={15}>15 min</option>
            <option value={20}>20 min</option>
            <option value={30}>30 min</option>
          </select>
        </div>
      </div>
    </div>
  );
}
