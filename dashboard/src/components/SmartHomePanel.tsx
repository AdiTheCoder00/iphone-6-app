import { useEffect, useState } from 'react';
import { Panel } from './Panel';
import { api, type Settings } from '../api';
import type { SmartDevices } from '../types';

interface Props {
  data: SmartDevices | null;
  error: string | null;
  loading: boolean;
  settings: Settings;
  onChanged: () => void;
}

export function SmartHomePanel({ data, error, loading, settings, onChanged }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  /* Optimistic overrides: entity_id -> state shown while its POST is in
   * flight. The toggle flips on click instead of after the full
   * dashboard -> backend -> Home Assistant -> plug roundtrip, which takes
   * seconds. The override dies as soon as a poll returns the same state —
   * the backend's data resumes being the single source of truth. */
  const [pending, setPending] = useState<Record<string, boolean>>({});

  useEffect(() => {
    setPending((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const [id, on] of Object.entries(next)) {
        const dev = data?.devices.find((d) => d.entity_id === id);
        if (!dev || dev.state === (on ? 'on' : 'off')) {
          delete next[id];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [data]);

  if (!loading && !error && data && !data.available) {
    return (
      <Panel
        title="Smart home"
        empty
        emptyText="Home Assistant isn't set up. Add HA_ENABLED and HA_TOKEN to backend/.env."
      />
    );
  }

  const toggle = async (entityId: string, turnOn: boolean) => {
    setBusy(entityId);
    setFailed(null);
    setPending((p) => ({ ...p, [entityId]: turnOn }));
    try {
      await api.setSmartDevice(settings, entityId, turnOn);
    } catch (e) {
      setPending((p) => {
        const next = { ...p };
        delete next[entityId];
        return next;
      });
      setFailed(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  return (
    <Panel
      title="Smart home"
      aside={data ? <span className="count">{data.devices.length}</span> : null}
      loading={loading}
      error={error}
      empty={!!data && data.devices.length === 0}
      emptyText="No lights or switches found."
    >
      {failed ? <p className="error">{failed}</p> : null}
      <ul className="rows">
        {data?.devices.map((d) => {
          const on = pending[d.entity_id] ?? d.state === 'on';
          return (
            <li key={d.entity_id} className="row">
              <div className="row__main">
                <div className="row__text">{d.name}</div>
                <div className="row__sub">
                  {d.domain} · {d.state}
                </div>
              </div>
              <div className="row__actions">
                <button
                  type="button"
                  className={`toggle ${on ? 'toggle--on' : ''}`}
                  role="switch"
                  aria-checked={on}
                  aria-label={`${d.name}: ${on ? 'on' : 'off'}`}
                  disabled={busy === d.entity_id}
                  onClick={() => toggle(d.entity_id, !on)}
                  title={on ? 'Turn off' : 'Turn on'}
                >
                  <span className="toggle__knob" />
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
