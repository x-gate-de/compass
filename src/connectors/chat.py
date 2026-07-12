# -----------------------------------------------------------------------------
# Skript: src/connectors/chat.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - IN-PROCESS Chat-Connector: liest Nachrichten direkt aus dem Account-eigenen
#   Chat-Archiv (users_dir/<slug>/messages.sqlite). Ersetzt den frueheren HTTP-Umweg
#   ueber die x-gate_chat Read-API -- in compass liegen die Chat-Daten lokal vor.
# Ablauf:
# - discover(): Kontakte + oeffentliche Raeume aus dem Archiv zur Auswahl in der UI.
# - poll(): pro Raum/Person (config.partner) die Nachrichten der letzten max_age_hours;
#   aeltere fallen per Abgleich (present_keys) automatisch raus.
# Betriebs- und Wartungshinweise:
# - Konfiguration (config-Dict): archive_path (Pfad zur messages.sqlite des Accounts),
#   partner (JID), is_room (bool), max_age_hours (int), include_outgoing (bool).
# - archive_path setzt die Web-UI beim Anlegen des Feeds (aus der Account-Registry).
# - Nur-Lesen: es werden ausschliesslich SELECTs ausgefuehrt. sqlite ist blockierend ->
#   Aufrufe via asyncio.to_thread.
# -----------------------------------------------------------------------------

import asyncio
import logging
import sqlite3
import time

from ..models import Item
from .base import Connector

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_HOURS = 24


class ChatConnector(Connector):
    connector_type = "chat"

    # Read-only-Verbindung zum Account-Archiv (eigene Verbindung je Aufruf).
    def _open(self, config):
        path = config.get("archive_path")
        if not path:
            raise ValueError("archive_path fehlt")
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def validate_config(self, config):
        if not config.get("archive_path"):
            return False, "archive_path ist erforderlich"
        if not config.get("partner"):
            return False, "partner (JID) ist erforderlich"
        try:
            await asyncio.to_thread(self._validate_sync, config)
            return True, "ok"
        except Exception as exc:
            return False, f"Archiv nicht lesbar: {type(exc).__name__}"

    def _validate_sync(self, config):
        conn = self._open(config)
        try:
            conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        finally:
            conn.close()

    # Listet Kontakte (1:1) und oeffentliche Raeume aus dem Archiv (je einer wird zum Feed).
    async def discover(self, config):
        return await asyncio.to_thread(self._discover_sync, config)

    def _discover_sync(self, config):
        conn = self._open(config)
        try:
            out = []
            for r in conn.execute("SELECT jid, name FROM contacts ORDER BY name"):
                label = (r["name"] or r["jid"])
                out.append({"id": r["jid"], "label": f"{label} (1:1)", "is_room": False})
            for r in conn.execute("SELECT room_jid, name FROM muc_available ORDER BY name"):
                label = (r["name"] or r["room_jid"])
                out.append({"id": r["room_jid"], "label": f"{label} (Raum)", "is_room": True})
            return out
        finally:
            conn.close()

    async def poll(self, ctx, config, since, known_keys=None):
        return await asyncio.to_thread(self._poll_sync, ctx, config)

    # Liefert (items, None, present_keys). present_keys = alle Nachrichten im Fenster ->
    # aeltere werden vom Daemon entfernt (Meldungen laufen nach Zeit ab).
    def _poll_sync(self, ctx, config):
        partner = config["partner"]
        is_room = bool(config.get("is_room"))
        hours = int(config.get("max_age_hours", _DEFAULT_MAX_AGE_HOURS))
        include_outgoing = bool(config.get("include_outgoing", False))
        cutoff = time.time() - hours * 3600
        conn = self._open(config)
        try:
            name = self._display_name(conn, partner)
            query = ("SELECT id, direction, body, sender, ts_received FROM messages "
                     "WHERE partner_jid = ? AND ts_received >= ?")
            args = [partner, cutoff]
            # Standard: nur eingehende Nachrichten als Feed-Item (eigene ausblenden).
            if not include_outgoing:
                query += " AND direction = 'in'"
            query += " ORDER BY ts_received"
            rows = conn.execute(query, args).fetchall()
        finally:
            conn.close()

        items = []
        present = set()
        deep_link = "/c/" + partner
        for r in rows:
            # Leerer Body = Anhang/nicht entschluesselbar -> Hinweis statt Text.
            body = r["body"] if r["body"] else "[Anhang/verschluesselt]"
            item = Item(
                feed_id=ctx.feed_id,
                user_id=ctx.user_id,
                source_type="groupchat" if is_room else "chat",
                external_id=str(r["id"]),
                title=name or partner,
                body=body,
                sender=r["sender"] or partner,
                url=deep_link,
                ts_source=r["ts_received"],
                ts_due=None,
                raw=None,
            )
            items.append(item)
            present.add(item.dedup_key)
        return items, None, present

    # Anzeigename fuer den Item-Titel (Kontaktname / Raumname / lokaler JID-Teil).
    def _display_name(self, conn, jid):
        row = conn.execute("SELECT name FROM contacts WHERE jid = ?", (jid,)).fetchone()
        if row and row["name"]:
            return row["name"]
        row = conn.execute("SELECT name FROM muc_available WHERE room_jid = ?", (jid,)).fetchone()
        if row and row["name"]:
            return row["name"]
        return (jid or "").split("@")[0]
