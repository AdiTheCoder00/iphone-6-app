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
 *
 * Each run aborts the previous in-flight request: without that, a fetch that
 * outlives its interval (a slow backend, a warm model) would stack duplicate
 * requests, and an older response could land after a newer one and overwrite
 * fresh data with stale. Aborting also covers the settings-change case — the
 * old request is killed the moment the new settings take effect.
 */
export function usePoll<T>(
  fetcher: (settings: Settings, signal?: AbortSignal) => Promise<T>,
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
  const abortRef = useRef<AbortController | null>(null);
  /* Generation counter for superseded runs: iOS 12.0/12.1 has no
   * AbortController, so an old request cannot be killed — it must instead be
   * ignored when it finally lands. Only the latest run may touch state. */
  const runGen = useRef(0);

  const run = useCallback(async () => {
    const gen = ++runGen.current;
    abortRef.current?.abort();
    const controller =
      typeof AbortController !== 'undefined' ? new AbortController() : null;
    abortRef.current = controller;
    try {
      const result = await fetcherRef.current(
        settings,
        controller ? controller.signal : undefined,
      );
      if (gen !== runGen.current) return;
      setData(result);
      setError(null);
    } catch (e) {
      if (gen !== runGen.current) return;
      /* An aborted request is the previous one being superseded — not an
       * error worth showing. */
      if (e instanceof DOMException && e.name === 'AbortError') return;
      setError(
        e instanceof UnauthorizedError
          ? 'Unauthorized — check the access token'
          : e instanceof Error
            ? e.message
            : 'Request failed',
      );
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      /* Only the latest run clears loading — an aborted first load must not
       * blank a panel while its replacement is still in flight. */
      if (gen === runGen.current) setLoading(false);
    }
  }, [settings]);

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      /* A backgrounded tab must not keep probing the backend on its
       * interval — on a phone-class client that is pure battery burn. The
       * visibilitychange handler re-runs immediately on return. */
      if (!cancelled && !document.hidden) void run();
    };
    const onVisible = () => {
      if (!document.hidden) void run();
    };
    tick();
    const id = setInterval(tick, intervalMs);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
      abortRef.current?.abort();
      abortRef.current = null;
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
/* Consecutive failed connects before giving up. A wrong token or a dead host
 * never fixes itself, and a flat retry loop would just generate failed
 * requests forever — after this many, the feed reports failure instead. */
const SSE_MAX_FAILURES = 10;

/**
 * Live SSE feed. Keeps the most recent `limit` events, and reconnects on drop
 * rather than trusting EventSource's own retry, which gives up once the
 * connection is closed outright.
 *
 * Backoff doubles per consecutive failure up to a minute. A flat retry is
 * right for a backend that restarted, but wrong for a missing or wrong token:
 * that never fixes itself, so after SSE_MAX_FAILURES the feed stops retrying
 * and reports `failed` to the UI.
 */
export function useEvents(settings: Settings, limit = 50) {
  const [events, setEvents] = useState<EventLogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const [failed, setFailed] = useState(false);
  const nextId = useRef(0);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    let delay = SSE_RETRY_MS;
    let failures = 0;
    let retryPending = false;

    /* Fresh settings mean a different backend: events from the old one are
     * from a different server and must not linger on screen. */
    setEvents([]);
    setConnected(false);
    setFailed(false);

    const scheduleRetry = () => {
      /* retryPending stops a second onerror from scheduling a second
       * EventSource before the first retry fires. */
      if (closed || retryPending) return;
      failures += 1;
      if (failures >= SSE_MAX_FAILURES) {
        setConnected(false);
        setFailed(true);
        /* A wrong token or dead host never fixes itself, but the backend can
         * come back later — keep one slow probe going so the live feed
         * recovers without a reload. onopen resets the failure counter. */
        retryPending = true;
        retry = setTimeout(() => {
          retryPending = false;
          connect();
        }, SSE_RETRY_MAX_MS);
        return;
      }
      retryPending = true;
      const wait = delay;
      delay = Math.min(delay * 2, SSE_RETRY_MAX_MS);
      retry = setTimeout(() => {
        retryPending = false;
        connect();
      }, wait);
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
        /* A real connection resets the backoff, the failure counter, and any
         * retry that was still pending — otherwise a late retry timer would
         * kill the healthy stream and reconnect for nothing. */
        delay = SSE_RETRY_MS;
        failures = 0;
        retryPending = false;
        if (retry) {
          clearTimeout(retry);
          retry = null;
        }
        setFailed(false);
        setConnected(true);
      };

      source.onmessage = (e) => {
        let payload: unknown;
        try {
          payload = JSON.parse(e.data);
        } catch {
          return;
        }
        /* The stream is server-controlled: guard its shape so a malformed
         * payload cannot crash the handler with a TypeError. */
        if (
          typeof payload !== 'object' ||
          payload === null ||
          typeof (payload as CompanionEvent).type !== 'string'
        ) {
          return;
        }
        const event = payload as CompanionEvent;
        /* Keepalive pings are not feed entries. */
        if (event.type === 'ping' || event.type === 'connected') {
          if (event.type === 'connected') setConnected(true);
          return;
        }
        setEvents((prev) =>
          [{ ...event, id: nextId.current++, at: new Date() }, ...prev].slice(0, limit),
        );
      };

      source.onerror = () => {
        setConnected(false);
        source?.close();
        source = null;
        scheduleRetry();
      };
    };

    /* The phone locks and iOS suspends the page mid-stream; on return the
     * connection is often a corpse the browser never reported. Jump straight
     * to a fresh connect instead of waiting out a retry timer or the slow
     * probe — mirror of the visibility logic in usePoll. */
    const onVisible = () => {
      if (document.hidden || closed) return;
      if (!source || source.readyState === EventSource.CLOSED) {
        if (retry) {
          clearTimeout(retry);
          retry = null;
        }
        retryPending = false;
        delay = SSE_RETRY_MS;
        failures = 0;
        connect();
      }
    };

    connect();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      source?.close();
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [settings, limit]);

  return { events, connected, failed };
}
