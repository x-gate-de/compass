# -----------------------------------------------------------------------------
# Skript: src/web/app.py
# Autor: Torben <github@x-gate.de>
# Version: 1.3.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Vereinte compass-Web-UI (FastAPI): EIN Login (XMPP-Bind) und eine Navigations-Shell
#   ueber die Bereiche NextUp (priorisierte Liste), Chat und Grafana (Dashboard-Embed).
# Ablauf:
# - Login validiert XMPP-Zugangsdaten ueber den Daemon-Manager (echte XMPP-Verbindung),
#   legt den Account in der Registry an (Passwort Fernet-verschluesselt) und setzt eine
#   signierte Session (session["jid"]). accounts.id dient als user_id fuer den Aggregator.
# Betriebs- und Wartungshinweise:
# - Zeigt entschluesselte Nachrichten und priorisierte Inhalte (Schutzbedarf HOCH).
# - Bindet nur an 127.0.0.1; extern nur ueber nginx -> traefik. Session-Cookie nur ueber
#   HTTPS (web.session_https_only). Grafana wird per iframe eingebettet (CSP: frame-src).
# - Diese Datei enthaelt die Shell + NextUp- und Grafana-Bereiche. Die Chat-Ansichten und
#   die Feed-Verwaltung werden in einem eigenen Modul ergaenzt (siehe SPEC F2/F5).
# -----------------------------------------------------------------------------

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import jinja2
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import db as _db
from .. import store
from ..accounts import AccountRegistry
from ..config import cfg_get
from ..calendar_feed import active_now, classify, covering_today, discover_calendars
from ..connectors.odoo import list_helpdesk_teams
from ..connectors.registry import available_types, get_connector
from ..db import connect as db_connect
from ..worktime import build_segments as worktime_segments
from .chat_views import register_chat_routes

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES = os.path.join(_HERE, "templates")
_STATIC = os.path.join(_HERE, "static")

# Wird auch als Cache-Buster fuer statische Assets genutzt (?v=...) ->
# bei Aenderungen an style.css/theme.js/app.js/dashboard.js hochzaehlen.
APP_VERSION = "1.4.6"


class NotAuthenticated(Exception):
    pass


# Baut die FastAPI-Anwendung aus der geladenen Konfiguration.
def create_app(cfg):
    app = FastAPI(title="compass", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db_path = cfg_get(cfg, "database.path", "compass.db")
    app.state.fernet_key = cfg_get(cfg, "security.fernet_key")
    app.state.registry = AccountRegistry(
        cfg["accounts"]["db_path"], cfg["security"]["fernet_key"], cfg["accounts"]["users_dir"]
    )
    app.state.grafana_url = (cfg_get(cfg, "grafana.base_url", "") or "").rstrip("/")
    # Arbeitszeit-Laufband nur zeigen, wenn die Zeiterfassung konfiguriert ist.
    app.state.worktime_enabled = bool(cfg_get(cfg, "worktime.base_url", "")
                                      and cfg_get(cfg, "worktime.api_key", ""))
    app.state.grafana_tls_verify = bool(cfg_get(cfg, "grafana.tls_verify", True))
    # Kurzlebiger Cache gerenderter Grafana-Panel-Bilder (panel_id -> (ts, bytes, ctype)).
    app.state.panel_cache = {}
    # Cache der Betriebs-Kachel (noc-Auswertung, 5 Minuten).
    app.state.ops_cache = {}

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg_get(cfg, "security.session_secret", "change-me"),
        https_only=bool(cfg_get(cfg, "web.session_https_only", True)),
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES),
        autoescape=True,
    )
    env.globals["app_version"] = APP_VERSION
    env.globals["grafana_configured"] = bool(app.state.grafana_url)
    env.filters["dt"] = lambda t: time.strftime("%d.%m.%Y %H:%M", time.localtime(t)) if t else "-"
    env.filters["ago"] = _ago
    app.state.env = env

    # Login-Haertung (Brute-Force / Schutz des XMPP-Servers) -- Zustand je App-Instanz.
    sec = cfg.get("security") or {}
    app.state.login_cfg = {
        "window": int(sec.get("login_window", 300)),
        "max_per_ip": int(sec.get("login_max_per_ip", 5)),
        "val_window": int(sec.get("validation_window", 60)),
        "val_max": int(sec.get("validation_max", 5)),
        "allowed_domains": [d.strip().lower() for d in (cfg["xmpp"].get("allowed_domains") or []) if d and d.strip()],
    }
    app.state.login_lock = threading.Lock()
    app.state.login_attempts = {}
    app.state.validation_times = []

    _register_routes(app)
    return app


# Unix-Zeitstempel -> relative Angabe ("vor 12s" / "in 3min"); fuer die Status-Anzeige.
def _ago(t):
    if not t:
        return "-"
    d = time.time() - t
    future = d < 0
    d = abs(d)
    if d < 60:
        s = f"{int(d)}s"
    elif d < 3600:
        s = f"{int(d // 60)}min"
    elif d < 86400:
        s = f"{int(d // 3600)}h"
    else:
        s = f"{int(d // 86400)}d"
    return ("in " + s) if future else ("vor " + s)


def _client_ip(request):
    # Echte Client-IP hinter traefik/nginx (linkester Eintrag in X-Forwarded-For).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _clamp_prio(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 3
    return 1 if value < 1 else 5 if value > 5 else value


# Wandelt eine Grafana-Panel-URL (aus dem Browser) in eine EINBETTBARE Ansicht um:
# /d/<uid>/<slug>?...&viewPanel=panel-6  ->  /d-solo/<uid>/<slug>?...&panelId=6
# Public-Dashboard-URLs werden unveraendert uebernommen. allowed_host beschraenkt auf die
# konfigurierte Grafana-Instanz (CSP-frame-src passt dazu). Wirft ValueError bei Problemen.
def grafana_embed_url(raw, allowed_host):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL fehlt")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Ungueltige URL")
    if allowed_host and parts.netloc.lower() != allowed_host.lower():
        raise ValueError("Nur Panels von %s erlaubt" % allowed_host)
    # Public Dashboards sind auth-frei einbettbar, koennen aber KEIN Einzel-Panel isolieren
    # -> viewPanel/panelId entfernen, sonst zeigt Grafana den Toast "Invalid panel id".
    if "/public-dashboards/" in parts.path:
        pq = dict(parse_qsl(parts.query, keep_blank_values=True))
        pq.pop("viewPanel", None)
        pq.pop("panelId", None)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pq), ""))
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    # viewPanel/panelId -> numerische panelId (Grafana d-solo erwartet die Zahl).
    vp = q.pop("viewPanel", None) or q.get("panelId")
    if vp:
        num = "".join(ch for ch in str(vp) if ch.isdigit())
        if num:
            q["panelId"] = num
    path = parts.path.replace("/d/", "/d-solo/", 1) if "/d/" in parts.path else parts.path
    # Einzel-Panel-Ansicht (d-solo) braucht zwingend eine Panel-ID; sonst rendert Grafana
    # nur die leere Dashboard-Ansicht. Frueh und verstaendlich abfangen.
    if "/d-solo/" in path and "panelId" not in q:
        raise ValueError("URL ohne Panel. In Grafana das Panel einzeln oeffnen "
                         "(die URL enthaelt dann viewPanel=panel-X) und diese kopieren.")
    q.setdefault("theme", "dark")
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(q), ""))


# Baut aus der d-solo-URL die Grafana-Render-URL (serverseitiges Bild via image-renderer).
def grafana_render_url(embed_url, width, height):
    parts = urlsplit(embed_url)
    if "/render/" in parts.path:
        path = parts.path
    elif "/d-solo/" in parts.path:
        path = parts.path.replace("/d-solo/", "/render/d-solo/", 1)
    elif "/d/" in parts.path:
        path = parts.path.replace("/d/", "/render/d-solo/", 1)
    else:
        path = "/render" + parts.path
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["width"] = str(int(width))
    q["height"] = str(int(height))
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(q), ""))


