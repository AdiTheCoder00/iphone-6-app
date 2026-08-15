import { useCallback, useEffect, useRef, useState } from 'react';
import { api, loadSettings, saveSettings, type Settings } from './api';
import { useEvents, usePoll } from './hooks';
import { StatusBar } from './components/StatusBar';
import { SettingsBar } from './components/SettingsBar';
import { NowPlayingHero } from './components/NowPlayingHero';
import { ActivityTicker } from './components/ActivityTicker';
import { PairingDialog } from './components/PairingDialog';
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
  const [settingsOpen, setSettingsOpen] = useState(!settings.token);
  const [pairing, setPairing] = useState(false);

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
  const { events, connected, failed, attempts } = useEvents(settings);

  /* Mockup 3f distinguishes "never connected" from "was live, then dropped".
   * Only the second keeps the stale data on screen, so the tell is that we
   * currently have an error but still hold a health payload from before it. */
  const reconnecting = !!health.error && !!health.data;
  /* When the panels went stale. Recorded on the last successful health read
   * rather than per panel: they all read the same backend, so per-panel
   * timestamps would be the same number repeated four times. */
  const lastSeen = useRef<Date | null>(null);
  useEffect(() => {
    if (health.data && !health.error) lastSeen.current = new Date();
  }, [health.data, health.error]);

  /* While reconnecting, a panel's own error must not replace the data it is
   * still holding — that is the whole point of 3f, and letting each panel
   * render "Failed to fetch" would throw away the last-known state the dimming
   * is there to present. One backend means one failure, so the banner reports
   * it once and the panels stay on their data. */
  const panelError = (e: string | null) => (reconnecting ? null : e);

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
        <div className="topbar__actions">
          <button type="button" className="btn" onClick={() => setPairing(true)}>
            Pair a phone
          </button>
          <SettingsBar
            settings={settings}
            onSave={updateSettings}
            open={settingsOpen}
            onOpenChange={setSettingsOpen}
          />
        </div>
      </header>

      <StatusBar
        health={health.data}
        error={health.error}
        streamConnected={connected}
        streamFailed={failed}
        streamAttempts={attempts}
        backendUrl={settings.backendUrl}
        reconnecting={reconnecting}
        lastSeen={lastSeen.current}
        onSettings={() => setSettingsOpen(true)}
      />

      {/* Mockup 3f: while reconnecting the panels keep their last-known data,
          dimmed as a whole rather than panel by panel — they all read the one
          backend, so they went stale together and at the same moment. */}
      <main className={`bento${reconnecting ? ' is-stale' : ''}`}>
        <NowPlayingHero
          status={pc.data}
          error={panelError(pc.error)}
          loading={pc.loading}
          settings={settings}
          onChanged={pc.refresh}
          onPair={() => setPairing(true)}
          stale={reconnecting}
        />

        <div className="bento__chat">
          <ConversationPanel
            messages={conversation.data}
            error={panelError(conversation.error)}
            loading={conversation.loading}
            settings={settings}
            onChanged={conversation.refresh}
          />
        </div>

        {/* Reminders and memory share the tile, as drawn: the facts sit under
            the reminder rows as chips. */}
        <div className="bento__left">
          <RemindersPanel
            reminders={reminders.data}
            error={panelError(reminders.error)}
            loading={reminders.loading}
            settings={settings}
            onChanged={reminders.refresh}
          />
          <MemoryPanel
            facts={facts.data}
            error={panelError(facts.error)}
            loading={facts.loading}
            settings={settings}
            onChanged={facts.refresh}
          />
        </div>

        <div className="bento__right">
          <SmartHomePanel
            data={smart.data}
            error={panelError(smart.error)}
            loading={smart.loading}
            settings={settings}
            onChanged={smart.refresh}
          />
        </div>
      </main>

      <ActivityTicker events={events} connected={connected} failed={failed} />

      {/* The ticker shows three; the feed is the whole history and stays
          mounted under it rather than being dropped from the page. */}
      <details className="feed-drawer">
        <summary>Full activity log</summary>
        <EventFeed events={events} connected={connected} failed={failed} />
      </details>

      {pairing ? (
        <PairingDialog settings={settings} onClose={() => setPairing(false)} />
      ) : null}
    </div>
  );
}
