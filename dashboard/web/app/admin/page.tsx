'use client';

import { useState } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import DataHealthList from '@/components/DataHealthList';
import useSWR from 'swr';
import { AdminHealth, Backtest, LogEntry, SelfTestReport, fetcher, api } from '@/lib/api';
import Link from 'next/link';

function BedTestCard() {
  const [report, setReport] = useState<SelfTestReport | null>(null);
  const [polling, setPolling] = useState(false);
  const [busy, setBusy] = useState(false);

  // Poll the live report while a test is running.
  useSWR(polling ? '/api/control/self-test' : null, fetcher, {
    refreshInterval: 3000,
    onSuccess: (d: { self_test: SelfTestReport | null }) => {
      if (d?.self_test) {
        setReport(d.self_test);
        if (!d.self_test.running) setPolling(false);
      }
    },
  });

  const run = async () => {
    setBusy(true);
    try {
      await api.startSelfTest('full');
      setReport(null);
      setPolling(true);
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    try {
      await api.cancelSelfTest();
    } catch {
      /* ignore */
    }
  };

  const running = report?.running || polling;
  const mark = (p: boolean | null) =>
    p === true ? '✓' : p === false ? '✗' : '•';
  const markColor = (p: boolean | null) =>
    p === true ? 'text-success' : p === false ? 'text-danger' : 'text-gray-500';
  const cal = report?.calibration;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">On-bed self-test</p>
        {running ? (
          <button
            onClick={cancel}
            className="text-xs px-3 py-1.5 rounded-lg bg-danger/20 text-danger font-medium border border-danger/30"
          >
            Cancel
          </button>
        ) : (
          <button
            onClick={run}
            disabled={busy}
            className="text-xs px-3 py-1.5 rounded-lg bg-brand text-white font-medium disabled:opacity-50"
          >
            {busy ? 'Starting…' : 'Run bed test'}
          </button>
        )}
      </div>
      <p className="text-[11px] text-gray-500 leading-relaxed">
        Run this lying on the bed, filled + primed. It checks the bed senses you (presence, HR,
        HRV, breathing) and measures how fast it cools and heats — the rates feed the pre-cool and
        wake-warmup timing. Drives cool→heat briefly, then always powers the side off.
      </p>

      {running && (
        <div className="flex items-center gap-2 text-xs text-brand">
          <div className="w-3.5 h-3.5 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          <span>Running… {report?.phase ?? 'starting'} (stay in bed, still)</span>
        </div>
      )}

      {report && (
        <div className="space-y-1.5">
          {!report.running && (
            <p
              className={`text-sm font-semibold ${
                report.aborted
                  ? 'text-warning'
                  : report.overall_passed
                  ? 'text-success'
                  : 'text-danger'
              }`}
            >
              {report.aborted
                ? '⚠ Test cancelled / aborted (side is off)'
                : report.overall_passed
                ? '✓ Bed passed — sensing + thermal response good'
                : `✗ ${report.n_fail} check(s) failed`}
              {report.simulated ? ' · simulator' : ''}
            </p>
          )}
          {report.checks.map((c) => (
            <div key={c.name} className="flex items-start justify-between text-xs gap-2">
              <span className="text-gray-400 shrink-0">
                <span className={markColor(c.passed)}>{mark(c.passed)}</span>{' '}
                {c.name.replace(/_/g, ' ')}
              </span>
              <span className="text-gray-500 text-right">{c.detail}</span>
            </div>
          ))}
          {cal && (cal.cool_f_per_min != null || cal.cool_levels_per_min != null) && (
            <div className="pt-2 mt-1 border-t border-surface-border space-y-1">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">
                Measured thermal calibration
              </p>
              <div className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Cools</span>
                <span className="text-gray-300">
                  {cal.cool_f_per_min != null ? `${cal.cool_f_per_min.toFixed(1)}°F/min` : '—'}
                  {cal.cool_lag_min != null && (
                    <span className="text-gray-600"> · ~{cal.cool_lag_min.toFixed(0)} min to settle</span>
                  )}
                </span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="text-gray-400">Heats</span>
                <span className="text-gray-300">
                  {cal.heat_f_per_min != null ? `+${cal.heat_f_per_min.toFixed(1)}°F/min` : '—'}
                  {cal.heat_lag_min != null && (
                    <span className="text-gray-600"> · ~{cal.heat_lag_min.toFixed(0)} min to settle</span>
                  )}
                </span>
              </div>
              <p className="text-[10px] text-gray-600 pt-0.5">
                Feeds pre-cool lead time + wake warm-up timing.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ValidateCard() {
  const [bt, setBt] = useState<Backtest | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      setBt(await api.runBacktest());
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Validate controller</p>
        <button
          onClick={run}
          disabled={busy}
          className="text-xs px-3 py-1.5 rounded-lg bg-brand text-white font-medium disabled:opacity-50"
        >
          {busy ? 'Running…' : 'Run backtest'}
        </button>
      </div>
      <p className="text-[11px] text-gray-500 leading-relaxed">
        Runs whole simulated nights through the real controller vs. leaving the bed uncontrolled,
        on a model where temperature actually moves sleep — and checks every safety limit.
      </p>
      {bt && (
        <div className="space-y-1.5">
          <p className={`text-sm font-semibold ${bt.improved ? 'text-success' : 'text-danger'}`}>
            {bt.improved ? '✓ Closed loop improves your night' : '✗ No improvement'}
          </p>
          {[
            ['Awakenings', 'wake_events'],
            ['Deep (min)', 'deep_min'],
            ['Efficiency', 'efficiency'],
            ['Grogginess', 'grogginess'],
          ].map(([label, key]) => (
            <div key={key} className="flex items-center justify-between text-xs">
              <span className="text-gray-400">{label}</span>
              <span className="text-gray-300">
                {bt.controller[key]} <span className="text-gray-600">vs</span> {bt.baseline[key]}
                <span className={bt.delta[key] === 0 ? 'text-gray-500' : 'text-success'}>
                  {' '}
                  ({bt.delta[key] > 0 ? '+' : ''}
                  {bt.delta[key]})
                </span>
              </span>
            </div>
          ))}
          <p className="text-[10px] text-gray-500 pt-1">
            Safety: max step {bt.safety.max_step_f}°F (limit {bt.safety.max_step_limit}) ·{' '}
            {bt.safety.out_of_bounds_ticks} out-of-bounds ticks
          </p>
        </div>
      )}
    </div>
  );
}

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

          {/* On-bed self-test / thermal calibration */}
          <BedTestCard />

          {/* Controller validation backtest */}
          <ValidateCard />

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
