# -----------------------------------------------------------------------------
# Skript: src/accounts.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Account-Registry fuer den Multi-User-Betrieb von compass: speichert je XMPP-Account
#   die Zugangsdaten (Passwort verschluesselt) und Verbindungsparameter. Der XMPP-Account
#   ist die zentrale Identitaet der Anwendung; accounts.id dient als user_id fuer den
#   Aggregator-Teil (compass.db).
# Ablauf:
# - SQLite-Tabelle accounts (eigene DB accounts.db); Passwoerter mit Fernet
#   (Server-Key) verschluesselt. Web schreibt beim Login, der Account-Manager liest
#   die aktiven Accounts.
# Betriebs- und Wartungshinweise:
# - Pro Operation eine eigene SQLite-Verbindung (Web nutzt mehrere Threads).
# - Der Fernet-Key liegt in config.yaml (security.fernet_key), Rechte 0600. Geht er
#   verloren, sind gespeicherte Passwoerter nicht mehr entschluesselbar.
# - Pro Account ein eigenes Chat-Archiv + OMEMO-State (Verzeichnis je Account-Slug).
# -----------------------------------------------------------------------------

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


# Erzeugt einen stabilen, dateisystemsicheren Ordnernamen aus einer JID.
def account_slug(jid):
    base = re.sub(r"[^a-zA-Z0-9]+", "_", jid.lower()).strip("_")
    digest = hashlib.sha1(jid.lower().encode("utf8")).hexdigest()[:8]
    return f"{base}_{digest}"


