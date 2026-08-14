import type { ReactNode } from 'react';

interface PanelProps {
  title: string;
  /* Right-aligned slot for a count, a status dot, an action button. */
  aside?: ReactNode;
  loading?: boolean;
  error?: string | null;
  /* Shown when there is no error and nothing to list. Distinguishing this
   * from an error state matters: "nothing scheduled" and "couldn't reach the
   * backend" look identical as an empty list, and only one is fine. */
  empty?: boolean;
  emptyText?: string;
  children?: ReactNode;
}

export function Panel({
  title,
  aside,
  loading,
  error,
  empty,
  emptyText = 'Nothing here.',
  children,
}: PanelProps) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2 className="panel__title">{title}</h2>
        {aside ? <div className="panel__aside">{aside}</div> : null}
      </header>
      <div className="panel__body">
        {loading ? (
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
