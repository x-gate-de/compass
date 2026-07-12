// -----------------------------------------------------------------------------
// Datei: static/app.js
// Autor: Torben <github@x-gate.de>
// Version: 1.1.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck: Live-Aktualisierung des Verlaufs, Web-Push-Abo/-Umschalter und
//        automatischer Refresh der OMEMO-Geraeteliste. Kein externes CDN.
//        Zitieren/Antworten, Bild-Lightbox, Anhang-Komfort (Auto-Senden bei
//        Dateiwahl, Drag&Drop mit 30-MB-Grenze).
// -----------------------------------------------------------------------------
(function () {
  "use strict";

  // Sicheres Anhaengen von Text (kein innerHTML mit Fremdinhalt).
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // --- Live-Verlauf ---------------------------------------------------------
  function renderMessage(box, m, isRoom) {
    var wrap = el("div", "m m-" + m.direction + (m.decrypted ? "" : " m-enc"));
    if (m.sender && m.direction === "in" && isRoom) wrap.appendChild(el("div", "m-sender", m.sender));
    if (m.quote) wrap.appendChild(el("div", "m-quote", m.quote));
    if (m.media) {
      var a = el("a", m.media.kind === "image" ? "" : "m-file");
      a.href = m.media.url; a.target = "_blank"; a.rel = "noopener";
      if (m.media.kind === "image") {
        var img = el("img", "m-img"); img.src = m.media.url; img.alt = m.media.name; a.appendChild(img);
      } else { a.textContent = "📎 " + m.media.name; }
      wrap.appendChild(a);
    } else {
      wrap.appendChild(el("div", "m-text", m.decrypted ? m.text : "[verschluesselt]"));
    }
    var meta = m.ts + (m.direction === "out" && m.status ? " · " + m.status : "");
    wrap.appendChild(el("div", "m-meta", meta));
    // Zitieren-Knopf wie beim serverseitigen Rendern (_message.html).
    if (m.decrypted) {
      var rb = el("button", "m-reply", "↪");
      rb.type = "button";
      rb.title = "Zitieren/Antworten";
      rb.setAttribute("data-reply", (m.text || (m.media && m.media.name) || "").slice(0, 160));
      wrap.appendChild(rb);
    }
    box.appendChild(wrap);
  }

  function startLive() {
    var box = document.getElementById("messages");
    if (!box) return;
    var partner = box.getAttribute("data-partner");
    var isRoom = box.getAttribute("data-is-room") === "1";
    var atBottom = true;
    box.addEventListener("scroll", function () {
      atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    });
    box.scrollTop = box.scrollHeight;

    async function tick() {
      var maxId = parseInt(box.getAttribute("data-max") || "0", 10);
      try {
        var r = await fetch("/api/messages/" + encodeURIComponent(partner) + "?after_id=" + maxId,
          { cache: "no-store" });
        if (r.ok) {
          var j = await r.json();
          if (j.messages && j.messages.length) {
            j.messages.forEach(function (m) { renderMessage(box, m, isRoom); if (m.id > maxId) maxId = m.id; });
            box.setAttribute("data-max", String(maxId));
            if (atBottom) box.scrollTop = box.scrollHeight;
          }
        }
      } catch (e) { /* still weiter versuchen */ }
      setTimeout(tick, 4000);
    }
    setTimeout(tick, 4000);
  }

  // --- Web Push -------------------------------------------------------------
  function urlB64ToUint8(base64) {
    var pad = "=".repeat((4 - base64.length % 4) % 4);
    var b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(b64), arr = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  async function ensureSubscription(publicKey) {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    var reg = await navigator.serviceWorker.register("/sw.js");
    var perm = await Notification.requestPermission();
    if (perm !== "granted") return false;
    var sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: urlB64ToUint8(publicKey),
      });
    }
    await fetch("/api/push/subscribe", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub),
    });
    return true;
  }

  async function startPush() {
    var btn = document.getElementById("push-toggle");
    if (!btn) return;
    var partner = btn.getAttribute("data-partner");
    var cfg = await (await fetch("/api/push/config", { cache: "no-store" })).json().catch(function () { return {}; });
    if (!cfg.enabled) return;  // Push serverseitig nicht konfiguriert
    btn.hidden = false;
    var pref = await (await fetch("/api/push/pref/" + encodeURIComponent(partner), { cache: "no-store" })).json();
    function paint(on) { btn.textContent = on ? "🔔 Push an" : "🔕 Push aus"; btn.dataset.on = on ? "1" : "0"; }
    paint(!!pref.enabled);
    btn.addEventListener("click", async function () {
      var turnOn = btn.dataset.on !== "1";
      if (turnOn) { var ok = await ensureSubscription(cfg.publicKey); if (!ok) return; }
      var fd = new FormData(); fd.append("value", turnOn ? "1" : "0");
      await fetch("/api/push/pref/" + encodeURIComponent(partner), { method: "POST", body: fd });
      paint(turnOn);
    });
  }

  // --- OMEMO-Geraeteliste (Auto-Refresh, da der Daemon sie asynchron fuellt) --
  function startDevices() {
    var list = document.getElementById("devlist");
    if (!list) return;
    var partner = list.getAttribute("data-partner");
    var tries = 0;
    async function refresh() {
      tries++;
      try {
        var devs = await (await fetch("/api/devices/" + encodeURIComponent(partner), { cache: "no-store" })).json();
        if (devs && devs.length) { location.reload(); return; }  // erstmalige Daten -> serverseitig rendern
      } catch (e) { /* ignore */ }
      if (tries < 6) setTimeout(refresh, 3000);
    }
    // Nur pollen, wenn noch keine Geraete gerendert sind.
    if (!list.querySelector(".dev")) setTimeout(refresh, 3000);
  }

  // --- Zitieren/Antworten -----------------------------------------------------
  function startReply() {
    var bar = document.getElementById("quote-bar");
    var field = document.getElementById("quote-field");
    var text = document.getElementById("quote-text");
    var body = document.getElementById("send-body");
    if (!bar || !field) return;
    // Delegiert: funktioniert auch fuer live nachgeladene Nachrichten.
    document.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest(".m-reply") : null;
      if (!btn) return;
      var q = btn.getAttribute("data-reply") || "";
      field.value = q;
      text.textContent = q;
      bar.hidden = false;
      if (body) body.focus();
    });
    var cancel = document.getElementById("quote-cancel");
    if (cancel) cancel.addEventListener("click", function () {
      field.value = ""; text.textContent = ""; bar.hidden = true;
    });
  }

  // --- Bild-Lightbox ------------------------------------------------------------
  function startLightbox() {
    document.addEventListener("click", function (e) {
      if (e.target && e.target.classList && e.target.classList.contains("m-img")) {
        e.preventDefault();
        var ov = el("div", "lightbox");
        var img = el("img");
        img.src = e.target.src;
        ov.appendChild(img);
        ov.addEventListener("click", function () { ov.remove(); });
        document.body.appendChild(ov);
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        var ov = document.querySelector(".lightbox");
        if (ov) ov.remove();
      }
    });
  }

  // --- Anhang-Komfort: Auto-Senden + Drag&Drop ---------------------------------
  var MEDIA_MAX = 30 * 1024 * 1024;

  function sendErr(msg) {
    var e = document.getElementById("send-err");
    if (!e) return;
    e.textContent = msg;
    e.hidden = false;
    setTimeout(function () { e.hidden = true; }, 6000);
  }

  function startAttach() {
    var form = document.getElementById("send-form");
    var input = document.getElementById("send-file");
    if (!form || !input) return;
    // Dateiwahl ueber die Bueroklammer: direkt senden (wie im Original).
    input.addEventListener("change", function () {
      if (!input.files.length) return;
      if (input.files[0].size > MEDIA_MAX) {
        sendErr("Datei zu gross (max. 30 MB)");
        input.value = "";
        return;
      }
      form.submit();
    });
    // Drag&Drop auf die ganze Konversation.
    var overlay = document.getElementById("drop-overlay");
    if (!overlay) return;
    var depth = 0;
    document.addEventListener("dragenter", function (e) {
      if (e.dataTransfer && Array.prototype.includes.call(e.dataTransfer.types, "Files")) {
        depth++; overlay.hidden = false;
      }
    });
    document.addEventListener("dragleave", function () {
      depth = Math.max(0, depth - 1);
      if (depth === 0) overlay.hidden = true;
    });
    document.addEventListener("dragover", function (e) { e.preventDefault(); });
    document.addEventListener("drop", function (e) {
      e.preventDefault();
      depth = 0; overlay.hidden = true;
      var files = e.dataTransfer ? e.dataTransfer.files : null;
      if (!files || !files.length) return;
      if (files[0].size > MEDIA_MAX) { sendErr("Datei zu gross (max. 30 MB)"); return; }
      var dt = new DataTransfer();
      dt.items.add(files[0]);
      input.files = dt.files;
      form.submit();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    startLive(); startPush(); startDevices();
    startReply(); startLightbox(); startAttach();
  });
})();
