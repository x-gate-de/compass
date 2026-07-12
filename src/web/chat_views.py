# -----------------------------------------------------------------------------
# Skript: src/web/chat_views.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Chat-Ansichten der vereinten Web-UI (aus x-gate_chat portiert): Konversationsliste,
#   Verlauf, Senden (Outbox), Live-/Aeltere-Nachrichten, Suche, Kontakte und Raeume.
#   Liest/schreibt das Account-eigene Archiv (users_dir/<slug>/messages.sqlite).
# - JSON-API fuer die Dashboard-Chatleiste: /api/conversations, /api/read, Senden
#   mit ajax=1 (JSON statt Redirect).
# Betriebs- und Wartungshinweise:
# - Zeigt entschluesselte private Nachrichten (Schutzbedarf HOCH).
# - Der Daemon (Bot) versendet aus der Outbox und entschluesselt eingehende Nachrichten;
#   die Web-UI schreibt nur Sendeauftraege und liest das Archiv.
# - OMEMO-Geraeteverwaltung, Media-Proxy und Web-Push folgen als eigene Scheibe.
# -----------------------------------------------------------------------------

import os
import re
import sqlite3
import ssl
import time
import urllib.request
import uuid
from datetime import datetime
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


def _open_ro(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _open_rw(db_path):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _initials(name, jid):
    base = (name or "").strip() or (jid or "").split("@")[0]
    parts = [p for p in base.replace(".", " ").replace("_", " ").replace("-", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (base[:2] or "?").upper()


def _hue(s):
    return sum(ord(c) for c in (s or "")) % 360


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Leere Nachrichten (Chat-States/Marker) ausblenden; unlesbare (decrypted=0) bleiben.
def _nonempty(prefix=""):
    return "(%sdecrypted = 0 OR (%sbody IS NOT NULL AND trim(%sbody) <> ''))" % (prefix, prefix, prefix)


# --- Anhaenge (OMEMO-Media, XEP-0454) ---------------------------------------
# Bilder/Dateien werden als "aesgcm://host/pfad#<iv+key-hex>" verschickt: die Datei
# liegt AES-256-GCM-verschluesselt auf dem HTTP-Upload-Server, Schluessel+IV im Fragment.
_MEDIA_IMG_EXT = ("jpg", "jpeg", "png", "gif", "webp", "bmp")
_MEDIA_CT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "svg": "image/svg+xml",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "ogg": "audio/ogg", "oga": "audio/ogg", "wav": "audio/wav",
    "pdf": "application/pdf", "txt": "text/plain",
}
_MEDIA_MAX = 30 * 1024 * 1024  # 30 MB Obergrenze pro Anhang


# Erkennt eine reine OMEMO-Media-URL als Anhang. Nur ein einzelnes URL-Token gilt als
# Anhang (URL + Freitext bleibt normaler Text). Rueckgabe: dict oder None.
def _media_info(body, msg_id):
    if not body:
        return None
    b = body.strip()
    if not b.lower().startswith("aesgcm://") or any(ch in b for ch in (" ", "\n", "\t")):
        return None
    name = b.split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "datei"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"url": "/media/%d" % msg_id, "name": name,
            "kind": "image" if ext in _MEDIA_IMG_EXT else "file"}


def _media_label(body):
    info = _media_info(body, 0)
    if not info:
        return None
    return "[Bild]" if info["kind"] == "image" else "[Anhang]"


def _account_domain(jid):
    return jid.split("@", 1)[1].lower() if "@" in jid else ""


# Spool-Verzeichnis je Account fuer zu sendende Anhaenge (Daemon liest + loescht).
def _account_spool_dir(archive_path):
    d = os.path.join(os.path.dirname(archive_path), "spool")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


# Laedt die verschluesselte Datei vom HTTP-Upload-Server und entschluesselt sie
# (AES-256-GCM). Schluessel+IV stammen aus dem URL-Fragment. SSRF-Schutz: nur von der
# eigenen XMPP-Domain laden. Schluessel bleiben serverseitig.
def _media_fetch_decrypt(body, allowed_domain):
    u = urlparse(body.strip())
    if u.scheme != "aesgcm" or not u.fragment or not u.netloc:
        raise HTTPException(status_code=404)
    host = (u.hostname or "").lower()
    if not allowed_domain or not (host == allowed_domain or host.endswith("." + allowed_domain)):
        raise HTTPException(status_code=403)
    try:
        raw = bytes.fromhex(u.fragment)
    except ValueError:
        raise HTTPException(status_code=404)
    if len(raw) < 33:  # mind. 1 Byte IV + 32 Byte Key
        raise HTTPException(status_code=404)
    key, iv = raw[-32:], raw[:-32]
    https = "https://%s%s" % (u.netloc, u.path)
    try:
        req = urllib.request.Request(https, headers={"User-Agent": "compass"})
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
            data = resp.read(_MEDIA_MAX + 1)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502)
    if len(data) > _MEDIA_MAX:
        raise HTTPException(status_code=413)
    try:
        plain = AESGCM(key).decrypt(iv, data, None)
    except Exception:
        raise HTTPException(status_code=502)
    ext = u.path.rsplit(".", 1)[-1].lower() if "." in u.path else ""
    return plain, _MEDIA_CT.get(ext, "application/octet-stream")


