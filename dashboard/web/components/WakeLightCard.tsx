'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, PlugConfig, PlugScan, PlugCloudSetup } from '@/lib/api';

/** The 10,000-lux therapy lamp on a Wi-Fi smart plug. It comes on when you press "I'm awake"
 *  (a 30-min morning dose) or when the smart alarm wakes you, and never during the night. */
export default function WakeLightCard() {
  const { data: cfg, mutate } = useSWR<PlugConfig>('/api/wake/plug/config', fetcher, {
    refreshInterval: 60000,
  });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [scan, setScan] = useState<PlugScan | null>(null);
  const [region, setRegion] = useState('us');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [choose, setChoose] = useState<PlugCloudSetup['choose']>(undefined);
  const [urlMode, setUrlMode] = useState(false);
  const [onUrl, setOnUrl] = useState('');
  const [offUrl, setOffUrl] = useState('');

  if (!cfg) return null;
  const ready = cfg.configured && cfg.enabled;

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(true);
    setMsg(label);
    try {
      await fn();
    } catch {
      setMsg('Could not reach the computer — try again in a moment.');
    } finally {
      setBusy(false);
    }
  };

  const find = () =>
    run('Listening for the plug on your Wi-Fi (about 20 seconds)…', async () => {
      const r = await api.plugScan();
      setScan(r);
      if (!r.ok) setMsg(r.error ?? 'Scan failed');
      else if (r.devices.length) setMsg(`Found ${r.devices.length} plug-type device(s) on your Wi-Fi.`);
      else setMsg(r.hint ?? 'Nothing answered.');
    });

  const connect = (device_id?: string) =>
    run('Fetching the plug’s key…', async () => {
      const r = await api.plugTuyaCloud({ region, api_key: apiKey, api_secret: apiSecret, device_id });
      if (r.ok) {
        setChoose(undefined);
        setApiSecret('');
        setMsg(
          r.found_on_lan
            ? `Connected to “${r.plug?.name || 'plug'}”. Tap Test to check the lamp.`
            : `Connected to “${r.plug?.name || 'plug'}”, but it wasn’t found on the Wi-Fi yet — the computer keeps looking every 10 minutes.`
        );
        mutate();
      } else {
        setChoose(r.choose);
        setMsg(r.error ?? 'Could not connect');
      }
    });

  const saveUrls = () =>
    run('Saving…', async () => {
      await api.plugConfigUpdate({
        enabled: true,
        backend: 'http',
        config: { on_url: onUrl.trim(), off_url: offUrl.trim() },
      });
      setMsg('Saved. Tap Test to check the lamp.');
      mutate();
    });

  const test = (on: boolean) =>
    run(on ? 'Turning the lamp on…' : 'Turning the lamp off…', async () => {
      const r = await api.plugTest(on);
      setMsg(r.ok ? (on ? 'The lamp should be on now.' : 'Off.') : 'The plug did not answer.');
    });

  const dose = (on: boolean) =>
    run(on ? 'Starting a 30-minute light dose…' : 'Turning the light off…', async () => {
      await api.wakeLight(on, on ? 30 : undefined);
      setMsg(on ? 'Light on for 30 minutes.' : 'Light off.');
    });

  const input =
    'w-full bg-surface-raised border border-surface-border rounded-lg px-2.5 py-2 text-xs text-white';
  const btn = 'text-xs px-3 py-2 rounded-lg font-medium disabled:opacity-50';

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Wake light · smart plug</p>
        {cfg.configured && (
          <button
            onClick={() => api.plugConfigUpdate({ enabled: !cfg.enabled }).then(() => mutate())}
            className={`relative w-11 h-6 rounded-full transition-colors ${
              cfg.enabled ? 'bg-success' : 'bg-surface-raised border border-surface-border'
            }`}
            aria-label="Toggle wake light"
          >
            <span
              className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
                cfg.enabled ? 'translate-x-5' : 'translate-x-0.5'
              }`}
            />
          </button>
        )}
      </div>

      <p className="text-[11px] text-gray-500 leading-relaxed">
        Your therapy lamp comes on for 30 minutes when you press “I’m awake” (between 4:30 am and 1 pm), or
        when the smart alarm wakes you. It never comes on during the night, and shuts off after 45 minutes
        at most.
      </p>

      {ready && (
        <div className="flex flex-wrap gap-2">
          <button onClick={() => dose(true)} disabled={busy} className={`${btn} bg-amber-500 text-black`}>
            Light on (30 min)
          </button>
          <button
            onClick={() => dose(false)}
            disabled={busy}
            className={`${btn} bg-surface-raised text-gray-200 border border-surface-border`}
          >
            Off
          </button>
          <button onClick={() => test(true)} disabled={busy} className="text-[11px] text-brand">
            Test
          </button>
        </div>
      )}

      {!cfg.configured && !urlMode && (
        <div className="space-y-2">
          <p className="text-[11px] text-gray-400 leading-relaxed">
            1. Add the plug in the <b>Smart Life</b> app on your home Wi-Fi.
          </p>
          <button onClick={find} disabled={busy} className={`${btn} bg-brand text-white`}>
            2. Find it on my Wi-Fi
          </button>
          <p className="text-[11px] text-gray-400 leading-relaxed">
            3. Paste the Access ID and Secret from your free Tuya developer project (iot.tuya.com → Cloud →
            your project, with the Smart Life app linked under Devices). They are used once to fetch the
            plug’s key and are not stored.
          </p>
          <select value={region} onChange={(e) => setRegion(e.target.value)} className={input}>
            <option value="us">Americas (us)</option>
            <option value="eu">Europe (eu)</option>
            <option value="in">India (in)</option>
            <option value="cn">China (cn)</option>
          </select>
          <input
            className={input}
            placeholder="Access ID / Client ID"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            autoCapitalize="off"
            autoCorrect="off"
          />
          <input
            className={input}
            placeholder="Access Secret / Client Secret"
            type="password"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
          />
          <button
            onClick={() => connect()}
            disabled={busy || !apiKey || !apiSecret}
            className={`${btn} bg-brand text-white`}
          >
            Connect
          </button>
          {choose && choose.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {choose.map((c) => (
                <button
                  key={c.device_id}
                  onClick={() => connect(c.device_id)}
                  className="text-[11px] px-2 py-1 rounded-lg bg-surface-raised text-gray-300 border border-surface-border"
                >
                  {c.name || c.device_id}
                  {c.on_lan ? ' · on Wi-Fi' : ''}
                </button>
              ))}
            </div>
          )}
          <button onClick={() => setUrlMode(true)} className="text-[11px] text-gray-500 underline">
            My plug isn’t a Smart Life plug — use on/off links instead
          </button>
        </div>
      )}

      {!cfg.configured && urlMode && (
        <div className="space-y-2">
          <p className="text-[11px] text-gray-400 leading-relaxed">
            Paste a link that turns the plug on and one that turns it off, from IFTTT webhooks, Home
            Assistant, or a Shelly/Tasmota plug.
          </p>
          <input className={input} placeholder="On link" value={onUrl} onChange={(e) => setOnUrl(e.target.value)} />
          <input className={input} placeholder="Off link" value={offUrl} onChange={(e) => setOffUrl(e.target.value)} />
          <button onClick={saveUrls} disabled={busy || !onUrl || !offUrl} className={`${btn} bg-brand text-white`}>
            Save
          </button>
          <button onClick={() => setUrlMode(false)} className="text-[11px] text-gray-500 underline">
            Back
          </button>
        </div>
      )}

      {cfg.configured && !cfg.enabled && (
        <p className="text-[11px] text-gray-500">Set up, but switched off.</p>
      )}
      {scan && scan.devices.length > 0 && !cfg.configured && (
        <p className="text-[10px] text-gray-500">
          On your Wi-Fi: {scan.devices.map((d) => `${d.ip}${d.version ? ` (v${d.version})` : ''}`).join(', ')}
        </p>
      )}
      {msg && <p className="text-[11px] text-gray-400">{msg}</p>}
    </div>
  );
}
