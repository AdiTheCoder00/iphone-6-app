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

const CACHE = 'companion-shell-v1';

/* Only the shell. API calls live on a different origin and must never be
 * served from cache — a stale reply or a stale reminder would be worse than
 * an error. */
const SHELL = [
  'companion.html',
  'manifest.json',
  'icons/icon-180.png',
  'icons/icon-192.png',
  'icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      /* Individually, so one missing file cannot fail the whole install. */
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;

  /* Same-origin GETs only. Anything cross-origin is the backend. */
  if (request.method !== 'GET') return;
  if (new URL(request.url).origin !== self.location.origin) return;

  /* Network first: an edited companion.html must win over the cached copy,
     otherwise every change would need a manual cache bust. The cache is the
     fallback, not the source of truth. */
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || Response.error()))
  );
});
