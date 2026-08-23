'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import BigButton from '@/components/BigButton';
import { api } from '@/lib/api';

// ---------------------------------------------------------------------------
// Forward a Polar Verity Sense straight from THIS browser tab over Web Bluetooth, instead of
// running scripts/verity_forwarder.py on the controller PC. iOS Safari has no Web Bluetooth --
// this exists for a Web-Bluetooth-capable browser (Bluefy is the common one) opened directly on
// the phone wearing the armband. Posts to the SAME /hr/ingest endpoint, on the SAME 2-second
// batch cadence as the Python forwarder (see verity_forwarder.py's --batch-seconds default), so
// the "Cardiac Sensor" status above this card is the truth about whether it's working -- this
// component's only job is to get bytes flowing into it.
// ---------------------------------------------------------------------------

const HR_SERVICE = 'heart_rate';
const HR_CHAR = 'heart_rate_measurement';
const BATCH_MS = 2000;
const RR_MIN_MS = 300;
const RR_MAX_MS = 2000; // plausibility bounds, ~30-200 bpm -- matches scripts/polar_pmd.py's PPI bounds in spirit

type ConnState = 'unsupported' | 'idle' | 'connecting' | 'connected' | 'lost';

// GATT Heart Rate Measurement decode -- identical to scripts/verity_stream_test.py::_parse_hr
// and the port already verified in the Night Pulse artifact, so this is the third independent
// use of the same known-correct byte layout, not a fresh guess.
function parseHr(view: DataView): { hr: number | null; rr: number[] } {
  const flags = view.getUint8(0);
  let idx = 1;
  let hr: number | null = null;
  if (flags & 0x01) {
    hr = view.getUint16(idx, true);
    idx += 2;
  } else {
    hr = view.getUint8(idx);
    idx += 1;
  }
  if (flags & 0x08) idx += 2; // energy expended, unused
  const rr: number[] = [];
  if (flags & 0x10) {
    while (idx + 1 < view.byteLength) {
      rr.push((view.getUint16(idx, true) * 1000.0) / 1024.0);
      idx += 2;
    }
  }
  return { hr, rr };
}

export default function VerityBluetoothCard() {
  const [state, setState] = useState<ConnState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [liveHr, setLiveHr] = useState<number | null>(null);
  const [deviceName, setDeviceName] = useState<string | null>(null);

  const deviceRef = useRef<BluetoothDevice | null>(null);
  const pendingHr = useRef<number | null>(null);
  const pendingRr = useRef<number[]>([]);
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!('bluetooth' in navigator)) setState('unsupported');
    return () => {
      if (flushTimer.current) clearInterval(flushTimer.current);
    };
  }, []);

  const flush = useCallback(() => {
    const hr = pendingHr.current;
    const rr = pendingRr.current;
    pendingRr.current = [];
    if (hr === null && rr.length === 0) return;
    const body: { hr?: number; rr?: number[] } = {};
    if (hr !== null) body.hr = hr;
    if (rr.length) body.rr = rr;
    api.ingestHr(body).catch(() => {
      // A dropped batch here mirrors the Python forwarder's own behaviour on a network blip:
      // log nothing alarming, just keep streaming and let the next flush try again. The
      // Cardiac Sensor status above will say so on its own if drops become persistent.
    });
  }, []);

  const onNotify = useCallback(
    (event: Event) => {
      const target = event.target as unknown as { value: DataView };
      const { hr, rr } = parseHr(target.value);
      if (hr !== null && hr > 0) {
        setLiveHr(hr);
        pendingHr.current = hr;
      }
      for (const ms of rr) {
        if (ms >= RR_MIN_MS && ms <= RR_MAX_MS) pendingRr.current.push(ms);
      }
    },
    []
  );

  const onDisconnected = useCallback(() => {
    setState('lost');
    setLiveHr(null);
    if (flushTimer.current) {
      clearInterval(flushTimer.current);
      flushTimer.current = null;
    }
  }, []);

  const connectGatt = useCallback(
    async (device: BluetoothDevice) => {
      const server = await device.gatt!.connect();
      const service = await server.getPrimaryService(HR_SERVICE);
      const char = await service.getCharacteristic(HR_CHAR);
      char.addEventListener('characteristicvaluechanged', onNotify);
      await char.startNotifications();
      setState('connected');
      setDeviceName(device.name ?? 'Verity');
      flushTimer.current = setInterval(flush, BATCH_MS);
    },
    [onNotify, flush]
  );

  const connect = useCallback(async () => {
    setError(null);
    setState('connecting');
    try {
      const device = await navigator.bluetooth.requestDevice({
        filters: [{ namePrefix: 'Polar' }],
        optionalServices: [HR_SERVICE],
      });
      deviceRef.current = device;
      device.addEventListener('gattserverdisconnected', onDisconnected);
      await connectGatt(device);
    } catch (err) {
      setState('idle');
      const name = (err as { name?: string })?.name;
      const message = (err as { message?: string })?.message ?? String(err);
      if (name === 'NotFoundError') {
        setError('No device selected. Make sure the armband is on (blue LED) and nearby.');
      } else {
        setError(
          `Couldn't connect: ${message}. Check that the Polar app isn't holding the link, ` +
            "and that you selected the Verity armband."
        );
      }
    }
  }, [connectGatt, onDisconnected]);

  const reconnect = useCallback(async () => {
    if (!deviceRef.current) return connect();
    setError(null);
    setState('connecting');
    try {
      await connectGatt(deviceRef.current);
    } catch (err) {
      setState('lost');
      const message = (err as { message?: string })?.message ?? String(err);
      setError(`Reconnect failed: ${message}. Tap Connect to search again.`);
    }
  }, [connect, connectGatt]);

  if (state === 'unsupported') {
    return (
      <p className="text-xs text-gray-500 mt-2">
        This browser doesn&apos;t support direct Bluetooth. On iPhone, open this page in the{' '}
        <span className="text-brand">Bluefy</span> app instead of Safari to connect the Verity
        straight from here.
      </p>
    );
  }

  return (
    <div className="mt-3 pt-3 border-t border-surface-border space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-300">
          {state === 'connected'
            ? `Connected · ${deviceName}`
            : state === 'connecting'
              ? 'Connecting…'
              : state === 'lost'
                ? 'Link dropped'
                : 'Connect straight from this phone'}
        </span>
        {state === 'connected' && liveHr != null && (
          <span className="text-sm font-medium text-white">{Math.round(liveHr)} bpm</span>
        )}
      </div>

      {state === 'idle' && (
        <BigButton variant="secondary" onClick={connect} fullWidth>
          Connect via Bluetooth
        </BigButton>
      )}
      {state === 'connecting' && (
        <BigButton variant="secondary" loading disabled fullWidth>
          Connecting…
        </BigButton>
      )}
      {state === 'lost' && (
        <BigButton variant="secondary" onClick={reconnect} fullWidth>
          Reconnect
        </BigButton>
      )}

      {error && <p className="text-xs text-danger">{error}</p>}

      <p className="text-xs text-gray-600">
        Single-press the armband to blue LED mode, wear it on the upper forearm, and close the
        Polar phone app first -- it holds an exclusive link. iOS suspends a locked or backgrounded
        tab, so keep the screen on; this is a direct spot-connection, not a background service.
      </p>
    </div>
  );
}
