'use client';

import { useState, useEffect } from 'react';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';
import BigButton from '@/components/BigButton';
import useSWR from 'swr';
import { SettingsResponse, fetcher, api } from '@/lib/api';

interface FieldConfig {
  key: string;
  label: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
}

const FIELDS: FieldConfig[] = [
  { key: 'neutral_temp_f', label: 'Neutral Temp', unit: '°F', min: 55, max: 115, step: 0.5 },
  { key: 'deep_bias_temp_f', label: 'Deep Sleep Bias', unit: '°F', min: -10, max: 10, step: 0.5 },
  { key: 'wake_ramp_temp_f', label: 'Wake Ramp', unit: '°F', min: -5, max: 15, step: 0.5 },
  { key: 'wake_window_min', label: 'Wake Window', unit: 'min', min: 1, max: 60, step: 1 },
  { key: 'wake_vibration_power', label: 'Vibration Power', unit: '%', min: 0, max: 100, step: 1 },
  { key: 'max_step_f', label: 'Max Step Size', unit: '°F', min: 0.5, max: 5, step: 0.5 },
  { key: 'hrv_target_ms', label: 'HRV Target', unit: 'ms', min: 10, max: 200, step: 1 },
  { key: 'wake_events_max', label: 'Max Wake Events', unit: '', min: 0, max: 20, step: 1 },
];

function SettingsContent() {
  const { data, mutate } = useSWR<SettingsResponse>('/api/settings', fetcher);
  const [values, setValues] = useState<Record<string, number>>({});
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [notifEnabled, setNotifEnabled] = useState(false);

  useEffect(() => {
    if (data) {
      const merged: Record<string, number> = {};
      const defaults = data.defaults as Record<string, number>;
      const stored = data.stored as Record<string, number>;
      for (const f of FIELDS) {
        merged[f.key] =
          (stored[f.key] as number) ?? (defaults[f.key] as number) ?? 0;
      }
      setValues(merged);
    }
  }, [data]);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 2500);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.saveSettings(values);
      await mutate();
      showToast('Settings saved');
    } catch {
      showToast('Failed to save settings');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    if (!data) return;
    const defaults = data.defaults as Record<string, number>;
    const reset: Record<string, number> = {};
    for (const f of FIELDS) {
      reset[f.key] = defaults[f.key] ?? 0;
    }
    setValues(reset);
    showToast('Reset to defaults (not saved)');
  };

  return (
    <div className="flex flex-col min-h-screen">
      {toast && (
        <div className="fixed top-4 left-4 right-4 z-50 bg-surface-card border border-surface-border rounded-xl px-4 py-3 text-sm text-white text-center shadow-lg">
          {toast}
        </div>
      )}

      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">Settings</h1>
          <p className="text-sm text-gray-500">Controller defaults and preferences</p>
        </div>

        <div className="px-4 space-y-4">
          {/* Notification toggle stub */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-white">Push Notifications</p>
                <p className="text-xs text-gray-500 mt-0.5">Alerts and wake reminders</p>
              </div>
              <button
                onClick={() => setNotifEnabled((v) => !v)}
                className={`w-12 h-7 rounded-full transition-colors relative ${
                  notifEnabled ? 'bg-brand' : 'bg-gray-700'
                }`}
                aria-label="Toggle notifications"
                role="switch"
                aria-checked={notifEnabled}
              >
                <span
                  className={`absolute top-0.5 left-0.5 w-6 h-6 bg-white rounded-full shadow transition-transform ${
                    notifEnabled ? 'translate-x-5' : 'translate-x-0'
                  }`}
                />
              </button>
            </div>
            {notifEnabled && (
              <p className="text-xs text-warning mt-2">
                Note: Push notifications require PWA install and backend webhook support
              </p>
            )}
          </div>

          {/* Settings fields */}
          <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider">Controller Defaults</p>
            {FIELDS.map((field) => (
              <div key={field.key}>
                <label className="block text-sm text-gray-300 mb-1">
                  {field.label}
                  {field.unit ? <span className="text-gray-500 ml-1">({field.unit})</span> : null}
                </label>
                <div className="flex items-center gap-3">
                  <input
                    type="number"
                    value={values[field.key] ?? ''}
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    onChange={(e) =>
                      setValues((prev) => ({
                        ...prev,
                        [field.key]: parseFloat(e.target.value),
                      }))
                    }
                    className="
                      flex-1 bg-surface-raised border border-surface-border rounded-xl
                      px-4 py-2.5 text-white text-sm
                      focus:outline-none focus:border-brand min-h-[44px]
                    "
                  />
                  {data && (
                    <span className="text-xs text-gray-600 w-16 text-right">
                      default: {(data.defaults as Record<string, number>)[field.key]}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Actions */}
          <div className="flex gap-3">
            <BigButton
              variant="ghost"
              onClick={handleReset}
              className="flex-1"
            >
              Reset
            </BigButton>
            <BigButton
              onClick={handleSave}
              loading={saving}
              className="flex-1"
            >
              Save
            </BigButton>
          </div>
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AuthGuard>
      <SettingsContent />
    </AuthGuard>
  );
}
