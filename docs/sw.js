/**
 * TickerDesk Service Worker
 * Handles Web Push notifications and click routing back to the dashboard.
 *
 * Lifecycle:
 *  1. Browser registers this file (navigator.serviceWorker.register('/sw.js'))
 *  2. SW installs + activates
 *  3. User grants Notification permission
 *  4. We call sw.pushManager.subscribe({ applicationServerKey: VAPID_PUB })
 *     and post the resulting endpoint+keys to Supabase (push_subscriptions)
 *  5. push_alerts.py POSTs signed payloads to the endpoint
 *  6. Browser wakes this SW, fires 'push' event, we show notification
 *  7. User clicks → 'notificationclick' event → open/focus tickerdesk.io
 *
 * Versioning: bump SW_VERSION to force an update propagation. Browsers
 * compare byte-for-byte on each register() call; any change wins.
 */

const SW_VERSION = 'tickerdesk-v1.0.0';
const SITE_URL = 'https://tickerdesk.io';

self.addEventListener('install', function (event) {
  // Skip waiting so updates take effect immediately
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  // Claim all open clients so they start using this SW right away
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  // Payload may be null in some edge cases; default to a generic title
  let data = {
    title: 'TickerDesk',
    body: 'Your watchlist has new activity.',
    url: SITE_URL,
    tag: 'tickerdesk-default',
  };
  if (event.data) {
    try {
      data = Object.assign(data, event.data.json());
    } catch (e) {
      // Fall back to plain text if payload isn't JSON
      try { data.body = event.data.text(); } catch (e2) {}
    }
  }

  const options = {
    body:    data.body,
    icon:    SITE_URL + '/icon-192.png',
    badge:   SITE_URL + '/icon-72.png',
    tag:     data.tag,           // notifications with same tag replace each other
    data:    { url: data.url || SITE_URL, ticker: data.ticker },
    renotify: data.renotify || false,
    requireInteraction: data.priority === 'high',
    // Action buttons (Chrome desktop). Mobile shows them on long-press.
    actions: data.actions || (data.ticker ? [
      { action: 'open-ticker', title: 'Open ' + data.ticker },
      { action: 'dismiss',     title: 'Dismiss' },
    ] : []),
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  if (event.action === 'dismiss') return;

  // Where the click should land
  const url = (event.notification.data && event.notification.data.url) || SITE_URL;

  // Try to focus an existing tab on tickerdesk.io; open a new one if none.
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(function (clientList) {
        for (const c of clientList) {
          // Same origin — focus + navigate
          if (c.url.startsWith(SITE_URL)) {
            return c.focus().then(function () {
              if (c.navigate && c.url !== url) return c.navigate(url);
            });
          }
        }
        return self.clients.openWindow(url);
      })
  );
});

// Push subscription change — service worker tells us when the browser
// rotated the endpoint (~once per year, or after user clears storage).
// We can't re-subscribe from the SW without VAPID, so just log and
// rely on the client to re-subscribe next time the user opens the site.
self.addEventListener('pushsubscriptionchange', function (event) {
  // No-op — client will detect stale subscription on next visit and
  // re-subscribe, which inserts a fresh row in push_subscriptions and
  // expires the old endpoint when push_alerts.py tries it (we delete
  // rows with fail_count >= 5).
});