# --- OMEMO-Geraete / Verifizierung ------------------------------------------

def _devices(db_path, partner):
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT jid, device_id, fingerprint, identity_hex, trust, is_own, label FROM omemo_devices "
            "WHERE jid = ? OR is_own = 1 ORDER BY is_own DESC, device_id",
            (partner,),
        ).fetchall()
    finally:
        conn.close()
    return [{"jid": r["jid"], "device_id": r["device_id"], "fingerprint": r["fingerprint"],
             "identity_hex": r["identity_hex"], "trust": r["trust"], "is_own": bool(r["is_own"]),
             "label": r["label"]} for r in rows]


# Schreibt eine Geraete-Anfrage fuer den Daemon (refresh oder trust).
def _omemo_request_row(db_path, action, jid, identity_hex=None, trust_value=None):
    conn = _open_rw(db_path)
    try:
        conn.execute(
            "INSERT INTO omemo_requests (action, jid, identity_hex, trust_value, status, created_ts) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (action, jid, identity_hex, trust_value, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _preview(last_body, last_dir, last_dec):
    if not last_dec:
        text = "Verschluesselte Nachricht"
    else:
        text = _media_label(last_body) or ((last_body or "").replace("\n", " ").strip() or "(leer)")
    if len(text) > 60:
        text = text[:60] + "…"
    return ("Du: " + text) if last_dir == "out" else text


def _conv_items(db_path):
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT m.partner_jid AS partner, COUNT(*) AS cnt, MAX(m.ts_received) AS last_ts, "
            "  SUM(CASE WHEN m.direction = 'in' AND m.ts_received > "
            "      COALESCE((SELECT last_read_ts FROM read_state r WHERE r.partner_jid = m.partner_jid), 0) "
            "    THEN 1 ELSE 0 END) AS unread, "
            "  (SELECT body FROM messages x WHERE x.partner_jid = m.partner_jid AND " + _nonempty("x.") + " ORDER BY x.id DESC LIMIT 1) AS last_body, "
            "  (SELECT direction FROM messages x WHERE x.partner_jid = m.partner_jid AND " + _nonempty("x.") + " ORDER BY x.id DESC LIMIT 1) AS last_dir, "
            "  (SELECT decrypted FROM messages x WHERE x.partner_jid = m.partner_jid AND " + _nonempty("x.") + " ORDER BY x.id DESC LIMIT 1) AS last_dec, "
            "  (SELECT name FROM contacts c WHERE c.jid = m.partner_jid) AS contact_name, "
            "  (SELECT name FROM muc_available a WHERE a.room_jid = m.partner_jid) AS room_name, "
            "  EXISTS(SELECT 1 FROM mucs g WHERE g.room_jid = m.partner_jid) AS is_room, "
            "  EXISTS(SELECT 1 FROM avatars av WHERE av.jid = m.partner_jid AND length(av.data) > 0) AS has_avatar "
            "FROM messages m WHERE " + _nonempty("m.") + " GROUP BY m.partner_jid ORDER BY last_ts DESC"
        ).fetchall()
        # Beigetretene Raeume OHNE Nachrichten trotzdem zeigen: sonst wirkt ein
        # frischer Beitritt wie "nichts passiert" (Liste baut sonst nur auf messages).
        empty_rooms = conn.execute(
            "SELECT g.room_jid, COALESCE(NULLIF(g.name, ''), a.name, g.room_jid) AS name "
            "FROM mucs g LEFT JOIN muc_available a ON a.room_jid = g.room_jid "
            "WHERE g.joined = 1 AND g.room_jid NOT IN "
            "  (SELECT DISTINCT partner_jid FROM messages WHERE " + _nonempty() + ")"
        ).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        is_room = bool(r["is_room"])
        name = r["contact_name"] or r["room_name"] or r["partner"]
        items.append({
            "partner": r["partner"], "name": name, "count": r["cnt"], "last": _fmt_ts(r["last_ts"]),
            "unread": r["unread"], "is_room": is_room,
            "preview": _preview(r["last_body"], r["last_dir"], r["last_dec"]),
            "initials": _initials(name if name != r["partner"] else "", r["partner"]),
            "hue": _hue(r["partner"]),
            "has_avatar": bool(r["has_avatar"]),
        })
    for r in empty_rooms:
        items.append({
            "partner": r["room_jid"], "name": r["name"], "count": 0, "last": "-",
            "unread": 0, "is_room": True,
            "preview": "Beigetreten — Verlauf laedt/leer",
            "initials": _initials(r["name"] if r["name"] != r["room_jid"] else "", r["room_jid"]),
            "hue": _hue(r["room_jid"]), "has_avatar": False,
        })
    return items


