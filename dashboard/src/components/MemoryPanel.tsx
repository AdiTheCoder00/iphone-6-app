import { useState } from 'react';
import { Panel } from './Panel';
import { api, type Settings } from '../api';
import type { Fact } from '../types';

interface Props {
  facts: Fact[] | null;
  error: string | null;
  loading: boolean;
  settings: Settings;
  onChanged: () => void;
}

/* Mockup 1k renders facts as chips rather than rows: they are short phrases,
 * and a full row each wasted a line apiece in a tile that sits under the
 * reminders. The delete stays inside the chip — memory you cannot delete is
 * memory you cannot trust. */
export function MemoryPanel({ facts, error, loading, settings, onChanged }: Props) {
  const [busy, setBusy] = useState<number | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const remove = async (id: number) => {
    setBusy(id);
    setFailed(null);
    try {
      await api.deleteFact(settings, id);
    } catch (e) {
      /* Surfaced rather than swallowed: an action that silently does nothing
       * is worse than one that says why it couldn't. */
      setFailed(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  return (
    <Panel
      title="Memory"
      aside={facts ? <span className="count">{facts.length}</span> : null}
      loading={loading}
      skeleton={loading}
      skeletonLines={2}
      error={error}
      empty={!!facts && facts.length === 0}
      emptyText="Nothing remembered yet."
    >
      {failed ? <p className="error">{failed}</p> : null}
      <div className="chips">
        {facts?.map((f) => (
          <span key={f.id} className="chip">
            <span className="chip__text">{f.text}</span>
            <button
              type="button"
              className="chip__remove"
              disabled={busy === f.id}
              onClick={() => remove(f.id)}
              title={`Forget "${f.text}"`}
              aria-label={`Forget "${f.text}"`}
            >
              ✕
            </button>
          </span>
        ))}
      </div>
    </Panel>
  );
}
