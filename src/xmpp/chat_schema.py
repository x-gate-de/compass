# -----------------------------------------------------------------------------
# Skript: src/xmpp/chat_schema.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Je-Account-SQLite-Schema fuer den Chat-Teil: Archiv, Outbox, Read-State, Kontakte,
#   MUC, OMEMO-Geraete, Avatare und Web-Push. Wird von Daemon (Schreiber) und Web-UI
#   genutzt. Liegt je Account in users_dir/<slug>/messages.sqlite.
# Betriebs- und Wartungshinweise:
# - WAL erlaubt gleichzeitiges Lesen/Schreiben durch beide Dienste.
# - Spaltenmigrationen sind idempotent (ADD COLUMN nur falls fehlend).
# - Bewusst getrennt vom Aggregator-Schema (src/schema.py); daher eigener
#   Funktionsname ensure_chat_schema.
# -----------------------------------------------------------------------------


# Ergaenzt eine Spalte, falls sie in der Tabelle noch nicht existiert.
def _add_column_if_missing(conn, table, column, ddl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# Legt alle Chat-Tabellen/Indizes idempotent an und aktiviert WAL + busy_timeout.
def ensure_chat_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Archivierte Nachrichten (1:1 entschluesselt, MUC unverschluesselt).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  dedup_key TEXT UNIQUE,"
        "  partner_jid TEXT NOT NULL,"        # 1:1-Partner oder Raum-JID
        "  direction TEXT NOT NULL,"          # 'in' / 'out'
        "  body TEXT,"
        "  decrypted INTEGER NOT NULL,"
        "  ts_received REAL NOT NULL,"
        "  namespace TEXT"
        ")"
    )
    # Migrationen fuer bestehende DBs.
    _add_column_if_missing(conn, "messages", "sender", "sender TEXT")       # MUC-Nick
    _add_column_if_missing(conn, "messages", "status", "status TEXT")       # out: sent/delivered/error
    _add_column_if_missing(conn, "messages", "msg_id", "msg_id TEXT")       # XMPP-Message-ID (Empfangsbestaetigung)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_partner_ts ON messages (partner_jid, ts_received)"
    )

    # Sendeauftraege der Web-UI; der Daemon arbeitet sie ab.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  recipient_jid TEXT NOT NULL,"
        "  body TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  error TEXT,"
        "  created_ts REAL NOT NULL,"
        "  sent_ts REAL"
        ")"
    )
    _add_column_if_missing(conn, "outbox", "kind", "kind TEXT NOT NULL DEFAULT 'chat'")  # chat/groupchat/media
    # Anhaenge (kind='media'): Spool-Datei + Metadaten fuer den Daemon-Upload.
    _add_column_if_missing(conn, "outbox", "media_path", "media_path TEXT")
    _add_column_if_missing(conn, "outbox", "media_name", "media_name TEXT")
    _add_column_if_missing(conn, "outbox", "media_mime", "media_mime TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status, id)")

    # Lesezustand je Konversation/Raum.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS read_state ("
        "  partner_jid TEXT PRIMARY KEY,"
        "  last_read_ts REAL NOT NULL"
        ")"
    )

    # Anfragen der Web-UI, aeltere Nachrichten per MAM (XEP-0313) nachzuladen.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mam_requests ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  target_jid TEXT NOT NULL,"
        "  kind TEXT NOT NULL,"              # 'chat' (1:1) oder 'muc' (Raum)
        "  status TEXT NOT NULL DEFAULT 'pending',"  # pending / done / error
        "  created_ts REAL NOT NULL"
        ")"
    )
    # Paginierungs-Stand je Ziel: Beginn des zuletzt geladenen Zeitfensters.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mam_state ("
        "  target_jid TEXT PRIMARY KEY,"
        "  oldest_ts REAL"
        ")"
    )

    # OMEMO-Geraete je JID (vom Daemon gefuellt) fuer Fingerprint-Anzeige/Verifizierung.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS omemo_devices ("
        "  jid TEXT NOT NULL,"
        "  device_id INTEGER NOT NULL,"
        "  fingerprint TEXT,"            # gruppiert, zur Anzeige
        "  identity_hex TEXT,"          # roh (fuer set_trust)
        "  trust TEXT,"
        "  is_own INTEGER NOT NULL DEFAULT 0,"
        "  label TEXT,"
        "  updated_ts REAL,"
        "  PRIMARY KEY (jid, device_id)"
        ")"
    )
    # Anfragen der Web-UI an den Daemon: Geraete aktualisieren / Trust setzen.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS omemo_requests ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  action TEXT NOT NULL,"        # 'refresh' | 'trust'
        "  jid TEXT NOT NULL,"
        "  identity_hex TEXT,"
        "  trust_value TEXT,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  created_ts REAL NOT NULL"
        ")"
    )

    # Roster-Kontakte (vom Daemon gepflegt) - Quelle der Userliste.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS contacts ("
        "  jid TEXT PRIMARY KEY,"
        "  name TEXT,"
        "  subscription TEXT"
        ")"
    )

    # Beigetretene MUC-Raeume (autojoin durch den Daemon).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mucs ("
        "  room_jid TEXT PRIMARY KEY,"
        "  name TEXT,"
        "  nick TEXT,"
        "  joined INTEGER NOT NULL DEFAULT 1"
        ")"
    )

    # Auf dem Server verfuegbare oeffentliche Raeume (vom Daemon per Disco befuellt).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS muc_available ("
        "  room_jid TEXT PRIMARY KEY,"
        "  name TEXT,"
        "  updated_ts REAL"
        ")"
    )

    # Web-Push: Geraete-Abos dieses Accounts (vom Web eingetragen, vom Daemon genutzt).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  endpoint TEXT UNIQUE NOT NULL,"
        "  p256dh TEXT NOT NULL,"
        "  auth TEXT NOT NULL,"
        "  created_ts REAL NOT NULL"
        ")"
    )
    # Push-Auswahl je Konversation/Raum (nur aktivierte loesen Push aus).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS push_prefs ("
        "  partner_jid TEXT PRIMARY KEY,"
        "  enabled INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    # Avatare je Kontakt (vCard-Foto, XEP-0153/0054). data leer = kein Foto
    # (Negativ-Marker, damit nicht bei jedem Presence neu geladen wird).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS avatars ("
        "  jid TEXT PRIMARY KEY,"
        "  mime TEXT,"
        "  data BLOB,"
        "  hash TEXT,"
        "  updated_ts REAL"
        ")"
    )
    conn.commit()
