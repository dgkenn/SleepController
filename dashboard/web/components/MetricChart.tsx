'use client';

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';

interface DataPoint {
  [key: string]: string | number;
}

interface LineConfig {
  key: string;
  label: string;
  color: string;
}

interface MetricChartProps {
  data: DataPoint[];
  lines: LineConfig[];
  xKey?: string;
  height?: number;
  xFormatter?: (v: string) => string;
  yFormatter?: (v: number) => string;
}

export default function MetricChart({
  data,
  lines,
  xKey = 'date',
  height = 220,
  xFormatter,
  yFormatter,
}: MetricChartProps) {
  if (!data || data.length === 0) {
    return (
      <div
        className="flex items-center justify-center bg-surface-raised rounded-2xl text-gray-600 text-sm"
        style={{ height }}
      >
        No data
      </div>
    );
  }

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2a2a3a" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={{ fill: '#6b7280', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={xFormatter}
          />
          <YAxis
            tick={{ fill: '#6b7280', fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={yFormatter}
            width={48}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: '#1e1e2a',
              border: '1px solid #2a2a3a',
              borderRadius: '12px',
              color: '#fff',
              fontSize: 12,
            }}
            formatter={
              yFormatter
                ? (v: number | string) =>
                    yFormatter(typeof v === 'number' ? v : parseFloat(String(v)))
                : undefined
            }
            labelFormatter={xFormatter ? (l: string) => xFormatter(l) : undefined}
          />
          {lines.length > 1 && (
            <Legend
              wrapperStyle={{ fontSize: 11, color: '#9ca3af' }}
              iconType="circle"
            />
          )}
          {lines.map((l) => (
            <Line
              key={l.key}
              type="monotone"
              dataKey={l.key}
              name={l.label}
              stroke={l.color}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 0 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
