/* Service worker: keep the shell available when the network is not.
 *
 * The point is not offline use — the companion is useless without its
 * backend. The point is that a Wi-Fi blip should not leave a home-screen app
 * showing a browser error page. With the shell cached, the face still loads
 * and the existing health-check retry drives the "can't reach the backend"
 * indicator, which is a far better failure than a dead white screen.
 *
 * NOTE: service workers require a secure context — HTTPS, or localhost. Served
 * over plain http:// from a LAN IP (the normal setup here) registration is
 * refused by the browser and this file simply never runs. companion.html
 * handles that silently. To actually get it, serve over HTTPS.
 */

/* Bump this version whenever the shell changes — the browser only checks for
 * a new service worker script on navigation, and an unchanged sw.js can keep
 * a stale companion.html in play for a long time on iOS. */
const CACHE = 'companion-shell-v11';

/* Only the shell. API calls live on a different origin and must never be
 * served from cache — a stale reply or a stale reminder would be worse than
 * an error. */
const SHELL = [
  'companion.html',
  'manifest.json',
  'qrcode.js',
  /* jsqr.js is loaded on demand by the pairing screen; precaching it keeps
     the scanner working through a Wi-Fi blip that would otherwise land on
     the "decoder unavailable" fallback. */
  'jsqr.js',
  'icons/icon-180.png',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/icon-maskable-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      /* Individually, so one missing file cannot fail the whole install.
         Promise.allSettled would be nicer but does not exist in iOS 12
         Safari, which is the primary target here. */
      .then((cache) => Promise.all(SHELL.map((url) => cache.add(url).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      /* A transient failure during install can leave the fresh shell
         incomplete (a single missing entry). Repair it here — quietly, so a
         blip cannot fail activation — otherwise the gap would persist until
         that exact URL happened to be fetched again. */
      .then(() => caches.open(CACHE))
      .then((cache) => Promise.all(
        SHELL.map((url) => cache.match(url).then((hit) => {
          if (!hit) return cache.add(url).catch(() => {});
        }))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;

  /* Same-origin GETs only. Anything cross-origin is the backend. */
  if (request.method !== 'GET') return;
  if (new URL(request.url).origin !== self.location.origin) return;

  /* SSE streams must pass straight through untouched: cloning a live body
     (below) would consume it and break the connection. EventSource sends
     this Accept header, so it is the reliable tell. */
  const accept = (request.headers.get('accept') || '').toLowerCase();
  if (accept.includes('text/event-stream')) return;

  /* Network first: an edited companion.html must win over the cached copy,
     otherwise every change would need a manual cache bust. The cache is the
     fallback, not the source of truth. */
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          /* Keep the cache write inside the event's lifetime and swallow
             failures (QuotaExceededError on a low-disk phone, or the put
             being killed mid-write when the fetch event completes). */
          event.waitUntil(
            caches.open(CACHE)
              .then((cache) => cache.put(request, copy))
              .catch(() => {})
          );
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => {
        /* Navigation requests: the server 302s `/` to companion.html, and
           that redirect target is never fetched by the worker itself, so
           offline the match would come up empty and the app would show a
           browser error page instead of the cached shell. */
        if (hit) return hit;
        if (request.mode === 'navigate') {
          return caches.match('companion.html');
        }
        return Response.error();
      }))
  );
});
