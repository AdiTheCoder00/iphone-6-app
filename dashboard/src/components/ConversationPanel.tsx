import { useState } from 'react';
import { Panel } from './Panel';
import { api, type Settings } from '../api';
import type { ChatMessage } from '../types';

interface Props {
  messages: ChatMessage[] | null;
  error: string | null;
  loading: boolean;
  settings: Settings;
  onChanged: () => void;
}

export function ConversationPanel({ messages, error, loading, settings, onChanged }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const clear = async () => {
    setBusy(true);
    try {
      await api.clearConversation(settings);
    } finally {
      setBusy(false);
      setConfirming(false);
      onChanged();
    }
  };

  /* Clearing history is not reversible, so it asks first — the only
   * destructive control on this dashboard, and it still only wipes the
   * transcript, not remembered facts. */
  const aside = messages?.length ? (
    confirming ? (
      <span className="confirm">
        <button type="button" className="btn btn--danger" disabled={busy} onClick={clear}>
          Clear all
        </button>
        <button type="button" className="btn btn--quiet" onClick={() => setConfirming(false)}>
          Cancel
        </button>
      </span>
    ) : (
      <button type="button" className="btn btn--quiet" onClick={() => setConfirming(true)}>
        Clear
      </button>
    )
  ) : null;

  return (
    <Panel
      title="Conversation"
      aside={aside}
      loading={loading}
      error={error}
      empty={!!messages && messages.length === 0}
      emptyText="No conversation yet."
    >
      <div className="chat">
        {messages?.map((m, i) => (
          <div key={i} className={`bubble bubble--${m.role}`}>
            {m.content}
          </div>
        ))}
      </div>
    </Panel>
  );
}
