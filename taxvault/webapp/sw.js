/* Service worker: shell caching only.
 *
 * The app shell is cached so the app opens on a bad connection. Nothing from
 * /api is ever cached -- a tax figure served from a stale cache is worse than
 * no figure, and an SSN-adjacent response has no business sitting in a disk
 * cache on a shared device.
 */
const SHELL = 'taxvault-shell-v1';
const ASSETS = ['/', '/static/styles.css', '/static/app.js', '/manifest.webmanifest', '/static/mark.svg', '/static/logo.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api')) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // Refresh the shell copy when the network answers.
        const copy = response.clone();
        caches.open(SHELL).then((cache) => cache.put(event.request, copy)).catch(() => {});
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match('/')))
  );
});
