const CACHE_NAME = "continnumos-pwa-v1";
const STATIC_ASSETS = [
  "/static/logo.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
  "/static/offline.html"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  const livePrefixes = ["/api/", "/ws", "/midias/"];
  if (livePrefixes.some(prefix => url.pathname.startsWith(prefix))) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/static/offline.html"))
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(response => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(request, clone));
        return response;
      }))
    );
  }
});
