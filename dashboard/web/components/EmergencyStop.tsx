'use client';

import { useState } from 'react';
import { api } from '@/lib/api';

interface EmergencyStopProps {
  onDone?: () => void;
}

export default function EmergencyStop({ onDone }: EmergencyStopProps) {
  const [loading, setLoading] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [done, setDone] = useState(false);

  const handlePress = async () => {
    if (!confirm) {
      setConfirm(true);
      setTimeout(() => setConfirm(false), 3000);
      return;
    }
    setLoading(true);
    try {
      await api.control('safe-default');
      setDone(true);
      onDone?.();
    } catch {
      // ignore
    } finally {
      setLoading(false);
      setConfirm(false);
    }
  };

  return (
    <button
      onClick={handlePress}
      disabled={loading || done}
      className={`
        w-full min-h-[60px] rounded-2xl font-bold text-lg
        transition-all duration-150 active:scale-[0.97]
        flex items-center justify-center gap-3
        border-2
        ${
          done
            ? 'bg-success/10 border-success/40 text-success'
            : confirm
            ? 'bg-danger border-danger text-white animate-pulse'
            : 'bg-danger/10 border-danger/40 text-danger hover:bg-danger/20'
        }
        disabled:opacity-50 disabled:cursor-not-allowed
      `}
      aria-label="Emergency stop — set safe defaults"
    >
      {loading ? (
        <svg className="w-6 h-6 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      ) : done ? (
        <>
          <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
            <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
          </svg>
          Safe Defaults Applied
        </>
      ) : confirm ? (
        <>
          <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
          </svg>
          Tap again to confirm
        </>
      ) : (
        <>
          <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm3.5-9c.83 0 1.5-.67 1.5-1.5V8c0-.83-.67-1.5-1.5-1.5h-7C7.67 6.5 7 7.17 7 8v1.5c0 .83.67 1.5 1.5 1.5H9V17h6v-6h1.5z" />
          </svg>
          Emergency Stop
        </>
      )}
    </button>
  );
}
