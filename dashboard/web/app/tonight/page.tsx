'use client';

import { useState, useEffect } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import ModeToggle from '@/components/ModeToggle';
import TempStepper from '@/components/TempStepper';
import WakeTimePicker from '@/components/WakeTimePicker';
import BigButton from '@/components/BigButton';
import EmergencyStop from '@/components/EmergencyStop';
import useSWR from 'swr';
import { TonightResponse, api, fetcher } from '@/lib/api';

function TonightContent() {
  const { data, mutate } = useSWR<TonightResponse>('/api/tonight', fetcher, {
    refreshInterval: 15000,
  });

  const [mode, setMode] = useState<'auto' | 'manual' | 'view'>('auto');
  const [targetTemp, setTargetTemp] = useState(70);
  const [wakeTime, setWakeTime] = useState('07:00');
  const [windowMin, setWindowMin] = useState(10);
  const [loading, setLoading] = useState<string | null>(null);
  const [toast, setToast] = useState('');

  useEffect(() => {
    if (data) {
      setMode(data.mode);
      setTargetTemp(data.target_temp_f ?? 70);
      if (data.schedule?.required_wake_time) {
        setWakeTime(data.schedule.required_wake_time);
      }
    }
  }, [data]);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 2500);
  };

  const handleControl = async (cmd: 'start' | 'pause' | 'resume' | 'stop') => {
    setLoading(cmd);
    try {
      await api.control(cmd);
      showToast(`Command "${cmd}" queued`);
      await mutate();
    } catch (e) {
      showToast(`Error: ${e instanceof Error ? e.message : 'Unknown'}`);
    } finally {
      setLoading(null);
    }
  };

  const handleModeChange = async (m: 'auto' | 'manual' | 'view') => {
    const prev = mode;
    setMode(m);
    try {
      await api.setMode(m);
      showToast(`Mode set to ${m}`);
    } catch {
      setMode(prev);
      showToast('Failed to update mode');
    }
  };

  const handleTempSave = async () => {
    setLoading('temp');
    try {
      await api.setTemp(targetTemp);
      showToast(`Target set to ${targetTemp}°F`);
      await mutate();
    } catch {
      showToast('Failed to update temperature');
    } finally {
      setLoading(null);
    }
  };

  const handleWakeSave = async (t: string, w: number) => {
    setWakeTime(t);
    setWindowMin(w);
    try {
      await api.setWake(t, w);
      showToast(`Wake time set to ${t}`);
    } catch {
      showToast('Failed to update wake time');
    }
  };

  const state = data?.state ?? 'idle';
  const canStart = ['idle', 'off', 'stopped'].includes(state.toLowerCase());
  const canPause = state.toLowerCase() === 'sleeping' || state.toLowerCase() === 'running';
  const canResume = state.toLowerCase() === 'paused';
  const canStop = !['idle', 'off', 'stopped'].includes(state.toLowerCase());

  return (
    <div className="flex flex-col min-h-screen">
      {/* Toast */}
      {toast && (
        <div className="fixed top-4 left-4 right-4 z-50 bg-surface-card border border-surface-border rounded-xl px-4 py-3 text-sm text-white text-center shadow-lg">
          {toast}
        </div>
      )}

      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Tonight</h1>
          <p className="text-sm text-gray-500">
            State: <span className="text-white font-medium">{state}</span>
            {data?.setpoint && (
              <span className="ml-3 text-gray-500">
                Setpoint v{data.setpoint.version}
              </span>
            )}
          </p>
        </div>

        <div className="px-4 space-y-5">
          {/* Mode Toggle */}
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">Mode</p>
            <ModeToggle value={mode} onChange={handleModeChange} />
          </div>

          {/* Temperature */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-4">
              Target Temperature
            </p>
            <TempStepper
              value={targetTemp}
              onChange={setTargetTemp}
              disabled={mode === 'auto' || mode === 'view'}
            />
            {mode === 'manual' && (
              <BigButton
                fullWidth
                className="mt-4"
                onClick={handleTempSave}
                loading={loading === 'temp'}
              >
                Set Temperature
              </BigButton>
            )}
            {mode === 'auto' && (
              <p className="text-xs text-gray-600 text-center mt-3">
                Auto mode — AI controls temperature
              </p>
            )}
          </div>

          {/* Wake Time */}
          <WakeTimePicker
            value={wakeTime}
            windowMin={windowMin}
            onChange={handleWakeSave}
            disabled={mode === 'view'}
          />

          {/* Control Buttons */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
            <p className="text-xs text-gray-500 uppercase tracking-wider">Controls</p>
            <div className="grid grid-cols-2 gap-3">
              <BigButton
                variant="primary"
                disabled={!canStart || !!loading}
                loading={loading === 'start'}
                onClick={() => handleControl('start')}
              >
                Start
              </BigButton>
              <BigButton
                variant="secondary"
                disabled={!canPause || !!loading}
                loading={loading === 'pause'}
                onClick={() => handleControl('pause')}
              >
                Pause
              </BigButton>
              <BigButton
                variant="secondary"
                disabled={!canResume || !!loading}
                loading={loading === 'resume'}
                onClick={() => handleControl('resume')}
              >
                Resume
              </BigButton>
              <BigButton
                variant="ghost"
                disabled={!canStop || !!loading}
                loading={loading === 'stop'}
                onClick={() => handleControl('stop')}
              >
                Stop
              </BigButton>
            </div>
          </div>

          {/* Setpoint info */}
          {data?.setpoint && (
            <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-3">
                Current Setpoint
              </p>
              <div className="grid grid-cols-2 gap-2 text-sm">
                {[
                  ['Neutral', `${data.setpoint.neutral_f.toFixed(1)}°F`],
                  ['Deep bias', `${data.setpoint.deep_bias_f > 0 ? '+' : ''}${data.setpoint.deep_bias_f.toFixed(1)}°F`],
                  ['REM offset', `${data.setpoint.rem_warm_offset_f > 0 ? '+' : ''}${data.setpoint.rem_warm_offset_f.toFixed(1)}°F`],
                  ['Wake ramp', `${data.setpoint.wake_ramp_f > 0 ? '+' : ''}${data.setpoint.wake_ramp_f.toFixed(1)}°F`],
                ].map(([label, val]) => (
                  <div key={label}>
                    <p className="text-gray-500 text-xs">{label}</p>
                    <p className="text-white font-medium">{val}</p>
                  </div>
                ))}
              </div>
              <p className="text-xs text-gray-600 mt-2">
                Source: {data.setpoint.source} · v{data.setpoint.version}
              </p>
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

export default function TonightPage() {
  return (
    <AuthGuard>
      <TonightContent />
    </AuthGuard>
  );
}
