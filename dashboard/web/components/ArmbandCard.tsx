'use client';

import useSWR from 'swr';
import { fetcher, WearablePipeline, WearableVerdict } from '@/lib/api';

const TONE: Record<WearableVerdict, { ring: string; text: string; dot: string; label: string }> = {
  streaming_full:   { ring: 'border-success/40 bg-success/10', text: 'text-success', dot: 'bg-success', label: 'Streaming' },
  streaming_partial:{ ring: 'border-warning/40 bg-warning/10', text: 'text-warning', dot: 'bg-warning', label: 'Partial stream' },
  streaming_unused: { ring: 'border-warning/40 bg-warning/10', text: 'text-warning', dot: 'bg-warning', label: 'Streaming, not used' },
  connected_silent: { ring: 'border-danger/40 bg-danger/10',   text: 'text-danger',  dot: 'bg-danger',  label: 'Linked, no data' },
  refusing:         { ring: 'border-danger/40 bg-danger/10',   text: 'text-danger',  dot: 'bg-danger',  label: 'Refusing connection' },
  absent:           { ring: 'border-danger/40 bg-danger/10',   text: 'text-danger',  dot: 'bg-danger',  label: 'Not connected' },
  not_connected:    { ring: 'border-danger/40 bg-danger/10',   text: 'text-danger',  dot: 'bg-danger',  label: 'Not connected' },
  unknown:          { ring: 'border-gray-600/30 bg-gray-700/20', text: 'text-gray-400', dot: 'bg-gray-600', label: 'Unknown' },
};

function age(s?: number | null): string {
  if (s == null) return '—';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function Chip({ name, ok, sub }: { name: string; ok: boolean; sub: string }) {
  return (
    <div className={`flex-1 rounded-xl px-2 py-2 text-center border ${
      ok ? 'border-success/40 bg-success/10' : 'border-surface-border bg-surface-raised'
    }`}>
      <p className={`text-xs font-bold ${ok ? 'text-success' : 'text-gray-500'}`}>{name}</p>
      <p className={`text-[11px] tabular-nums ${ok ? 'text-gray-200' : 'text-gray-600'}`}>{sub}</p>
    </div>
  );
}

/**
 * The armband, as three separate facts: is it CONNECTED, is it STREAMING (which streams, how
 * fresh), and is the controller USING it. One word covered all three before, and on 2026-09-07
 * "streaming" was true for sixteen hours of a link that delivered nothing. Polls every 5 s so a
 * power-cycle shows up while you are still holding the button.
 */
export default function ArmbandCard() {
  const { data, error } = useSWR<WearablePipeline>('/api/wearable/pipeline', fetcher, {
    refreshInterval: 5000, revalidateOnFocus: true,
  });
  const v: WearableVerdict = data?.verdict ?? 'unknown';
  const tone = TONE[v] ?? TONE.unknown;
  const live = v === 'streaming_full' || v === 'streaming_partial' || v === 'streaming_unused';
  const failing = (data?.used?.checks ?? []).filter((c) => !c.ok);
  const passing = (data?.used?.checks ?? []).filter((c) => c.ok);

  return (
    <div className={`rounded-2xl p-4 border ${tone.ring} space-y-3`}>
      <div className="flex items-center gap-3">
        <span className={`w-3 h-3 rounded-full shrink-0 ${tone.dot} ${live ? 'animate-pulseDot' : ''}`} />
        <div className="min-w-0 flex-1">
          <p className={`text-sm font-bold ${tone.text}`}>Armband · {tone.label}</p>
          <p className="text-xs text-gray-300 leading-snug">
            {error ? "Couldn't reach the pipeline check" : (data?.headline ?? 'Checking…')}
          </p>
        </div>
        {data?.battery?.pct != null && (
          <span className={`text-xs tabular-nums shrink-0 ${data.battery.pct <= 40 ? 'text-warning' : 'text-gray-400'}`}>
            {data.battery.pct}%
          </span>
        )}
      </div>

      <div className="flex gap-2">
        <Chip name="HR"  ok={!!data?.hr?.ok}  sub={data?.hr?.ok ? `${data.hr.bpm ?? '—'} bpm · ${age(data.hr.age_s)}` : age(data?.hr?.age_s)} />
        <Chip name="PPI" ok={!!data?.ppi?.ok} sub={data?.ppi?.ok ? `${data.ppi.intervals_5min ?? 0} beats · ${age(data.ppi.age_s)}` : age(data?.ppi?.age_s)} />
        <Chip name="ACC" ok={!!data?.acc?.ok} sub={data?.acc?.ok ? `${data.acc.fs ?? '—'} Hz · ${age(data.acc.age_s)}` : age(data?.acc?.age_s)} />
      </div>

      {data?.remedy && !live && (
        <p className="text-xs text-brand leading-relaxed">Fix: {data.remedy}</p>
      )}
      {live && failing.length > 0 && (
        <ul className="text-xs text-warning space-y-0.5">
          {failing.map((c) => <li key={c.id}>⚠ {c.detail}</li>)}
        </ul>
      )}
      {live && failing.length === 0 && passing.length > 0 && (
        <p className="text-[11px] text-gray-500">
          In use: {passing.map((c) => c.detail).join(' · ')}
        </p>
      )}
      {live && data?.used && !data.used.in_session && (
        <p className="text-[11px] text-gray-600">Controller idle — usage is judged once a session starts.</p>
      )}
    </div>
  );
}
