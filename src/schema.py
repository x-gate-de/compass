# -----------------------------------------------------------------------------
# Skript: src/schema.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Zentrales SQLite-Schema fuer den Aggregator-Teil von compass (compass.db):
#   Feed-Gruppen, Feeds, normalisierte Items, deren LLM-Bewertung und Nutzer-Status.
#   Wird von Daemon (Schreiber) und Web-UI (Leser) gemeinsam genutzt.
# Ablauf:
# - ensure_schema(conn) legt alle Tabellen/Indizes idempotent an, aktiviert WAL.
# - Spaltenmigrationen sind idempotent (ADD COLUMN nur falls fehlend).
# Betriebs- und Wartungshinweise:
# - WAL erlaubt gleichzeitiges Lesen (Web-UI) und Schreiben (Daemon).
# - user_id ist die accounts.id aus der Account-Registry (accounts.db). Es gibt hier
#   bewusst KEINE Cross-DB-Fremdschluessel; user_id ist denormalisiert. Beim Loeschen
#   eines Accounts raeumt purge_user() die zugehoerigen Aggregator-Daten auf.
# - Die Chat-Nachrichten selbst liegen NICHT hier, sondern je Account in
#   users_dir/<slug>/messages.sqlite (siehe Account-Registry).
# -----------------------------------------------------------------------------

import logging

logger = logging.getLogger(__name__)


