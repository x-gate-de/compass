// -----------------------------------------------------------------------------
// Skript: src/web/static/dashboard.js
// Autor: Torben <github@x-gate.de>
// Version: 1.1.0
// Lizenz: AGPL-3.0-or-later — siehe LICENSE.
// Zweck:
// - Dashboard-Chatleiste: Konversationsliste (/api/conversations), Verlauf
//   (/api/older, /api/messages), Senden (/c/<jid>/send mit ajax=1), Gelesen-Status.
// - Inline-Antworten auf Chat-Karten im NextUp-Strom (Formular-Intercept).
// - News-Ticker-Laufband: Schlagzeilen via /api/ticker, Laufgeschwindigkeit
//   proportional zur Textbreite, Pause bei Hover (CSS).
// - Sanftes Nachladen der Item-Liste (ersetzt den frueheren Meta-Refresh), damit
//   getippter Text in Chatleiste/Inline-Antwort nicht verloren geht.
// Betriebs- und Wartungshinweise:
// - Kein Inline-JS (CSP); DOM-Aufbau nur ueber textContent (kein HTML-Injection-Risiko).
// - Zusammenspiel: theme.js exportiert window.compassRelayout fuer das Signalfeld.
// -----------------------------------------------------------------------------

(function () {
  var root = document.documentElement;
  var bar = document.getElementById("chatbar");

  // --- Hilfen ---------------------------------------------------------------

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function postForm(url, data) {
    var fd = new FormData();
    Object.keys(data || {}).forEach(function (k) { fd.append(k, data[k]); });
    return fetch(url, { method: "POST", body: fd, credentials: "same-origin" });
  }

  // JID sicher in den Pfad einer {partner:path}-Route einsetzen.
  function jidPath(base, jid) {
    return base + jid.split("/").map(encodeURIComponent).join("/");
  }

  // --- Chatleiste -------------------------------------------------------------

  if (bar) initChatbar();

  function initChatbar() {
    var toggle = document.getElementById("cb-toggle");
    var badge = document.getElementById("cb-badge");
    var listEl = document.getElementById("cb-list");
    var convEl = document.getElementById("cb-conv");
    var convName = document.getElementById("cb-conv-name");
    var convLink = document.getElementById("cb-conv-link");
    var msgsEl = document.getElementById("cb-msgs");
    var sendForm = document.getElementById("cb-send");
    var input = document.getElementById("cb-input");
    var backBtn = document.getElementById("cb-back");

    var state = { partner: null, isRoom: false, maxId: 0, msgTimer: null, listTimer: null };

    // Zustand (auf/zu) pro Browser merken; Attribut steuert das CSS.
    var open = "on";
    try { open = localStorage.getItem("chatbar") || "on"; } catch (e) {}
    root.setAttribute("data-chatbar", open);

    toggle.addEventListener("click", function () {
      var v = root.getAttribute("data-chatbar") === "off" ? "on" : "off";
      root.setAttribute("data-chatbar", v);
      try { localStorage.setItem("chatbar", v); } catch (e) {}
      // Signalfeld nutzt window.innerWidth-Breakpoints -> nach dem Umklappen neu layouten.
      if (window.compassRelayout) window.compassRelayout();
    });

    function setBadge(total) {
      if (total > 0) {
        badge.textContent = total > 99 ? "99+" : String(total);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    }

    function renderList(convs) {
      listEl.textContent = "";
      var total = 0;
      convs.forEach(function (c) { total += c.unread || 0; });
      setBadge(total);
      if (!convs.length) {
        listEl.appendChild(el("p", "muted cb-hint", "Keine Konversationen."));
        return;
      }
      convs.forEach(function (c) {
        var b = el("button", "cb-item" + (c.unread > 0 ? " cb-unread-item" : ""));
        b.type = "button";
        var av = el("span", "cb-av", c.has_avatar ? null : c.initials);
        av.style.background = "hsl(" + c.hue + " 45% 42%)";
        if (c.has_avatar) {
          var im = document.createElement("img");
          im.className = "cb-av-img";
          im.src = "/avatar/" + encodeURIComponent(c.partner);
          im.alt = "";
          av.appendChild(im);
        }
        b.appendChild(av);
        var mid = el("span", "cb-mid");
        var top = el("span", "cb-top");
        top.appendChild(el("span", "cb-name", c.name));
        if (c.unread > 0) top.appendChild(el("span", "cb-unread", c.unread > 99 ? "99+" : String(c.unread)));
        mid.appendChild(top);
        mid.appendChild(el("span", "cb-preview", c.preview || ""));
        b.appendChild(mid);
        b.addEventListener("click", function () { openConv(c.partner, c.name, c.is_room); });
        listEl.appendChild(b);
      });
    }

    function loadList() {
      getJSON("/api/conversations").then(function (d) {
        // Nur neu rendern, wenn die Liste sichtbar ist (sonst nur Badge aktualisieren).
        var convs = d.conversations || [];
        if (convEl.hidden) renderList(convs);
        else {
          var total = 0;
          convs.forEach(function (c) { total += c.unread || 0; });
          setBadge(total);
        }
      }).catch(function () {});
    }

    function renderMsg(m) {
      var d = el("div", "cb-msg " + (m.direction === "out" ? "out" : "in"));
      if (state.isRoom && m.direction !== "out" && m.sender) {
        d.appendChild(el("div", "cb-sender", m.sender));
      }
      if (!m.decrypted) {
        d.appendChild(el("em", "cb-enc", "Verschluesselte Nachricht"));
      } else if (m.media) {
        if (m.media.kind === "image") {
          var img = document.createElement("img");
          img.className = "cb-img";
          img.src = m.media.url;
          img.alt = m.media.name;
          img.loading = "lazy";
          d.appendChild(img);
        } else {
          var a = el("a", "cb-file", "📎 " + m.media.name);
          a.href = m.media.url;
          a.target = "_blank";
          a.rel = "noopener";
          d.appendChild(a);
        }
      } else {
        if (m.quote) d.appendChild(el("div", "cb-quote", m.quote));
        if (m.text) d.appendChild(el("div", "cb-text", m.text));
      }
      d.appendChild(el("div", "cb-ts", m.ts));
      return d;
    }

    function renderPending(pending) {
      // Alte Pending-Bubbles entfernen und aktuellen Stand neu anzeigen.
      msgsEl.querySelectorAll(".cb-pending").forEach(function (n) { n.remove(); });
      (pending || []).forEach(function (p) {
        var d = el("div", "cb-msg out cb-pending");
        d.appendChild(el("div", "cb-text", p.body || ""));
        d.appendChild(el("div", "cb-ts", p.status === "error" ? "Fehler: " + (p.error || "senden fehlgeschlagen") : "sendet …"));
        msgsEl.appendChild(d);
      });
    }

    function scrollDown() { msgsEl.scrollTop = msgsEl.scrollHeight; }

    function pollMessages() {
      if (!state.partner) return;
      getJSON(jidPath("/api/messages/", state.partner) + "?after_id=" + state.maxId).then(function (d) {
        var atBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 60;
        (d.messages || []).forEach(function (m) {
          msgsEl.insertBefore(renderMsg(m), msgsEl.querySelector(".cb-pending"));
          if (m.id > state.maxId) state.maxId = m.id;
        });
        renderPending(d.pending);
        if ((d.messages || []).length && atBottom) scrollDown();
      }).catch(function () {});
    }

    function openConv(partner, name, isRoom) {
      state.partner = partner;
      state.isRoom = !!isRoom;
      state.maxId = 0;
      convName.textContent = name;
      convLink.href = jidPath("/c/", partner);
      msgsEl.textContent = "";
      listEl.hidden = true;
      convEl.hidden = false;
      getJSON(jidPath("/api/older/", partner)).then(function (d) {
        (d.messages || []).forEach(function (m) {
          msgsEl.appendChild(renderMsg(m));
          if (m.id > state.maxId) state.maxId = m.id;
        });
        scrollDown();
        postForm(jidPath("/api/read/", partner), {}).catch(function () {});
      }).catch(function () {
        msgsEl.appendChild(el("p", "muted cb-hint", "Verlauf konnte nicht geladen werden."));
      });
      clearInterval(state.msgTimer);
      state.msgTimer = setInterval(pollMessages, 4000);
      input.focus();
    }

    function closeConv() {
      clearInterval(state.msgTimer);
      state.partner = null;
      convEl.hidden = true;
      listEl.hidden = false;
      loadList();
    }

    backBtn.addEventListener("click", closeConv);

    sendForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = input.value.trim();
      if (!text || !state.partner) return;
      input.value = "";
      postForm(jidPath("/c/", state.partner) + "/send", { body: text, ajax: "1" })
        .then(pollMessages)
        .catch(function () { input.value = text; });
    });

    loadList();
    state.listTimer = setInterval(loadList, 20000);
  }

  // --- Inline-Antworten auf Chat-Karten ---------------------------------------

  function bindInlineReplies(scope) {
    var forms = (scope || document).querySelectorAll("form.inline-reply");
    Array.prototype.forEach.call(forms, function (f) {
      if (f.dataset.bound) return;
      f.dataset.bound = "1";
      f.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var inp = f.querySelector("input[name=body]");
        var ok = f.querySelector(".ir-ok");
        var text = (inp.value || "").trim();
        if (!text) return;
        postForm(f.getAttribute("action"), { body: text, ajax: "1" }).then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          inp.value = "";
          if (ok) {
            ok.hidden = false;
            setTimeout(function () { ok.hidden = true; }, 3000);
          }
        }).catch(function () { inp.value = text; });
      });
    });
  }

  bindInlineReplies(document);

  // --- News-Ticker-Laufband ----------------------------------------------------

  var tickerTrack = document.getElementById("tk-track");
  var workTrack = document.getElementById("wt-track");

  // Nahtlose Schleife: Inhalt so oft klonen, bis er den Viewport fuellt, dann die
  // gesamte Haelfte einmal duplizieren -> Animation ueber -50% laeuft ohne Leerlauf.
  function loopTrack(track) {
    if (!track || !track.children.length) return;
    Array.prototype.slice.call(track.querySelectorAll("[data-clone]")).forEach(function (n) { n.remove(); });
    var vw = (track.parentElement && track.parentElement.clientWidth) || window.innerWidth;
    var originals = Array.prototype.slice.call(track.children);
    var guard = 0;
    while (track.scrollWidth < vw && guard < 20) {
      originals.forEach(function (n) {
        var c = n.cloneNode(true);
        c.setAttribute("data-clone", "1");
        c.setAttribute("aria-hidden", "true");
        track.appendChild(c);
      });
      guard++;
    }
    Array.prototype.slice.call(track.children).forEach(function (n) {
      var c = n.cloneNode(true);
      c.setAttribute("data-clone", "1");
      c.setAttribute("aria-hidden", "true");
      track.appendChild(c);
    });
    track.classList.add("tk-loop");
  }

  // Laufdauer an die Textbreite koppeln; Geschwindigkeit (px/s) aus den Settings
  // (data-speed am Track, per /api/worktime aktualisierbar).
  function tickerSpeed(track) {
    if (!track) return;
    var pps = parseInt(track.getAttribute("data-speed"), 10) || 90;
    var w = track.classList.contains("tk-loop") ? track.scrollWidth / 2 : track.scrollWidth;
    track.style.animationDuration = Math.max(8, Math.round(w / pps)) + "s";
  }

  function renderTicker(entries) {
    if (!tickerTrack) return;
    tickerTrack.textContent = "";
    entries.forEach(function (t) {
      var s = el("span", "tk-item");
      s.appendChild(el("b", "tk-team", t.team));
      s.appendChild(document.createTextNode(" " + (t.headline || "sammle Daten …")));
      if (t.count != null) s.appendChild(el("span", "tk-count", " (" + t.count + ")"));
      tickerTrack.appendChild(s);
    });
    loopTrack(tickerTrack);
    tickerSpeed(tickerTrack);
  }

  function renderWorktime(segs) {
    if (!workTrack) return;
    workTrack.textContent = "";
    segs.forEach(function (sg) {
      var s = el("span", "tk-item tk-" + (sg.kind || "plain"));
      s.appendChild(el("b", "tk-team", sg.label));
      if (sg.parts) {
        s.appendChild(document.createTextNode(" "));
        sg.parts.forEach(function (pt) {
          var sp = el("span", "", pt.t);
          // Farbwert kommt serverseitig validiert (Hex) aus den Settings.
          if (pt.c) sp.style.color = pt.c;
          s.appendChild(sp);
        });
      } else if (sg.text) {
        s.appendChild(document.createTextNode(" " + sg.text));
      }
      workTrack.appendChild(s);
    });
    loopTrack(workTrack);
    tickerSpeed(workTrack);
  }

  function pollTicker() {
    getJSON("/api/ticker").then(function (d) {
      if ((d.ticker || []).length) renderTicker(d.ticker);
    }).catch(function () {});
    if (workTrack) {
      getJSON("/api/worktime").then(function (d) {
        if (d.speed) workTrack.setAttribute("data-speed", String(d.speed));
        var band = document.getElementById("wticker");
        if (band) band.style.setProperty("--wtc", d.color || "");
        if ((d.segments || []).length) renderWorktime(d.segments);
      }).catch(function () {});
    }
  }

  if (tickerTrack || workTrack) {
    loopTrack(tickerTrack);
    loopTrack(workTrack);
    tickerSpeed(tickerTrack);
    tickerSpeed(workTrack);
    setInterval(pollTicker, 60000);
  }

  // --- Sanftes Nachladen der Item-Liste (statt Meta-Refresh) ------------------

  function refreshItems() {
    var main = document.querySelector(".dash-main");
    if (!main) return;
    // Nicht ersetzen, waehrend im Hauptbereich getippt wird oder Text ungesendet ist.
    var ae = document.activeElement;
    if (ae && main.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
    var dirty = Array.prototype.some.call(
      main.querySelectorAll(".inline-reply input"),
      function (i) { return (i.value || "").trim() !== ""; }
    );
    if (dirty) return;
    fetch(location.href, { credentials: "same-origin", headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var pairs = [[".dash-main .items", ".dash-main .items"],
                     [".topbar .dash-status", ".topbar .dash-status"]];
        pairs.forEach(function (p) {
          var neu = doc.querySelector(p[0]);
          var cur = document.querySelector(p[1]);
          if (neu && cur) cur.innerHTML = neu.innerHTML;
        });
        bindInlineReplies(main);
        if (window.compassRelayout) window.compassRelayout();
      })
      .catch(function () {});
  }

  setInterval(refreshItems, 60000);
})();
