// Minimal service worker: only here so notifications work on Android, where
// Chrome refuses `new Notification()` and requires registration.showNotification.
// No caching, no push - the page itself decides when to notify.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const hash = event.notification.data?.hash ?? "#messages";
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if ("focus" in client) {
          client.postMessage({ type: "open", hash });
          return client.focus();
        }
      }
      // Resolved against the registration scope, not the server root, so the
      // notification opens the right page under Home Assistant ingress.
      return self.clients.openWindow(new URL(hash, self.registration.scope).href);
    })(),
  );
});
