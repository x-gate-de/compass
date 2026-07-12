# -----------------------------------------------------------------------------
# Skript: src/config.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Laedt und validiert die YAML-Konfiguration von compass (Multi-User-Betrieb).
#   Vereint die Sektionen aus Chat (xmpp/omemo/accounts/push) und Aggregator
#   (database/llm/scoring/retention/polling) und ergaenzt die Grafana-Sektion.
# Ablauf:
# - YAML einlesen, Pflichtfelder pruefen, Defaults setzen.
# Betriebs- und Wartungshinweise:
# - Nutzer-/Feed-Zugangsdaten kommen NICHT aus der config, sondern verschluesselt
#   aus der DB (Login bzw. Feed-Verwaltung). Die config haelt nur globale
#   Einstellungen + Server-Keys (fernet_key, session_secret). Rechte 0600.
# -----------------------------------------------------------------------------

import logging
import os

import yaml

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


# Holt einen verschachtelten Wert ueber Punktpfad (z.B. "llm.base_url").
# Liefert default, wenn ein Teilpfad fehlt.
def cfg_get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# Laedt die Konfiguration aus einer YAML-Datei und validiert die Pflichtfelder.
# Setzt fuer alle Sektionen sinnvolle Defaults, damit der Rest des Codes ohne
# staendige Existenzpruefungen auskommt.
def load_config(path):
    if not os.path.isfile(path):
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")

    with open(path, encoding="utf8") as f:
        data = yaml.safe_load(f) or {}

    # --- Web ---
    web = data.get("web") or {}
    web.setdefault("bind_host", "127.0.0.1")
    web.setdefault("bind_port", 8100)
    web.setdefault("base_path", "/")
    # Secure-Flag der Session-Cookies (true, sobald hinter TLS erreichbar).
    web.setdefault("session_https_only", True)
    data["web"] = web

    # --- Chat: globale XMPP-Defaults (werden beim Login vorbelegt) ---
    xmpp = data.get("xmpp") or {}
    xmpp.setdefault("default_host", "")
    xmpp.setdefault("default_port", 5222)
    xmpp.setdefault("resource", "compass")
    xmpp.setdefault("tls_verify", True)
    xmpp.setdefault("reconnect_after_seconds", 60)
    xmpp.setdefault("allowed_domains", [])
    data["xmpp"] = xmpp

    omemo = data.get("omemo") or {}
    omemo.setdefault("trust_policy", "btbv")
    data["omemo"] = omemo

    # --- Account-Registry (accounts.db) + je-Account-Verzeichnisse ---
    accounts = data.get("accounts") or {}
    accounts.setdefault("db_path", "/var/lib/compass/accounts.db")
    accounts.setdefault("users_dir", "/var/lib/compass/users")
    data["accounts"] = accounts

    # --- Aggregator: zentrale Item-DB ---
    database = data.get("database") or {}
    database.setdefault("path", "/var/lib/compass/compass.db")
    data["database"] = database

    # --- Server-Keys sind Pflicht: ohne sie kein Verschluesseln/Sessions ---
    security = data.get("security") or {}
    if not security.get("fernet_key"):
        raise ConfigError("security.fernet_key fehlt (Schluessel fuer Passwort-/Credential-Verschluesselung)")
    if not security.get("session_secret"):
        raise ConfigError("security.session_secret fehlt (Schluessel fuer Web-Sessions)")
    security.setdefault("login_window", 300)
    security.setdefault("login_max_per_ip", 5)
    security.setdefault("validation_window", 60)
    security.setdefault("validation_max", 5)
    data["security"] = security

    # --- Aggregator: LLM-Bewertung (internes Ollama) ---
    llm = data.get("llm") or {}
    llm.setdefault("backend", "ollama")
    llm.setdefault("base_url", "http://127.0.0.1:11434")
    llm.setdefault("model", "llama3.1")
    llm.setdefault("api_key", "")
    llm.setdefault("tls_verify", True)
    llm.setdefault("request_timeout", 60)
    data["llm"] = llm

    scoring = data.get("scoring") or {}
    scoring.setdefault("rescore_interval", 0)
    scoring.setdefault("override_threshold", 90)
    scoring.setdefault("override_floor", 1000)
    scoring.setdefault("time_decay_enabled", True)
    scoring.setdefault("decay_tick", 300)
    scoring.setdefault("score_tick", 60)
    scoring.setdefault("rerank_enabled", True)
    scoring.setdefault("rerank_top_n", 10)
    scoring.setdefault("rerank_interval", 600)
    scoring.setdefault("rerank_timeout", 240)
    data["scoring"] = scoring

    retention = data.get("retention") or {}
    retention.setdefault("done_age", 604800)
    retention.setdefault("due_grace", 86400)
    retention.setdefault("news_age", 1209600)
    retention.setdefault("default_age", 2592000)
    data["retention"] = retention

    polling = data.get("polling") or {}
    polling.setdefault("default_interval", 3600)
    polling.setdefault("error_backoff_start", 60)
    polling.setdefault("error_backoff_max", 3600)
    polling.setdefault("commands_tick", 5)
    data["polling"] = polling

    # --- Grafana-Anzeige (Dashboard-Einbettung per iframe/Embed) ---
    grafana = data.get("grafana") or {}
    grafana.setdefault("base_url", "")
    grafana.setdefault("api_token", "")
    grafana.setdefault("tls_verify", True)
    grafana.setdefault("request_timeout", 30)
    data["grafana"] = grafana

    # --- Web Push (optional). Leere Schluessel = Push deaktiviert ---
    push = data.get("push") or {}
    push.setdefault("vapid_private_key", "")
    push.setdefault("vapid_public_key", "")
    push.setdefault("vapid_subject", "")
    data["push"] = push

    # --- Aggregator-Debug (LLM-Ein-/Ausgabe je Item mitschneiden) ---
    debug = data.get("debug") or {}
    debug.setdefault("capture_llm", True)
    debug.setdefault("max_events", 1000)
    data["debug"] = debug

    log = data.get("logging") or {}
    log.setdefault("level", "INFO")
    log.setdefault("file", None)
    data["logging"] = log

    return data
