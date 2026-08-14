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

export function MemoryPanel({ facts, error, loading, settings, onChanged }: Props) {
  const [busy, setBusy] = useState<number | null>(null);

  const remove = async (id: number) => {
    setBusy(id);
    try {
      await api.deleteFact(settings, id);
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
      error={error}
      empty={!!facts && facts.length === 0}
      emptyText="Nothing remembered yet."
    >
      <ul className="rows">
        {facts?.map((f) => (
          <li key={f.id} className="row">
            <div className="row__main">
              <div className="row__text">{f.text}</div>
            </div>
            <div className="row__actions">
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy === f.id}
                onClick={() => remove(f.id)}
                title="Forget this"
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
