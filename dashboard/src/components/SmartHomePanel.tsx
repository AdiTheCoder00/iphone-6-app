import { useState } from 'react';
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
    try {
      await api.setSmartDevice(settings, entityId, turnOn);
    } catch (e) {
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
          const on = d.state === 'on';
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
