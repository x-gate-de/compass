# -----------------------------------------------------------------------------
# Skript: src/xmpp/archive.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Schreibseite des Chat-Daemons: Archiv, Outbox, Kontakte (Roster) und MUC-Raeume.
# Betriebs- und Wartungshinweise:
# - Enthaelt entschluesselte private Nachrichten (Schutzbedarf HOCH). Rechte 0600.
# - Schema in src/xmpp/chat_schema.py (gemeinsam mit der Web-UI).
# -----------------------------------------------------------------------------

import logging
import os
import sqlite3
import time

from .chat_schema import ensure_chat_schema

logger = logging.getLogger(__name__)


# Schreibzugriff des Daemons auf Archiv, Outbox, Kontakte und Raeume.
class MessageArchive:
    def __init__(self, path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path)
        ensure_chat_schema(self._conn)
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Konnte Dateirechte fuer Archiv nicht setzen: %s", path)

    # Speichert eine Nachricht idempotent. Rueckgabe: True wenn neu gespeichert.
    # ts: expliziter Zeitstempel (z. B. MAM-Originalzeit); None = jetzt.
    def store(self, partner_jid, direction, body, stanza_id, decrypted=True,
              namespace=None, sender=None, status=None, msg_id=None, ts=None):
        dedup_key = f"{direction}:{partner_jid}:{sender or ''}:{stanza_id}"
        try:
            self._conn.execute(
                "INSERT INTO messages "
                "(dedup_key, partner_jid, direction, body, decrypted, ts_received, namespace, sender, status, msg_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dedup_key, partner_jid, direction, body, 1 if decrypted else 0,
                 ts if ts is not None else time.time(), namespace, sender, status, msg_id),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # Prueft, ob eine Nachricht bereits archiviert ist (Dedup vor OMEMO-Entschluesselung).
    def has(self, partner_jid, direction, sender, stanza_id):
        key = f"{direction}:{partner_jid}:{sender or ''}:{stanza_id}"
        return self._conn.execute("SELECT 1 FROM messages WHERE dedup_key = ?", (key,)).fetchone() is not None

    # --- MAM (aeltere Nachrichten nachladen) --------------------------------

    def claim_pending_mam(self):
        rows = self._conn.execute(
            "SELECT id, target_jid, kind FROM mam_requests WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def mark_mam_done(self, req_id, ok=True):
        self._conn.execute(
            "UPDATE mam_requests SET status = ? WHERE id = ?", ("done" if ok else "error", req_id)
        )
        self._conn.commit()

    def mam_oldest(self, target):
        row = self._conn.execute("SELECT oldest_ts FROM mam_state WHERE target_jid = ?", (target,)).fetchone()
        return row[0] if row else None

    def set_mam_oldest(self, target, ts):
        self._conn.execute(
            "INSERT INTO mam_state (target_jid, oldest_ts) VALUES (?, ?) "
            "ON CONFLICT(target_jid) DO UPDATE SET oldest_ts = excluded.oldest_ts",
            (target, ts),
        )
        self._conn.commit()

    def oldest_message_ts(self, target):
        row = self._conn.execute(
            "SELECT MIN(ts_received) FROM messages WHERE partner_jid = ?", (target,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # --- OMEMO-Geraete / Verifizierung --------------------------------------

    def claim_pending_omemo(self):
        rows = self._conn.execute(
            "SELECT id, action, jid, identity_hex, trust_value FROM omemo_requests WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def mark_omemo_done(self, req_id, ok=True):
        self._conn.execute(
            "UPDATE omemo_requests SET status = ? WHERE id = ?", ("done" if ok else "error", req_id)
        )
        self._conn.commit()

    # Ersetzt die Geraeteliste fuer die in rows vorkommenden JIDs.
    def set_omemo_devices(self, rows):
        jids = set(r["jid"] for r in rows)
        for j in jids:
            self._conn.execute("DELETE FROM omemo_devices WHERE jid = ?", (j,))
        now = time.time()
        self._conn.executemany(
            "INSERT OR REPLACE INTO omemo_devices "
            "(jid, device_id, fingerprint, identity_hex, trust, is_own, label, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["jid"], r["device_id"], r["fingerprint"], r["identity_hex"], r["trust"],
              1 if r["is_own"] else 0, r["label"], now) for r in rows],
        )
        self._conn.commit()

    # Markiert eine gesendete Nachricht als zugestellt (Empfangsbestaetigung XEP-0184).
    def mark_delivered(self, msg_id):
        self._conn.execute(
            "UPDATE messages SET status = 'delivered' WHERE msg_id = ? AND direction = 'out'",
            (msg_id,),
        )
        self._conn.commit()

    # --- Outbox -------------------------------------------------------------

    def claim_pending_outbox(self):
        rows = self._conn.execute(
            "SELECT id, recipient_jid, body, kind, media_path, media_name, media_mime "
            "FROM outbox WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]

    def mark_outbox_sent(self, outbox_id):
        self._conn.execute(
            "UPDATE outbox SET status = 'sent', sent_ts = ? WHERE id = ?",
            (time.time(), outbox_id),
        )
        self._conn.commit()

    def mark_outbox_error(self, outbox_id, error):
        self._conn.execute(
            "UPDATE outbox SET status = 'error', error = ? WHERE id = ?",
            (str(error)[:200], outbox_id),
        )
        self._conn.commit()

    # --- Kontakte (Roster) --------------------------------------------------

    def upsert_contact(self, jid, name, subscription):
        self._conn.execute(
            "INSERT INTO contacts (jid, name, subscription) VALUES (?, ?, ?) "
            "ON CONFLICT(jid) DO UPDATE SET name = excluded.name, subscription = excluded.subscription",
            (jid, name, subscription),
        )
        self._conn.commit()

    # --- Avatare (vCard-Foto) -----------------------------------------------

    # Speichert das Avatar-Foto eines Kontakts. data leer = Negativ-Marker
    # (Kontakt hat kein Foto), damit nicht bei jedem Presence neu geladen wird.
    def store_avatar(self, jid, mime, data, ahash):
        self._conn.execute(
            "INSERT OR REPLACE INTO avatars (jid, mime, data, hash, updated_ts) VALUES (?, ?, ?, ?, ?)",
            (jid, mime or "", data or b"", ahash or "", time.time()),
        )
        self._conn.commit()

    # Gespeicherter Foto-Hash (zum Abgleich mit dem Presence-Hash) oder None.
    def avatar_hash(self, jid):
        row = self._conn.execute("SELECT hash FROM avatars WHERE jid = ?", (jid,)).fetchone()
        return row[0] if row else None

    # --- MUC ----------------------------------------------------------------

    def set_available_rooms(self, rooms):
        # rooms: Liste von (room_jid, name). Tabelle komplett ersetzen.
        self._conn.execute("DELETE FROM muc_available")
        now = time.time()
        self._conn.executemany(
            "INSERT OR REPLACE INTO muc_available (room_jid, name, updated_ts) VALUES (?, ?, ?)",
            [(r[0], r[1], now) for r in rooms],
        )
        self._conn.commit()

    def joined_rooms(self):
        rows = self._conn.execute(
            "SELECT room_jid, nick FROM mucs WHERE joined = 1"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def is_room(self, jid):
        row = self._conn.execute(
            "SELECT 1 FROM mucs WHERE room_jid = ? UNION SELECT 1 FROM muc_available WHERE room_jid = ?",
            (jid, jid),
        ).fetchone()
        return row is not None

    # Anzeigename fuer Push-Benachrichtigungen (Kontaktname / Raumname / lokaler Teil).
    def display_name(self, jid):
        row = self._conn.execute("SELECT name FROM contacts WHERE jid = ?", (jid,)).fetchone()
        if row and row[0]:
            return row[0]
        row = self._conn.execute("SELECT name FROM muc_available WHERE room_jid = ?", (jid,)).fetchone()
        if row and row[0]:
            return row[0]
        return (jid or "").split("@")[0]

    # --- Web-Push -----------------------------------------------------------

    def push_pref_enabled(self, partner_jid):
        row = self._conn.execute(
            "SELECT enabled FROM push_prefs WHERE partner_jid = ?", (partner_jid,)
        ).fetchone()
        return bool(row and row[0])

    def push_subscriptions(self):
        rows = self._conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()
        return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2]} for r in rows]

    def delete_push_subscription(self, endpoint):
        self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self._conn.commit()
