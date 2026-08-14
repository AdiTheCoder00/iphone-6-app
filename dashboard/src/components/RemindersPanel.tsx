import { useState } from 'react';
import { Panel } from './Panel';
import { api, type Settings } from '../api';
import type { Reminder } from '../types';

interface Props {
  reminders: Reminder[] | null;
  error: string | null;
  loading: boolean;
  settings: Settings;
  onChanged: () => void;
}

function formatWhen(epochSeconds: number): string {
  const when = new Date(epochSeconds * 1000);
  const time = when.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (when.toDateString() === new Date().toDateString()) return time;
  return `${when.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`;
}

function relative(epochSeconds: number): string {
  const deltaMin = Math.round((epochSeconds * 1000 - Date.now()) / 60000);
  if (deltaMin < 0) return 'overdue';
  if (deltaMin < 1) return 'now';
  if (deltaMin < 60) return `in ${deltaMin}m`;
  const hours = Math.floor(deltaMin / 60);
  if (hours < 24) return `in ${hours}h${deltaMin % 60 ? ` ${deltaMin % 60}m` : ''}`;
  return `in ${Math.floor(hours / 24)}d`;
}

export function RemindersPanel({ reminders, error, loading, settings, onChanged }: Props) {
  const [busy, setBusy] = useState<number | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusy(id);
    setFailed(null);
    try {
      await fn();
    } catch (e) {
      /* Surfaced rather than swallowed: an action that silently does nothing
       * is worse than one that says why it couldn't. */
      setFailed(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(null);
      /* Refetch either way, so a failed action self-corrects on the next read
       * rather than leaving the row in a lying state. */
      onChanged();
    }
  };

  return (
    <Panel
      title="Reminders"
      aside={reminders ? <span className="count">{reminders.length}</span> : null}
      loading={loading}
      error={error}
      empty={!!reminders && reminders.length === 0}
      emptyText="Nothing scheduled."
    >
      {failed ? <p className="error">{failed}</p> : null}
      <ul className="rows">
        {reminders?.map((r) => (
          <li key={r.id} className="row">
            <div className="row__main">
              <div className="row__text">{r.text}</div>
              <div className="row__sub">
                {formatWhen(r.fire_time)} · {relative(r.fire_time)}
              </div>
            </div>
            {/* No snooze button here on purpose. The backend's snooze is
                `WHERE fired = 1` — it re-arms an already-delivered reminder
                from its notification, and rejects a still-pending one with a
                409. Offering it on this list would be a button that can only
                ever fail. */}
            <div className="row__actions">
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy === r.id}
                onClick={() => act(r.id, () => api.cancelReminder(settings, r.id))}
                title="Cancel reminder"
              >
                ✕
              </button>
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