def _is_room(conn, jid):
    return conn.execute(
        "SELECT 1 FROM mucs WHERE room_jid = ? UNION SELECT 1 FROM muc_available WHERE room_jid = ?",
        (jid, jid),
    ).fetchone() is not None


def _split_quote(body):
    if not body:
        return None, body
    lines = body.split("\n")
    i, qlines = 0, []
    while i < len(lines) and lines[i].startswith(">"):
        qlines.append(lines[i][1:].lstrip())
        i += 1
    if not qlines:
        return None, body
    return "\n".join(qlines), "\n".join(lines[i:]).lstrip("\n")


def _msg_dict(r):
    quote, text = _split_quote(r["body"])
    media = _media_info(r["body"], r["id"]) if r["decrypted"] else None
    if media:
        # Anhang ersetzt den (sonst als Rohtext sichtbaren) aesgcm-Link.
        quote, text = None, ""
    return {"id": r["id"], "direction": r["direction"], "quote": quote, "text": text,
            "media": media, "decrypted": bool(r["decrypted"]), "ts": _fmt_ts(r["ts_received"]),
            "ts_raw": r["ts_received"], "sender": r["sender"], "status": r["status"]}


def _messages(db_path, partner, after_id=0):
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT id, direction, body, decrypted, ts_received, sender, status FROM messages "
            "WHERE partner_jid = ? AND id > ? AND " + _nonempty() + " ORDER BY id ASC",
            (partner, after_id),
        ).fetchall()
    finally:
        conn.close()
    return [_msg_dict(r) for r in rows]


def _messages_page(db_path, partner, before_ts=None, before_id=None, limit=50):
    conn = _open_ro(db_path)
    try:
        if before_ts is None:
            rows = conn.execute(
                "SELECT id, direction, body, decrypted, ts_received, sender, status FROM messages "
                "WHERE partner_jid = ? AND " + _nonempty() + " ORDER BY ts_received DESC, id DESC LIMIT ?",
                (partner, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, direction, body, decrypted, ts_received, sender, status FROM messages "
                "WHERE partner_jid = ? AND (ts_received < ? OR (ts_received = ? AND id < ?)) "
                "AND " + _nonempty() + " ORDER BY ts_received DESC, id DESC LIMIT ?",
                (partner, before_ts, before_ts, before_id, limit),
            ).fetchall()
    finally:
        conn.close()
    has_more = len(rows) == limit
    return [_msg_dict(r) for r in reversed(rows)], has_more


def _pending(db_path, partner):
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT id, body, status, error FROM outbox WHERE recipient_jid = ? AND status IN ('pending','error') ORDER BY id",
            (partner,),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "body": r["body"], "status": r["status"], "error": r["error"]} for r in rows]


