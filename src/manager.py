# -----------------------------------------------------------------------------
# Skript: src/manager.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Account-Manager fuer den Multi-User-Betrieb: haelt je aktivem Account eine
#   dauerhafte XMPP/OMEMO-Verbindung (eigenes Chat-Archiv + OMEMO-State).
# - run_daemon() ist der Prozess-Einstieg des compass-Daemons; hier werden in
#   Etappe 3 zusaetzlich die Aggregator-Schleifen im selben Loop gestartet.
# Ablauf:
# - Pollt die Account-Registry; verbindet neue Accounts, trennt entfernte/
#   deaktivierte. Jeder Bot archiviert in das Account-eigene Verzeichnis.
# Betriebs- und Wartungshinweise:
# - Laeuft als ein Prozess im gemeinsamen asyncio-Loop.
# - Es werden keine Passwoerter geloggt.
# -----------------------------------------------------------------------------

import asyncio
import logging
import shutil
import time

from .accounts import AccountRegistry
from .aggregator import AggregatorDaemon
from .config import cfg_get
from .db import connect as agg_connect
from .llm import LLMClient
from .schema import purge_user
from .xmpp.daemon import build_daemon

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, registry, config):
        self._registry = registry
        self._config = config
        self._default_host = config["xmpp"].get("default_host") or ""
        self._default_port = int(config["xmpp"].get("default_port") or 5222)
        self._bots = {}
        # Watchdog-Kadenz: Abstand, in dem bei anhaltend getrennter Verbindung ein
        # Reconnect des BESTEHENDEN Bots erneut angestossen wird (kein Neuaufbau).
        self._reconnect_after = float(config["xmpp"].get("reconnect_after_seconds", 60))
        self._down_since = {}  # jid -> monotone Zeit, seit der der Bot getrennt ist

    # Baut die per-Account-Konfiguration in der vom Daemon erwarteten Form.
    def _build_config(self, acc):
        jid = acc["jid"]
        return {
            "xmpp": {
                "jid": jid,
                "password": acc["password"],
                "resource": acc["resource"] or self._config["xmpp"].get("resource", "compass"),
                "muc_nick": acc["muc_nick"],
                "tls_verify": self._config["xmpp"].get("tls_verify", True),
            },
            "omemo": {
                "state_path": self._registry.state_path(jid),
                "trust_policy": self._config["omemo"].get("trust_policy", "btbv"),
            },
            "archive": {"db_path": self._registry.archive_path(jid)},
            "push": self._config.get("push") or {},
        }

    def _connect(self, acc):
        jid = acc["jid"]
        try:
            self._registry.set_auth_state(jid, "connecting")
            bot = build_daemon(self._build_config(acc))
            bot.auto_reconnect = True
            # Auth-Ergebnis ueber die echte Verbindung zurueckmelden (Login-Validierung).
            bot.add_event_handler("session_start", lambda _e, j=jid: self._registry.set_auth_state(j, "ok"))
            # Verbindungsverlust im Auth-State spiegeln -> UI zeigt "Verbindet ..." statt
            # faelschlich "Online". Nur aus dem Zustand "ok" heraus, damit "failed"/
            # "pending" (Login-Validierung) nicht ueberschrieben werden.
            bot.add_event_handler("disconnected", lambda _e, j=jid: self._on_bot_disconnected(j))

            def _on_failed(_e, j=jid):
                # Falsche Zugangsdaten: als fehlgeschlagen markieren und deaktivieren,
                # damit nicht endlos weiterprobiert wird.
                logger.warning("Authentifizierung fehlgeschlagen: %s", j)
                self._registry.set_auth_state(j, "failed")
                self._registry.set_enabled(j, False)

            bot.add_event_handler("failed_auth", _on_failed)
            host = acc["host"] or self._default_host
            port = int(acc["port"] or self._default_port)
            if host:
                bot.connect(host, port)
            else:
                bot.connect()
            self._bots[jid] = bot
            logger.info("Account verbindet: %s", jid)
        except Exception as e:
            logger.error("Verbindung %s fehlgeschlagen: %s", jid, type(e).__name__)

    # Spiegelt einen Verbindungsabbruch in den Auth-State (fuer die UI-Anzeige).
    # Nur aus "ok" heraus -> "failed"/"pending" der Login-Validierung bleiben erhalten.
    def _on_bot_disconnected(self, jid):
        if self._registry.get_auth_state(jid) == "ok":
            self._registry.set_auth_state(jid, "connecting")

    def _disconnect(self, jid):
        bot = self._bots.pop(jid, None)
        self._down_since.pop(jid, None)
        if bot is not None:
            try:
                # Eigenen Reconnect des verworfenen Bots unterbinden, damit keine
                # Geister-Verbindung neben dem frisch aufgebauten Bot weiterlaeuft.
                bot.auto_reconnect = False
                bot.disconnect()
            except Exception:
                pass
            logger.info("Account getrennt: %s", jid)

    # Watchdog: erkennt tote Verbindungen und stoesst einen Reconnect des BESTEHENDEN
    # Bots an. slixmpp reconnektet nach einem Abbruch (in 1.16) nicht von selbst, daher
    # muss der Manager anstossen -- ABER es wird bewusst KEIN neuer Bot gebaut. Zwei Bots
    # je Account wuerden sich mit derselben Resource /compass gegenseitig vom Server
    # werfen (Flap-Storm). Ein bot.connect() auf dem vorhandenen Bot retryt intern mit
    # Backoff und haelt die Verbindung eindeutig.
    def _reconnect_same_bot(self, jid, bot):
        try:
            bot.connect()
        except Exception as e:
            logger.warning("Reconnect-Anstoss %s fehlgeschlagen: %s", jid, type(e).__name__)

    def _check_health(self, jid, acc):
        bot = self._bots.get(jid)
        if bot is None:
            return
        if bot.is_connected():
            self._down_since.pop(jid, None)
            return
        now = time.monotonic()
        first = self._down_since.get(jid)
        if first is None:
            # Erstmals getrennt erkannt -> sofort einen Reconnect anstossen.
            self._down_since[jid] = now
            logger.info("Verbindung weg -- Reconnect (bestehender Bot): %s", jid)
            self._reconnect_same_bot(jid, bot)
        elif now - first >= self._reconnect_after:
            # Weiterhin getrennt -> periodisch erneut anstossen (kein neuer Bot).
            self._down_since[jid] = now
            logger.warning("Weiter getrennt seit >%.0fs -- Reconnect erneut anstossen: %s",
                           self._reconnect_after, jid)
            self._reconnect_same_bot(jid, bot)

    # Endlosschleife: aktive Accounts mit den laufenden Bots abgleichen.
    async def run(self):
        logger.info("Account-Manager gestartet")
        while True:
            try:
                wanted = {a["jid"]: a for a in self._registry.enabled_accounts()}
                for jid, acc in wanted.items():
                    if jid not in self._bots:
                        self._connect(acc)
                    elif acc["auth_state"] == "pending":
                        # Web hat sich (erneut) angemeldet, evtl. mit neuem Passwort
                        # -> alte Verbindung trennen und mit aktuellen Daten neu verbinden.
                        self._disconnect(jid)
                        self._connect(acc)
                    else:
                        self._check_health(jid, acc)
                for jid in list(self._bots):
                    if jid not in wanted:
                        self._disconnect(jid)
                # Vorgemerkte Loeschungen ausfuehren -- erst wenn der Bot getrennt ist
                # (kein offener Zugriff mehr auf die Account-Daten).
                for jid in self._registry.pending_deletions():
                    if jid not in self._bots:
                        self._delete_account(jid)
            except Exception as e:
                logger.error("Manager-Schleife fehlgeschlagen: %s", type(e).__name__)
            await asyncio.sleep(3)

    # Loescht das Account-Verzeichnis (Chat-Archiv, OMEMO-State, Spool), die Aggregator-
    # Daten desselben Nutzers (compass.db) und den Account-Eintrag. Unwiderruflich --
    # nur nach ausdruecklicher Bestaetigung in der Web-UI.
    def _delete_account(self, jid):
        try:
            # user_id VOR dem Entfernen des Account-Eintrags bestimmen.
            uid = self._registry.account_id(jid)
            shutil.rmtree(self._registry.account_dir(jid), ignore_errors=True)
            if uid is not None:
                try:
                    conn = agg_connect(self._config["database"]["path"])
                    try:
                        purge_user(conn, uid)
                    finally:
                        conn.close()
                except Exception as e:
                    logger.error("Aggregator-Daten fuer %s nicht geloescht: %s", jid, type(e).__name__)
            self._registry.finalize_deletion(jid)
            logger.info("Account und Daten geloescht: %s", jid)
        except Exception as e:
            logger.error("Account-Loeschung %s fehlgeschlagen: %s", jid, type(e).__name__)


