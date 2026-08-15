import { useCallback, useEffect, useRef, useState } from 'react';
import { api, loadSettings, saveSettings, type Settings } from './api';
import { useEvents, usePoll } from './hooks';
import { StatusBar } from './components/StatusBar';
import { SettingsBar } from './components/SettingsBar';
import { PCPanel } from './components/PCPanel';
import { RemindersPanel } from './components/RemindersPanel';
import { MemoryPanel } from './components/MemoryPanel';
import { SmartHomePanel } from './components/SmartHomePanel';
import { ConversationPanel } from './components/ConversationPanel';
import { EventFeed } from './components/EventFeed';

/* Different data changes at very different rates, so it gets very different
 * poll intervals. PC stats are the only genuinely live numbers; reminders and
 * memory change only when something acts on them, and the SSE feed already
 * announces most of those the moment they happen. */
const PC_INTERVAL = 5_000;
const HEALTH_INTERVAL = 10_000;
const LIST_INTERVAL = 20_000;

export default function App() {
  const [settings, setSettings] = useState<Settings>(loadSettings);

  const updateSettings = useCallback((next: Settings) => {
    saveSettings(next);
    setSettings(next);
  }, []);

  const health = usePoll(api.health, settings, HEALTH_INTERVAL);
  const pc = usePoll(api.pcStatus, settings, PC_INTERVAL);
  const reminders = usePoll(api.reminders, settings, LIST_INTERVAL);
  const facts = usePoll(api.facts, settings, LIST_INTERVAL);
  const conversation = usePoll(api.conversation, settings, LIST_INTERVAL);
  const smart = usePoll(api.smartDevices, settings, LIST_INTERVAL);
  const { events, connected, failed } = useEvents(settings);

  /* A fired reminder or an unprompted message means the lists are already
   * stale — refresh them off the event stream instead of waiting out the
   * 20s timer. Refreshers are held in a ref so this fires on a new event
   * only, not every time a poll hook hands back a new callback identity. */
  const latestEventId = events[0]?.id ?? -1;
  const refreshers = useRef({
    reminders: reminders.refresh,
    conversation: conversation.refresh,
    facts: facts.refresh,
    smart: smart.refresh,
  });
  refreshers.current = {
    reminders: reminders.refresh,
    conversation: conversation.refresh,
    facts: facts.refresh,
    smart: smart.refresh,
  };

  /* Refresh() aborts the poll currently in flight; firing all four per event
   * during a burst (or with a slow backend) would keep aborting polls that
   * never get to finish, leaving the panels dependent on the refreshers
   * alone. Coalesce: at most one refresh per panel per cooldown, and none at
   * all while the tab is hidden. */
  const REFRESH_COOLDOWN_MS = 3_000;
  const lastRefreshed = useRef<Record<string, number>>({});

  useEffect(() => {
    if (latestEventId < 0 || document.hidden) return;
    const now = Date.now();
    const names = ['reminders', 'conversation', 'facts', 'smart'] as const;
    for (const name of names) {
      if (now - (lastRefreshed.current[name] ?? 0) < REFRESH_COOLDOWN_MS) continue;
      lastRefreshed.current[name] = now;
      refreshers.current[name]();
    }
  }, [latestEventId]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__eyes" aria-hidden>
            <i />
            <i />
          </span>
          <h1>Companion</h1>
        </div>
        <SettingsBar settings={settings} onSave={updateSettings} />
      </header>

      <StatusBar
        health={health.data}
        error={health.error}
        streamConnected={connected}
        streamFailed={failed}
      />

      <main className="grid">
        <PCPanel status={pc.data} error={pc.error} loading={pc.loading} />
        <RemindersPanel
          reminders={reminders.data}
          error={reminders.error}
          loading={reminders.loading}
          settings={settings}
          onChanged={reminders.refresh}
        />
        <MemoryPanel
          facts={facts.data}
          error={facts.error}
          loading={facts.loading}
          settings={settings}
          onChanged={facts.refresh}
        />
        <SmartHomePanel
          data={smart.data}
          error={smart.error}
          loading={smart.loading}
          settings={settings}
          onChanged={smart.refresh}
        />
        <EventFeed events={events} connected={connected} failed={failed} />
        <ConversationPanel
          messages={conversation.data}
          error={conversation.error}
          loading={conversation.loading}
          settings={settings}
          onChanged={conversation.refresh}
        />
      </main>
    </div>
  );
}
