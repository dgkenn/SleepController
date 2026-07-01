'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

// Goal #2: a silent controller/bed outage should become a 2-minute fix, not a 6-hour
// one — this card is the phone-side half of that: subscribing to Web Push so newly
// appearing CRITICAL health alerts (see app/health_monitor.py + app/push_sender.py on
// the backend) buzz the phone even when the dashboard tab isn't open.

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

type Status = 'unsupported' | 'not-configured' | 'denied' | 'subscribed' | 'unsubscribed' | 'checking';

export default function PushEnableCard() {
  const [status, setStatus] = useState<Status>('checking');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        if (!cancelled) setStatus('unsupported');
        return;
      }
      try {
        const key = await api.vapidPublicKey();
        if (!key.configured || !key.public_key) {
          if (!cancelled) setStatus('not-configured');
          return;
        }
        const reg = await navigator.serviceWorker.ready;
        const existing = await reg.pushManager.getSubscription();
        if (!cancelled) {
          if (Notification.permission === 'denied') setStatus('denied');
          else setStatus(existing ? 'subscribed' : 'unsubscribed');
        }
      } catch {
        if (!cancelled) setStatus('unsubscribed');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const enable = async () => {
    setBusy(true);
    setError('');
    try {
      const key = await api.vapidPublicKey();
      if (!key.configured || !key.public_key) {
        setStatus('not-configured');
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setStatus('denied');
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key.public_key) as BufferSource,
      });
      const json = sub.toJSON();
      await api.pushSubscribe({
        endpoint: json.endpoint!,
        keys: { p256dh: json.keys!.p256dh, auth: json.keys!.auth },
      });
      setStatus('subscribed');
    } catch (e) {
      setError('Could not enable push alerts.');
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    setError('');
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api.pushUnsubscribe(sub.endpoint);
        await sub.unsubscribe();
      }
      setStatus('unsubscribed');
    } catch {
      setError('Could not disable push alerts.');
    } finally {
      setBusy(false);
    }
  };

  const subtitle = {
    checking: 'Checking…',
    unsupported: 'Not supported in this browser',
    'not-configured': 'Server has no VAPID keys configured yet',
    denied: 'Notifications blocked — enable in browser settings',
    subscribed: 'This device will get critical outage alerts',
    unsubscribed: 'Get a push the moment the controller goes down',
  }[status];

  return (
    <div className="bg-surface-card rounded-2xl p-4 border border-surface-border">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-white">Push Notifications</p>
          <p className="text-xs text-gray-500 mt-0.5">{subtitle}</p>
        </div>
        {status === 'subscribed' ? (
          <button
            onClick={disable}
            disabled={busy}
            className="w-12 h-7 rounded-full transition-colors relative bg-brand disabled:opacity-50"
            aria-label="Disable push notifications"
            role="switch"
            aria-checked="true"
          >
            <span className="absolute top-0.5 left-0.5 w-6 h-6 bg-white rounded-full shadow transition-transform translate-x-5" />
          </button>
        ) : (
          <button
            onClick={enable}
            disabled={busy || status === 'unsupported' || status === 'not-configured' || status === 'checking'}
            className="w-12 h-7 rounded-full transition-colors relative bg-gray-700 disabled:opacity-40"
            aria-label="Enable push notifications"
            role="switch"
            aria-checked="false"
          >
            <span className="absolute top-0.5 left-0.5 w-6 h-6 bg-white rounded-full shadow transition-transform translate-x-0" />
          </button>
        )}
      </div>
      {error && <p className="text-xs text-danger mt-2">{error}</p>}
      {status === 'not-configured' && (
        <p className="text-xs text-warning mt-2">
          Alerts still show in-app under Alerts — push needs VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY
          set on the server (see deploy/.env.example).
        </p>
      )}
    </div>
  );
}
