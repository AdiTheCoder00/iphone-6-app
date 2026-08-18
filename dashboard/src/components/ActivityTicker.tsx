import type { EventLogEntry } from '../hooks';

/* Same labels as EventFeed — keep the two in step. */
const LABELS: Record<string, string> = {
  reminder: 'Reminder fired',
  timer: 'Timer done',
  wake: 'Wake word',
  proactive: 'Spoke first',
};

interface Props {
  events: EventLogEntry[];
  connected: boolean;
  failed: boolean;
  /* False while the full activity feed is open: the feed announces the same
   * events, and reading the newest event from two live regions at once makes
   * a screen reader say it twice back-to-back. The ticker itself stays
   * visible — only the announcement defers to the feed. */
  announce?: boolean;
}

/* Mockup 1k: the last three events on one line. The full EventFeed panel still
 * exists below — this is what the dashboard shows when it is being glanced at
 * rather than read. */
export function ActivityTicker({ events, connected, failed, announce = true }: Props) {
  return (
    <section className="ticker">
      <span className="ticker__label">Live activity</span>
      <span className={`pill ${connected ? 'pill--ok' : failed ? 'pill--bad' : 'pill--warn'}`}>
        {connected ? 'live' : failed ? 'offline' : 'reconnecting'}
      </span>
      {/* aria-live on the list, not the section: announcing the "live/offline"
          pill on every reconnect would talk over the events themselves. The
          live region sits on the newest event only (index 0 — events are
          prepended), so connect-time backlog is not read out in full. */}
      <div className="ticker__events">
        {events.length === 0 ? (
          <span className="muted">Nothing yet.</span>
        ) : (
          events.slice(0, 3).map((e, i) => (
            <span
              key={e.id}
              className="ticker__event"
              {...(i === 0 && announce ? { 'aria-live': 'polite' } : {})}
            >
              <span className={`tag tag--${e.type}`}>{LABELS[e.type] ?? e.type}</span>
              {e.text ? <span className="ticker__text">{e.text}</span> : null}
              <time dateTime={e.at.toISOString()}>
                {e.at.toLocaleTimeString(undefined, {
                  hour: 'numeric',
                  minute: '2-digit',
                  second: '2-digit',
                })}
              </time>
            </span>
          ))
        )}
      </div>
    </section>
  );
}
