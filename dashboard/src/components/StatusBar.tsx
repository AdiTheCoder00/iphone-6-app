import type { Health } from '../types';

interface Props {
  health: Health | null;
  error: string | null;
  streamConnected: boolean;
  /* The reconnect backoff gave up: wrong token or dead host. */
  streamFailed: boolean;
  /* Consecutive failed reconnects, for the "(attempt N)" readout. */
  streamAttempts: number;
  backendUrl: string;
  /* True once we have held data and then lost the backend — mockup 3f's
   * "dropped mid-session", as distinct from never having connected. */
  reconnecting: boolean;
  /* When the now-dimmed data was last read. */
  lastSeen: Date | null;
  onSettings: () => void;
}

function Dot({ ok, label, detail }: { ok: boolean; label: string; detail?: string }) {
  return (
    <div className="stat">
      <span className={`dot ${ok ? 'dot--ok' : 'dot--bad'}`} aria-hidden />
      <span className="stat__label">{label}</span>
      {detail ? <span className="stat__detail">{detail}</span> : null}
    </div>
  );
}

export function StatusBar({
  health,
  error,
  streamConnected,
  streamFailed,
  streamAttempts,
  backendUrl,
  reconnecting,
  lastSeen,
  onSettings,
}: Props) {
  /* role=status: the connection state is the one thing that can change while
   * the user is not looking, so announce it to screen readers. */

  /* Mockup 3f — dropped mid-session. Amber, not red, and no Settings button:
   * the config was demonstrably right a moment ago, so pointing the user at
   * the settings would send them to change something that is not broken. The
   * panels below keep their last-known data, dimmed. */
  if (error && reconnecting) {
    return (
      <div className="statusbar statusbar--warn" role="status" aria-live="polite">
        <span className="dot dot--warn dot--pulse" aria-hidden />
        <span>
          Connection dropped — reconnecting…
          {streamAttempts > 0 ? ` (attempt ${streamAttempts})` : ''}
        </span>
        {lastSeen ? (
          <span className="statusbar__detail">
            showing data from{' '}
            {lastSeen.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
          </span>
        ) : null}
      </div>
    );
  }

  /* Mockup 1l — never reached it at all. Red, names the address it tried, and
   * offers the one thing that can fix it. */
  if (error) {
    return (
      <div className="statusbar statusbar--down">
        <span className="dot dot--bad" aria-hidden />
        {/* role=status on the text, not the container: a live region must not
            contain interactive controls (the Settings button). */}
        <span role="status">
          Can’t reach {backendUrl} — {error}
        </span>
        <button type="button" className="btn statusbar__action" onClick={onSettings}>
          Settings
        </button>
      </div>
    );
  }
  if (!health) {
    return (
      <div className="statusbar" role="status" aria-live="polite">
        <span className="dot dot--warn" aria-hidden />
        <span>Connecting…</span>
      </div>
    );
  }

  /* 'unavailable' means the backend is up but the LLM could not be reached
   * (missing or invalid key, no internet, rate limit) — worth surfacing as a
   * failure rather than letting the model dot look healthy. */
  const modelOk = health.model_status === 'ready';
  const modelDetail =
    health.model_status === 'unavailable'
      ? `${health.model} · unavailable`
      : health.model;

  return (
    <div className="statusbar" role="status" aria-live="polite">
      <Dot ok={health.status === 'ok'} label="Backend" />
      <Dot ok={health.llm_connected && modelOk} label="Model" detail={modelDetail} />
      <Dot ok={health.tts_enabled} label="Voice" detail={health.tts_enabled ? 'on' : 'off'} />
      <Dot
        ok={health.ha_connected}
        label="Smart home"
        detail={health.ha_connected ? 'connected' : 'not set up'}
      />
      {/* A wrong token never reaches /health (it is auth-exempt), so the
       * backend dot can look healthy while every other panel 401s — the
       * stream failing for good is the tell, and the detail says so. */}
      <Dot
        ok={streamConnected}
        label="Live feed"
        detail={
          streamConnected ? 'live' : streamFailed ? 'offline — check the access token' : 'reconnecting'
        }
      />
    </div>
  );
}
