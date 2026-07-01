// Service Worker — offline app-shell cache
const CACHE_NAME = 'sleepctl-v1';

// App-shell resources to cache on install
const PRECACHE_URLS = [
  '/',
  '/tonight',
  '/data',
  '/learning',
  '/analytics',
  '/manifest.json',
  '/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Don't intercept API calls or SSE streams — always go to network
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // Network-first for navigation, cache fallback
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match('/').then((r) => r || new Response('Offline', { status: 503 }))
      )
    );
    return;
  }

  // Cache-first for static assets
  event.respondWith(
    caches.match(event.request).then(
      (cached) =>
        cached ||
        fetch(event.request).then((response) => {
          if (response.ok && event.request.method === 'GET') {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((c) => c.put(event.request, clone));
          }
          return response;
        })
    )
  );
});

// ---------------------------------------------------------------------------
// Web Push — goal #2: a silent controller/bed outage should buzz the phone.
// The backend (dashboard/api/app/push_sender.py) sends a JSON payload shaped
// {title, body, tag, severity, url} (see push_sender.build_payload). ``tag``
// dedupes so re-delivery of the same still-open issue replaces rather than
// stacks notifications.
// ---------------------------------------------------------------------------
self.addEventListener('push', (event) => {
  let data = { title: 'SleepCtl alert', body: 'A controller issue needs attention.', tag: 'sleepctl-alert', url: '/' };
  if (event.data) {
    try {
      data = { ...data, ...event.data.json() };
    } catch (e) {
      data.body = event.data.text() || data.body;
    }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: data.tag,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data: { url: data.url || '/' },
      requireInteraction: data.severity === 'critical',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const c of clientList) {
        if ('focus' in c) {
          c.navigate(url);
          return c.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(url);
      }
    })
  );
});
