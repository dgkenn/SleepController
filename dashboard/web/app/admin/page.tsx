'use client';

import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import DataHealthList from '@/components/DataHealthList';
import useSWR from 'swr';
import { AdminHealth, LogEntry, fetcher } from '@/lib/api';
import Link from 'next/link';

function AdminContent() {
  const { data: health } = useSWR<AdminHealth>('/api/admin/health', fetcher, {
    refreshInterval: 10000,
  });

  const { data: logs } = useSWR<LogEntry[]>('/api/admin/logs?limit=50', fetcher, {
    refreshInterval: 15000,
  });

  const levelColor: Record<string, string> = {
    ERROR: 'text-danger',
    WARNING: 'text-warning',
    INFO: 'text-gray-300',
    DEBUG: 'text-gray-500',
  };

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Admin</h1>
          <p className="text-sm text-gray-500">System health and diagnostics</p>
        </div>

        <div className="px-4 space-y-4">
          {/* Daemon status */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">Daemon</p>
            {!health ? (
              <div className="flex items-center justify-center py-4">
                <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Status</span>
                  <span
                    className={`text-sm font-semibold ${
                      health.daemon.alive ? 'text-success' : 'text-danger'
                    }`}
                  >
                    {health.daemon.alive ? 'Alive' : 'Dead'}
                    {health.daemon.stale && ' (stale)'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Mode</span>
                  <span className="flex items-center gap-1.5">
                    <span
                      className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${
                        health.daemon.live
                          ? 'bg-success/10 border-success/30 text-success'
                          : 'bg-surface-raised border-surface-border text-gray-400'
                      }`}
                    >
                      {health.daemon.live ? 'Live (real Pod)' : 'Simulator'}
                    </span>
                    {health.daemon.live && health.daemon.dry_run && (
                      <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-warning/15 border border-warning/30 text-warning">
                        dry-run
                      </span>
                    )}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Last update</span>
                  <span className="text-sm text-gray-400">
                    {health.daemon.updated
                      ? new Date(health.daemon.updated).toLocaleTimeString()
                      : 'N/A'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Pending commands</span>
                  <span className="text-sm text-white font-medium">
                    {health.pending_commands}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* Data sources */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">Data Sources</p>
            {health ? (
              <DataHealthList sources={health.sources} />
            ) : (
              <p className="text-sm text-gray-600 text-center py-2">Loading…</p>
            )}
          </div>

          {/* Phone sensor (iPhone accelerometer fusion) */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">Phone Sensor</p>
            {!health ? (
              <p className="text-sm text-gray-600 text-center py-2">Loading…</p>
            ) : !health.phone_sensor ? (
              <p className="text-sm text-gray-500 py-1">
                Not streaming. See <span className="text-brand">IPHONE_SENSOR.md</span> to stream
                your iPhone&apos;s accelerometer as a fast in-bed motion sensor.
              </p>
            ) : (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Status</span>
                  <span className="flex items-center gap-1.5">
                    <span
                      className={`w-2 h-2 rounded-full ${
                        health.phone_sensor.fusing
                          ? 'bg-success'
                          : health.phone_sensor.streaming
                          ? 'bg-warning'
                          : 'bg-danger'
                      }`}
                    />
                    <span className="text-sm font-semibold text-white">
                      {health.phone_sensor.fusing
                        ? 'Fusing'
                        : health.phone_sensor.streaming
                        ? 'Streaming (stale)'
                        : 'Idle'}
                    </span>
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Source</span>
                  <span className="text-sm text-gray-400">{health.phone_sensor.source}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Last sample</span>
                  <span className="text-sm text-gray-400">
                    {health.phone_sensor.age_seconds != null
                      ? `${health.phone_sensor.age_seconds}s ago`
                      : 'N/A'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Movement</span>
                  <span className="text-sm text-white font-medium">
                    {health.phone_sensor.movement != null
                      ? health.phone_sensor.movement.toFixed(3)
                      : '—'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-gray-300">Bed presence</span>
                  <span className="text-sm text-gray-400">
                    {health.phone_sensor.in_bed
                      ? 'In bed — fused'
                      : 'Out of bed — ignored'}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* Quick nav */}
          <div className="grid grid-cols-2 gap-3">
            {[
              { href: '/settings', label: 'Settings' },
              { href: '/learning', label: 'ML Overview' },
            ].map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="bg-surface-card rounded-2xl p-4 border border-surface-border text-center text-sm font-medium text-brand min-h-[52px] flex items-center justify-center"
              >
                {item.label}
              </Link>
            ))}
          </div>

          {/* Logs */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
              Controller Logs
            </p>
            {!logs || logs.length === 0 ? (
              <p className="text-sm text-gray-600 text-center py-4">No logs available</p>
            ) : (
              <div className="space-y-1 font-mono text-xs max-h-80 overflow-y-auto">
                {logs.map((log, i) => (
                  <div key={i} className="flex gap-2 items-start">
                    <span className="text-gray-600 shrink-0">
                      {new Date(log.ts).toLocaleTimeString()}
                    </span>
                    <span
                      className={`shrink-0 w-14 ${
                        levelColor[log.level] ?? 'text-gray-400'
                      }`}
                    >
                      {log.level}
                    </span>
                    <span className="text-gray-300 break-all">{log.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function AdminPage() {
  return (
    <AuthGuard>
      <AdminContent />
    </AuthGuard>
  );
}
