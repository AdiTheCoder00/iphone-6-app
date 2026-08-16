import type { ReactNode } from 'react';

interface PanelProps {
  title: string;
  /* Right-aligned slot for a count, a status dot, an action button. */
  aside?: ReactNode;
  loading?: boolean;
  /* Mockup 1l: render shaped placeholder bars instead of the word "Loading…".
   * Only for a first load — a background refresh must never replace a panel
   * that already has good data with skeletons. */
  skeleton?: boolean;
  skeletonLines?: number;
  error?: string | null;
  /* Shown when there is no error and nothing to list. Distinguishing this
   * from an error state matters: "nothing scheduled" and "couldn't reach the
   * backend" look identical as an empty list, and only one is fine. */
  empty?: boolean;
  emptyText?: string;
  children?: ReactNode;
}

function Skeleton({ lines }: { lines: number }) {
  return (
    <div className="skeleton" aria-hidden>
      <div className="skeleton__block" />
      {Array.from({ length: Math.max(0, lines) }, (_, i) => (
        /* Descending widths so it reads as text, not as a stack of bars. */
        <div key={i} className="skeleton__line" style={{ width: `${70 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function Panel({
  title,
  aside,
  loading,
  skeleton,
  skeletonLines = 2,
  error,
  empty,
  emptyText = 'Nothing here.',
  children,
}: PanelProps) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2 className="panel__title">{title}</h2>
        {aside ? (
          <div className="panel__aside">{aside}</div>
        ) : null}
      </header>
      <div className="panel__body">
        {skeleton ? (
          <Skeleton lines={skeletonLines} />
        ) : loading ? (
          <p className="muted">Loading…</p>
        ) : error ? (
          <p className="error">{error}</p>
        ) : empty ? (
          <p className="muted">{emptyText}</p>
        ) : (
          children
        )}
      </div>
    </section>
  );
}
