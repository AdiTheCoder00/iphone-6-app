import { useCallback, useEffect, useRef, useState } from 'react';
import { UnauthorizedError, eventsUrl, type Settings } from './api';
import type { CompanionEvent } from './types';

export interface Poll<T> {
  data: T | null;
  error: string | null;
  /* True only for the very first load. A background refresh must not blank
   * out a panel that already has good data on screen. */
  loading: boolean;
  refresh: () => void;
}

/**
 * Poll an endpoint on an interval, with the refresh exposed so a mutation can
 * force an immediate re-read instead of waiting out the timer.
 */
export function usePoll<T>(
  fetcher: (settings: Settings) => Promise<T>,
  settings: Settings,
  intervalMs: number,
): Poll<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  /* Held in a ref so the effect below does not re-subscribe on every render
   * just because an inline arrow function is a new identity each time. */
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async () => {
    try {
      const result = await fetcherRef.current(settings);
      setData(result);
      setError(null);
    } catch (e) {
      setError(
        e instanceof UnauthorizedError
          ? 'Unauthorized — check the access token'
          : e instanceof Error
            ? e.message
            : 'Request failed',
      );
    } finally {
      setLoading(false);
    }
  }, [settings]);

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (!cancelled) void run();
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [run, intervalMs]);

  return { data, error, loading, refresh: run };
}

export interface EventLogEntry extends CompanionEvent {
  id: number;
  at: Date;
}

const SSE_RETRY_MS = 3000;
const SSE_RETRY_MAX_MS = 60000;

/**
 * Live SSE feed. Keeps the most recent `limit` events, and reconnects on drop
 * rather than trusting EventSource's own retry, which gives up once the
 * connection is closed outright.
 *
 * Backoff doubles per consecutive failure up to a minute. A flat retry is
 * right for a backend that restarted, but wrong for a missing or wrong token:
 * that never fixes itself, and a flat 3s loop just generates failed requests
 * forever.
 */
export function useEvents(settings: Settings, limit = 50) {
  const [events, setEvents] = useState<EventLogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const nextId = useRef(0);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    let delay = SSE_RETRY_MS;

    const scheduleRetry = () => {
      if (closed) return;
      const wait = delay;
      delay = Math.min(delay * 2, SSE_RETRY_MAX_MS);
      retry = setTimeout(connect, wait);
    };

    const connect = () => {
      if (closed) return;
      try {
        source = new EventSource(eventsUrl(settings));
      } catch {
        scheduleRetry();
        return;
      }

      source.onopen = () => {
        /* A real connection resets the backoff. */
        delay = SSE_RETRY_MS;
        setConnected(true);
      };

      source.onmessage = (e) => {
        let payload: CompanionEvent;
        try {
          payload = JSON.parse(e.data) as CompanionEvent;
        } catch {
          return;
        }
        if (payload.type === 'connected') {
          setConnected(true);
          return;
        }
        setEvents((prev) =>
          [{ ...payload, id: nextId.current++, at: new Date() }, ...prev].slice(0, limit),
        );
      };

      source.onerror = () => {
        setConnected(false);
        source?.close();
        source = null;
        scheduleRetry();
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      source?.close();
    };
  }, [settings, limit]);

  return { events, connected };
}