def _mark_read(db_path, partner):
    conn = _open_rw(db_path)
    try:
        conn.execute(
            "INSERT INTO read_state (partner_jid, last_read_ts) VALUES (?, ?) "
            "ON CONFLICT(partner_jid) DO UPDATE SET last_read_ts = excluded.last_read_ts",
            (partner, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _search(db_path, q, limit=100):
    pat = "%" + _like_escape(q) + "%"
    # Fuer das Treffer-Highlight: Snippet an den Fundstellen aufteilen (ungerade
    # Indizes = Treffer; das Template setzt dort <mark>).
    hi = re.compile("(%s)" % re.escape(q), re.IGNORECASE)
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT m.id, m.partner_jid, m.direction, m.body, m.ts_received, m.sender, "
            "  (SELECT name FROM contacts c WHERE c.jid = m.partner_jid) AS contact_name, "
            "  (SELECT name FROM muc_available a WHERE a.room_jid = m.partner_jid) AS room_name "
            "FROM messages m WHERE m.decrypted = 1 AND m.body LIKE ? ESCAPE '\\' "
            "ORDER BY m.ts_received DESC LIMIT ?",
            (pat, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        name = r["contact_name"] or r["room_name"] or r["partner_jid"]
        body = (r["body"] or "").replace("\n", " ")
        snippet = body[:200] + ("…" if len(body) > 200 else "")
        out.append({"partner": r["partner_jid"], "name": name, "ts": _fmt_ts(r["ts_received"]),
                    "direction": r["direction"], "sender": r["sender"],
                    "snippet": snippet, "parts": hi.split(snippet)})
    return out


# Registriert die Chat-Routen an der App. require_account/render/account_state stammen
# aus der Haupt-App (gemeinsame Auth + Templating).
def register_chat_routes(app, require_account, render, account_state):

    @app.get("/chat", response_class=HTMLResponse)
    def conversations(acc: dict = Depends(require_account)):
        return render("conversations.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), items=_conv_items(acc["archive_path"]))

    @app.get("/c/{partner:path}", response_class=HTMLResponse)
    def conversation(partner: str, acc: dict = Depends(require_account)):
        db_path = acc["archive_path"]
        conn = _open_ro(db_path)
        try:
            is_room = _is_room(conn, partner)
            row = conn.execute("SELECT name FROM contacts WHERE jid = ?", (partner,)).fetchone()
            contact_name = row["name"] if row else None
            rrow = conn.execute("SELECT name FROM muc_available WHERE room_jid = ?", (partner,)).fetchone()
            room_name = rrow["name"] if rrow else None
            arow = conn.execute("SELECT length(data) AS n FROM avatars WHERE jid = ?", (partner,)).fetchone()
            has_avatar = bool(arow and arow["n"])
        finally:
            conn.close()
        name = (room_name if is_room else contact_name) or partner
        messages, has_more = _messages_page(db_path, partner)
        max_id = max((m["id"] for m in messages), default=0)
        oldest = messages[0] if messages else None
        _mark_read(db_path, partner)
        return render("conversation.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), partner=partner, name=name,
                      messages=messages, max_id=max_id, pending=_pending(db_path, partner),
                      oldest_ts=(oldest["ts_raw"] if oldest else 0),
                      oldest_id=(oldest["id"] if oldest else 0), has_more=has_more,
                      is_room=is_room, initials=_initials(name if name != partner else "", partner),
                      hue=_hue(partner), has_avatar=has_avatar)

    @app.post("/c/{partner:path}/send")
    def send_message(partner: str, body: str = Form(""), quote: str = Form(""), ajax: str = Form(""),
                     file: UploadFile = File(None), acc: dict = Depends(require_account)):
        db_path = acc["archive_path"]
        text = (body or "").strip()
        quote = (quote or "").strip()

        # Anhang: nur fuer 1:1 (OMEMO-Media). In Raeumen (unverschluesselt) nicht unterstuetzt.
        if file is not None and (file.filename or ""):
            conn = _open_ro(db_path)
            try:
                is_room = _is_room(conn, partner)
            finally:
                conn.close()
            data = file.file.read(_MEDIA_MAX + 1)
            if not is_room and data and len(data) <= _MEDIA_MAX:
                spool = _account_spool_dir(db_path)
                name = os.path.basename(file.filename) or "datei"
                path = os.path.join(spool, uuid.uuid4().hex + os.path.splitext(name)[1])
                with open(path, "wb") as out:
                    out.write(data)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                conn = _open_rw(db_path)
                try:
                    conn.execute(
                        "INSERT INTO outbox (recipient_jid, body, status, created_ts, kind, "
                        "media_path, media_name, media_mime) VALUES (?, ?, 'pending', ?, 'media', ?, ?, ?)",
                        (partner, name, time.time(), path, name, file.content_type or "application/octet-stream"),
                    )
                    conn.commit()
                finally:
                    conn.close()

        if text and quote:
            text = "\n".join("> " + ln for ln in quote.split("\n")) + "\n" + text
        if text:
            conn = _open_rw(db_path)
            try:
                kind = "groupchat" if _is_room(conn, partner) else "chat"
                conn.execute(
                    "INSERT INTO outbox (recipient_jid, body, status, created_ts, kind) VALUES (?, ?, 'pending', ?, ?)",
                    (partner, text, time.time(), kind),
                )
                conn.commit()
            finally:
                conn.close()
        # ajax=1: Aufruf aus der Dashboard-Chatleiste/Inline-Antwort -> JSON statt Redirect.
        if ajax:
            return JSONResponse({"ok": True})
        return RedirectResponse(url=f"/c/{partner}", status_code=status.HTTP_303_SEE_OTHER)

    # Entschluesselter Media-Proxy: Body der eigenen Nachricht lesen (Auth ueber Session),
    # verschluesselte Datei holen und Klartext ausliefern. Schluessel bleiben serverseitig.
    @app.get("/media/{msg_id:int}")
    def media(msg_id: int, acc: dict = Depends(require_account)):
        conn = _open_ro(acc["archive_path"])
        try:
            row = conn.execute("SELECT body, decrypted FROM messages WHERE id = ?", (msg_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["decrypted"] or not row["body"]:
            raise HTTPException(status_code=404)
        data, ctype = _media_fetch_decrypt(row["body"], _account_domain(acc["jid"]))
        return Response(content=data, media_type=ctype, headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        })

    # --- OMEMO-Geraete / Verifizierung ---------------------------------------

    @app.get("/devices/{partner:path}", response_class=HTMLResponse)
    def devices(partner: str, acc: dict = Depends(require_account)):
        _omemo_request_row(acc["archive_path"], "refresh", partner)  # frische Daten anstossen
        return render("devices.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), partner=partner,
                      devices=_devices(acc["archive_path"], partner))

    @app.get("/api/devices/{partner:path}")
    def api_devices(partner: str, acc: dict = Depends(require_account)):
        return _devices(acc["archive_path"], partner)

    @app.post("/devices/{partner:path}/trust")
    def devices_trust(partner: str, identity_hex: str = Form(...), value: str = Form(...),
                      acc: dict = Depends(require_account)):
        if value in ("verify", "distrust") and identity_hex:
            _omemo_request_row(acc["archive_path"], "trust", partner,
                               identity_hex=identity_hex, trust_value=value)
        return JSONResponse({"ok": True})

    # --- Web Push -------------------------------------------------------------

    push = (app.state.cfg.get("push") or {})
    push_public = push.get("vapid_public_key") or ""
    push_enabled = bool(push_public and (push.get("vapid_private_key") or ""))

    @app.get("/api/push/config")
    def push_config(acc: dict = Depends(require_account)):
        return {"enabled": push_enabled, "publicKey": push_public}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, acc: dict = Depends(require_account)):
        sub = await request.json()
        endpoint = (sub or {}).get("endpoint")
        keys = (sub or {}).get("keys") or {}
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if not (endpoint and p256dh and auth):
            raise HTTPException(status_code=400)
        conn = _open_rw(acc["archive_path"])
        try:
            conn.execute(
                "INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
                (endpoint, p256dh, auth, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    @app.get("/api/push/pref/{partner:path}")
    def push_pref_get(partner: str, acc: dict = Depends(require_account)):
        conn = _open_ro(acc["archive_path"])
        try:
            row = conn.execute("SELECT enabled FROM push_prefs WHERE partner_jid = ?", (partner,)).fetchone()
            subs = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
        finally:
            conn.close()
        return {"enabled": bool(row and row["enabled"]), "subscribed": bool(subs), "push": push_enabled}

    @app.post("/api/push/pref/{partner:path}")
    def push_pref_set(partner: str, value: str = Form(...), acc: dict = Depends(require_account)):
        conn = _open_rw(acc["archive_path"])
        try:
            conn.execute(
                "INSERT INTO push_prefs (partner_jid, enabled) VALUES (?, ?) "
                "ON CONFLICT(partner_jid) DO UPDATE SET enabled = excluded.enabled",
                (partner, 1 if value == "1" else 0),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    @app.get("/api/messages/{partner:path}")
    def api_messages(partner: str, after_id: int = 0, acc: dict = Depends(require_account)):
        db_path = acc["archive_path"]
        msgs = _messages(db_path, partner, after_id)
        if msgs:
            _mark_read(db_path, partner)
        return {"messages": msgs, "pending": _pending(db_path, partner)}

    # Konversationsliste als JSON (Dashboard-Chatleiste).
    @app.get("/api/conversations")
    def api_conversations(acc: dict = Depends(require_account)):
        return {"conversations": _conv_items(acc["archive_path"])}

    # Als gelesen markieren, ohne den ganzen Verlauf zu laden (Chatleiste beim Oeffnen).
    @app.post("/api/read/{partner:path}")
    def api_read(partner: str, acc: dict = Depends(require_account)):
        _mark_read(acc["archive_path"], partner)
        return {"ok": True}

    @app.get("/api/older/{partner:path}")
    def api_older(partner: str, before_ts: float = 0, before_id: int = 0,
                  acc: dict = Depends(require_account)):
        msgs, has_more = _messages_page(acc["archive_path"], partner,
                                        before_ts=before_ts if before_ts else None, before_id=before_id)
        return {"messages": msgs, "has_more": has_more}

    # Fordert das Nachladen aelterer Nachrichten (MAM) an (Daemon arbeitet es ab).
    @app.post("/c/{partner:path}/loadmore")
    def load_more(partner: str, acc: dict = Depends(require_account)):
        conn = _open_rw(acc["archive_path"])
        try:
            kind = "muc" if _is_room(conn, partner) else "chat"
            conn.execute(
                "INSERT INTO mam_requests (target_jid, kind, status, created_ts) VALUES (?, ?, 'pending', ?)",
                (partner, kind, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse(url=f"/c/{partner}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/new")
    def new_conversation(partner: str = Form(...), acc: dict = Depends(require_account)):
        target = (partner or "").strip()
        if not target:
            return RedirectResponse(url="/chat", status_code=status.HTTP_303_SEE_OTHER)
        return RedirectResponse(url=f"/c/{target}", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/search", response_class=HTMLResponse)
    def search(q: str = "", acc: dict = Depends(require_account)):
        q = (q or "").strip()
        results = _search(acc["archive_path"], q) if len(q) >= 2 else []
        return render("search.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), q=q, results=results)

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts(acc: dict = Depends(require_account)):
        conn = _open_ro(acc["archive_path"])
        try:
            rows = conn.execute(
                "SELECT jid, name, EXISTS(SELECT 1 FROM avatars av WHERE av.jid = contacts.jid "
                "AND length(av.data) > 0) AS has_avatar "
                "FROM contacts ORDER BY (name = '' OR name IS NULL), LOWER(name), jid"
            ).fetchall()
        finally:
            conn.close()
        items = [{"jid": r["jid"], "name": r["name"] or r["jid"],
                  "initials": _initials(r["name"], r["jid"]), "hue": _hue(r["jid"]),
                  "has_avatar": bool(r["has_avatar"])} for r in rows]
        return render("contacts.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), items=items)

    @app.get("/rooms", response_class=HTMLResponse)
    def rooms(acc: dict = Depends(require_account)):
        conn = _open_ro(acc["archive_path"])
        try:
            joined = conn.execute(
                "SELECT room_jid, name FROM mucs WHERE joined = 1 ORDER BY LOWER(COALESCE(name, room_jid))"
            ).fetchall()
            joined_set = {r["room_jid"] for r in joined}
            available = conn.execute(
                "SELECT room_jid, name FROM muc_available ORDER BY LOWER(COALESCE(name, room_jid))"
            ).fetchall()
        finally:
            conn.close()
        joined_items = [{"jid": r["room_jid"], "name": r["name"] or r["room_jid"]} for r in joined]
        avail_items = [{"jid": r["room_jid"], "name": r["name"] or r["room_jid"],
                        "joined": r["room_jid"] in joined_set} for r in available]
        return render("rooms.html", nav_active="chat", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), joined=joined_items, available=avail_items)

    @app.post("/rooms/join")
    def join_room(room_jid: str = Form(...), acc: dict = Depends(require_account)):
        target = (room_jid or "").strip()
        if target:
            conn = _open_rw(acc["archive_path"])
            try:
                row = conn.execute("SELECT name FROM muc_available WHERE room_jid = ?", (target,)).fetchone()
                name = row[0] if row else None
                conn.execute(
                    "INSERT INTO mucs (room_jid, name, nick, joined) VALUES (?, ?, NULL, 1) "
                    "ON CONFLICT(room_jid) DO UPDATE SET joined = 1",
                    (target, name),
                )
                # Direkt Verlauf vom Server nachladen (MAM): ohne das wirkt ein
                # frisch beigetretener Raum leer ("Join funktioniert nicht").
                conn.execute(
                    "INSERT INTO mam_requests (target_jid, kind, status, created_ts) "
                    "VALUES (?, 'muc', 'pending', ?)",
                    (target, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        return RedirectResponse(url=f"/c/{target}", status_code=status.HTTP_303_SEE_OTHER)

    # Raum verlassen: joined-Markierung entfernen; der Daemon verlaesst den Raum
    # daraufhin per XMPP (_leave_stale_rooms). Archivierte Nachrichten bleiben.
    @app.post("/rooms/leave")
    def leave_room(room_jid: str = Form(...), acc: dict = Depends(require_account)):
        target = (room_jid or "").strip()
        if target:
            conn = _open_rw(acc["archive_path"])
            try:
                conn.execute("UPDATE mucs SET joined = 0 WHERE room_jid = ?", (target,))
                conn.commit()
            finally:
                conn.close()
        return RedirectResponse(url="/rooms", status_code=status.HTTP_303_SEE_OTHER)

    # Fehlgeschlagenen Sendeauftrag verwerfen (nur status=error; laufende bleiben).
    @app.post("/c/{partner:path}/dismiss/{outbox_id}")
    def dismiss_outbox(partner: str, outbox_id: int, acc: dict = Depends(require_account)):
        conn = _open_rw(acc["archive_path"])
        try:
            conn.execute("DELETE FROM outbox WHERE id = ? AND recipient_jid = ? AND status = 'error'",
                         (outbox_id, partner))
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse(url=f"/c/{partner}", status_code=status.HTTP_303_SEE_OTHER)

    # vCard-Avatar ausliefern (der Daemon holt und speichert die Fotos).
    @app.get("/avatar/{jid:path}")
    def avatar(jid: str, acc: dict = Depends(require_account)):
        conn = _open_ro(acc["archive_path"])
        try:
            row = conn.execute("SELECT mime, data FROM avatars WHERE jid = ?", (jid,)).fetchone()
        finally:
            conn.close()
        if not row or not row["data"]:
            raise HTTPException(status_code=404)
        return Response(content=row["data"], media_type=row["mime"] or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600",
                                 "X-Content-Type-Options": "nosniff"})

    # --- Konto ---------------------------------------------------------------

    registry = app.state.registry

    # "Immer online" umschalten: enabled steuert, ob der Daemon die XMPP-Verbindung
    # haelt (Archivierung im Hintergrund). Klick auf den Status in der Kopfzeile.
    @app.post("/account/online")
    def account_online(request: Request, acc: dict = Depends(require_account)):
        st = registry.get_state(acc["jid"])
        registry.set_enabled(acc["jid"], not (st and st["enabled"]))
        return RedirectResponse(request.headers.get("referer", "/"),
                                status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/api/account_status")
    def api_account_status(acc: dict = Depends(require_account)):
        return account_state(acc["jid"])

    # Account-Loeschung: zweistufig (Bestaetigungsseite -> Vormerkung). Der Daemon
    # trennt die Verbindung, loescht Archiv/OMEMO-State und die Aggregator-Daten.
    @app.get("/settings/delete", response_class=HTMLResponse)
    def delete_confirm(acc: dict = Depends(require_account)):
        return render("settings_delete.html", nav_active="", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]))

    @app.post("/settings/delete")
    def delete_account(request: Request, acc: dict = Depends(require_account)):
        registry.request_deletion(acc["jid"])
        request.session.clear()
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
