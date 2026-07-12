// -----------------------------------------------------------------------------
// Skript: src/web/static/settings.js
// Autor: Torben <github@x-gate.de>
// Version: 1.0.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - Zwei-Spalten-Einstellungsseite: Navigationsleiste schaltet das aktive Modul um;
//   das gewaehlte Modul wird pro Browser gemerkt und ueber das Neuladen nach dem
//   Speichern (Redirect auf /settings) hinweg wiederhergestellt.
// Betriebs- und Wartungshinweise:
// - Kein Inline-Skript (CSP). Progressive Enhancement: ohne JS zeigt das Template
//   das erste Modul (Darstellung).
// -----------------------------------------------------------------------------
(function () {
  "use strict";
  var KEY = "compass.settings.panel";
  var navs = Array.prototype.slice.call(document.querySelectorAll(".set-nav"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".set-panel"));
  if (!navs.length || !panels.length) return;

  function show(k) {
    var hit = panels.some(function (p) { return p.getAttribute("data-panel") === k; });
    if (!hit) k = panels[0].getAttribute("data-panel");
    panels.forEach(function (p) { p.classList.toggle("cur", p.getAttribute("data-panel") === k); });
    navs.forEach(function (n) { n.classList.toggle("cur", n.getAttribute("data-goto") === k); });
    try { localStorage.setItem(KEY, k); } catch (e) {}
  }

  navs.forEach(function (n) {
    n.addEventListener("click", function () {
      show(n.getAttribute("data-goto"));
      window.scrollTo(0, 0);
    });
  });

  // Startmodul: URL-Hash (#set-<name>) hat Vorrang, dann der gemerkte Stand.
  var initial = (location.hash || "").replace(/^#set-/, "");
  if (!initial) {
    try { initial = localStorage.getItem(KEY) || ""; } catch (e) {}
  }
  if (initial) show(initial);
})();