# Platzhalter-Bild (SVG) bei Renderfehler -- ohne Secrets, nur der Fehlergrund.
def _panel_error_svg(detail):
    safe = (str(detail)[:80].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="120">'
        '<rect width="600" height="120" fill="#161b22"/>'
        '<text x="16" y="52" fill="#8b949e" font-family="system-ui" font-size="14">'
        'Grafana-Panel nicht ladbar</text>'
        '<text x="16" y="78" fill="#f85149" font-family="monospace" font-size="12">'
        + safe + '</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


# Panel-Breite als Spannweite im 12-Spalten-Raster. Erlaubt sind Bruchteile, die 12 teilen;
# Legacy-Werte (full/half) werden gemappt. Rueckgabe: String-Span ("12".."3").
_PANEL_SPANS = {"12", "9", "8", "6", "4", "3"}


def _norm_panel_width(width):
    width = (width or "").strip()
    if width in _PANEL_SPANS:
        return width
    return {"full": "12", "half": "6"}.get(width, "12")


# Rahmenfarbe der Panel-Kachel: nur Hex-Farben zulassen (geht als inline-Style ins
# Template -> strikt validieren). Leer/ungueltig = None (Standardrahmen).
def _norm_panel_color(color):
    color = (color or "").strip()
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else None


# Verwebt positionierte Bloecke (Grafana-Panels + Funktionskacheln) in den
# Gesamt-Kachelfluss. position = Einfuege-Slot (0 = erste Kachel, 1 = zweite, ...).
# Jeder Block ist ein Dict mit mindestens {btype, position, id, ...daten}.
# Rueckgabe: Liste von Dicts {kind: 'item'|'block', ...}.
def _dashboard_stream(items, blocks):
    blocks = sorted(blocks, key=lambda b: (b["position"], b["btype"], b["id"]))
    items = list(items)
    result, ii, pi, slot = [], 0, 0, 0
    while ii < len(items) or pi < len(blocks):
        if pi < len(blocks) and (blocks[pi]["position"] <= slot or ii >= len(items)):
            result.append({"kind": "block", "b": blocks[pi]})
            pi += 1
        else:
            result.append({"kind": "item", "it": items[ii], "idx": ii})
            ii += 1
        slot += 1
    return result


# Baut die Connector-Konfiguration aus dem Bearbeiten-Formular neu auf. Leere
# Secret-Felder (Passwort/Token) behalten den gespeicherten Wert. Fuer Chat bleibt der
# serverseitige archive_path erhalten (nie aus dem Formular).
def _rebuild_feed_config(connector_type, form, stored, archive_path):
    def keep(field, key):
        value = (form.get(field) or "").strip()
        return value if value else stored.get(key, "")

    def checked(field):
        return form.get(field) == "on"

    if connector_type == "chat":
        return {
            "archive_path": archive_path,
            "partner": stored.get("partner"),
            "is_room": stored.get("is_room", False),
            "max_age_hours": int(form.get("max_age_hours") or stored.get("max_age_hours") or 24),
            "include_outgoing": stored.get("include_outgoing", False),
        }
    if connector_type == "imap":
        return {
            "host": (form.get("host") or "").strip(),
            "port": int(form.get("port") or 993),
            "username": (form.get("username") or "").strip(),
            "password": keep("password", "password"),
            "ssl": checked("ssl"),
            "tls_verify": checked("tls_verify"),
            "folders": [f for f in form.getlist("folders")],
            "window_days": int(form.get("window_days") or stored.get("window_days") or 30),
        }
    if connector_type == "caldav":
        return {
            "url": (form.get("url") or "").strip(),
            "username": (form.get("username") or "").strip(),
            "password": keep("password", "password"),
            "tls_verify": checked("tls_verify"),
            "calendars": [c for c in form.getlist("calendars")],
            "window_days": int(form.get("window_days") or 14),
        }
    if connector_type in ("odoo_helpdesk", "odoo_project"):
        return {
            "url": (form.get("url") or "").strip(),
            "database": (form.get("database") or "").strip(),
            "username": (form.get("username") or "").strip(),
            "access_token": keep("access_token", "access_token"),
            "tls_verify": checked("tls_verify"),
            "open_only": checked("open_only"),
        }
    raise ValueError("Unbekannter Connector-Typ")


def _register_routes(app):
    env = app.state.env
    registry = app.state.registry
    xmpp = app.state.cfg["xmpp"]
    lc = app.state.login_cfg

    def render(template, **ctx):
        return HTMLResponse(env.get_template(template).render(**ctx))

    # Online-Status eines Accounts fuer die Kopfzeile.
    def account_state(jid):
        st = registry.get_state(jid)
        if not st or not st["enabled"]:
            return {"enabled": False, "label": "Offline", "cls": "off"}
        auth = st["auth_state"]
        if auth == "ok":
            return {"enabled": True, "label": "Online", "cls": "on"}
        if auth == "failed":
            return {"enabled": True, "label": "Anmeldung fehlgeschlagen", "cls": "error"}
        return {"enabled": True, "label": "Verbindet …", "cls": "connecting"}

    # --- Auth ----------------------------------------------------------------

    # Liefert {jid, user_id, archive_path} des eingeloggten Accounts oder erzwingt Login.
    def require_account(request: Request):
        jid = request.session.get("jid")
        if not jid or not registry.exists(jid):
            raise NotAuthenticated()
        return {"jid": jid, "user_id": registry.account_id(jid),
                "archive_path": registry.archive_path(jid)}

    @app.exception_handler(NotAuthenticated)
    async def _on_not_auth(request: Request, _exc):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "Nicht angemeldet"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    def _ip_blocked(ip):
        now = time.time()
        with app.state.login_lock:
            att = [t for t in app.state.login_attempts.get(ip, []) if now - t < lc["window"]]
            app.state.login_attempts[ip] = att
            return len(att) >= lc["max_per_ip"]

    def _record_fail(ip):
        with app.state.login_lock:
            app.state.login_attempts.setdefault(ip, []).append(time.time())

    def _validation_allowed():
        now = time.time()
        with app.state.login_lock:
            recent = [t for t in app.state.validation_times if now - t < lc["val_window"]]
            app.state.validation_times[:] = recent
            if len(recent) >= lc["val_max"]:
                return False
            app.state.validation_times.append(now)
            return True

    # --- DB ------------------------------------------------------------------

    def open_db():
        # Request-lokale Verbindung zur Aggregator-DB (WAL, geteilt mit dem Daemon).
        return db_connect(app.state.db_path)

    # --- Login / Logout ------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str = "", throttle: str = ""):
        if request.session.get("jid"):
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        pending = request.session.get("pending")
        return render("login.html", default_server=xmpp.get("default_host", ""),
                      error=bool(error), throttle=bool(throttle),
                      waiting=bool(pending), pending_jid=pending or "")

    @app.post("/login")
    def login(request: Request, jid: str = Form(...), password: str = Form(...), server: str = Form("")):
        ip = _client_ip(request)
        jid = (jid or "").strip()
        server = (server or "").strip() or xmpp.get("default_host", "")
        host, port = server, xmpp.get("default_port", 5222)
        if ":" in server:
            host, _, p = server.partition(":")
            port = int(p) if p.isdigit() else port
        if not jid or not password:
            return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        if _ip_blocked(ip):
            return RedirectResponse(url="/login?throttle=1", status_code=status.HTTP_303_SEE_OTHER)
        # Schnellpfad: bereits validierter Account mit unveraendertem Passwort (kein XMPP-Hit).
        if registry.verified_match(jid, password):
            request.session.pop("pending", None)
            request.session["jid"] = jid
            return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        # Aktiv + validiert, aber Passwort falsch -> ablehnen, aktive Verbindung nicht stoeren.
        if registry.is_ok(jid):
            _record_fail(ip)
            return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        # Domain-Whitelist: nur konfigurierte JID-Domains zulassen.
        domain = jid.split("@")[-1].lower() if "@" in jid else ""
        if lc["allowed_domains"] and domain not in lc["allowed_domains"]:
            _record_fail(ip)
            return RedirectResponse(url="/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        # Globale Drossel der XMPP-Validierungen (schuetzt den XMPP-Server / unsere IP).
        if not _validation_allowed():
            _record_fail(ip)
            return RedirectResponse(url="/login?throttle=1", status_code=status.HTTP_303_SEE_OTHER)
        _record_fail(ip)
        # Account anlegen/aktualisieren; der Daemon-Manager validiert per echter Verbindung.
        local = jid.split("@")[0]
        registry.upsert(jid, password, host=host, port=port,
                        resource=xmpp.get("resource", "compass"), muc_nick=local)
        request.session.pop("jid", None)
        request.session["pending"] = jid
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/api/login_status")
    def login_status(request: Request):
        jid = request.session.get("pending")
        if not jid:
            return {"status": "ok"} if request.session.get("jid") else {"status": "none"}
        state = registry.get_auth_state(jid)
        if state == "ok":
            request.session.pop("pending", None)
            request.session["jid"] = jid
            return {"status": "ok"}
        if state == "failed":
            request.session.pop("pending", None)
            return {"status": "failed"}
        return {"status": "pending"}

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # --- NextUp: Dashboard ----------------------------------------------------

    # Baut die gemeinsamen Dashboard-Daten (Items-Stream aus Ranking + Bloecken,
    # News-Ticker, Arbeitszeit-Segmente). Von Dashboard UND Kiosk genutzt.
    def _dashboard_data(conn, acc, group=None):
        now = time.time()
        ranking = store.get_ranking(conn, acc["user_id"], now, group_id=group)
        blocks = []
        for p in store.list_grafana_panels(conn, acc["user_id"]):
            b = dict(p); b["btype"] = "grafana"; blocks.append(b)
        for tl in store.list_tiles(conn, acc["user_id"]):
            b = dict(tl); b["btype"] = tl["kind"]
            b["data"] = _tile_data(conn, acc, tl["kind"]); blocks.append(b)
        stream = _dashboard_stream(ranking, blocks)
        ticker = store.list_ticker_teams(conn, acc["user_id"])
        wt_segments, wt_opts = None, {}
        if app.state.worktime_enabled:
            row = store.get_worktime_status(conn)
            payload = json.loads(row["payload"]) if (row and row["payload"]) else None
            wt_opts = _worktime_opts(conn)
            wt_segments = worktime_segments(payload, wt_opts, _calendar_payload(conn))
        return {"ranking": ranking, "stream": stream, "ticker": ticker,
                "wt_segments": wt_segments, "wt_opts": wt_opts,
                "ticker_speed": store.get_setting_int(conn, "ticker.speed", 90)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, acc: dict = Depends(require_account), group: int = None):
        conn = open_db()
        try:
            now = time.time()
            groups = store.list_groups(conn, acc["user_id"])
            st = store.get_status(conn, acc["user_id"])
            t = st["totals"]
            dash = {
                "alive": bool(st["last_event"] and (now - st["last_event"]) < 5400),
                "scored": t["scored"], "queue": t["open_unscored"], "total": t["items"],
            }
            dd = _dashboard_data(conn, acc, group)
            ranking = dd["ranking"]; stream = dd["stream"]; ticker = dd["ticker"]
            wt_segments = dd["wt_segments"]; wt_opts = dd["wt_opts"]; ticker_speed = dd["ticker_speed"]
            return render("dashboard.html", nav_active="nextup", account_jid=acc["jid"],
                          account_state=account_state(acc["jid"]), groups=groups,
                          items=ranking, stream=stream, active_group=group, dash=dash,
                          ticker=ticker, wt_segments=wt_segments, wt=wt_opts,
                          ticker_speed=ticker_speed,
                          grafana_configured=bool(app.state.grafana_url))
        finally:
            conn.close()

    @app.post("/items/{item_id}/action")
    def item_action(request: Request, item_id: int, action: str = Form(...),
                    minutes: int = Form(0), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            # Mandantenpruefung: Item muss dem angemeldeten Nutzer gehoeren.
            owned = conn.execute(
                "SELECT id FROM items WHERE id = ? AND user_id = ?", (item_id, acc["user_id"])
            ).fetchone()
            if owned:
                now = time.time()
                if action == "seen":
                    store.set_state(conn, item_id, now, seen=True)
                elif action == "done":
                    store.set_state(conn, item_id, now, done=True)
                elif action == "snooze":
                    store.set_state(conn, item_id, now, snoozed_until=now + max(1, minutes) * 60)
            return RedirectResponse(request.headers.get("referer", "/"), status_code=status.HTTP_303_SEE_OTHER)
        finally:
            conn.close()

    @app.get("/api/status")
    def api_status(acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            st = store.get_status(conn, acc["user_id"])
            t = st["totals"]
            now = time.time()
            processed = t["scored"] + t["open_unscored"]
            pct = (100 * t["scored"] // processed) if processed else 100
            return JSONResponse({
                "alive": bool(st["last_event"] and now - st["last_event"] < 5400),
                "scored": t["scored"], "queue": t["open_unscored"], "done": t["done"],
                "total": t["items"], "pending": t["pending_sync"], "pct": pct,
                "last_event_ago": _ago(st["last_event"]),
            })
        finally:
            conn.close()

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request, dbg: str = None, msg: str = None,
                    acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            st = store.get_status(conn, acc["user_id"])
            events = store.get_debug_events(conn, acc["user_id"], limit=120, kind=dbg)
            return render("status.html", nav_active="status", account_jid=acc["jid"],
                          account_state=account_state(acc["jid"]), st=st, events=events,
                          now=time.time(), active_dbg=dbg, msg=msg)
        finally:
            conn.close()

    # Globaler Wichtigkeits-Profiltext (geht in jede Bewertung ein -> Items neu bewerten).
    @app.post("/profile")
    def update_profile(request: Request, profile_text: str = Form(""),
                       acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.set_user_profile(conn, acc["user_id"], profile_text or None, time.time())
            store.clear_scores_for_user(conn, acc["user_id"])
            return RedirectResponse("/settings?msg=Profil+gespeichert", status_code=status.HTTP_303_SEE_OTHER)
        finally:
            conn.close()

    # --- Einstellungen (zentrale Seite, gruppiert nach Modulen) ----------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, msg: str = None, error: str = None,
                      acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            panels = store.list_grafana_panels(conn, acc["user_id"])
            ticker_teams = store.list_ticker_teams(conn, acc["user_id"])
            prow = conn.execute("SELECT profile_text FROM user_profiles WHERE user_id = ?",
                                (acc["user_id"],)).fetchone()
            feed_counts = conn.execute(
                "SELECT f.connector_type AS ct, COUNT(*) AS n, "
                "SUM(CASE WHEN f.enabled THEN 1 ELSE 0 END) AS aktiv "
                "FROM feeds f JOIN feed_groups g ON g.id = f.group_id "
                "WHERE g.user_id = ? GROUP BY f.connector_type ORDER BY f.connector_type",
                (acc["user_id"],)).fetchall()
            ticker_interval = store.get_setting_int(conn, "ticker.interval", 600)
            ticker_speed = store.get_setting_int(conn, "ticker.speed", 90)
            worktime_interval = store.get_setting_int(conn, "worktime.interval", 300)
            worktime_on = store.get_setting(conn, "worktime.enabled", "1") != "0"
            wt_opts = _worktime_opts(conn)
            # Kalender-Modul: URL/Nutzer anzeigen (nie das Passwort), Status vom Daemon.
            cal_enc = store.get_setting(conn, "calendar.config_enc", "")
            cal_cfg = _db.decrypt_config(app.state.fernet_key, cal_enc) if cal_enc else {}
            cal_interval = store.get_setting_int(conn, "calendar.interval", 900)
            cal_payload = _calendar_payload(conn)
            cal_feeds = store.list_calendar_feeds(conn)
            tiles = store.list_tiles(conn, acc["user_id"])
            briefing_hour = store.get_setting_int(conn, "briefing.hour", 7)
            ops_room = store.get_setting(conn, "ops.room", "monitoring@conference.example.com")
            ops_hours = store.get_setting_int(conn, "ops.hours", 24)
            review_days = store.get_setting_int(conn, "review.days", 7)
            review_exclude = store.get_setting(conn, "review.exclude",
                                               "vermutlich SPAM, Systemmeldungen")
            # Status-Hinweise fuer die Navigationsleiste: Fehler je Modul.
            news_error = any(t["status"] == "error" for t in ticker_teams)
            cal_error = bool(cal_payload and cal_payload.get("feeds")
                             and any(f.get("error") for f in cal_payload["feeds"]))
            kiosk_cfg = _kiosk_cfg(conn)
        finally:
            conn.close()
        # Absolute Display-URL aus dem Host-Header (extern via nginx/traefik).
        host = request.headers.get("host", "")
        kiosk_url = ("https://%s/kiosk/%s" % (host, kiosk_cfg["token"])) if kiosk_cfg["token"] else ""
        return render("settings.html", nav_active="settings", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]),
                      grafana_panels=panels, ticker_teams=ticker_teams,
                      profile_text=(prow["profile_text"] if prow else "") or "",
                      feed_counts=feed_counts, ticker_interval=ticker_interval,
                      ticker_speed=ticker_speed,
                      worktime_interval=worktime_interval, worktime_on=worktime_on,
                      wt=wt_opts,
                      cal_url=cal_cfg.get("url", ""), cal_user=cal_cfg.get("username", ""),
                      cal_configured=bool(cal_cfg.get("url")), cal_interval=cal_interval,
                      cal_status=cal_payload, cal_feeds=cal_feeds,
                      news_error=news_error, cal_error=cal_error,
                      kiosk=kiosk_cfg, kiosk_url=kiosk_url,
                      tiles=tiles, briefing_hour=briefing_hour, ops_room=ops_room,
                      ops_hours=ops_hours, review_days=review_days,
                      review_exclude=review_exclude,
                      worktime_configured=app.state.worktime_enabled,
                      grafana_configured=bool(app.state.grafana_url), msg=msg, error=error)

    # --- Funktionskacheln: Inhalte je Art aufbereiten ---------------------------

    # Betriebs-Kachel: Meldungsaufkommen des Monitoring-Raums (24h) aus dem
    # Chat-Archiv, Top-Verursacher per Hostnamen-Erkennung. 5-Minuten-Cache.
    _HOST_RE = re.compile(r"\b([a-z][a-z0-9\-]*(?:\.[a-z0-9\-]+){2,})\b")

    def _ops_data(conn, acc):
        room = store.get_setting(conn, "ops.room", "monitoring@conference.example.com")
        hours = store.get_setting_int(conn, "ops.hours", 24)
        key = (acc["user_id"], room, hours)
        now = time.time()
        ent = app.state.ops_cache.get(key)
        if ent and now - ent[0] < 300:
            return ent[1]
        top, total, last_ts = [], 0, None
        try:
            arch = sqlite3.connect("file:%s?mode=ro" % acc["archive_path"], uri=True, timeout=5)
            arch.row_factory = sqlite3.Row
            try:
                rows = arch.execute(
                    "SELECT body, ts_received FROM messages WHERE partner_jid = ? "
                    "AND ts_received >= ? AND decrypted = 1",
                    (room, now - hours * 3600)).fetchall()
            finally:
                arch.close()
            counts = {}
            for r in rows:
                total += 1
                last_ts = max(last_ts or 0, r["ts_received"])
                m = _HOST_RE.search((r["body"] or "").lower())
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        except Exception:
            pass
        data = {"room": room, "hours": hours, "total": total, "top": top, "last_ts": last_ts}
        app.state.ops_cache[key] = (now, data)
        return data

    # Team-Kachel: Verfuegbarkeit aus Zeiterfassungs- und Kalender-Snapshot.
    def _team_data(conn):
        data = {"active": [], "absent": [], "oncall": [], "upcoming": [], "oncall_gap": False}
        row = store.get_worktime_status(conn)
        try:
            wt = json.loads(row["payload"]) if (row and row["payload"]) else {}
        except ValueError:
            wt = {}
        for u in wt.get("users") or []:
            if u.get("active"):
                data["active"].append(u["name"])
        cal = _calendar_payload(conn) or {}
        events = cal.get("events") or []
        now = time.time()
        data["oncall"] = [e["summary"] for e in active_now(events) if e.get("role") == "oncall"]
        for e in covering_today(events):
            if e.get("role") == "absence":
                data["absent"].append(e["summary"])
        seen = set()
        for e in events:
            if e.get("role") == "absence" and now < e["start"] < now + 7 * 86400 \
                    and e["summary"] not in seen:
                seen.add(e["summary"])
                data["upcoming"].append("%s (ab %s)" % (
                    e["summary"][:40], time.strftime("%d.%m.", time.localtime(e["start"]))))
        # Planungsluecke: keine Rufbereitschaft, die in den naechsten 7 Tagen laeuft.
        has_oncall_week = any(e.get("role") == "oncall" and e["end"] > now
                              and e["start"] < now + 7 * 86400 for e in events)
        has_oncall_cal = any(e.get("role") == "oncall" for e in events)
        data["oncall_gap"] = has_oncall_cal and not has_oncall_week
        return data

    def _tile_data(conn, acc, kind):
        if kind == "team":
            return _team_data(conn)
        if kind == "ops":
            return _ops_data(conn, acc)
        # briefing / review: vom Daemon erzeugte Inhalte.
        row = store.get_tile_content(conn, kind)
        if not (row and row["payload"]):
            return None
        try:
            data = json.loads(row["payload"])
        except ValueError:
            return None
        data["updated_ts"] = row["updated_ts"]
        return data

    # Kalender-Snapshot (Daemon schreibt; None wenn nie geholt).
    def _calendar_payload(conn):
        row = store.get_calendar_status(conn)
        if not (row and row["payload"]):
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    # Kalender-Modul konfigurieren (iCal-URL + Basic-Auth, Fernet-verschluesselt).
    # Leeres Passwort laesst das gespeicherte unveraendert.
    @app.post("/settings/calendar")
    def settings_calendar(request: Request, cal_url: str = Form(""), cal_user: str = Form(""),
                          cal_pass: str = Form(""), cal_interval: int = Form(900),
                          tls_verify: str = Form("on"), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            now = time.time()
            url = cal_url.strip()
            if not url:
                # Leere URL = Modul deaktivieren (Zugang loeschen).
                store.set_setting(conn, "calendar.config_enc", "", now)
                return RedirectResponse("/settings?msg=Kalender+deaktiviert", status_code=303)
            if not url.lower().startswith("https://"):
                return RedirectResponse("/settings?error=Kalender:+nur+https-URLs", status_code=303)
            old_enc = store.get_setting(conn, "calendar.config_enc", "")
            old = _db.decrypt_config(app.state.fernet_key, old_enc) if old_enc else {}
            cfg = {"url": url, "username": cal_user.strip(),
                   "password": cal_pass if cal_pass else old.get("password", ""),
                   "tls_verify": (tls_verify == "on")}
            store.set_setting(conn, "calendar.config_enc",
                              _db.encrypt_config(app.state.fernet_key, cfg), now)
            store.set_setting(conn, "calendar.interval",
                              max(300, min(int(cal_interval), 86400)), now)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kalender+gespeichert", status_code=303)

    # Kalender-Zuordnung Schritt 1: Sammlungen des Kontos auflisten (PROPFIND).
    @app.post("/settings/calendar/discover", response_class=HTMLResponse)
    async def calendar_discover(request: Request, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            enc = store.get_setting(conn, "calendar.config_enc", "")
            assigned = {f["path"]: f["role"] for f in store.list_calendar_feeds(conn)}
        finally:
            conn.close()
        if not enc:
            return RedirectResponse("/settings?error=Kalender:+erst+Zugang+speichern", status_code=303)
        try:
            cfg = _db.decrypt_config(app.state.fernet_key, enc)
            cals = await asyncio.to_thread(discover_calendars, cfg)
        except Exception as exc:
            return RedirectResponse(f"/settings?error=Kalender:+{type(exc).__name__}", status_code=303)
        return render("settings_calendars.html", nav_active="settings", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), calendars=cals, assigned=assigned)

    # Kalender-Zuordnung Schritt 2: Rollen speichern (Gesamtstand ersetzt Bestand).
    @app.post("/settings/calendar/assign")
    async def calendar_assign(request: Request, acc: dict = Depends(require_account)):
        form = await request.form()
        count = int(form.get("count") or 0)
        entries = []
        for i in range(count):
            role = form.get(f"role_{i}") or "ignore"
            path = form.get(f"path_{i}") or ""
            name = (form.get(f"name_{i}") or path)[:120]
            if role in ("absence", "oncall", "other") and path:
                entries.append({"path": path, "name": name, "role": role})
        conn = open_db()
        try:
            store.replace_calendar_feeds(conn, entries, time.time())
            # Alter Snapshot passt nicht mehr zur neuen Zuordnung -> leeren; der
            # Daemon fuellt ihn im naechsten Zyklus neu.
            store.set_calendar_status(conn, "", time.time())
        finally:
            conn.close()
        return RedirectResponse(f"/settings?msg={len(entries)}+Kalender+zugeordnet", status_code=303)

    # --- Dashboard-Kacheln (Briefing/Team/Betrieb/Rueckblick) -------------------

    _TILE_KINDS = {"briefing", "team", "ops", "review"}

    @app.post("/settings/tiles")
    def tiles_add(request: Request, kind: str = Form(...), title: str = Form(""),
                  position: int = Form(0), width: str = Form("4"), color: str = Form(""),
                  acc: dict = Depends(require_account)):
        if kind not in _TILE_KINDS:
            return RedirectResponse("/settings?error=Unbekannte+Kachel", status_code=303)
        conn = open_db()
        try:
            store.create_tile(conn, acc["user_id"], kind, title.strip(),
                              max(0, int(position)), _norm_panel_width(width),
                              _norm_panel_color(color), time.time())
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kachel+hinzugefuegt", status_code=303)

    @app.post("/settings/tiles/{tile_id}/update")
    def tiles_update(request: Request, tile_id: int, title: str = Form(""),
                     position: int = Form(0), width: str = Form("4"), color: str = Form(""),
                     briefing_hour: int = Form(None), review_days: int = Form(None),
                     review_exclude: str = Form(None), ops_hours: int = Form(None),
                     ops_room: str = Form(None), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.update_tile(conn, acc["user_id"], tile_id, title.strip(),
                              max(0, int(position)), _norm_panel_width(width),
                              _norm_panel_color(color))
            # Kachel-spezifische Laufzeit-Option (Intervall/Zeitraum) direkt am Tile
            # mitspeichern -- steht im selben Bearbeiten-Formular je Kachelart.
            row = conn.execute("SELECT kind FROM dashboard_tiles WHERE id = ? AND user_id = ?",
                               (tile_id, acc["user_id"])).fetchone()
            kind = row["kind"] if row else None
            now = time.time()
            if kind == "briefing" and briefing_hour is not None:
                store.set_setting(conn, "briefing.hour", max(0, min(int(briefing_hour), 23)), now)
            elif kind == "review":
                if review_days is not None:
                    store.set_setting(conn, "review.days", max(1, min(int(review_days), 90)), now)
                if review_exclude is not None:
                    store.set_setting(conn, "review.exclude", review_exclude.strip(), now)
                store.set_tile_content(conn, "review", "", now)  # naechster Lauf mit neuem Zeitraum
            elif kind == "ops":
                if ops_hours is not None:
                    store.set_setting(conn, "ops.hours", max(1, min(int(ops_hours), 168)), now)
                if ops_room is not None and ops_room.strip():
                    store.set_setting(conn, "ops.room", ops_room.strip(), now)
                app.state.ops_cache.clear()
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kachel+aktualisiert", status_code=303)

    @app.post("/settings/tiles/{tile_id}/delete")
    def tiles_delete(request: Request, tile_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.delete_tile(conn, acc["user_id"], tile_id)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kachel+entfernt", status_code=303)

    # Kachel-Optionen: Briefing-Stunde + Monitoring-Raum der Betriebs-Kachel.
    @app.post("/settings/tiles/opts")
    def tiles_opts(request: Request, briefing_hour: int = Form(7), ops_room: str = Form(""),
                   ops_hours: int = Form(24), review_days: int = Form(7),
                   review_exclude: str = Form(""), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            now = time.time()
            store.set_setting(conn, "briefing.hour", max(0, min(int(briefing_hour), 23)), now)
            if ops_room.strip():
                store.set_setting(conn, "ops.room", ops_room.strip(), now)
            store.set_setting(conn, "ops.hours", max(1, min(int(ops_hours), 168)), now)
            store.set_setting(conn, "review.days", max(1, min(int(review_days), 90)), now)
            store.set_setting(conn, "review.exclude", review_exclude.strip(), now)
            # Rueckblick mit neuem Zeitraum beim naechsten Daemon-Zyklus; alten Stand
            # leeren, damit kein veralteter Zeitraum angezeigt wird.
            store.set_tile_content(conn, "review", "", now)
            app.state.ops_cache.clear()
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kachel-Optionen+gespeichert", status_code=303)

    # Direkten iCal-Feed hinzufuegen (z.B. Zeiterfassung timetracking.example.com/ical);
    # optional mit eigenen Zugangsdaten (sonst gilt der Modul-Zugang).
    @app.post("/settings/calendar/direct")
    def calendar_direct(request: Request, feed_url: str = Form(...), feed_name: str = Form(...),
                        feed_role: str = Form("absence"), feed_user: str = Form(""),
                        feed_pass: str = Form(""), acc: dict = Depends(require_account)):
        url = feed_url.strip()
        if not url.lower().startswith("https://"):
            return RedirectResponse("/settings?error=Kalender:+nur+https-URLs", status_code=303)
        if feed_role not in ("absence", "oncall", "other"):
            feed_role = "other"
        enc = None
        if feed_user.strip():
            enc = _db.encrypt_config(app.state.fernet_key,
                                     {"username": feed_user.strip(), "password": feed_pass})
        conn = open_db()
        try:
            store.create_direct_calendar_feed(conn, url, feed_name.strip()[:120], feed_role,
                                              enc, time.time())
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=iCal-Feed+gespeichert", status_code=303)

    @app.post("/settings/calendar/feed/{feed_id}/delete")
    def calendar_feed_delete(request: Request, feed_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.delete_calendar_feed(conn, feed_id)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Kalender-Feed+entfernt", status_code=303)

    # --- Kiosk / Buero-Display -------------------------------------------------

    # Erlaubte Werte je Display-Einstellung (Whitelist -> geht als data-Attribut ins HTML).
    _KIOSK_ALLOWED = {
        "theme": {"auto", "light", "dark"},
        "accent": {"blue", "teal", "green", "purple", "orange", "red"},
        "view": {"signal", "grid", "list"},
        "lines": {"0", "1", "2", "3", "5"},
        "cols": {"auto", "2", "3", "4"},
        "r1": {"1", "2", "3", "4", "6"}, "r2": {"1", "2", "3", "4", "6"},
        "r3": {"1", "2", "3", "4", "6"}, "rn": {"1", "2", "3", "4", "6"},
        "max": {"0", "10", "20", "30", "50"},
    }
    _KIOSK_DEFAULT = {"theme": "dark", "accent": "green", "view": "signal", "lines": "1",
                      "cols": "auto", "r1": "3", "r2": "4", "r3": "6", "rn": "6", "max": "0"}

    def _kiosk_cfg(conn):
        cfg = {k: store.get_setting(conn, "kiosk." + k, v) for k, v in _KIOSK_DEFAULT.items()}
        cfg["tickers"] = store.get_setting(conn, "kiosk.tickers", "1") != "0"
        cfg["refresh"] = store.get_setting_int(conn, "kiosk.refresh", 90)
        cfg["token"] = store.get_setting(conn, "kiosk.token", "")
        return cfg

    @app.post("/settings/kiosk")
    async def settings_kiosk(request: Request, acc: dict = Depends(require_account)):
        form = await request.form()
        conn = open_db()
        try:
            now = time.time()
            for k, allowed in _KIOSK_ALLOWED.items():
                v = (form.get(k) or "").strip()
                if v in allowed:
                    store.set_setting(conn, "kiosk." + k, v, now)
            store.set_setting(conn, "kiosk.tickers", "1" if form.get("tickers") == "on" else "0", now)
            try:
                refresh = max(15, min(int(form.get("refresh") or 90), 3600))
            except (TypeError, ValueError):
                refresh = 90
            store.set_setting(conn, "kiosk.refresh", refresh, now)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Display-Einstellungen+gespeichert", status_code=303)

    # Token (neu) erzeugen -> bindet das Display an dieses Konto; alte URL wird ungueltig.
    @app.post("/settings/kiosk/token")
    def settings_kiosk_token(request: Request, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            now = time.time()
            store.set_setting(conn, "kiosk.token", secrets.token_urlsafe(24), now)
            store.set_setting(conn, "kiosk.user_id", acc["user_id"], now)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Neue+Display-URL+erzeugt", status_code=303)

    @app.post("/settings/kiosk/revoke")
    def settings_kiosk_revoke(request: Request, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.set_setting(conn, "kiosk.token", "", time.time())
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Display-URL+deaktiviert", status_code=303)

    # Kiosk-Anzeige: token-geschuetzt, OHNE Login, read-only, ohne Chat. Fuer Buero-
    # Displays ohne Bedienung -> Aussehen kommt komplett aus den Display-Einstellungen.
    @app.get("/kiosk/{token}", response_class=HTMLResponse)
    def kiosk(token: str):
        conn = open_db()
        try:
            k = _kiosk_cfg(conn)
            if not k["token"] or not secrets.compare_digest(token, k["token"]):
                raise HTTPException(status_code=404)
            uid = store.get_setting_int(conn, "kiosk.user_id", 0)
            jid = registry.jid_for_id(uid) if uid else None
            if not jid:
                raise HTTPException(status_code=404)
            acc = {"user_id": uid, "jid": jid, "archive_path": registry.archive_path(jid)}
            dd = _dashboard_data(conn, acc)
        finally:
            conn.close()
        show_tk = k["tickers"]
        return render("kiosk.html", k=k, items=dd["ranking"], stream=dd["stream"],
                      ticker=(dd["ticker"] if show_tk else None),
                      wt_segments=(dd["wt_segments"] if show_tk else None),
                      wt=dd["wt_opts"], ticker_speed=dd["ticker_speed"])

    # Panel-Bild fuers Kiosk-Display: dieselbe serverseitige Renderung wie im
    # Dashboard, aber token-authentifiziert. Das Display hat weder eine Session
    # noch eigenen Grafana-Zugang -> compass rendert, cached und liefert das Bild.
    # Cache-Dauer = Reload-Intervall des Displays (schont die Grafana-Instanz).
    @app.get("/kiosk/{token}/panel/{panel_id}/img")
    async def kiosk_panel_img(token: str, panel_id: int, w: int = 1400, h: int = 300):
        conn = open_db()
        try:
            k = _kiosk_cfg(conn)
            if not k["token"] or not secrets.compare_digest(token, k["token"]):
                raise HTTPException(status_code=404)
            uid = store.get_setting_int(conn, "kiosk.user_id", 0)
        finally:
            conn.close()
        if not uid:
            raise HTTPException(status_code=404)
        return await _render_panel_response(uid, panel_id, w, h, ttl=max(30, k["refresh"]))

    # Anzeige-/Schwellwert-Optionen des Arbeitszeit-Laufbands (app_settings).
    def _worktime_opts(conn):
        return {
            "color": store.get_setting(conn, "worktime.color", "") or "",
            "speed": store.get_setting_int(conn, "worktime.speed", 90),
            "max_vacation": store.get_setting_int(conn, "worktime.max_vacation", 0),
            "vacation_color": store.get_setting(conn, "worktime.vacation_color", "#f85149"),
            "max_sick": store.get_setting_int(conn, "worktime.max_sick", 0),
            "sick_color": store.get_setting(conn, "worktime.sick_color", "#f85149"),
        }

    # News-Ticker: Intervall + Laufgeschwindigkeit (app_settings; Daemon/JS uebernehmen).
    @app.post("/settings/ticker")
    def settings_ticker(request: Request, ticker_interval: int = Form(600),
                        ticker_speed: int = Form(90),
                        acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            now = time.time()
            store.set_setting(conn, "ticker.interval",
                              max(300, min(int(ticker_interval), 86400)), now)
            store.set_setting(conn, "ticker.speed",
                              max(30, min(int(ticker_speed), 400)), now)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=News-Ticker+gespeichert", status_code=303)

    @app.post("/settings/worktime")
    def settings_worktime(request: Request, worktime_interval: int = Form(300),
                          worktime_enabled: str = Form("off"),
                          worktime_speed: int = Form(90), worktime_color: str = Form(""),
                          max_vacation: int = Form(0), vacation_color: str = Form(""),
                          max_sick: int = Form(0), sick_color: str = Form(""),
                          acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            now = time.time()
            store.set_setting(conn, "worktime.interval",
                              max(60, min(int(worktime_interval), 86400)), now)
            store.set_setting(conn, "worktime.enabled",
                              "1" if worktime_enabled == "on" else "0", now)
            store.set_setting(conn, "worktime.speed",
                              max(30, min(int(worktime_speed), 400)), now)
            # Farben strikt validieren (gehen als inline-Styles ins Template).
            store.set_setting(conn, "worktime.color", _norm_panel_color(worktime_color) or "", now)
            store.set_setting(conn, "worktime.max_vacation", max(0, min(int(max_vacation), 365)), now)
            store.set_setting(conn, "worktime.vacation_color",
                              _norm_panel_color(vacation_color) or "#f85149", now)
            store.set_setting(conn, "worktime.max_sick", max(0, min(int(max_sick), 365)), now)
            store.set_setting(conn, "worktime.sick_color",
                              _norm_panel_color(sick_color) or "#f85149", now)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Arbeitszeit-Einstellungen+gespeichert", status_code=303)

    # --- Grafana (Dashboard-Einbettung per iframe) ----------------------------

    @app.get("/grafana", response_class=HTMLResponse)
    def grafana_view(request: Request, d: str = "", acc: dict = Depends(require_account)):
        base = app.state.grafana_url
        # Nur Pfade unterhalb der konfigurierten internen Grafana-Instanz einbetten
        # (die CSP frame-src im nginx beschraenkt zusaetzlich auf genau diesen Host).
        embed_url = ""
        if base:
            path = d if d.startswith("/") else ("/" + d if d else "")
            embed_url = base + path
        return render("grafana.html", nav_active="grafana", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), embed_url=embed_url,
                      grafana_base=base)

    # Grafana-Panel fuer das NextUp-Dashboard anlegen. Zugangsdaten (Login/Passwort ODER
    # API-Token) werden Fernet-verschluesselt gespeichert; das Rendern laeuft serverseitig.
    @app.post("/grafana/panels")
    def grafana_panel_add(request: Request, url: str = Form(...), title: str = Form(""),
                          position: int = Form(0), width: str = Form("full"),
                          height: int = Form(260), gf_user: str = Form(""), gf_pass: str = Form(""),
                          gf_token: str = Form(""), mode: str = Form("image"),
                          color: str = Form(""), acc: dict = Depends(require_account)):
        base = app.state.grafana_url
        if not base:
            return RedirectResponse("/settings?error=grafana.base_url+nicht+konfiguriert", status_code=303)
        allowed_host = urlsplit(base).netloc
        try:
            embed = grafana_embed_url(url, allowed_host)
        except ValueError as exc:
            return RedirectResponse(f"/settings?error=Grafana:+{exc}", status_code=303)
        mode = "iframe" if mode == "iframe" else "image"
        # Public Dashboards koennen nicht serverseitig gerendert werden -> immer iframe.
        if "/public-dashboards/" in embed:
            mode = "iframe"
        # Zugangsdaten (optional) verschluesselt ablegen: Token hat Vorrang vor Login/Passwort.
        auth = None
        if gf_token.strip():
            auth = {"token": gf_token.strip()}
        elif gf_user.strip():
            auth = {"user": gf_user.strip(), "pass": gf_pass}
        auth_enc = _db.encrypt_config(app.state.fernet_key, auth) if auth else None
        conn = open_db()
        try:
            store.create_grafana_panel(
                conn, acc["user_id"], title.strip(),
                embed, max(0, int(position)), _norm_panel_width(width),
                max(120, min(int(height), 900)), auth_enc, mode, time.time(),
                color=_norm_panel_color(color))
        finally:
            conn.close()
        # Cache leeren, damit ein neu/umkonfiguriertes Panel sofort frisch rendert
        # (verhindert, dass ein altes Fehlerbild aus dem Cache angezeigt wird).
        app.state.panel_cache.clear()
        return RedirectResponse("/settings?msg=Grafana-Panel+hinzugefuegt", status_code=303)

    # Grafana-Panel nachtraeglich bearbeiten (Groesse/Position/Modus/Titel; URL/Token optional).
    @app.post("/grafana/panels/{panel_id}/update")
    def grafana_panel_update(request: Request, panel_id: int, title: str = Form(""),
                             position: int = Form(0), width: str = Form("full"),
                             height: int = Form(260), mode: str = Form("image"),
                             url: str = Form(""), gf_user: str = Form(""), gf_pass: str = Form(""),
                             gf_token: str = Form(""), color: str = Form(""),
                             acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            p = store.get_grafana_panel(conn, acc["user_id"], panel_id)
            if not p:
                return RedirectResponse("/settings?error=Panel+unbekannt", status_code=303)
            # URL nur aendern, wenn eine neue angegeben wurde.
            embed = None
            if url.strip():
                allowed_host = urlsplit(app.state.grafana_url).netloc if app.state.grafana_url else ""
                try:
                    embed = grafana_embed_url(url, allowed_host)
                except ValueError as exc:
                    return RedirectResponse(f"/settings?error=Grafana:+{exc}", status_code=303)
            m = "iframe" if mode == "iframe" else "image"
            # Public Dashboards koennen kein Einzel-Panel rendern -> immer iframe.
            eff_embed = embed if embed is not None else p["embed_url"]
            if "/public-dashboards/" in eff_embed:
                m = "iframe"
            # Zugangsdaten nur ersetzen, wenn neu angegeben (sonst unveraendert lassen).
            auth_enc = None
            if gf_token.strip():
                auth_enc = _db.encrypt_config(app.state.fernet_key, {"token": gf_token.strip()})
            elif gf_user.strip():
                auth_enc = _db.encrypt_config(app.state.fernet_key, {"user": gf_user.strip(), "pass": gf_pass})
            store.update_grafana_panel(
                conn, acc["user_id"], panel_id, title.strip(), max(0, int(position)),
                _norm_panel_width(width), max(120, min(int(height), 900)), m,
                embed_url=embed, auth_enc=auth_enc, color=_norm_panel_color(color))
        finally:
            conn.close()
        app.state.panel_cache.clear()
        return RedirectResponse("/settings?msg=Panel+aktualisiert", status_code=303)

    @app.post("/grafana/panels/{panel_id}/delete")
    def grafana_panel_delete(request: Request, panel_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.delete_grafana_panel(conn, acc["user_id"], panel_id)
        finally:
            conn.close()
        app.state.panel_cache.clear()
        return RedirectResponse("/settings?msg=Grafana-Panel+entfernt", status_code=303)

    # Serverseitig gerendertes Panel-Bild (same-origin -> keine CSP-/iframe-/Cookie-Probleme).
    # Nutzt die hinterlegten Grafana-Zugangsdaten (Basic-Auth bzw. Bearer-Token).
    # Rendert ein Grafana-Panel serverseitig (image-renderer), cached das Bild kurz
    # und liefert es aus. Auf Fehler wird das letzte gute Bild weitergereicht (das
    # Display bleibt bei kurzem Grafana-Ausfall ruhig), sonst ein Platzhalter-SVG.
    # w/h werden geklemmt, damit ein Aufrufer keine riesigen Renderauftraege stellt.
    async def _render_panel_response(user_id, panel_id, w, h, ttl=60):
        w = max(100, min(int(w), 3000))
        h = max(50, min(int(h), 2000))
        key = (user_id, panel_id, w, h)
        now = time.time()
        ent = app.state.panel_cache.get(key)
        if ent and now - ent[0] < ttl:
            return Response(ent[1], media_type=ent[2], headers={"Cache-Control": "private, max-age=60"})
        conn = open_db()
        try:
            p = store.get_grafana_panel(conn, user_id, panel_id)
        finally:
            conn.close()
        if not p:
            raise HTTPException(status_code=404)
        render = grafana_render_url(p["embed_url"], w, h)
        auth = _db.decrypt_config(app.state.fernet_key, p["auth_enc"]) if p["auth_enc"] else {}
        headers, basic = {}, None
        if auth.get("token"):
            headers["Authorization"] = "Bearer " + auth["token"]
        elif auth.get("user"):
            basic = (auth["user"], auth.get("pass", ""))
        try:
            async with httpx.AsyncClient(verify=app.state.grafana_tls_verify, timeout=45,
                                         follow_redirects=True) as client:
                r = await client.get(render, headers=headers, auth=basic)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image"):
                app.state.panel_cache[key] = (now, r.content, ctype)
                return Response(r.content, media_type=ctype, headers={"Cache-Control": "private, max-age=60"})
            # Kein Bild -> haeufig fehlendes image-renderer-Plugin oder Auth. Nur Statuscode zeigen.
            detail = "HTTP %d (image-renderer/Login pruefen)" % r.status_code
        except Exception as exc:
            detail = type(exc).__name__
        # Fallback: letztes gutes (ggf. abgelaufenes) Bild statt Fehler-Platzhalter,
        # damit ein Buero-Display bei kurzer Grafana-Stoerung nicht "kaputt" wirkt.
        if ent:
            return Response(ent[1], media_type=ent[2], headers={"Cache-Control": "private, max-age=30"})
        return _panel_error_svg(detail)

    @app.get("/grafana/panels/{panel_id}/img")
    async def grafana_panel_img(panel_id: int, w: int = 1000, h: int = 300,
                                acc: dict = Depends(require_account)):
        return await _render_panel_response(acc["user_id"], panel_id, w, h)

    # --- Feed-Verwaltung ------------------------------------------------------

    # Laedt einen Feed, der dem angemeldeten Nutzer gehoert (sonst None).
    def owned_feed(conn, user_id, feed_id):
        return conn.execute(
            "SELECT f.* FROM feeds f JOIN feed_groups g ON g.id = f.group_id "
            "WHERE f.id = ? AND g.user_id = ?",
            (feed_id, user_id),
        ).fetchone()

    def owned_group(conn, user_id, group_id):
        return conn.execute(
            "SELECT id FROM feed_groups WHERE id = ? AND user_id = ?", (group_id, user_id)
        ).fetchone()

    @app.get("/feeds", response_class=HTMLResponse)
    def feeds_page(request: Request, msg: str = None, error: str = None,
                   acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            groups = store.list_groups(conn, acc["user_id"])
            # Die Gruppen-UI ist entfernt; ohne Gruppe koennte aber kein Feed angelegt
            # werden -> stillschweigend eine Standard-Gruppe bereitstellen.
            if not groups:
                store.create_group(conn, acc["user_id"], "Feeds", time.time())
                groups = store.list_groups(conn, acc["user_id"])
            feeds_by_group = {}
            for g in groups:
                feeds_by_group[g["id"]] = conn.execute(
                    "SELECT * FROM feeds WHERE group_id = ? ORDER BY id", (g["id"],)
                ).fetchall()
            # Grafana-Panels und News-Ticker werden auf /settings verwaltet.
            return render("feeds.html", nav_active="feeds", account_jid=acc["jid"],
                          account_state=account_state(acc["jid"]), groups=groups,
                          feeds_by_group=feeds_by_group, connector_types=available_types(),
                          grafana_configured=bool(app.state.grafana_url),
                          msg=msg, error=error)
        finally:
            conn.close()

    @app.post("/groups")
    def create_group(request: Request, name: str = Form(...), priority: int = Form(3),
                     profile_text: str = Form(""), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.create_group(conn, acc["user_id"], name.strip(), time.time(),
                               priority=_clamp_prio(priority), profile_text=profile_text or None)
            return RedirectResponse("/feeds?msg=Gruppe+angelegt", status_code=303)
        except Exception as exc:
            return RedirectResponse(f"/feeds?error={type(exc).__name__}", status_code=303)
        finally:
            conn.close()

    # Chat (in-process): Kontakte/Raeume aus dem eigenen Archiv zur Auswahl anbieten.
    @app.post("/feeds/chat/discover", response_class=HTMLResponse)
    async def chat_discover(request: Request, group_id: int = Form(...),
                            acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
        finally:
            conn.close()
        try:
            rooms = await get_connector("chat").discover({"archive_path": acc["archive_path"]})
        except Exception as exc:
            return RedirectResponse(f"/feeds?error=Chat:+{type(exc).__name__}", status_code=303)
        return render("feeds_chat_rooms.html", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), rooms=rooms,
                      form={"group_id": group_id}, error=None)

    # Chat (in-process): je ausgewaehltem Raum/Person einen Feed anlegen.
    @app.post("/feeds/chat/create")
    async def chat_create(request: Request, acc: dict = Depends(require_account)):
        form = await request.form()
        group_id = int(form.get("group_id"))
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
            count = int(form.get("count") or 0)
            created = 0
            now = time.time()
            for i in range(count):
                if form.get(f"include_{i}") != "on":
                    continue
                partner = form.get(f"partner_{i}")
                if not partner:
                    continue
                # archive_path serverseitig aus der Session (nicht aus dem Formular).
                cfg = {
                    "archive_path": acc["archive_path"], "partner": partner,
                    "is_room": form.get(f"room_{i}") == "1",
                    "max_age_hours": int(form.get(f"exp_{i}") or 24),
                    "include_outgoing": False,
                }
                enc = _db.encrypt_config(app.state.fernet_key, cfg)
                store.create_feed(conn, group_id, "chat", (form.get(f"label_{i}") or partner)[:120],
                                  enc, now, priority=_clamp_prio(form.get(f"prio_{i}", 3)),
                                  llm_scoring=(form.get(f"llm_{i}") == "on"))
                created += 1
            if created == 0:
                return RedirectResponse("/feeds?error=Keine+Auswahl+getroffen", status_code=303)
            return RedirectResponse(f"/feeds?msg={created}+Chat-Feed(s)+angelegt", status_code=303)
        finally:
            conn.close()

    # IMAP: Ordner zur Auswahl anbieten (Schritt 1).
    @app.post("/feeds/imap/discover", response_class=HTMLResponse)
    async def imap_discover(request: Request, group_id: int = Form(...), name: str = Form(...),
                            priority: int = Form(3), host: str = Form(...), port: int = Form(993),
                            username: str = Form(...), password: str = Form(...),
                            ssl: str = Form("on"), tls_verify: str = Form("on"),
                            poll_interval: int = Form(0), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
        finally:
            conn.close()
        base = {"host": host.strip(), "port": int(port), "username": username.strip(),
                "password": password, "ssl": (ssl == "on"), "tls_verify": (tls_verify == "on")}
        try:
            folders = await get_connector("imap").discover(base)
        except Exception as exc:
            return RedirectResponse(f"/feeds?error=IMAP:+{type(exc).__name__}", status_code=303)
        return render("feeds_imap_folders.html", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), folders=folders,
                      form={"group_id": group_id, "name": name, "priority": priority,
                            "poll_interval": poll_interval, **base})

    # IMAP: ausgewaehlte Ordner speichern (Schritt 2).
    @app.post("/feeds/imap/create")
    async def imap_create(request: Request, group_id: int = Form(...), name: str = Form(...),
                          priority: int = Form(3), host: str = Form(...), port: int = Form(993),
                          username: str = Form(...), password: str = Form(...),
                          ssl: str = Form("on"), tls_verify: str = Form("on"),
                          poll_interval: int = Form(0), folders: List[str] = Form(default=[]),
                          acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
            if not folders:
                return RedirectResponse("/feeds?error=Keine+Ordner+gewaehlt", status_code=303)
            feed_cfg = {"host": host.strip(), "port": int(port), "username": username.strip(),
                        "password": password, "ssl": (ssl == "on"), "tls_verify": (tls_verify == "on"),
                        "folders": folders}
            ok, detail = await get_connector("imap").validate_config(feed_cfg)
            if not ok:
                return RedirectResponse(f"/feeds?error=Validierung:+{detail}", status_code=303)
            enc = _db.encrypt_config(app.state.fernet_key, feed_cfg)
            store.create_feed(conn, group_id, "imap", name.strip(), enc, time.time(),
                              priority=_clamp_prio(priority), poll_interval=(poll_interval or None))
            return RedirectResponse("/feeds?msg=Mail-Feed+angelegt", status_code=303)
        finally:
            conn.close()

    # CalDAV: Kalender zur Auswahl anbieten (Schritt 1).
    @app.post("/feeds/caldav/discover", response_class=HTMLResponse)
    async def caldav_discover(request: Request, group_id: int = Form(...), name: str = Form(...),
                              priority: int = Form(3), url: str = Form(...),
                              username: str = Form(...), password: str = Form(...),
                              tls_verify: str = Form("on"), window_days: int = Form(14),
                              poll_interval: int = Form(0), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
        finally:
            conn.close()
        base = {"url": url.strip(), "username": username.strip(), "password": password,
                "tls_verify": (tls_verify == "on")}
        try:
            calendars = await get_connector("caldav").discover(base)
        except Exception as exc:
            return RedirectResponse(f"/feeds?error=CalDAV:+{type(exc).__name__}", status_code=303)
        return render("feeds_caldav_calendars.html", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), calendars=calendars,
                      form={"group_id": group_id, "name": name, "priority": priority,
                            "poll_interval": poll_interval, "window_days": window_days, **base})

    # CalDAV: ausgewaehlte Kalender speichern (Schritt 2).
    @app.post("/feeds/caldav/create")
    async def caldav_create(request: Request, group_id: int = Form(...), name: str = Form(...),
                            priority: int = Form(3), url: str = Form(...),
                            username: str = Form(...), password: str = Form(...),
                            tls_verify: str = Form("on"), window_days: int = Form(14),
                            poll_interval: int = Form(0), calendars: List[str] = Form(default=[]),
                            acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
            if not calendars:
                return RedirectResponse("/feeds?error=Kein+Kalender+gewaehlt", status_code=303)
            feed_cfg = {"url": url.strip(), "username": username.strip(), "password": password,
                        "tls_verify": (tls_verify == "on"), "calendars": calendars,
                        "window_days": int(window_days)}
            ok, detail = await get_connector("caldav").validate_config(feed_cfg)
            if not ok:
                return RedirectResponse(f"/feeds?error=Validierung:+{detail}", status_code=303)
            enc = _db.encrypt_config(app.state.fernet_key, feed_cfg)
            store.create_feed(conn, group_id, "caldav", name.strip(), enc, time.time(),
                              priority=_clamp_prio(priority), poll_interval=(poll_interval or None))
            return RedirectResponse("/feeds?msg=Kalender-Feed+angelegt", status_code=303)
        finally:
            conn.close()

    # Odoo (einstufig): mir zugewiesene HelpDesk-Tickets oder Projekt-Aufgaben.
    @app.post("/feeds/odoo")
    async def create_odoo_feed(request: Request, group_id: int = Form(...),
                               connector_type: str = Form(...), name: str = Form(...),
                               priority: int = Form(3), url: str = Form(...),
                               database: str = Form(...), username: str = Form(...),
                               access_token: str = Form(...), tls_verify: str = Form("on"),
                               open_only: str = Form("on"), poll_interval: int = Form(0),
                               acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if connector_type not in ("odoo_helpdesk", "odoo_project"):
                return RedirectResponse("/feeds?error=Ungueltiger+Odoo-Typ", status_code=303)
            if not owned_group(conn, acc["user_id"], group_id):
                return RedirectResponse("/feeds?error=Gruppe+unbekannt", status_code=303)
            feed_cfg = {"url": url.strip(), "database": database.strip(),
                        "username": username.strip(), "access_token": access_token.strip(),
                        "tls_verify": (tls_verify == "on"), "open_only": (open_only == "on")}
            ok, detail = await get_connector(connector_type).validate_config(feed_cfg)
            if not ok:
                return RedirectResponse(f"/feeds?error=Validierung:+{detail}", status_code=303)
            enc = _db.encrypt_config(app.state.fernet_key, feed_cfg)
            store.create_feed(conn, group_id, connector_type, name.strip(), enc, time.time(),
                              priority=_clamp_prio(priority), poll_interval=(poll_interval or None))
            return RedirectResponse("/feeds?msg=Odoo-Feed+angelegt", status_code=303)
        finally:
            conn.close()

    # --- News-Ticker (Odoo-HelpDesk-Teams) -------------------------------------

    _TICKER_MAX_TEAMS = 3

    # Schritt 1: Odoo-Zugang pruefen und HelpDesk-Teams zur Auswahl anbieten.
    @app.post("/ticker/discover", response_class=HTMLResponse)
    async def ticker_discover(request: Request, url: str = Form(...), database: str = Form(...),
                              username: str = Form(...), access_token: str = Form(...),
                              tls_verify: str = Form("on"), acc: dict = Depends(require_account)):
        creds = {"url": url.strip(), "database": database.strip(),
                 "username": username.strip(), "access_token": access_token.strip(),
                 "tls_verify": (tls_verify == "on")}
        try:
            # XML-RPC ist blockierend -> nicht im Event-Loop ausfuehren.
            teams = await asyncio.to_thread(list_helpdesk_teams, creds)
        except PermissionError:
            return RedirectResponse("/settings?error=Ticker:+Odoo-Login+abgelehnt", status_code=303)
        except Exception as exc:
            return RedirectResponse(f"/settings?error=Ticker:+{type(exc).__name__}", status_code=303)
        conn = open_db()
        try:
            existing = {t["team_id"] for t in store.list_ticker_teams(conn, acc["user_id"])}
        finally:
            conn.close()
        return render("feeds_ticker_teams.html", account_jid=acc["jid"],
                      account_state=account_state(acc["jid"]), teams=teams,
                      existing=existing, max_teams=_TICKER_MAX_TEAMS, form=creds)

    # Schritt 2: ausgewaehlte Teams speichern (max. 3 gesamt). Zugang je Team
    # Fernet-verschluesselt; der Daemon holt Tickets + LLM-Schlagzeile zyklisch.
    @app.post("/ticker/create")
    async def ticker_create(request: Request, acc: dict = Depends(require_account)):
        form = await request.form()
        creds = {"url": (form.get("url") or "").strip(),
                 "database": (form.get("database") or "").strip(),
                 "username": (form.get("username") or "").strip(),
                 "access_token": (form.get("access_token") or "").strip(),
                 "tls_verify": form.get("tls_verify") == "on"}
        chosen = []
        for raw in form.getlist("teams"):
            # Wert: "<team_id>|<team_name>" aus der Auswahlseite.
            tid, _, tname = str(raw).partition("|")
            if tid.isdigit() and tname:
                chosen.append((int(tid), tname[:120]))
        if not chosen:
            return RedirectResponse("/settings?error=Kein+Team+gewaehlt", status_code=303)
        conn = open_db()
        try:
            existing = store.list_ticker_teams(conn, acc["user_id"])
            existing_ids = {t["team_id"] for t in existing}
            chosen = [(tid, tname) for tid, tname in chosen if tid not in existing_ids]
            if len(existing) + len(chosen) > _TICKER_MAX_TEAMS:
                return RedirectResponse(
                    f"/settings?error=Maximal+{_TICKER_MAX_TEAMS}+Ticker-Teams", status_code=303)
            enc = _db.encrypt_config(app.state.fernet_key, creds)
            now = time.time()
            pos = len(existing)
            for tid, tname in chosen:
                store.create_ticker_team(conn, acc["user_id"], tid, tname, enc, now, position=pos)
                pos += 1
        finally:
            conn.close()
        return RedirectResponse(f"/settings?msg={len(chosen)}+Ticker-Team(s)+angelegt", status_code=303)

    @app.post("/ticker/{ticker_id}/delete")
    def ticker_delete(request: Request, ticker_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            store.delete_ticker_team(conn, acc["user_id"], ticker_id)
        finally:
            conn.close()
        return RedirectResponse("/settings?msg=Ticker-Team+entfernt", status_code=303)

    # Aktuelle Schlagzeilen fuer das Laufband (dashboard.js pollt zyklisch).
    @app.get("/api/ticker")
    def api_ticker(acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            rows = store.list_ticker_teams(conn, acc["user_id"])
        finally:
            conn.close()
        return {"ticker": [{
            "id": r["id"], "team": r["team_name"], "headline": r["headline"],
            "count": r["ticket_count"], "ts": r["headline_ts"], "status": r["status"],
        } for r in rows]}

    # Arbeitszeit-Laufband: aktuelle Segmente (dashboard.js pollt zyklisch).
    @app.get("/api/worktime")
    def api_worktime(acc: dict = Depends(require_account)):
        if not app.state.worktime_enabled:
            return {"segments": [], "enabled": False}
        conn = open_db()
        try:
            row = store.get_worktime_status(conn)
            opts = _worktime_opts(conn)
            cal = _calendar_payload(conn)
        finally:
            conn.close()
        payload = json.loads(row["payload"]) if (row and row["payload"]) else None
        return {"segments": worktime_segments(payload, opts, cal), "enabled": True,
                "color": opts["color"], "speed": opts["speed"],
                "ts": row["updated_ts"] if row else None}

    # Manueller Sofort-Sync eines Feeds (Daemon arbeitet die Anfrage zeitnah ab).
    @app.post("/feeds/{feed_id}/sync")
    def sync_feed(request: Request, feed_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if owned_feed(conn, acc["user_id"], feed_id):
                now = time.time()
                store.mark_feed_due(conn, feed_id, now)
                store.create_sync_request(conn, feed_id, now)
            return RedirectResponse("/status?msg=Sync+angestossen", status_code=303)
        finally:
            conn.close()

    @app.post("/feeds/{feed_id}/settings")
    def feed_settings(request: Request, feed_id: int, name: str = Form(...), priority: int = Form(3),
                      poll_interval: int = Form(0), enabled: str = Form("off"),
                      llm_scoring: str = Form("off"), acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            feed = owned_feed(conn, acc["user_id"], feed_id)
            if feed:
                new_prio = _clamp_prio(priority)
                new_llm = (llm_scoring == "on")
                conn.execute("UPDATE feeds SET name = ? WHERE id = ?", (name.strip(), feed_id))
                conn.commit()
                store.update_feed_settings(conn, feed_id, priority=new_prio,
                                           poll_interval=int(poll_interval), enabled=(enabled == "on"),
                                           llm_scoring=new_llm)
                # Prioritaet/LLM-Schalter fliessen in den Score ein -> neu bewerten.
                if new_prio != feed["priority"] or bool(new_llm) != bool(feed["llm_scoring"]):
                    store.clear_scores_for_feed(conn, feed_id)
            return RedirectResponse(f"/feeds/{feed_id}/edit?msg=Einstellungen+gespeichert", status_code=303)
        finally:
            conn.close()

    # Connector-Konfiguration aendern. Leere Secret-Felder lassen den Wert unveraendert.
    @app.post("/feeds/{feed_id}/config")
    async def feed_config(request: Request, feed_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            feed = owned_feed(conn, acc["user_id"], feed_id)
            if not feed:
                return RedirectResponse("/feeds?error=Feed+unbekannt", status_code=303)
            stored = _db.decrypt_config(app.state.fernet_key, feed["config_enc"])
            form = await request.form()
            ct = feed["connector_type"]
            cfg = _rebuild_feed_config(ct, form, stored, acc["archive_path"])
            ok, detail = await get_connector(ct).validate_config(cfg)
            if not ok:
                return RedirectResponse(f"/feeds/{feed_id}/edit?error=Validierung:+{detail}", status_code=303)
            enc = _db.encrypt_config(app.state.fernet_key, cfg)
            conn.execute("UPDATE feeds SET config_enc = ? WHERE id = ?", (enc, feed_id))
            conn.commit()
            return RedirectResponse(f"/feeds/{feed_id}/edit?msg=Konfiguration+gespeichert", status_code=303)
        finally:
            conn.close()

    @app.post("/feeds/{feed_id}/delete")
    def feed_delete(request: Request, feed_id: int, acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            if owned_feed(conn, acc["user_id"], feed_id):
                store.delete_feed(conn, feed_id)
            return RedirectResponse("/feeds?msg=Feed+geloescht", status_code=303)
        finally:
            conn.close()

    @app.get("/feeds/{feed_id}/edit", response_class=HTMLResponse)
    async def feed_edit(request: Request, feed_id: int, msg: str = None, error: str = None,
                        acc: dict = Depends(require_account)):
        conn = open_db()
        try:
            feed = owned_feed(conn, acc["user_id"], feed_id)
            if not feed:
                return RedirectResponse("/feeds?error=Feed+unbekannt", status_code=303)
            cfg = _db.decrypt_config(app.state.fernet_key, feed["config_enc"])
            selectable, discover_error = None, None
            if feed["connector_type"] in ("imap", "caldav"):
                try:
                    selectable = await get_connector(feed["connector_type"]).discover(cfg)
                except Exception as exc:
                    discover_error = type(exc).__name__
            selected = set(cfg.get("folders") or cfg.get("calendars") or [])
            # Secrets nicht ins Template geben.
            safe_cfg = {k: v for k, v in cfg.items()
                        if k not in ("password", "token", "access_token", "archive_path")}
            return render("feed_edit.html", nav_active="feeds", account_jid=acc["jid"],
                          account_state=account_state(acc["jid"]), feed=feed, cfg=safe_cfg,
                          selectable=selectable, selected=selected, discover_error=discover_error,
                          msg=msg, error=error)
        finally:
            conn.close()

    # --- PWA -----------------------------------------------------------------

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": "compass", "short_name": "compass", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#0d1117", "theme_color": "#0d1117",
        }, media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        path = os.path.join(_STATIC, "sw.js")
        if not os.path.isfile(path):
            return Response(content="// kein Service Worker\n", media_type="text/javascript")
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": APP_VERSION}

    # Chat-Ansichten (eigenes Modul): teilen sich Auth + Templating der Haupt-App.
    register_chat_routes(app, require_account, render, account_state)


# Prozess-Einstieg der Web-UI (von run_web.py aufgerufen): uvicorn mit der App starten.
def serve(cfg):
    import uvicorn
    app = create_app(cfg)
    uvicorn.run(app, host=cfg_get(cfg, "web.bind_host", "127.0.0.1"),
                port=int(cfg_get(cfg, "web.bind_port", 8100)), log_level="info")
    return 0
