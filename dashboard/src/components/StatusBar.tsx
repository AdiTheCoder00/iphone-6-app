import type { Health } from '../types';

interface Props {
  health: Health | null;
  error: string | null;
  streamConnected: boolean;
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

export function StatusBar({ health, error, streamConnected }: Props) {
  if (error) {
    return (
      <div className="statusbar statusbar--down">
        <span className="dot dot--bad" aria-hidden />
        <span>{error}</span>
      </div>
    );
  }
  if (!health) {
    return (
      <div className="statusbar">
        <span className="dot dot--warn" aria-hidden />
        <span>Connecting…</span>
      </div>
    );
  }

  /* "warming" is deliberately not an error: the backend is up and answering,
   * the local model just is not resident yet. Showing it as a failure would
   * send you debugging a problem that resolves itself in ~12s. */
  const modelOk = health.model_status === 'ready';
  const modelDetail =
    health.model_status === 'warming'
      ? `${health.model} · warming up`
      : health.model_status === 'unavailable'
        ? `${health.model} · unavailable`
        : health.model;

  return (
    <div className="statusbar">
      <Dot ok={health.status === 'ok'} label="Backend" />
      <Dot ok={health.ollama_connected && modelOk} label="Model" detail={modelDetail} />
      <Dot ok={health.tts_enabled} label="Voice" detail={health.tts_enabled ? 'on' : 'off'} />
      <Dot
        ok={health.ha_connected}
        label="Smart home"
        detail={health.ha_connected ? 'connected' : 'not set up'}
      />
      <Dot ok={streamConnected} label="Live feed" detail={streamConnected ? 'live' : 'reconnecting'} />
    </div>
  );
}
