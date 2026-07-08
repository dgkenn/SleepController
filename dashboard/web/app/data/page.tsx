'use client';

import { useState, useEffect } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import NightCard from '@/components/NightCard';
import NoteEditor from '@/components/NoteEditor';
import MetricChart from '@/components/MetricChart';
import useSWR from 'swr';
import { NightSummary, NightSample, Note, Intervention, fetcher, api } from '@/lib/api';

function DataContent() {
  const today = new Date().toISOString().slice(0, 10);
  const [selectedDate, setSelectedDate] = useState(today);

  const { data: nights } = useSWR<NightSummary[]>('/api/nights?limit=30', fetcher, {
    refreshInterval: 60000,
  });

  const { data: samples } = useSWR<NightSample[]>(
    selectedDate ? `/api/nights/${selectedDate}/samples` : null,
    fetcher
  );

  const { data: interventions } = useSWR<Intervention[]>(
    '/api/interventions?limit=50',
    fetcher
  );

  const [note, setNote] = useState<string>('');

  useEffect(() => {
    api.notes(selectedDate).then((notes: Note[]) => {
      setNote(notes[0]?.text ?? '');
    }).catch(() => setNote(''));
  }, [selectedDate]);

  const lastNight = nights?.[0] ?? null;
  const displayDate = selectedDate;

  // Format samples for chart
  const chartData = (samples ?? []).map((s) => ({
    ts: new Date(s.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    hr: s.heart_rate,
    bed: s.bed_temp_f,
    hrv: s.hrv,
  }));

  // HR/HRV are Pod-physiology-gated (e.g. subscription-gated) -- bed temp always comes from the
  // thermal side and is unaffected. Don't let an all-null series render as a flat "0" line.
  const hasHr = chartData.some((d) => d.hr != null);
  const hasHrv = chartData.some((d) => d.hrv != null);
  const NO_PHYSIOLOGY_MSG =
    'No HR/HRV/stage data — Pod subscription-gated; connect the iPhone motion sensor (Admin → Phone Sensor).';

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Sleep Data</h1>
        </div>

        <div className="px-4 space-y-4">
          {/* Date selector */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-400">Date:</label>
            <input
              type="date"
              value={selectedDate}
              max={today}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="
                bg-surface-card border border-surface-border rounded-xl
                px-3 py-2 text-white text-sm
                focus:outline-none focus:border-brand min-h-[44px]
              "
            />
          </div>

          {/* Last night summary */}
          {lastNight ? (
            <NightCard night={lastNight} />
          ) : (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border text-center text-gray-500 text-sm">
              No nights recorded yet
            </div>
          )}

          {/* Sample chart */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
              Heart Rate & Bed Temp — {displayDate}
            </p>
            <MetricChart
              data={chartData}
              xKey="ts"
              lines={
                hasHr
                  ? [
                      { key: 'hr', label: 'HR (bpm)', color: '#ef4444' },
                      { key: 'bed', label: 'Bed Temp °F', color: '#60a5fa' },
                    ]
                  : [{ key: 'bed', label: 'Bed Temp °F', color: '#60a5fa' }]
              }
              yFormatter={(v) => v.toFixed(0)}
              height={200}
            />
            {!hasHr && chartData.length > 0 && (
              <p className="text-xs text-gray-600 mt-3 leading-relaxed">{NO_PHYSIOLOGY_MSG}</p>
            )}
          </div>

          {/* HRV chart */}
          {chartData.length > 0 && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                HRV — {displayDate}
              </p>
              {hasHrv ? (
                <MetricChart
                  data={chartData}
                  xKey="ts"
                  lines={[{ key: 'hrv', label: 'HRV (ms)', color: '#22c55e' }]}
                  yFormatter={(v) => `${v.toFixed(0)}ms`}
                  height={160}
                />
              ) : (
                <MetricChart data={[]} lines={[]} height={160} emptyMessage={NO_PHYSIOLOGY_MSG} />
              )}
            </div>
          )}

          {/* Interventions */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
              Recent Interventions
            </p>
            {!interventions || interventions.length === 0 ? (
              <p className="text-sm text-gray-600 text-center py-2">
                No interventions recorded
              </p>
            ) : (
              <div className="divide-y divide-surface-border">
                {interventions.slice(0, 10).map((iv, i) => (
                  <div key={i} className="py-2.5 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-white capitalize">
                        {iv.action}{' '}
                        {iv.magnitude_f ? (
                          <span className="text-gray-400 font-normal">
                            {Math.abs(iv.magnitude_f).toFixed(1)}°F
                          </span>
                        ) : null}
                      </p>
                      <p className="text-xs text-gray-500 truncate">{iv.reason}</p>
                    </div>
                    <div className="text-right text-xs shrink-0">
                      <p className="text-gray-400">
                        {iv.ts
                          ? new Date(iv.ts).toLocaleTimeString([], {
                              hour: '2-digit',
                              minute: '2-digit',
                            })
                          : ''}
                      </p>
                      <p className="text-gray-600">{iv.state}</p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Night history */}
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3 px-0">
              Night History
            </p>
            <div className="space-y-2">
              {(nights ?? []).slice(1, 8).map((n) => (
                <button
                  key={n.date}
                  onClick={() => setSelectedDate(n.date)}
                  className="w-full text-left"
                >
                  <NightCard night={n} compact />
                </button>
              ))}
            </div>
          </div>

          {/* Note editor */}
          <NoteEditor date={displayDate} initialText={note} />
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function DataPage() {
  return (
    <AuthGuard>
      <DataContent />
    </AuthGuard>
  );
}
