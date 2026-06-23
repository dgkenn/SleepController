'use client';

import { useState } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import StateBadge from '@/components/StateBadge';
import StatusHero from '@/components/StatusHero';
import RecommendationCard from '@/components/RecommendationCard';
import AlertBanner from '@/components/AlertBanner';
import EmergencyStop from '@/components/EmergencyStop';
import QuickTemp from '@/components/QuickTemp';
import CheckInCard from '@/components/CheckInCard';
import { useStatusStream } from '@/lib/useStatusStream';
import useSWR from 'swr';
import { CheckInStatus, fetcher } from '@/lib/api';

function HomeContent() {
  const { data, isLive, error } = useStatusStream();
  const { data: checkin, mutate: mutateCheckin } = useSWR<CheckInStatus>(
    '/api/checkin/status', fetcher, { refreshInterval: 60000 }
  );
  const [alerts, setAlerts] = useState(data?.alerts ?? []);

  // Sync alerts when data changes
  const currentAlerts = data?.alerts ?? alerts;

  const handleAck = (id: string) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id));
  };

  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center flex-1 gap-4 text-center px-6">
        <svg className="w-12 h-12 text-danger" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
        </svg>
        <div>
          <p className="text-lg font-semibold text-white">Cannot reach controller</p>
          <p className="text-sm text-gray-500 mt-1">Check that the backend is running</p>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex items-center justify-center flex-1">
        <div className="w-8 h-8 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        {/* Header */}
        <div className="px-4 pt-14 pb-4 space-y-3">
          <div className="flex items-center justify-between">
            <h1 className="text-xl font-bold text-white">Status</h1>
            <div className="flex items-center gap-1.5">
              <span
                className={`w-2 h-2 rounded-full ${isLive ? 'bg-success' : 'bg-gray-600'}`}
              />
              <span className="text-xs text-gray-500">{isLive ? 'Live' : 'Polling'}</span>
            </div>
          </div>
          <StateBadge state={data.state} mode={data.mode} stale={data.stale} />
        </div>

        <div className="px-4 space-y-4">
          {/* Alerts */}
          {currentAlerts.length > 0 && (
            <AlertBanner alerts={currentAlerts} onAck={handleAck} />
          )}

          {/* Wake-up exit survey (shown when a check-in is due) */}
          {checkin?.due && (
            <CheckInCard date={checkin.date} onDone={() => mutateCheckin()} />
          )}

          {/* Hero temps */}
          <StatusHero data={data} />

          {/* Realtime temperature control */}
          <QuickTemp targetF={data.target_temp_f} powerOn={data.power_on ?? true} />

          {/* Recommendation */}
          <RecommendationCard recommendation={data.recommendation} />

          {/* Last night summary */}
          {data.last_night && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider">
                  Last Night
                </h2>
                {data.last_night.perfect_sleep && (
                  <div className="flex items-center gap-2" title={data.last_night.perfect_sleep.rationale}>
                    <span className="text-[10px] text-gray-500 uppercase">Perfect Sleep</span>
                    <span
                      className={`text-sm font-bold tabular-nums ${
                        data.last_night.perfect_sleep.score >= 80
                          ? 'text-success'
                          : data.last_night.perfect_sleep.score >= 60
                            ? 'text-warning'
                            : 'text-danger'
                      }`}
                    >
                      {data.last_night.perfect_sleep.score.toFixed(0)}
                      <span className="text-gray-600 text-xs font-normal">/100</span>
                    </span>
                  </div>
                )}
              </div>
              <div className="grid grid-cols-3 gap-3 text-center">
                {[
                  {
                    label: 'Sleep',
                    value: `${Math.floor(data.last_night.total_sleep_min / 60)}h ${data.last_night.total_sleep_min % 60}m`,
                  },
                  {
                    label: 'Efficiency',
                    value: `${(data.last_night.sleep_efficiency * 100).toFixed(0)}%`,
                  },
                  {
                    label: 'Score',
                    value: data.last_night.outcome_score.toFixed(0),
                  },
                ].map((s) => (
                  <div key={s.label}>
                    <p className="text-xs text-gray-500">{s.label}</p>
                    <p className="text-lg font-bold text-white">{s.value}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Emergency Stop */}
          <EmergencyStop />
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function HomePage() {
  return (
    <AuthGuard>
      <HomeContent />
    </AuthGuard>
  );
}
