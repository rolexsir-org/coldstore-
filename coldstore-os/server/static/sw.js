/* ColdStore OS service worker.
   Policy: STATIC assets cache-first (app shell works fully offline);
   API calls NEVER cached — business writes go through the explicit outbox
   with stable Operation IDs, and reads always hit the server when online. */
const VERSION = 'coldstore-m1-0003';
const SHELL = ['/', '/index.html', '/styles.css', '/app.js', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== VERSION) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return; // network-only
  e.respondWith(
    caches.match(e.request).then((hit) => hit ||
      fetch(e.request).then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy));
        }
        return resp;
      }).catch(() => caches.match('/index.html')))
  );
});
