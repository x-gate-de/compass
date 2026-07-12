// -----------------------------------------------------------------------------
// Datei: static/sw.js
// Autor: Torben <github@x-gate.de>
// Version: 1.1.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck: Service Worker fuer PWA-Installierbarkeit und Web-Push. Kein Offline-Cache
//        sensibler Inhalte -- Nachrichten/Items bleiben serverseitig.
// -----------------------------------------------------------------------------
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

// Push: inhaltslose Benachrichtigung (Titel/Text + Link), keine Nachrichteninhalte.
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  var title = data.title || "compass";
  var body = data.body || "Neue Nachricht";
  var url = data.url || "/chat";
  event.waitUntil(
    self.registration.showNotification(title, { body: body, data: { url: url }, tag: url })
  );
});

// Klick auf die Benachrichtigung: bestehendes Fenster fokussieren oder oeffnen.
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/chat";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if ("focus" in list[i]) { list[i].navigate(url); return list[i].focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
