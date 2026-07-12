// -----------------------------------------------------------------------------
// Skript: src/web/static/theme.js
// Autor: Torben <github@x-gate.de>
// Version: 2.4.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - Setzt die Design-Einstellungen (Modus, Akzentfarbe, Ansicht, Dichte, Spalten,
//   Boxen-pro-Reihe) VOR dem ersten Paint und verdrahtet das Einstellungsmenue (Zahnrad).
// - Signalfeld-Layout: verteilt die Boxen so, dass jede Reihe die volle Breite fuellt;
//   Boxen pro Reihe 1/2/3/weitere frei einstellbar.
// - Max. Eintraege: begrenzt die Zahl der angezeigten NextUp-Karten (alle Ansichten);
//   Grafana-Panels zaehlen nicht mit (bewusst platziert).
// Betriebs- und Wartungshinweise:
// - Persistenz pro Browser via localStorage.
// - Keine Inline-Skripte (CSP): Bedienung ueber addEventListener.
// -----------------------------------------------------------------------------

(function () {
  var KEYS = ["theme", "accent", "view", "density", "cols", "lines", "r1", "r2", "r3", "rn", "max"];
  var DEFAULTS = { theme: "auto", accent: "blue", view: "signal", density: "comfortable",
                   cols: "auto", lines: "1", r1: "1", r2: "2", r3: "3", rn: "4", max: "0" };
  // Erlaubte Boxen-Zahlen pro Reihe (muessen 12 teilen -> Reihe fuellt exakt die Breite).
  var ALLOWED = { 1: 12, 2: 6, 3: 4, 4: 3, 6: 2 };
  var LAYOUT_KEYS = { view: 1, r1: 1, r2: 1, r3: 1, rn: 1, max: 1 };
  var root = document.documentElement;

  // 1) Gespeicherte Einstellungen (oder Defaults) sofort anwenden.
  KEYS.forEach(function (k) {
    var v = DEFAULTS[k];
    try { v = localStorage.getItem(k) || DEFAULTS[k]; } catch (e) {}
    root.setAttribute("data-" + k, v);
  });

  function mark(key, val) {
    var els = document.querySelectorAll("[data-" + key + "-set]");
    for (var i = 0; i < els.length; i++) {
      els[i].classList.toggle("active", els[i].getAttribute("data-" + key + "-set") === val);
    }
  }

  function num(key) {
    var v = parseInt(root.getAttribute("data-" + key), 10);
    return ALLOWED[v] ? v : parseInt(DEFAULTS[key], 10);
  }

  // Signalfeld: EINHEITLICHES Reihen-Layout ueber ALLE Kacheln (Grafana-Panels UND Items)
  // in Stream-Reihenfolge. Reihe 1 hat r1 Boxen, Reihe 2 r2, Reihe 3 r3, danach rn -- jede
  // Box spannt 12/Boxen, sodass jede Reihe exakt voll wird. Schmal: 1 pro Reihe.
  // Zusaetzlich (alle Ansichten): "Max. Eintraege" blendet NextUp-Karten jenseits des
  // Limits aus; Grafana-Panels bleiben immer sichtbar und zaehlen nicht mit.
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
      // Limit zuerst: ausgeblendete Karten belegen auch keinen Reihen-Slot.
      if (maxItems > 0 && !li.classList.contains("gpanel")) {
        shown++;
        if (shown > maxItems) { li.style.display = "none"; continue; }
      }
      li.style.display = "";
      if (!signal) { li.style.gridColumn = ""; continue; }
      if (w < 700) { li.style.gridColumn = "1 / -1"; continue; }
      var boxes = rowIdx < 3 ? rows[rowIdx] : rest;
      // Auf mittleren Screens die Boxen/Reihe deckeln (Lesbarkeit).
      if (w < 1100 && boxes > 3) boxes = 3;
      li.style.gridColumn = "span " + ALLOWED[boxes];
      inRow++;
      if (inRow >= boxes) { inRow = 0; rowIdx++; }
    }
  }

  // Nach dem sanften Nachladen der Item-Liste (dashboard.js) neu layouten koennen.
  window.compassRelayout = layoutSignal;

  function apply(key, val) {
    root.setAttribute("data-" + key, val);
    try { localStorage.setItem(key, val); } catch (e) {}
    mark(key, val);
    if (LAYOUT_KEYS[key]) layoutSignal();
  }

  // 2) Menue-Buttons verdrahten, sobald das DOM steht.
  document.addEventListener("DOMContentLoaded", function () {
    KEYS.forEach(function (key) {
      var els = document.querySelectorAll("[data-" + key + "-set]");
      for (var i = 0; i < els.length; i++) {
        (function (b) {
          b.addEventListener("click", function () { apply(key, b.getAttribute("data-" + key + "-set")); });
        })(els[i]);
      }
      mark(key, root.getAttribute("data-" + key) || DEFAULTS[key]);
    });

    layoutSignal();

    // Menue schliessen bei Klick ausserhalb.
    document.addEventListener("click", function (e) {
      var menu = document.querySelector("details.design-menu");
      if (menu && menu.open && !menu.contains(e.target)) menu.removeAttribute("open");
    });
  });

  // Bei Groessenaenderung neu layouten (Breakpoints), leicht entprellt.
  var rt;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(layoutSignal, 150);
  });

  // PWA: Service Worker registrieren (fuer Installierbarkeit auf iOS/Android).
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }
})();
