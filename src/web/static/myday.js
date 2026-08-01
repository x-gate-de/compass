// -----------------------------------------------------------------------------
// Skript: src/web/static/myday.js
// Autor: Torben <github@x-gate.de>
// Version: 1.1.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - "Mein Tag": kopiert den Antwort-Entwurf einer Aufgabe in die Zwischenablage
//   ("Kopieren") bzw. kopiert ihn UND oeffnet die Odoo-Aufgabe ("Als Notiz in Odoo
//   oeffnen"), damit der Entwurf dort als Log-Notiz eingefuegt werden kann.
// Betriebs- und Wartungshinweise:
// - compass schreibt NICHT nach Odoo (Quellsysteme read-only). Der Knopf oeffnet nur
//   die Aufgabe und legt den Text in die Zwischenablage; Einfuegen/Speichern macht der
//   Nutzer. Odoo bietet keine URL-Vorbefuellung der Notiz.
// - CSP-konform (externes Skript). navigator.clipboard braucht einen sicheren Kontext
//   (HTTPS); Fallback: execCommand. Das Fenster wird SYNCHRON im Klick geoeffnet
//   (sonst greift der Popup-Blocker), erst danach kopiert.
// -----------------------------------------------------------------------------
(function () {
  "use strict";

  function flash(btn) {
    var old = btn.textContent;
    btn.textContent = "Kopiert";
    setTimeout(function () { btn.textContent = old; }, 1500);
  }

  function copyText(ta, btn) {
    if (!ta) return;
    ta.focus();
    ta.select();
    var ok = function () { if (btn) flash(btn); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(ta.value).then(ok, function () {
        try { document.execCommand("copy"); ok(); } catch (err) { /* Text bleibt markiert */ }
      });
    } else {
      try { document.execCommand("copy"); ok(); } catch (err) { /* Text bleibt markiert */ }
    }
  }

  document.addEventListener("click", function (e) {
    var openBtn = e.target.closest(".myday-open");
    if (openBtn) {
      var url = openBtn.getAttribute("data-open-url");
      // Fenster zuerst (im Klick-Gesture) oeffnen, sonst blockiert der Popup-Blocker.
      if (url) window.open(url, "_blank", "noopener");
      var det1 = openBtn.closest(".myday-reply");
      copyText(det1 && det1.querySelector(".myday-reply-text"), openBtn);
      return;
    }
    var copyBtn = e.target.closest(".myday-copy");
    if (copyBtn) {
      var det2 = copyBtn.closest(".myday-reply");
      copyText(det2 && det2.querySelector(".myday-reply-text"), copyBtn);
    }
  });
})();
