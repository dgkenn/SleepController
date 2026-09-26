'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, PhoneAlarmConfig, PhoneAlarmSetup } from '@/lib/api';

/** Copy text even on the plain-http LAN address, where navigator.clipboard is unavailable. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the textarea copy */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** The phone alarm: at the wake it rings your phone (ntfy or Pushover) and keeps ringing until
 *  you press "I'm awake", get out of bed, or clear the alarm. */
export default function PhoneAlarmCard() {
  const { data: cfg, mutate } = useSWR<PhoneAlarmConfig>('/api/wake/phone-alarm/config', fetcher, {
    refreshInterval: 60000,
  });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [setup, setSetup] = useState<PhoneAlarmSetup | null>(null);
  const [userKey, setUserKey] = useState('');
  const [appToken, setAppToken] = useState('');

  if (!cfg) return null;
  const isNtfy = cfg.backend === 'ntfy';
  const hasTopic = !!cfg.ntfy.topic;

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

  const choose = (backend: 'ntfy' | 'pushover') =>
    run('', async () => {
      await api.phoneAlarmUpdate({ backend });
      setSetup(null);
      mutate();
    });

  const generate = () => {
    if (hasTopic && !window.confirm('Replace the topic? You will need to subscribe to the new one.')) return;
    return run('Generating a topic…', async () => {
      await api.phoneAlarmUpdate({ backend: 'ntfy', generate_topic: true, enabled: true });
      setSetup(await api.phoneAlarmSetup());
      setMsg('');
      mutate();
    });
  };

  const show = () =>
    run('', async () => {
      setSetup(await api.phoneAlarmSetup());
    });

  const copy = async () => {
    if (!setup?.topic) return;
    setMsg((await copyText(setup.topic)) ? 'Topic copied.' : 'Could not copy — select it and copy by hand.');
  };

  const savePushover = () =>
    run('Saving…', async () => {
      await api.phoneAlarmUpdate({
        backend: 'pushover',
        enabled: true,
        pushover: { user_key: userKey.trim() || undefined, app_token: appToken.trim() || undefined },
      });
      setUserKey('');
      setAppToken('');
      setMsg('Saved. Tap “Test alarm” to check your phone.');
      mutate();
    });

  const test = () =>
    run('Sending a test alarm…', async () => {
      const r = await api.phoneAlarmTest();
      setMsg(r.ok ? 'Test alarm sent — your phone should ring now.' : r.error || 'The test alarm did not go through.');
    });

  const input =
    'w-full bg-surface-raised border border-surface-border rounded-lg px-2.5 py-2 text-xs text-white';
  const btn = 'text-xs px-3 py-2 rounded-lg font-medium disabled:opacity-50';
  const seg = (on: boolean) =>
    `flex-1 text-xs py-1.5 rounded-lg ${on ? 'bg-brand text-white' : 'bg-surface-raised text-gray-400 border border-surface-border'}`;

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500 uppercase tracking-wider">Phone alarm</p>
        {cfg.configured && (
          <button
            onClick={() => api.phoneAlarmUpdate({ enabled: !cfg.enabled }).then(() => mutate())}
            className={`relative w-11 h-6 rounded-full transition-colors ${
              cfg.enabled ? 'bg-success' : 'bg-surface-raised border border-surface-border'
            }`}
            aria-label="Toggle phone alarm"
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
        At your wake time your phone rings, and keeps ringing until you press “I’m awake”, get out of bed, or
        clear the alarm. It only rings on nights (and naps) with a wake time set.
      </p>

      <div className="flex gap-1.5">
        <button onClick={() => choose('ntfy')} disabled={busy} className={seg(isNtfy)}>
          ntfy (free)
        </button>
        <button onClick={() => choose('pushover')} disabled={busy} className={seg(!isNtfy)}>
          Pushover
        </button>
      </div>

      {isNtfy && (
        <div className="space-y-2">
          <p className="text-[11px] text-gray-400 leading-relaxed">
            Install the ntfy app, tap +, subscribe to this topic; on iPhone allow Time Sensitive notifications.
          </p>
          {setup?.topic ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 min-w-0 break-all bg-surface-raised border border-surface-border rounded-lg px-2.5 py-2 text-[11px] text-white select-all">
                {setup.topic}
              </code>
              <button onClick={copy} className={`${btn} bg-surface-raised text-gray-200 border border-surface-border`}>
                Copy
              </button>
            </div>
          ) : (
            hasTopic && (
              <button onClick={show} disabled={busy} className="text-[11px] text-brand">
                Show topic
              </button>
            )
          )}
          {setup?.topic && setup.server !== 'https://ntfy.sh' && (
            <p className="text-[10px] text-gray-500">Server: {setup.server}</p>
          )}
          <button
            onClick={generate}
            disabled={busy}
            className={hasTopic ? 'text-[11px] text-gray-500 underline' : `${btn} bg-brand text-white`}
          >
            {hasTopic ? 'Generate a new topic' : 'Generate'}
          </button>
        </div>
      )}

      {!isNtfy && (
        <div className="space-y-2">
          <p className="text-[11px] text-gray-400 leading-relaxed">
            From pushover.net: your user key, and the API token of an application you create there.
          </p>
          <input
            className={input}
            placeholder={cfg.pushover.user_key ? 'User key (saved)' : 'User key'}
            value={userKey}
            onChange={(e) => setUserKey(e.target.value)}
            autoCapitalize="off"
            autoCorrect="off"
          />
          <input
            className={input}
            placeholder={cfg.pushover.app_token ? 'App token (saved)' : 'App token'}
            type="password"
            value={appToken}
            onChange={(e) => setAppToken(e.target.value)}
          />
          <button
            onClick={savePushover}
            disabled={busy || (!userKey && !appToken)}
            className={`${btn} bg-brand text-white`}
          >
            Save
          </button>
        </div>
      )}

      {cfg.configured && (
        <button
          onClick={test}
          disabled={busy}
          className={`${btn} bg-surface-raised text-gray-200 border border-surface-border`}
        >
          Test alarm
        </button>
      )}
      {cfg.configured && !cfg.enabled && <p className="text-[11px] text-gray-500">Set up, but switched off.</p>}
      {msg && <p className="text-[11px] text-gray-400">{msg}</p>}
    </div>
  );
}
