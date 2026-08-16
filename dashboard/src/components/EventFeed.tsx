import { Panel } from './Panel';
import type { EventLogEntry } from '../hooks';

interface Props {
  events: EventLogEntry[];
  connected: boolean;
  /* True after the reconnect backoff gave up (wrong token, dead host). */
  failed: boolean;
}

/* Human labels for the SSE event types the backend pushes. */
const LABELS: Record<string, string> = {
  reminder: 'Reminder fired',
  timer: 'Timer done',
  wake: 'Wake word',
  proactive: 'Spoke first',
};

export function EventFeed({ events, connected, failed }: Props) {
  return (
    <Panel
      title="Live activity"
      aside={
        <span className={`pill ${connected ? 'pill--ok' : failed ? 'pill--bad' : 'pill--warn'}`}>
          {connected ? 'live' : failed ? 'offline' : 'reconnecting'}
        </span>
      }
      empty={events.length === 0}
      emptyText="Nothing yet — reminders, wake triggers and unprompted messages appear here as they happen."
    >
      {/* Only the newest event (index 0 — events are prepended) is a live
          announcement. The list can hold 50 items, and a live region reads
          everything inside it on mount — the whole backlog would be read out
          on every connect. */}
      <ul className="rows">
        {events.map((e, i) => (
          <li
            key={e.id}
            className="row"
            {...(i === 0 ? { 'aria-live': 'polite' } : {})}
          >
            <div className="row__main">
              <div className="row__text">
                <span className={`tag tag--${e.type}`}>{LABELS[e.type] ?? e.type}</span>
                {e.text ? <span className="event__text">{e.text}</span> : null}
              </div>
              <div className="row__sub">
                {e.at.toLocaleTimeString(undefined, {
                  hour: 'numeric',
                  minute: '2-digit',
                  second: '2-digit',
                })}
                {e.emotion ? ` · ${e.emotion}` : ''}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