# Baut den LLM-Client fuer die Dringlichkeitsbewertung aus der Konfiguration.
def _build_llm(cfg):
    return LLMClient(
        backend=cfg_get(cfg, "llm.backend", "ollama"),
        base_url=cfg_get(cfg, "llm.base_url", "http://127.0.0.1:11434"),
        model=cfg_get(cfg, "llm.model", "llama3.1"),
        api_key=cfg_get(cfg, "llm.api_key", ""),
        tls_verify=bool(cfg_get(cfg, "llm.tls_verify", True)),
        timeout=int(cfg_get(cfg, "llm.request_timeout", 60)),
        override_threshold=int(cfg_get(cfg, "scoring.override_threshold", 90)),
    )


# Prozess-Einstieg des compass-Daemons: Chat-Manager (XMPP/OMEMO) UND Aggregator-Schleifen
# (Poll/Scoring/Decay/Retention/Re-Rank) laufen gemeinsam in einem asyncio-Loop.
def run_daemon(cfg):
    registry = AccountRegistry(
        cfg["accounts"]["db_path"],
        cfg["security"]["fernet_key"],
        cfg["accounts"]["users_dir"],
    )
    manager = AccountManager(registry, cfg)

    # Eigenen Event-Loop einrichten; die slixmpp-Bots haengen sich hier ein. Vor der
    # Aggregator-Konstruktion setzen, damit dessen asyncio.Lock den Loop findet.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Aggregator: eigene Verbindung zur zentralen Item-DB (compass.db, WAL) + LLM-Client.
    agg_conn = agg_connect(cfg["database"]["path"])
    aggregator = AggregatorDaemon(agg_conn, cfg, _build_llm(cfg))

    loop.create_task(manager.run())
    loop.create_task(aggregator.run())
    logger.info("compass-Daemon laeuft (Chat-Manager + Aggregator)")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    return 0
