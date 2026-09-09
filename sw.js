'use strict';

/**
 * NJDROP service worker.
 *
 * Caches the static app shell (HTML/CSS/JS/icons/fonts-CDN) for fast
 * repeat loads and basic offline access to the UI itself. Never caches
 * /api/* calls — every download request must hit the live backend.
 */

const CACHE_NAME = 'njdrop-shell-v1';

const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/style.css',
  '/script.js',
  '/logo.svg',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Never intercept API calls — downloads must always go live to the network.
  if (request.url.includes('/api/')) {
    return;
  }

  // Only handle same-origin GET requests for the shell.
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;

      return fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone));
          }
          return response;
        })
        .catch(() => {
          // Offline fallback: serve the shell page for navigations.
          if (request.mode === 'navigate') {
            return caches.match('/index.html');
          }
        });
    })
  );
});
