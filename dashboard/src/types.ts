/* Mirrors backend/app/models/schemas.py. Kept hand-written rather than
 * generated: the API is small and stable, and a generator would be more
 * machinery than the whole dashboard. If a field here disagrees with the
 * backend, the backend is right. */

export type ModelStatus = 'warming' | 'ready' | 'unavailable';

export interface Health {
  status: string;
  ollama_connected: boolean;
  model: string;
  model_status: ModelStatus;
  tts_enabled: boolean;
  ha_connected: boolean;
}

export interface NowPlaying {
  title: string;
  artist: string;
  app: string;
  status: string;
}

export interface PCStatus {
  available: boolean;
  now_playing: NowPlaying | null;
  volume_percent: number | null;
  muted: boolean | null;
  cpu_percent: number | null;
  ram_percent: number | null;
  battery_percent: number | null;
  battery_plugged: boolean | null;
}

export interface Reminder {
  id: number;
  text: string;
  /* Epoch seconds — formatted in the browser's own timezone. */
  fire_time: number;
}

export interface Fact {
  id: number;
  text: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface SmartDevice {
  entity_id: string;
  name: string;
  domain: string;
  state: string;
}

export interface SmartDevices {
  available: boolean;
  devices: SmartDevice[];
}

/* Events arriving over SSE. `connected` is the stream handshake; the rest are
 * things the companion did or was told without being asked. */
export interface CompanionEvent {
  type: 'connected' | 'reminder' | 'wake' | 'proactive' | string;
  text?: string;
  emotion?: string;
}
