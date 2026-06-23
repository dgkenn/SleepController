'use client';

interface TempStepperProps {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}

export default function TempStepper({
  value,
  onChange,
  min = 55,
  max = 115,
  step = 0.5,
  disabled = false,
}: TempStepperProps) {
  const decrement = () => {
    const next = Math.round((value - step) * 10) / 10;
    if (next >= min) onChange(next);
  };

  const increment = () => {
    const next = Math.round((value + step) * 10) / 10;
    if (next <= max) onChange(next);
  };

  const pct = ((value - min) / (max - min)) * 100;
  const isWarm = value > 80;

  return (
    <div className="space-y-4">
      {/* Display */}
      <div className="flex items-center justify-center">
        <div className="relative">
          {/* Arc background */}
          <svg viewBox="0 0 200 120" className="w-48 h-28">
            <path
              d="M 20 100 A 80 80 0 0 1 180 100"
              fill="none"
              stroke="#2a2a3a"
              strokeWidth="12"
              strokeLinecap="round"
            />
            <path
              d="M 20 100 A 80 80 0 0 1 180 100"
              fill="none"
              stroke={isWarm ? '#f97316' : '#60a5fa'}
              strokeWidth="12"
              strokeLinecap="round"
              strokeDasharray={`${(pct / 100) * 251.3} 251.3`}
              className="transition-all duration-300"
            />
          </svg>
          <div className="absolute inset-0 flex items-center justify-center pt-6">
            <div className="text-center">
              <span className="text-4xl font-bold text-white tabular-nums">
                {value.toFixed(1)}
              </span>
              <span className="text-lg text-gray-400">°F</span>
            </div>
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center justify-center gap-8">
        <button
          onClick={decrement}
          disabled={disabled || value <= min}
          className="w-16 h-16 rounded-2xl bg-surface-raised border border-surface-border text-3xl font-light text-white
            flex items-center justify-center active:scale-90 transition-all duration-100
            disabled:opacity-30 disabled:cursor-not-allowed hover:bg-surface-card"
          aria-label="Decrease temperature"
        >
          −
        </button>

        <div className="text-center">
          <span className="text-xs text-gray-500">
            {min}° – {max}°F
          </span>
        </div>

        <button
          onClick={increment}
          disabled={disabled || value >= max}
          className="w-16 h-16 rounded-2xl bg-surface-raised border border-surface-border text-3xl font-light text-white
            flex items-center justify-center active:scale-90 transition-all duration-100
            disabled:opacity-30 disabled:cursor-not-allowed hover:bg-surface-card"
          aria-label="Increase temperature"
        >
          +
        </button>
      </div>

      {/* Slider */}
      <div className="px-2">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(parseFloat(e.target.value))}
          className="w-full h-2 accent-brand disabled:opacity-30"
          aria-label="Temperature slider"
        />
        <div className="flex justify-between text-xs text-gray-600 mt-1">
          <span>{min}°F</span>
          <span>{max}°F</span>
        </div>
      </div>
    </div>
  );
}
