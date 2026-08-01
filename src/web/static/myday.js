// -----------------------------------------------------------------------------
// Skript: src/web/static/myday.js
// Autor: Torben <github@x-gate.de>
// Version: 1.0.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - "Mein Tag": kopiert den Antwort-Entwurf einer Aufgabe in die Zwischenablage.
// Betriebs- und Wartungshinweise:
// - CSP-konform (externes Skript, kein Inline). navigator.clipboard braucht einen
//   sicheren Kontext (HTTPS); als Fallback bleibt der Text im Textfeld markierbar.
// -----------------------------------------------------------------------------
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".myday-copy");
    if (!btn) return;
    var det = btn.closest(".myday-reply");
    var ta = det && det.querySelector(".myday-reply-text");
    if (!ta) return;
    ta.focus();
    ta.select();
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "Kopiert";
      setTimeout(function () { btn.textContent = old; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(ta.value).then(done, function () {
        // Fallback: alte execCommand-API, wenn die Clipboard-API scheitert.
        try { document.execCommand("copy"); done(); } catch (err) { /* Text bleibt markiert */ }
      });
    } else {
      try { document.execCommand("copy"); done(); } catch (err) { /* Text bleibt markiert */ }
    }
  });
})();
