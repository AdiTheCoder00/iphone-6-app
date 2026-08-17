/* Typed client for the companion backend.
 *
 * Every call carries the shared token in X-Companion-Token, matching
 * companion.html and the ESP32 firmware. /events is the exception: an
 * EventSource cannot set request headers, so the stream opens with a
 * short-lived one-time ticket minted by /events-ticket — never the token
 * itself, which must not ride a URL. */

import type {
  ChatMessage,
  Fact,
  Health,
  MediaAction,
  PCStatus,
  Reminder,
  SmartDevices,
} from './types';

const SETTINGS_KEY = 'companion.dashboard.v1';

export interface Settings {
  backendUrl: string;
  token: string;
}

/* The dashboard is normally opened on the phone from the same machine that
 * runs the backend — https://<LAN-IP>:8443/dashboard/ — so derive the
 * backend URL from the page origin: same scheme and host, port 8000. That
 * gets the scheme right automatically (the backend is HTTPS when certs
 * exist) and never points at the phone's own localhost. */
function defaultBackendUrl(): string {
  if (typeof window !== 'undefined' && window.location && window.location.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return 'http://localhost:8000';
}

const DEFAULTS: Settings = {
  backendUrl: defaultBackendUrl(),
  token: '',
};

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return {
      backendUrl:
        typeof parsed.backendUrl === 'string' && parsed.backendUrl
          ? parsed.backendUrl
          : DEFAULTS.backendUrl,
      token: typeof parsed.token === 'string' ? parsed.token : DEFAULTS.token,
    };
  } catch {
    /* Private mode or a corrupt value — fall back rather than fail to boot. */
    return { ...DEFAULTS };
  }
}

export function saveSettings(settings: Settings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    /* Nothing useful to do; the app still works for this session. */
  }
}

/* Thrown for a 401 specifically, so the UI can say "your token is wrong"
 * instead of the much less actionable "something went wrong". */
export class UnauthorizedError extends Error {
  constructor() {
    super('Unauthorized');
    this.name = 'UnauthorizedError';
  }
}

function base(settings: Settings): string {
  return settings.backendUrl.replace(/\/+$/, '');
}

/* fetch() has no timeout of its own; a half-open connection would otherwise
 * hold a poll in flight forever and the body read would hang even after the
 * headers arrived. The internal controller aborts the request on timeout,
 * and the same timer also bounds the json() read (iOS 12.0/12.1 have no
 * AbortController — there the timeout rejects and the request is left to
 * die on its own, exactly like companion.html). */
const REQUEST_TIMEOUT_MS = 15_000;

async function request<T>(
  settings: Settings,
  path: string,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (settings.token) headers['X-Companion-Token'] = settings.token;
  if (init.body) headers['Content-Type'] = 'application/json';

  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      if (controller) controller.abort();
      reject(new Error('Request timed out'));
    }, REQUEST_TIMEOUT_MS);
  });
  try {
    const response = await Promise.race([
      fetch(base(settings) + path, { ...init, headers, signal: signal ?? controller?.signal }),
      timeout,
    ]);
    if (response.status === 401) throw new UnauthorizedError();
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (response.status === 204) return undefined as T;
    /* The body read is not covered by the caller's abort (which only cancels
     * the response phase), so race it against the same timeout. */
    return (await Promise.race([response.json(), timeout])) as T;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: (s: Settings, signal?: AbortSignal) =>
    request<Health>(s, '/health', {}, signal),
  pcStatus: (s: Settings, signal?: AbortSignal) =>
    request<PCStatus>(s, '/status/pc', {}, signal),
  reminders: (s: Settings, signal?: AbortSignal) =>
    request<{ reminders: Reminder[] }>(s, '/reminders', {}, signal).then((r) => r.reminders),
  cancelReminder: (s: Settings, id: number) =>
    request<unknown>(s, `/reminders/${id}`, { method: 'DELETE' }),
  facts: (s: Settings, signal?: AbortSignal) =>
    request<{ facts: Fact[] }>(s, '/facts', {}, signal).then((r) => r.facts),
  deleteFact: (s: Settings, id: number) =>
    request<unknown>(s, `/facts/${id}`, { method: 'DELETE' }),
  conversation: (s: Settings, signal?: AbortSignal) =>
    request<{ messages: ChatMessage[] }>(s, '/conversation', {}, signal).then((r) => r.messages),
  clearConversation: (s: Settings) =>
    request<unknown>(s, '/conversation', { method: 'DELETE' }),
  /* Transport only — the backend's Literal schema rejects anything else. */
  media: (s: Settings, action: MediaAction) =>
    request<{ action: MediaAction }>(s, '/pc/media', {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),
  smartDevices: (s: Settings, signal?: AbortSignal) =>
    request<SmartDevices>(s, '/smart/devices', {}, signal),
  setSmartDevice: (s: Settings, entityId: string, turnOn: boolean) =>
    request<unknown>(s, `/smart/devices/${encodeURIComponent(entityId)}/state`, {
      method: 'POST',
      body: JSON.stringify({ turn_on: turnOn }),
    }),
  /* One-time ticket for opening an /events stream (see eventsUrl). */
  eventsTicket: (s: Settings) =>
    request<{ ticket: string; expires_in: number }>(s, '/events-ticket'),
};

/* EventSource has no API for request headers, hence the query parameter. The
 * ticket is single-use and expires in seconds, so it is safe in a URL where
 * the shared token is not. */
export function eventsUrl(settings: Settings, ticket: string): string {
  return `${base(settings)}/events?ticket=${encodeURIComponent(ticket)}`;
}
