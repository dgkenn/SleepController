'use client';

import { useState } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import MetricChart from '@/components/MetricChart';
import useSWR from 'swr';
import {
  TrendsResponse,
  EffectivenessResponse,
  fetcher,
} from '@/lib/api';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

const METRICS = [
  { value: 'wake_events', label: 'Wake Events' },
  { value: 'sleep_efficiency', label: 'Sleep Efficiency %' },
  { value: 'outcome_score', label: 'Outcome Score' },
  { value: 'total_sleep_min', label: 'Total Sleep (min)' },
  { value: 'avg_hrv', label: 'Avg HRV (ms)' },
  { value: 'deep_min', label: 'Deep Sleep (min)' },
  { value: 'rem_min', label: 'REM Sleep (min)' },
];

const WINDOWS = [7, 14, 30, 60, 90];

function AnalyticsContent() {
  const [metric, setMetric] = useState('wake_events');
  const [window, setWindow] = useState(30);

  const { data: trends, error: trendsError } = useSWR<TrendsResponse>(
    `/api/analytics/trends?metric=${metric}&window=${window}`,
    fetcher,
    { refreshInterval: 60000 }
  );

  const { data: effectiveness } = useSWR<EffectivenessResponse>(
    '/api/analytics/effectiveness',
    fetcher,
    { refreshInterval: 60000 }
  );

  const trendPoints = (trends?.points ?? []).map((p) => ({
    date: p.date,
    value: p.value,
  }));

  const metricLabel =
    METRICS.find((m) => m.value === metric)?.label ?? metric;

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Analytics</h1>
          <p className="text-sm text-gray-500">Sleep trends and intervention effectiveness</p>
        </div>

        <div className="px-4 space-y-4">
          {/* Selectors */}
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-gray-500 mb-1">Metric</label>
              <select
                value={metric}
                onChange={(e) => setMetric(e.target.value)}
                className="
                  w-full bg-surface-card border border-surface-border rounded-xl
                  px-3 py-2.5 text-white text-sm
                  focus:outline-none focus:border-brand min-h-[44px]
                "
              >
                {METRICS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="w-24">
              <label className="block text-xs text-gray-500 mb-1">Window</label>
              <select
                value={window}
                onChange={(e) => setWindow(parseInt(e.target.value))}
                className="
                  w-full bg-surface-card border border-surface-border rounded-xl
                  px-3 py-2.5 text-white text-sm
                  focus:outline-none focus:border-brand min-h-[44px]
                "
              >
                {WINDOWS.map((w) => (
                  <option key={w} value={w}>
                    {w}d
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Trend chart */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
              {metricLabel} — last {window} days
            </p>
            {trendsError ? (
              <p className="text-sm text-danger text-center py-4">Failed to load trend data</p>
            ) : (
              <MetricChart
                data={trendPoints}
                lines={[{ key: 'value', label: metricLabel, color: '#6366f1' }]}
                xKey="date"
                height={220}
                xFormatter={(d) => d.slice(5)} // MM-DD
              />
            )}
          </div>

          {/* Effectiveness bar chart */}
          {effectiveness && effectiveness.by_action.length > 0 && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                Intervention Effectiveness
              </p>
              <div style={{ height: 200 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={effectiveness.by_action}
                    margin={{ top: 8, right: 8, bottom: 0, left: -16 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#2a2a3a" vertical={false} />
                    <XAxis
                      dataKey="action"
                      tick={{ fill: '#6b7280', fontSize: 10 }}
                      tickLine={false}
                      axisLine={false}
                    />
                    <YAxis
                      tick={{ fill: '#6b7280', fontSize: 11 }}
                      tickLine={false}
                      axisLine={false}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: '#1e1e2a',
                        border: '1px solid #2a2a3a',
                        borderRadius: '12px',
                        color: '#fff',
                        fontSize: 12,
                      }}
                      formatter={(v: number | string) => [
                        typeof v === 'number' ? v.toFixed(3) : v,
                        'Mean Reward',
                      ]}
                    />
                    <Bar dataKey="mean_reward" radius={[6, 6, 0, 0]}>
                      {effectiveness.by_action.map((entry, index) => (
                        <Cell
                          key={index}
                          fill={entry.mean_reward >= 0 ? '#22c55e' : '#ef4444'}
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-2 divide-y divide-surface-border">
                {effectiveness.by_action.map((item) => (
                  <div key={item.action} className="flex items-center justify-between py-2 text-sm">
                    <span className="text-gray-300">{item.action}</span>
                    <div className="text-right text-xs">
                      <span className="text-gray-500">n={item.n}</span>
                      <span
                        className={`ml-2 font-medium ${
                          item.mean_reward >= 0 ? 'text-success' : 'text-danger'
                        }`}
                      >
                        {item.mean_reward >= 0 ? '+' : ''}{item.mean_reward.toFixed(3)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function AnalyticsPage() {
  return (
    <AuthGuard>
      <AnalyticsContent />
    </AuthGuard>
  );
}
