'use client';

import { TonightResponse } from '@/lib/api';

function Dot({ ok, warn }: { ok: boolean; warn?: boolean }) {
  const c = ok ? 'bg-success' : warn ? 'bg-warning' : 'bg-danger';
  return <span className={`inline-block w-2 h-2 rounded-full ${c}`} />;
}

/** Live device health: power, link, water, and the water-side thermal-response check. */
export default function DeviceStatusCard({ data }: { data: TonightResponse }) {
  const dev = data.device ?? {};
  const th = data.thermal_health;
  const daemonOk = data.daemon_alive && !data.stale;

  const TH_META: Record<string, { label: string; warn: boolean; ok: boolean }> = {
    ok: { label: 'At setpoint', warn: false, ok: true },
    ramping: { label: 'Responding', warn: false, ok: true },
    stalled: { label: 'Not responding', warn: true, ok: false },
    unknown: { label: 'Waiting for data', warn: true, ok: false },
  };
  const thm = th ? TH_META[th.state] ?? TH_META.unknown : null;

  const rows: Array<{ label: string; ok: boolean; warn?: boolean; value: string }> = [
    { label: 'Controller', ok: !!daemonOk, warn: !daemonOk,
      value: daemonOk ? 'live' : 'offline / stale' },
    { label: 'Bed power', ok: !!data.power_on, warn: !data.power_on,
      value: data.power_on ? 'on' : (data.away ? 'away' : 'off') },
  ];
  if (dev.online != null)
    rows.push({ label: 'Pod link', ok: !!dev.online, value: dev.online ? 'online' : 'offline' });
  if (dev.has_water != null)
    rows.push({ label: 'Water', ok: !!dev.has_water, warn: !dev.has_water,
      value: dev.has_water ? (dev.priming ? 'priming' : 'OK') : 'LOW — add water' });

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Device</p>
        {dev.simulated && (
          <span className="text-[10px] text-gray-500 uppercase">simulator</span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-2">
        {rows.map((r) => (
          <div key={r.label} className="flex items-center justify-between gap-2">
            <span className="text-xs text-gray-500">{r.label}</span>
            <span className="flex items-center gap-1.5 text-xs text-white">
              <Dot ok={r.ok} warn={r.warn} /> {r.value}
            </span>
          </div>
        ))}
      </div>

      {thm && (
        <div className="bg-surface-raised rounded-xl px-3 py-2">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-gray-500 uppercase tracking-wider">
              Heating/cooling
            </span>
            <span className="flex items-center gap-1.5 text-xs text-white">
              <Dot ok={thm.ok} warn={thm.warn} /> {thm.label}
            </span>
          </div>
          {th && th.state === 'stalled' && (
            <p className="text-[11px] text-warning mt-1 leading-relaxed">{th.reason}</p>
          )}
          {th && th.device_level != null && (
            <p className="text-[11px] text-gray-600 mt-1">
              level {th.device_level} → target {th.target_level}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
