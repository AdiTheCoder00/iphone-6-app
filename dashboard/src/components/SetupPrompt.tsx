interface Props {
  title: string;
  detail: string;
  actionLabel?: string;
  onAction?: () => void;
}

/* Mockup 3e: when a capability is missing, the hero slot gets an explanation
 * and a way forward instead of an empty box. A dashed border rather than the
 * solid panel border — it reads as a placeholder for something that could be
 * there, not as a card reporting a value. */
export function SetupPrompt({ title, detail, actionLabel, onAction }: Props) {
  return (
    <section className="setup">
      <div className="setup__icon" aria-hidden>
        ⚡
      </div>
      <div className="setup__main">
        <div className="setup__title">{title}</div>
        <div className="setup__detail">{detail}</div>
      </div>
      {actionLabel && onAction ? (
        <button type="button" className="btn" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </section>
  );
}
