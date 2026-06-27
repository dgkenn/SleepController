'use client';

import useSWR from 'swr';
import { WeatherForecast, fetcher } from '@/lib/api';

const TREND_META: Record<string, { arrow: string; color: string; label: string }> = {
  warming: { arrow: '↑', color: 'text-warning', label: 'Warming' },
  cooling: { arrow: '↓', color: 'text-cool', label: 'Cooling' },
  stable: { arrow: '→', color: 'text-gray-400', label: 'Stable' },
};

function fmtTemp(f: number | null | undefined): string {
  return f != null ? `${Math.round(f)}°` : '—';
}

/** Weather feed-forward — overnight forecast and pre-biasing of the bed. */
export default function WeatherCard() {
  const { data } = useSWR<WeatherForecast>('/api/weather/forecast', fetcher, {
    refreshInterval: 600000,
  });

  if (!data || data.source === 'error') return null;

  const trend = data.trend ? TREND_META[data.trend] : null;
  const hours = data.forecast?.hours ?? [];
  const temps = hours.map((h) => h.temp_f);
  const minT = temps.length ? Math.min(...temps) : 0;
  const maxT = temps.length ? Math.max(...temps) : 1;
  const span = Math.max(1, maxT - minT);

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Overnight Weather</p>
        {trend && (
          <span className={`text-xs font-semibold ${trend.color}`}>
            {trend.arrow} {trend.label}
          </span>
        )}
      </div>

      <div className="flex items-end gap-2">
        <p className="text-3xl font-bold text-white tabular-nums">
          {fmtTemp(data.overnight_low_f)}
          <span className="text-gray-600 text-xl font-normal"> → </span>
          {fmtTemp(data.overnight_high_f)}
        </p>
        <span className="text-xs text-gray-500 mb-1.5">overnight low → high</span>
      </div>

      {data.pre_cool && data.bias_f !== 0 && (
        <div className="bg-cool/10 border border-cool/30 rounded-xl px-3 py-2">
          <p className="text-xs text-cool">
            Pre-biasing bed {Math.abs(data.bias_f).toFixed(1)}°F{' '}
            {data.bias_f < 0 ? 'cooler' : 'warmer'} ahead of the forecast.
          </p>
        </div>
      )}

      {data.reason && <p className="text-xs text-gray-400 leading-relaxed">{data.reason}</p>}

      {hours.length > 0 && (
        <div className="flex items-end gap-1 h-12">
          {hours.map((h, i) => (
            <div
              key={`${h.hour}-${i}`}
              title={`${h.hour}: ${Math.round(h.temp_f)}°F`}
              className="flex-1 bg-cool/40 rounded-sm"
              style={{ height: `${20 + ((h.temp_f - minT) / span) * 80}%` }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