class AccountRegistry:
    def __init__(self, db_path, fernet_key, users_dir):
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db_path = db_path
        self._users_dir = users_dir
        self._fernet = Fernet(fernet_key.encode("utf8") if isinstance(fernet_key, str) else fernet_key)
        conn = self._open()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS accounts ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  jid TEXT UNIQUE NOT NULL,"
                "  password_enc TEXT NOT NULL,"
                "  host TEXT,"
                "  port INTEGER,"
                "  resource TEXT NOT NULL DEFAULT 'compass',"
                "  muc_nick TEXT,"
                "  enabled INTEGER NOT NULL DEFAULT 1,"
                "  auth_state TEXT NOT NULL DEFAULT 'pending',"  # pending/connecting/ok/failed
                "  created_ts REAL NOT NULL,"
                "  last_login_ts REAL"
                ")"
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
            if "auth_state" not in cols:
                try:
                    conn.execute("ALTER TABLE accounts ADD COLUMN auth_state TEXT NOT NULL DEFAULT 'pending'")
                except sqlite3.OperationalError:
                    pass
            # Vorgemerkte Account-Loeschungen (Web stellt ein, Manager fuehrt aus).
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_deletions ("
                "  jid TEXT PRIMARY KEY,"
                "  requested_ts REAL NOT NULL"
                ")"
            )
            # API-Token je Account (nur der SHA-256-Hash wird gespeichert, nie der
            # Klartext-Token). Authentifiziert die optionale Read-API.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS api_tokens ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  jid TEXT NOT NULL,"
                "  token_hash TEXT UNIQUE NOT NULL,"
                "  label TEXT,"
                "  created_ts REAL NOT NULL,"
                "  last_used_ts REAL"
                ")"
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            logger.warning("Konnte Dateirechte fuer accounts.db nicht setzen")

    # Frische Verbindung je Operation (thread-sicher fuer den Web-Threadpool).
    def _open(self):
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # Pfade je Account (eigenes Chat-Archiv + OMEMO-State).
    def archive_path(self, jid):
        return os.path.join(self._users_dir, account_slug(jid), "messages.sqlite")

    def state_path(self, jid):
        return os.path.join(self._users_dir, account_slug(jid), "omemo_state.sqlite")

    def account_dir(self, jid):
        return os.path.join(self._users_dir, account_slug(jid))

    def _ensure_dir(self, jid):
        os.makedirs(os.path.join(self._users_dir, account_slug(jid)), exist_ok=True)

    # Legt einen Account an oder aktualisiert ihn (Passwort wird verschluesselt).
    # Setzt auth_state='pending' -> der Manager validiert ueber die echte Verbindung.
    def upsert(self, jid, password, host=None, port=5222, resource="compass", muc_nick=None):
        self._ensure_dir(jid)
        enc = self._fernet.encrypt(password.encode("utf8")).decode("ascii")
        now = time.time()
        conn = self._open()
        try:
            conn.execute(
                "INSERT INTO accounts (jid, password_enc, host, port, resource, muc_nick, enabled, auth_state, created_ts, last_login_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?) "
                "ON CONFLICT(jid) DO UPDATE SET "
                "  password_enc = excluded.password_enc, host = excluded.host, port = excluded.port, "
                "  resource = excluded.resource, muc_nick = excluded.muc_nick, enabled = 1, "
                "  auth_state = 'pending', last_login_ts = excluded.last_login_ts",
                (jid, enc, host, port, resource, muc_nick, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def _exec(self, sql, params=()):
        conn = self._open()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _query_one(self, sql, params=()):
        conn = self._open()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    # accounts.id einer JID = user_id fuer den Aggregator-Teil (compass.db).
    def account_id(self, jid):
        row = self._query_one("SELECT id FROM accounts WHERE jid = ?", (jid,))
        return row["id"] if row else None

    # JID zu einer accounts.id (Rueckabbildung fuer Aggregator -> Chat/Anzeige).
    def jid_for_id(self, user_id):
        row = self._query_one("SELECT jid FROM accounts WHERE id = ?", (user_id,))
        return row["jid"] if row else None

    def set_enabled(self, jid, enabled):
        self._exec("UPDATE accounts SET enabled = ? WHERE jid = ?", (1 if enabled else 0, jid))

    # Markiert einen Account zur vollstaendigen Loeschung: deaktiviert ihn sofort
    # (der Manager trennt die Verbindung) und merkt ihn vor. Die Daten werden danach
    # vom Manager geloescht, sobald die Verbindung getrennt ist (kein offener DB-Zugriff).
    def request_deletion(self, jid):
        self._exec("UPDATE accounts SET enabled = 0 WHERE jid = ?", (jid,))
        self._exec(
            "INSERT INTO pending_deletions (jid, requested_ts) VALUES (?, ?) "
            "ON CONFLICT(jid) DO NOTHING",
            (jid, time.time()),
        )

    def pending_deletions(self):
        conn = self._open()
        try:
            return [r["jid"] for r in conn.execute("SELECT jid FROM pending_deletions").fetchall()]
        finally:
            conn.close()

    def finalize_deletion(self, jid):
        self._exec("DELETE FROM api_tokens WHERE jid = ?", (jid,))
        self._exec("DELETE FROM accounts WHERE jid = ?", (jid,))
        self._exec("DELETE FROM pending_deletions WHERE jid = ?", (jid,))

    def set_auth_state(self, jid, state):
        self._exec("UPDATE accounts SET auth_state = ? WHERE jid = ?", (state, jid))

    def get_auth_state(self, jid):
        row = self._query_one("SELECT auth_state FROM accounts WHERE jid = ?", (jid,))
        return row["auth_state"] if row else None

    # Aktiv-Flag + Auth-Status fuer die Online-Anzeige in der UI.
    def get_state(self, jid):
        row = self._query_one("SELECT enabled, auth_state FROM accounts WHERE jid = ?", (jid,))
        if not row:
            return None
        return {"enabled": bool(row["enabled"]), "auth_state": row["auth_state"]}

    def exists(self, jid):
        return self._query_one("SELECT 1 FROM accounts WHERE jid = ?", (jid,)) is not None

    # True, wenn der Account aktiv und bereits erfolgreich validiert/verbunden ist.
    def is_ok(self, jid):
        row = self._query_one("SELECT enabled, auth_state FROM accounts WHERE jid = ?", (jid,))
        return bool(row and row["enabled"] and row["auth_state"] == "ok")

    # True, wenn der Account aktiv, validiert (ok) und das Passwort uebereinstimmt.
    def verified_match(self, jid, password):
        row = self._query_one("SELECT password_enc, enabled, auth_state FROM accounts WHERE jid = ?", (jid,))
        if not row or not row["enabled"] or row["auth_state"] != "ok":
            return False
        try:
            return self._fernet.decrypt(row["password_enc"].encode("ascii")).decode("utf8") == password
        except Exception:
            return False

    # Liefert aktive Accounts inkl. entschluesseltem Passwort (fuer den Manager).
    def enabled_accounts(self):
        conn = self._open()
        try:
            rows = conn.execute(
                "SELECT jid, password_enc, host, port, resource, muc_nick, auth_state FROM accounts WHERE enabled = 1"
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            try:
                password = self._fernet.decrypt(r["password_enc"].encode("ascii")).decode("utf8")
            except Exception:
                logger.error("Passwort fuer %s nicht entschluesselbar (falscher fernet_key?)", r["jid"])
                continue
            result.append({
                "jid": r["jid"], "password": password, "host": r["host"], "port": r["port"],
                "resource": r["resource"] or "compass", "muc_nick": r["muc_nick"],
                "auth_state": r["auth_state"],
            })
        return result

    # --- API-Token ----------------------------------------------------------

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode("utf8")).hexdigest()

    # Erzeugt einen neuen API-Token (gibt den Klartext EINMALIG zurueck; gespeichert
    # wird nur der Hash). label ist eine frei waehlbare Bezeichnung.
    def create_api_token(self, jid, label=""):
        token = secrets.token_urlsafe(32)
        self._exec(
            "INSERT INTO api_tokens (jid, token_hash, label, created_ts) VALUES (?, ?, ?, ?)",
            (jid, self._hash_token(token), (label or "").strip()[:80], time.time()),
        )
        return token

    def list_api_tokens(self, jid):
        conn = self._open()
        try:
            rows = conn.execute(
                "SELECT id, label, created_ts, last_used_ts FROM api_tokens WHERE jid = ? ORDER BY id DESC",
                (jid,),
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r["id"], "label": r["label"], "created_ts": r["created_ts"],
                 "last_used_ts": r["last_used_ts"]} for r in rows]

    def revoke_api_token(self, jid, token_id):
        self._exec("DELETE FROM api_tokens WHERE id = ? AND jid = ?", (token_id, jid))

    # Loest einen Klartext-Token zur JID auf (oder None). Aktualisiert last_used_ts.
    def account_for_token(self, token):
        if not token:
            return None
        row = self._query_one(
            "SELECT id, jid FROM api_tokens WHERE token_hash = ?", (self._hash_token(token),)
        )
        if not row:
            return None
        self._exec("UPDATE api_tokens SET last_used_ts = ? WHERE id = ?", (time.time(), row["id"]))
        return row["jid"]
