// -----------------------------------------------------------------------------
// Skript: src/web/static/kiosk.js
// Autor: Torben <github@x-gate.de>
// Version: 1.0.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - Kiosk-Anzeige (Buero-Display): Signalfeld-Layout (Boxen pro Reihe + Max-Eintraege)
//   aus den serverseitig gesetzten data-Attributen, nahtlose Ticker-Schleife und ein
//   periodischer Voll-Reload. KEIN localStorage (das Display folgt nur den Settings).
// Betriebs- und Wartungshinweise:
// - Eigenstaendig (kein theme.js/dashboard.js), damit die Browser-Einstellungen des
//   Displays nichts ueberschreiben. Kein Chat, keine Bedienung.
// -----------------------------------------------------------------------------
(function () {
  "use strict";
  var root = document.documentElement;
  var ALLOWED = { 1: 12, 2: 6, 3: 4, 4: 3, 6: 2 };

  function num(key) {
    var v = parseInt(root.getAttribute("data-" + key), 10);
    return ALLOWED[v] ? v : 1;
  }

  // Signalfeld: einheitliches Reihen-Layout ueber ALLE Kacheln; Max-Eintraege deckelt
  // die NextUp-Karten (Grafana/Funktionskacheln zaehlen nicht mit, bleiben sichtbar).
  function layoutSignal() {
    var ul = document.querySelector(".items");
    if (!ul) return;
    var lis = ul.children;
    var signal = root.getAttribute("data-view") === "signal";
    var w = window.innerWidth;
    var rows = [num("r1"), num("r2"), num("r3")];
    var rest = num("rn");
    var maxItems = parseInt(root.getAttribute("data-max"), 10) || 0;
    var rowIdx = 0, inRow = 0, shown = 0;
    for (var k = 0; k < lis.length; k++) {
      var li = lis[k];
      if (maxItems > 0 && !li.classList.contains("gpanel")) {
        shown++;
        if (shown > maxItems) { li.style.display = "none"; continue; }
      }
      li.style.display = "";
      if (!signal) { li.style.gridColumn = ""; continue; }
      if (w < 700) { li.style.gridColumn = "1 / -1"; continue; }
      var boxes = rowIdx < 3 ? rows[rowIdx] : rest;
      if (w < 1100 && boxes > 3) boxes = 3;
      li.style.gridColumn = "span " + ALLOWED[boxes];
      inRow++;
      if (inRow >= boxes) { inRow = 0; rowIdx++; }
    }
  }

  // Nahtlose Ticker-Schleife (Inhalt duplizieren, Animation -50%).
  function loopTrack(track) {
    if (!track || !track.children.length) return;
    var vw = (track.parentElement && track.parentElement.clientWidth) || window.innerWidth;
    var originals = Array.prototype.slice.call(track.children);
    var guard = 0;
    while (track.scrollWidth < vw && guard < 20) {
      originals.forEach(function (n) { track.appendChild(n.cloneNode(true)); });
      guard++;
    }
    Array.prototype.slice.call(track.children).forEach(function (n) {
      track.appendChild(n.cloneNode(true));
    });
    track.classList.add("tk-loop");
    var pps = parseInt(track.getAttribute("data-speed"), 10) || 90;
    track.style.animationDuration = Math.max(8, Math.round(track.scrollWidth / 2 / pps)) + "s";
  }

  function boot() {
    ["tk-track", "wt-track"].forEach(function (id) { loopTrack(document.getElementById(id)); });
    layoutSignal();
  }

  document.addEventListener("DOMContentLoaded", boot);
  var rt;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(layoutSignal, 200); });

  // Periodischer Voll-Reload: holt frische Items/Ticker/Panels ohne Bedienung.
  var self = document.currentScript;
  var refresh = self ? parseInt(self.getAttribute("data-refresh"), 10) : 90;
  if (refresh && refresh >= 15) {
    setTimeout(function () { location.reload(); }, refresh * 1000);
  }
})();