# Ergaenzt eine Spalte, falls sie in der Tabelle noch nicht existiert.
def _add_column_if_missing(conn, table, column, ddl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# Legt alle Aggregator-Tabellen/Indizes idempotent an und aktiviert WAL + busy_timeout.
def ensure_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    # Grosszuegiger Lock-Timeout: Daemon (Bewertung) und Web schreiben beide compass.db.
    conn.execute("PRAGMA busy_timeout=15000")
    # Fremdschluessel innerhalb dieser DB aktiv halten (feed_groups->feeds->items->scores).
    conn.execute("PRAGMA foreign_keys=ON")

    # Globaler Wichtigkeits-Profiltext je Nutzer (Scoring-Kontext). user_id = accounts.id.
    # Ersetzt die fruehere users.profile_text aus NextUp (dort gab es eine eigene
    # Nutzertabelle; in compass ist der XMPP-Account die Identitaet).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_profiles ("
        "  user_id INTEGER PRIMARY KEY,"
        "  profile_text TEXT,"
        "  updated_ts REAL"
        ")"
    )

    # Feed-Gruppen je Nutzer. priority ist eine Stufe 1..5 (Default 3 = neutral).
    # profile_text ergaenzt den Nutzer-Profiltext fuer das Scoring dieser Gruppe.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feed_groups ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL,"
        "  name TEXT NOT NULL,"
        "  priority INTEGER NOT NULL DEFAULT 3,"
        "  position INTEGER NOT NULL DEFAULT 0,"
        "  profile_text TEXT,"
        "  created_ts REAL NOT NULL,"
        "  UNIQUE (user_id, name)"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_groups_user ON feed_groups (user_id)")

    # Feeds = konfigurierte Instanz eines Connector-Typs. config_enc enthaelt die
    # komplette Feed-Konfiguration inkl. Secrets als Fernet-Text (JSON -> Fernet).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feeds ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  group_id INTEGER NOT NULL REFERENCES feed_groups(id) ON DELETE CASCADE,"
        "  connector_type TEXT NOT NULL,"       # 'chat','imap','caldav','odoo_helpdesk','odoo_project','grafana','rss'
        "  name TEXT NOT NULL,"
        "  priority INTEGER NOT NULL DEFAULT 3,"
        "  config_enc TEXT NOT NULL,"           # Fernet-verschluesselte Konfiguration (Secrets)
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  poll_interval INTEGER,"              # NULL = Default aus config.yaml
        "  last_poll_ts REAL,"
        "  next_poll_ts REAL,"
        "  status TEXT NOT NULL DEFAULT 'new'," # new / ok / error
        "  status_detail TEXT,"                 # Fehlertext (ohne Secrets) zur Anzeige
        "  error_count INTEGER NOT NULL DEFAULT 0,"
        "  created_ts REAL NOT NULL"
        ")"
    )
    # poll_cursor haelt den hoechsten gesehenen Quell-Zeitstempel (since fuer den naechsten
    # Abruf); getrennt von last_poll_ts (Wanduhr des letzten Pollversuchs).
    _add_column_if_missing(conn, "feeds", "poll_cursor", "poll_cursor REAL")
    # llm_scoring=0: Items dieses Feeds NICHT vom LLM bewerten (deterministisch aus
    # Prioritaet + Aktualitaet). Fuer Hochvolumen-Quellen wie Syslog-Raeume.
    _add_column_if_missing(conn, "feeds", "llm_scoring", "llm_scoring INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feeds_group ON feeds (group_id)")
    # Faelligkeit des naechsten Polls: Scheduler holt enabled-Feeds nach next_poll_ts.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feeds_due ON feeds (enabled, next_poll_ts)")

    # Normalisierte Eintraege aus allen Quellen. dedup_key (Quelle + externe ID) macht
    # das Einspielen idempotent. ts_due treibt bei terminbezogenen Quellen den Decay.
    # content_hash erkennt inhaltliche Aenderungen und loest Re-Scoring aus.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS items ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,"
        "  user_id INTEGER NOT NULL,"           # denormalisiert fuer schnelle Mandantenfilter
        "  dedup_key TEXT UNIQUE NOT NULL,"
        "  source_type TEXT NOT NULL,"
        "  external_id TEXT,"
        "  title TEXT,"
        "  body TEXT,"
        "  sender TEXT,"
        "  url TEXT,"
        "  ts_source REAL,"                     # Zeitpunkt aus der Quelle (Mail-Datum etc.)
        "  ts_due REAL,"                        # optional: Termin/Frist -> Decay-Boost
        "  ts_fetched REAL NOT NULL,"
        "  content_hash TEXT,"
        "  raw_json TEXT"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_feed ON items (feed_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_user ON items (user_id)")

    # Bewertung je Item (1:1). base_score = urgency * Prio-Gewichte; final_score nach
    # Decay/Override. override nur gueltig, wenn urgency >= Schwelle (siehe SPEC F3).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_scores ("
        "  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,"
        "  urgency INTEGER,"
        "  reason TEXT,"
        "  override INTEGER NOT NULL DEFAULT 0,"
        "  group_prio INTEGER,"
        "  feed_prio INTEGER,"
        "  base_score REAL,"
        "  final_score REAL,"
        "  model TEXT,"
        "  scored_ts REAL,"                     # letzte LLM-Bewertung
        "  decay_ts REAL"                       # letzte deterministische Decay-Aktualisierung
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_final ON item_scores (final_score)")
    # Vergleichender Re-Rank-Pass (LLM sieht die Top-N gemeinsam): rank_score = Anzeige-
    # Kennzahl 0..100, rank_sort = Sortierschluessel auf der final_score-Skala. Beide NULL,
    # solange ein Item nicht (mehr) im Re-Rank-Fenster liegt.
    _add_column_if_missing(conn, "item_scores", "rank_score", "rank_score REAL")
    _add_column_if_missing(conn, "item_scores", "rank_sort", "rank_sort REAL")

    # Nutzer-Status je Item (nur in compass, kein Rueckschreiben in Quellsysteme).
    # snoozed_until in der Zukunft blendet das Item bis dahin aus; done dauerhaft.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS item_state ("
        "  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,"
        "  seen INTEGER NOT NULL DEFAULT 0,"
        "  snoozed_until REAL,"
        "  done INTEGER NOT NULL DEFAULT 0,"
        "  updated_ts REAL"
        ")"
    )

    # Manuelle Sync-Anstoesse aus der Web-UI ("jetzt synchronisieren"); der Daemon
    # arbeitet sie zeitnah ab (Sofort-Poll des Feeds, unabhaengig vom Poll-Intervall).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_requests ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,"
        "  status TEXT NOT NULL DEFAULT 'pending',"   # pending / done / error
        "  detail TEXT,"
        "  created_ts REAL NOT NULL,"
        "  done_ts REAL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_status ON sync_requests (status, id)")

    # Debug-Ereignisse fuer die Web-UI (Entwicklung): Poll-Ergebnisse und exakte
    # LLM-Ein-/Ausgabe je Item. Wird auf die letzten N Eintraege getrimmt.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS debug_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts REAL NOT NULL,"
        "  feed_id INTEGER,"
        "  user_id INTEGER,"
        "  kind TEXT NOT NULL,"          # poll / llm / sync / error
        "  summary TEXT,"
        "  detail TEXT"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_debug_user ON debug_events (user_id, id)")

    # Grafana-Panels, die im NextUp-Dashboard an gewaehlter Position eingebettet werden.
    # embed_url ist die bereits einbettbare URL (d-solo bzw. public-dashboard). position =
    # Einfuege-Index im Item-Strom (0 = ganz oben). width: 'half'|'full'. height in px.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grafana_panels ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL,"
        "  title TEXT,"
        "  embed_url TEXT NOT NULL,"
        "  position INTEGER NOT NULL DEFAULT 0,"
        "  width TEXT NOT NULL DEFAULT 'full',"
        "  height INTEGER NOT NULL DEFAULT 260,"
        "  auth_enc TEXT,"                # Fernet-verschluesselt: {"user","pass"} oder {"token"}
        "  mode TEXT NOT NULL DEFAULT 'image',"  # 'image' (Render+Token) oder 'iframe' (Live)
        "  created_ts REAL NOT NULL"
        ")"
    )
    _add_column_if_missing(conn, "grafana_panels", "auth_enc", "auth_enc TEXT")
    _add_column_if_missing(conn, "grafana_panels", "mode", "mode TEXT NOT NULL DEFAULT 'image'")
    # Rahmenfarbe der Panel-Kachel (Hex, leer = Standardrahmen).
    _add_column_if_missing(conn, "grafana_panels", "color", "color TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gpanels_user ON grafana_panels (user_id, position, id)")

    # News-Ticker: je Nutzer bis zu 3 Odoo-HelpDesk-Teams. Der Daemon holt zyklisch alle
    # offenen/in-Bearbeitung-Tickets des Teams, laesst das LLM EINE Top-Schlagzeile
    # formulieren und legt sie hier ab; die Web-UI zeigt sie als Laufband.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ticker_teams ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL,"
        "  team_id INTEGER NOT NULL,"          # Odoo helpdesk.team-ID
        "  team_name TEXT NOT NULL,"
        "  config_enc TEXT NOT NULL,"          # Fernet: Odoo-Zugang (url/db/login/api-key)
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  position INTEGER NOT NULL DEFAULT 0,"
        "  headline TEXT,"                     # letzte LLM-Schlagzeile
        "  ticket_count INTEGER,"              # Anzahl offener Tickets beim letzten Lauf
        "  headline_ts REAL,"
        "  status TEXT NOT NULL DEFAULT 'new'," # new / ok / error
        "  status_detail TEXT,"                 # Fehlertext (ohne Secrets)
        "  created_ts REAL NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_user ON ticker_teams (user_id, position, id)")

    # Laufzeit-Einstellungen (Settings-Seite): Key-Value, ueberschreiben die Defaults
    # aus config.yaml (z.B. ticker.interval). Der Daemon liest sie je Zyklus neu ->
    # Aenderungen wirken ohne Neustart.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_settings ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL,"
        "  updated_ts REAL NOT NULL"
        ")"
    )

    # Dashboard-Kacheln: frei positionierbare Funktionskacheln im NextUp-Strom
    # (kind: briefing / team / ops / review), Layout wie Grafana-Panels.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_tiles ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER NOT NULL,"
        "  kind TEXT NOT NULL,"
        "  title TEXT,"
        "  position INTEGER NOT NULL DEFAULT 0,"
        "  width TEXT NOT NULL DEFAULT '4',"
        "  color TEXT,"
        "  created_ts REAL NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_user ON dashboard_tiles (user_id, position, id)")

    # Vom Daemon erzeugte Kachel-Inhalte (Briefing-Text, Rueckblick-Daten) als JSON.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tile_content ("
        "  kind TEXT PRIMARY KEY,"
        "  payload TEXT,"
        "  updated_ts REAL"
        ")"
    )

    # Kalender-Zuordnung: welche SoGo-Sammlung dient welcher Funktion
    # (absence = Abwesenheiten, oncall = Rufbereitschaft). Zugang liegt zentral
    # Fernet-verschluesselt in app_settings (calendar.config_enc).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calendar_feeds ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  path TEXT NOT NULL UNIQUE,"    # DAV-Pfad der Sammlung ODER https-ICS-URL
        "  name TEXT NOT NULL,"
        "  role TEXT NOT NULL,"           # absence / oncall / other
        "  created_ts REAL NOT NULL"
        ")"
    )
    # Direkte iCal-Feeds (z.B. phatman timetracking.example.com/ical) koennen eigene
    # Zugangsdaten tragen (Fernet); NULL = Standard-Zugang des Kalender-Moduls.
    _add_column_if_missing(conn, "calendar_feeds", "config_enc", "config_enc TEXT")

    # Kalender-Modul: EIN globaler Snapshot des iCal-Feeds (SoGo) als JSON
    # (Ereignisfenster; genutzt u.a. vom Arbeitszeit-Band fuer NICHT DA/Rufbereitschaft).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calendar_status ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  payload TEXT,"
        "  updated_ts REAL"
        ")"
    )

    # Arbeitszeit-Laufband: EIN globaler Snapshot der Zeiterfassung (phatman) als JSON.
    # Global, weil es genau eine Firmen-Zeiterfassung gibt; Zugang liegt in config.yaml.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS worktime_status ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  payload TEXT,"                # JSON-Snapshot (users, date, year, error)
        "  updated_ts REAL"
        ")"
    )

    conn.commit()


# Entfernt alle Aggregator-Daten eines Nutzers (Account-Loeschung). Feeds/Items/Scores
# haengen per ON DELETE CASCADE an feed_groups; Profil und Items werden zusaetzlich
# direkt geraeumt (items.user_id ist denormalisiert, ohne Cross-DB-FK).
def purge_user(conn, user_id):
    conn.execute("DELETE FROM feed_groups WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM items WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM debug_events WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM grafana_panels WHERE user_id = ?", (user_id,))
    conn.commit()
