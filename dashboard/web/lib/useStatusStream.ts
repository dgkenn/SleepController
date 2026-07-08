'use client';

import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { StatusResponse, fetcher } from './api';

export function useStatusStream() {
  const [streamData, setStreamData] = useState<StatusResponse | null>(null);
  const [streamError, setStreamError] = useState<boolean>(false);
  const esRef = useRef<EventSource | null>(null);

  const isLive = !!streamData && !streamError;

  // SWR polling fallback (every 10s) -- only polls when SSE isn't live, so it acts purely
  // as a fallback rather than double-fetching status while the stream is already up.
  const { data: pollData, error: pollError, mutate } = useSWR<StatusResponse>(
    '/api/status',
    fetcher,
    { refreshInterval: isLive ? 0 : 10000, revalidateOnFocus: true }
  );

  useEffect(() => {
    if (typeof window === 'undefined') return;

    let es: EventSource;

    const connect = () => {
      try {
        es = new EventSource('/api/stream/status', { withCredentials: true });
        esRef.current = es;

        es.onmessage = (e) => {
          try {
            const parsed = JSON.parse(e.data) as StatusResponse;
            setStreamData(parsed);
            setStreamError(false);
          } catch {
            // ignore parse errors
          }
        };

        es.onerror = () => {
          setStreamError(true);
          es.close();
          esRef.current = null;
          // Retry after 15s
          setTimeout(connect, 15000);
        };
      } catch {
        setStreamError(true);
      }
    };

    connect();

    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

  // Prefer SSE data, fall back to poll data
  const data = streamData ?? pollData ?? null;

  return { data, isLive, error: !data && (!!pollError || streamError), mutate };
}
