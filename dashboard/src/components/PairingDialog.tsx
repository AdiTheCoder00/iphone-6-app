import { useCallback, useEffect, useRef, useState } from 'react';
import qrcode from '../vendor/qrcode';
import type { Settings } from '../api';

interface Props {
  settings: Settings;
  onClose: () => void;
}

/* A phone that has not yet trusted the local CA cannot connect to any HTTPS
 * page or API on this machine (iOS rejects the TLS handshake outright), so a
 * fresh phone must install the root certificate first. The certificate
 * download is served over plain HTTP precisely so an untrusting phone can
 * reach it: serve_frontend.py's side listener on :8081 serves only /ca.crt,
 * never keys. */
function certInstallUrl(): string | null {
  if (typeof window === 'undefined' || !window.location || !window.location.hostname) {
    return null;
  }
  return `http://${window.location.hostname}:8081/ca.crt`;
}

/* The scanned payload (mockups 1d/3c).
 *
 * Self-describing JSON rather than a bare URL: the phone's pairing screen
 * scans this with its own in-app camera, not the system one, so there is no
 * benefit to a format iOS would offer to "open" — and a versioned object means
 * the phone can reject a code from a future dashboard cleanly instead of
 * misreading it. Bump `v` if the shape ever changes. */
export function pairingPayload(settings: Settings): string {
  return JSON.stringify({ v: 1, url: settings.backendUrl, token: settings.token });
}

/* Quiet zone is 4 modules per the QR spec; less and some scanners refuse the
 * code entirely. */
const QUIET_MODULES = 4;
const TARGET_PX = 196;
/* Must match .modal--closing's animation duration in styles.css. */
const EXIT_MS = 180;

export function PairingDialog({ settings, onClose }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const modalRef = useRef<HTMLDivElement | null>(null);
  /* The element that opened the dialog, so focus can return to it on close —
     otherwise it falls back to the body and a keyboard user loses their place. */
  const openerRef = useRef<HTMLElement | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [drawError, setDrawError] = useState<string | null>(null);

  /* This page is over HTTPS whenever certs exist (the only case the phone
   * needs to install one), so the block only shows then. */
  const https = typeof window !== 'undefined' && window.location?.protocol === 'https:';
  const certUrl = https ? certInstallUrl() : null;

  /* An element cannot animate while unmounting, so the close is held for the
   * length of the exit and the dialog renders a closing state meanwhile. This
   * is the one thing here CSS genuinely cannot do alone — everything else is a
   * transition on an element that stays in the tree. */
  const [closing, setClosing] = useState(false);
  const exitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const close = useCallback(() => {
    /* Guarded: Escape and a click on the overlay can both land before the
     * timer fires, and two pending closes would call onClose twice. */
    if (exitTimer.current !== null) return;
    setClosing(true);
    exitTimer.current = setTimeout(onClose, EXIT_MS);
  }, [onClose]);
  useEffect(() => () => {
    if (exitTimer.current !== null) clearTimeout(exitTimer.current);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    try {
      /* Version 0 = "smallest that fits". Error correction M survives a bit of
       * glare on a screen without inflating the symbol the way H would. */
      const qr = qrcode(0, 'M');
      qr.addData(pairingPayload(settings), 'Byte');
      qr.make();

      const count = qr.getModuleCount();
      const total = count + QUIET_MODULES * 2;
      /* Integer module size, then size the canvas to match: a fractional scale
       * lands module edges mid-pixel and the blur is enough to break a scan. */
      const scale = Math.max(1, Math.floor(TARGET_PX / total));
      const size = total * scale;

      const dpr = window.devicePixelRatio || 1;
      canvas.width = size * dpr;
      canvas.height = size * dpr;
      canvas.style.width = `${size}px`;
      canvas.style.height = `${size}px`;

      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas is unavailable');
      ctx.scale(dpr, dpr);
      /* Always white-on-black-modules regardless of the page theme — scanners
         expect dark modules on a light field, and a themed QR is a broken QR. */
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = '#000000';
      for (let r = 0; r < count; r += 1) {
        for (let c = 0; c < count; c += 1) {
          if (!qr.isDark(r, c)) continue;
          ctx.fillRect((c + QUIET_MODULES) * scale, (r + QUIET_MODULES) * scale, scale, scale);
        }
      }
      setDrawError(null);
    } catch (e) {
      setDrawError(e instanceof Error ? e.message : 'Could not render the code');
    }
  }, [settings]);

  useEffect(() => {
    /* Focus management: the Done button gets focus on open, Tab stays inside
     * the dialog, and focus returns to the opener on unmount. */
    openerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        close();
        return;
      }
      if (e.key !== 'Tab') return;
      const modal = modalRef.current;
      if (!modal) return;
      const nodes = modal.querySelectorAll<HTMLElement>(
        'button, [href], input, [tabindex]:not([tabindex="-1"])',
      );
      const list = Array.from(nodes).filter((n) => !n.hasAttribute('disabled'));
      if (!list.length) return;
      const first = list[0];
      const last = list[list.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === modal)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      openerRef.current?.focus();
    };
  }, [close]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(settings.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* Clipboard is permission-gated and blocked outright on insecure
       * origins; the reveal toggle is the fallback, so fail quietly. */
      setRevealed(true);
    }
  };

  return (
    <div
      className={`modal${closing ? ' modal--closing' : ''}`}
      ref={modalRef}
      onClick={close}
    >
      {/* The panel stops the overlay's click-to-close from firing on any click
          inside it. */}
      <div
        className="modal__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="pair-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="modal__title" id="pair-title">
          Pair a phone
        </h2>
        <p className="modal__sub">
          Open the companion on your phone and scan this from its pairing screen.
        </p>

        {certUrl ? (
          <div className="pair__cert">
            <p className="pair__cert__title">New phone? Install the certificate first</p>
            <p className="pair__cert__body">
              The backend uses a self-signed certificate. On the phone, open{' '}
              <code>{certUrl}</code>, install the profile, then enable full
              trust under Settings › General › About › Certificate Trust
              Settings. Without this the phone gets “no answer”.
            </p>
          </div>
        ) : null}

        <div className="pair__code">
          {drawError ? (
            <p className="error">{drawError}</p>
          ) : (
            <canvas ref={canvasRef} aria-label="Pairing QR code" role="img" />
          )}
        </div>

        <p className="pair__warn">
          This code contains your access token — don’t screen-share or photograph it.
        </p>

        <div className="pair__divider">
          <span>OR TYPE IT</span>
        </div>

        <div className="pair__field">
          <span className="pair__label">Backend</span>
          <code className="pair__value">{settings.backendUrl}</code>
        </div>
        <div className="pair__field">
          <span className="pair__label">Access token</span>
          <code className="pair__value">
            {settings.token ? (revealed ? settings.token : '•'.repeat(16)) : 'not set'}
          </code>
          {settings.token ? (
            <div className="pair__actions">
              <button
                type="button"
                className="btn btn--quiet"
                onClick={() => setRevealed((v) => !v)}
              >
                {revealed ? 'Hide' : 'Reveal'}
              </button>
              <button type="button" className="btn btn--quiet" onClick={copy}>
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          ) : null}
        </div>

        <div className="modal__foot">
          <button type="button" className="btn" ref={closeRef} onClick={close}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
