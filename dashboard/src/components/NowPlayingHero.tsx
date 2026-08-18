import { useState } from 'react';
import { Panel } from './Panel';
import { SetupPrompt } from './SetupPrompt';
import { api, type Settings } from '../api';
import type { MediaAction, PCStatus } from '../types';

interface Props {
  status: PCStatus | null;
  error: string | null;
  loading: boolean;
  settings: Settings;
  onChanged: () => void;
  onPair: () => void;
  /* Backend unreachable but we still hold the last good snapshot (mockup 3f). */
  stale?: boolean;
}

function Meter({
  label,
  percent,
  suffix,
}: {
  label: string;
  percent: number | null;
  suffix?: string;
}) {
  if (percent === null) return null;
  /* Same bands as PCPanel — "is this a problem", not a precise reading. */
  const level = percent >= 90 ? 'bad' : percent >= 70 ? 'warn' : 'ok';
  return (
    <div className="meter">
      <div className="meter__head">
        <span>{label}</span>
        <span className="meter__value">
          {Math.round(percent)}%{suffix}
        </span>
      </div>
      <div className="meter__track">
        <div
          className={`meter__fill meter__fill--${level}`}
          style={{ transform: `scaleX(${Math.max(0, Math.min(100, percent)) / 100})` }}
        />
      </div>
    </div>
  );
}

/* The hero is PCPanel's data at page scale (mockup 1k).
 *
 * Two departures from the mockup, both because the data behind them does not
 * exist: it drew a scrub bar at 38% and a "3:12 / 8:10" readout, but
 * /status/pc carries no track position — a bar that always sat at 38% would be
 * decoration pretending to be a reading. The volume level is real, so it gets
 * the bar instead. Artwork is likewise absent from the endpoint, so there is
 * no placeholder box for it. */
export function NowPlayingHero({
  status,
  error,
  loading,
  settings,
  onChanged,
  onPair,
  stale,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const send = async (action: MediaAction) => {
    setBusy(true);
    setFailed(null);
    try {
      await api.media(settings, action);
    } catch (e) {
      /* Surfaced, not swallowed — a transport button that silently does
       * nothing is indistinguishable from one that worked. */
      setFailed(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(false);
      /* Refetch either way, so the title corrects itself after a failed tap. */
      onChanged();
    }
  };

  if (loading) {
    return (
      <div className="bento__hero">
        <Panel title="This PC" skeleton skeletonLines={3} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="bento__hero">
        <Panel title="This PC" error={error} />
      </div>
    );
  }

  /* Mockup 3e: no PC control on this host — the hero's slot explains why and
   * offers the pairing route rather than rendering a large empty card. */
  if (status && !status.available) {
    return (
      <div className="bento__hero">
        <SetupPrompt
          title="PC control isn't set up on this host"
          detail="Run the backend on the machine you want to see here to get CPU, memory and now-playing."
          actionLabel="Pair a phone"
          onAction={onPair}
        />
      </div>
    );
  }

  const track = status?.now_playing ?? null;
  const canControl = !!status?.available && !stale;

  return (
    <section className={`hero bento__hero${stale ? ' is-stale' : ''}`}>
      <div className="hero__main">
        <h2 className="panel__title">Now playing</h2>
        {track ? (
          <>
            <div className="hero__title">{track.title || 'Unknown track'}</div>
            <div className="hero__meta">
              {[track.artist, track.app].filter(Boolean).join(' · ') || ' '}
            </div>
          </>
        ) : (
          <>
            <div className="hero__title hero__title--idle">Nothing playing</div>
            <div className="hero__meta">Start something and it shows up here.</div>
          </>
        )}

        <div className="hero__transport">
          <button
            type="button"
            className="btn btn--round"
            disabled={busy || !canControl}
            onClick={() => send('previous')}
            aria-label="Previous track"
            title="Previous track"
          >
            ⏮
          </button>
          <button
            type="button"
            className="btn btn--round"
            disabled={busy || !canControl}
            onClick={() => send('play_pause')}
            /* The endpoint is a single play/pause toggle and the reported
             * status can lag a beat behind the key tap, so the label names the
             * toggle rather than claiming which way it will go. */
            aria-label="Play or pause"
            title="Play or pause"
          >
            {track?.status === 'playing' ? '❚❚' : '▶'}
          </button>
          <button
            type="button"
            className="btn btn--round"
            disabled={busy || !canControl}
            onClick={() => send('next')}
            aria-label="Next track"
            title="Next track"
          >
            ⏭
          </button>

          {status?.volume_percent !== null && status?.volume_percent !== undefined ? (
            <>
              <div
                className="hero__scrub"
                role="img"
                aria-label={`Volume ${status.volume_percent}%${status.muted ? ', muted' : ''}`}
              >
                <i
                  style={{ transform: `scaleX(${Math.max(0, Math.min(100, status.volume_percent)) / 100})` }}
                />
              </div>
              <span className="hero__time">
                vol {Math.max(0, Math.min(100, status.volume_percent))}%
                {status.muted ? ' · muted' : ''}
              </span>
            </>
          ) : null}
        </div>

        {failed ? <p className="error hero__error">{failed}</p> : null}
      </div>

      <div className="hero__meters">
        <Meter label="CPU" percent={status?.cpu_percent ?? null} />
        <Meter label="Memory" percent={status?.ram_percent ?? null} />
        <Meter
          label="Battery"
          percent={status?.battery_percent ?? null}
          suffix={status?.battery_plugged ? ' · charging' : ''}
        />
      </div>
    </section>
  );
}
