import { Panel } from './Panel';
import type { PCStatus } from '../types';

interface Props {
  status: PCStatus | null;
  error: string | null;
  loading: boolean;
}

function Meter({ label, percent }: { label: string; percent: number | null }) {
  if (percent === null) return null;
  /* Bands rather than a gradient: at a glance you want "is this a problem",
   * not a precise reading. */
  const level = percent >= 90 ? 'bad' : percent >= 70 ? 'warn' : 'ok';
  return (
    <div className="meter">
      <div className="meter__head">
        <span>{label}</span>
        <span className="meter__value">{Math.round(percent)}%</span>
      </div>
      <div className="meter__track">
        <div className={`meter__fill meter__fill--${level}`} style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
    </div>
  );
}

export function PCPanel({ status, error, loading }: Props) {
  if (!loading && !error && status && !status.available) {
    return (
      <Panel title="This PC" empty emptyText="PC control is disabled or unavailable on this host." />
    );
  }

  const track = status?.now_playing ?? null;

  return (
    <Panel title="This PC" loading={loading} error={error}>
      <div className="nowplaying">
        {track ? (
          <>
            <div className="nowplaying__title">{track.title || 'Unknown track'}</div>
            <div className="nowplaying__meta">
              {[track.artist, track.app].filter(Boolean).join(' · ')}
              {track.status ? ` · ${track.status}` : ''}
            </div>
          </>
        ) : (
          <div className="muted">Nothing playing</div>
        )}
      </div>

      {status?.volume_percent !== null && status?.volume_percent !== undefined ? (
        <div className="kv">
          <span>Volume</span>
          <strong>
            {status.volume_percent}%{status.muted ? ' (muted)' : ''}
          </strong>
        </div>
      ) : null}

      {status?.battery_percent !== null && status?.battery_percent !== undefined ? (
        <div className="kv">
          <span>Battery</span>
          <strong>
            {status.battery_percent}%{status.battery_plugged ? ' · charging' : ''}
          </strong>
        </div>
      ) : null}

      <Meter label="CPU" percent={status?.cpu_percent ?? null} />
      <Meter label="Memory" percent={status?.ram_percent ?? null} />
    </Panel>
  );
}
