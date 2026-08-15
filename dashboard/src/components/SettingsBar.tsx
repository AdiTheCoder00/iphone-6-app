import { useState } from 'react';
import type { Settings } from '../api';

interface Props {
  settings: Settings;
  onSave: (next: Settings) => void;
  /* Controlled by App so the offline banner's Settings button can open this —
   * the thing that fixes a wrong URL or token is up here, and a button that
   * only scrolls you toward it is not much help. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SettingsBar({ settings, onSave, open, onOpenChange }: Props) {
  const [backendUrl, setBackendUrl] = useState(settings.backendUrl);
  const [token, setToken] = useState(settings.token);

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({ backendUrl: backendUrl.trim(), token: token.trim() });
    onOpenChange(false);
  };

  if (!open) {
    return (
      <div className="settingsbar settingsbar--collapsed">
        <span className="muted">{settings.backendUrl}</span>
        <button type="button" className="btn btn--quiet" onClick={() => onOpenChange(true)}>
          Settings
        </button>
      </div>
    );
  }

  return (
    <form className="settingsbar" onSubmit={save}>
      <label className="field">
        <span>Backend URL</span>
        <input
          value={backendUrl}
          onChange={(e) => setBackendUrl(e.target.value)}
          placeholder="https://<computer-LAN-IP>:8000"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label className="field">
        <span>Access token</span>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="COMPANION_TOKEN from backend/.env"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <button type="submit" className="btn">
        Save
      </button>
    </form>
  );
}
