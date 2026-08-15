/* Typed client for the companion backend.
 *
 * Every call carries the shared token in X-Companion-Token, matching
 * companion.html and the ESP32 firmware. /events is the exception: an
 * EventSource cannot set request headers, so the backend also accepts the
 * token as a query parameter on that one read-only endpoint. */

import type {
  ChatMessage,
  Fact,
  Health,
  PCStatus,
  Reminder,
  SmartDevices,
} from './types';

const SETTINGS_KEY = 'companion.dashboard.v1';

export interface Settings {
  backendUrl: string;
  token: string;
}

const DEFAULTS: Settings = {
  backendUrl: 'http://localhost:8000',
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
 * hold a poll in flight until the next tick's abort. Race every request
 * against this, matching the companion.html wrapper. */
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

  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('Request timed out')), REQUEST_TIMEOUT_MS);
  });
  try {
    const response = await Promise.race([
      fetch(base(settings) + path, { ...init, headers, signal }),
      timeout,
    ]);
    if (response.status === 401) throw new UnauthorizedError();
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
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
  snoozeReminder: (s: Settings, id: number, minutes: number) =>
    request<Reminder>(s, `/reminders/${id}/snooze`, {
      method: 'POST',
      body: JSON.stringify({ minutes }),
    }),
  facts: (s: Settings, signal?: AbortSignal) =>
    request<{ facts: Fact[] }>(s, '/facts', {}, signal).then((r) => r.facts),
  deleteFact: (s: Settings, id: number) =>
    request<unknown>(s, `/facts/${id}`, { method: 'DELETE' }),
  conversation: (s: Settings, signal?: AbortSignal) =>
    request<{ messages: ChatMessage[] }>(s, '/conversation', {}, signal).then((r) => r.messages),
  clearConversation: (s: Settings) =>
    request<unknown>(s, '/conversation', { method: 'DELETE' }),
  smartDevices: (s: Settings, signal?: AbortSignal) =>
    request<SmartDevices>(s, '/smart/devices', {}, signal),
  setSmartDevice: (s: Settings, entityId: string, turnOn: boolean) =>
    request<unknown>(s, `/smart/devices/${encodeURIComponent(entityId)}/state`, {
      method: 'POST',
      body: JSON.stringify({ turn_on: turnOn }),
    }),
  chat: (s: Settings, message: string, history: ChatMessage[]) =>
    request<{ reply: string; emotion: string }>(s, '/chat', {
      method: 'POST',
      body: JSON.stringify({ message, history }),
    }),
};

/* EventSource has no API for request headers, hence the query parameter. */
export function eventsUrl(settings: Settings): string {
  const url = base(settings) + '/events';
  return settings.token ? `${url}?token=${encodeURIComponent(settings.token)}` : url;
}
