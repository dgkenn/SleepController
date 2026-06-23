'use client';

import { useState } from 'react';
import { Alert, api } from '@/lib/api';

interface AlertBannerProps {
  alerts: Alert[];
  onAck?: (id: string) => void;
}

const severityStyles: Record<string, string> = {
  error: 'bg-danger/10 border-danger/30 text-danger',
  warning: 'bg-warning/10 border-warning/30 text-warning',
  info: 'bg-brand/10 border-brand/30 text-brand',
};

export default function AlertBanner({ alerts, onAck }: AlertBannerProps) {
  const [acking, setAcking] = useState<string | null>(null);

  if (!alerts || alerts.length === 0) return null;

  const handleAck = async (id: string) => {
    setAcking(id);
    try {
      await api.ackAlert(id);
      onAck?.(id);
    } catch {
      // ignore
    } finally {
      setAcking(null);
    }
  };

  return (
    <div className="space-y-2">
      {alerts.map((alert) => (
        <div
          key={alert.id}
          className={`flex items-start gap-3 p-3 rounded-xl border ${
            severityStyles[alert.severity] ?? severityStyles.info
          }`}
        >
          <svg className="w-5 h-5 mt-0.5 shrink-0" viewBox="0 0 24 24" fill="currentColor">
            {alert.severity === 'error' ? (
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
            ) : alert.severity === 'warning' ? (
              <path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z" />
            ) : (
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z" />
            )}
          </svg>
          <p className="flex-1 text-sm leading-snug">{alert.message}</p>
          <button
            onClick={() => handleAck(alert.id)}
            disabled={acking === alert.id}
            className="shrink-0 text-current opacity-60 hover:opacity-100 min-h-[44px] min-w-[44px] flex items-center justify-center -mr-2 -my-2"
            aria-label="Dismiss alert"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
              <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
