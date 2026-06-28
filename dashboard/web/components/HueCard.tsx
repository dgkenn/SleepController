'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, HueConfig } from '@/lib/api';

/** Philips Hue dawn light: drive your bulbs as a silent sunrise during the wake window —
 *  dim warm at the start, brighter toward your wake time, off once you're up. Works with any
 *  regular Hue bulbs; pick one room or several individual lights. */
export default function HueCard() {
  const { data: cfg, mutate } = useSWR<HueConfig>('/api/wake/light/config', fetcher, {
    refreshInterval: 60000,
  });
  const [lights, setLights] = useState<Record<string, string> | null>(null);
  const [groups, setGroups] = useState<Record<string, string> | null>(null);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  if (!cfg) return null;

  const save = async (v: Partial<Omit<HueConfig, 'paired'>>) => {
    await api.hueConfigUpdate(v);
    mutate();
  };

  const pair = async () => {
    setBusy(true);
    setMsg('Press the round button on your Hue bridge now…');
    try {
      const r = await api.huePair(cfg.bridge_ip ?? undefined);
      if (r.ok) {
        setMsg('Paired! Now pick your light(s).');
        mutate();
        loadLights();
      } else {
        setMsg(r.error ?? 'Pairing failed');
      }
    } catch {
      setMsg('Pairing failed — is the bridge IP right?');
    } finally {
      setBusy(false);
    }
  };

  const loadLights = async () => {
    const r = await api.hueLights();
    if (!r.error) {
      setLights(r.lights ?? {});
      setGroups(r.groups ?? {});
    }
  };

  const toggleLight = (id: string) => {
    const set = new Set(cfg.target_ids);
    set.has(id) ? set.delete(id) : set.add(id);
    save({ target_ids: Array.from(set), kind: 'lights' });
  };

  const pickGroup = (id: string) => save({ target_ids: [id], kind: 'group' });

  const test = async () => {
    setBusy(true);
    setMsg('Flashing your light(s)…');
    const r = await api.hueTest();
    setMsg(r.ok ? 'Look — your lights should be glowing.' : r.error ?? 'Test failed');
    setBusy(false);
  };

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Dawn light · Hue</p>
        <button
          onClick={() => save({ enabled: !cfg.enabled })}
          disabled={!cfg.paired || cfg.target_ids.length === 0}
          className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-40 ${
            cfg.enabled ? 'bg-success' : 'bg-surface-raised border border-surface-border'
          }`}
          aria-label="Toggle dawn light"
        >
          <span
            className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
              cfg.enabled ? 'translate-x-5' : 'translate-x-0.5'
            }`}
          />
        </button>
      </div>

      <p className="text-[11px] text-gray-500 leading-relaxed">
        A silent sunrise on your regular Hue bulbs during the wake window — dim warm → brighter →
        off once you&apos;re up. Pairs over your LAN; no cloud account.
      </p>

      {/* Bridge IP + pairing */}
      <div className="flex items-center gap-2">
        <input
          type="text"
          inputMode="decimal"
          placeholder="Bridge IP (optional — auto-discovers)"
          defaultValue={cfg.bridge_ip ?? ''}
          onBlur={(e) => e.target.value && save({ bridge_ip: e.target.value.trim() })}
          className="flex-1 bg-surface-raised border border-surface-border rounded-lg px-2.5 py-2 text-xs text-white"
        />
        <button
          onClick={pair}
          disabled={busy}
          className="text-xs px-3 py-2 rounded-lg bg-brand text-white font-medium disabled:opacity-50"
        >
          {cfg.paired ? 'Re-pair' : 'Pair'}
        </button>
      </div>

      {cfg.paired && (
        <button onClick={loadLights} className="text-[11px] text-brand">
          {lights || groups ? 'Refresh lights' : 'Load my lights'}
        </button>
      )}

      {/* Rooms (control both bulbs at once) */}
      {groups && Object.keys(groups).length > 0 && (
        <div>
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Rooms (both at once)</p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(groups).map(([id, name]) => (
              <button
                key={id}
                onClick={() => pickGroup(id)}
                className={`text-[11px] px-2 py-1 rounded-lg ${
                  cfg.kind === 'group' && cfg.target_ids.includes(id)
                    ? 'bg-brand text-white'
                    : 'bg-surface-raised text-gray-300 border border-surface-border'
                }`}
              >
                {name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Individual bulbs (pick your two) */}
      {lights && Object.keys(lights).length > 0 && (
        <div>
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Individual bulbs</p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(lights).map(([id, name]) => (
              <button
                key={id}
                onClick={() => toggleLight(id)}
                className={`text-[11px] px-2 py-1 rounded-lg ${
                  cfg.kind === 'lights' && cfg.target_ids.includes(id)
                    ? 'bg-brand text-white'
                    : 'bg-surface-raised text-gray-300 border border-surface-border'
                }`}
              >
                {name}
              </button>
            ))}
          </div>
        </div>
      )}

      {cfg.paired && cfg.target_ids.length > 0 && (
        <button onClick={test} disabled={busy} className="text-[11px] text-brand disabled:opacity-50">
          Test the sunrise now
        </button>
      )}

      {msg && <p className="text-[11px] text-gray-400">{msg}</p>}
    </div>
  );
}
