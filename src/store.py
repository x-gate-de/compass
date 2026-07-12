# -----------------------------------------------------------------------------
# Skript: src/store.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Datenzugriffsschicht ueber dem Aggregator-Schema (compass.db): Gruppen, Feeds, Items,
#   Bewertungen, Status, Ranking-Abfrage, Nutzerprofile und Retention-Aufraeumen.
# Betriebs- und Wartungshinweise:
# - upsert_item ist idempotent (Dedup ueber dedup_key) und meldet inhaltliche Aenderungen,
#   damit der Daemon gezielt ein Re-Scoring ausloesen kann.
# - Alle nutzerbezogenen Abfragen filtern strikt auf user_id (Mandantentrennung).
# - user_id = accounts.id aus der Account-Registry (accounts.db). Es gibt keine lokale
#   users-Tabelle; der globale Wichtigkeits-Profiltext liegt in user_profiles.
# -----------------------------------------------------------------------------

import json
import logging

logger = logging.getLogger(__name__)


# --- Nutzerprofil (globaler Scoring-Kontext) --------------------------------

def get_user_profile(conn, user_id):
    row = conn.execute("SELECT profile_text FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    return row["profile_text"] if row else None


# Setzt/aktualisiert den globalen Wichtigkeits-Profiltext eines Nutzers. Eine Aenderung
# sollte vom Aufrufer mit clear_scores_for_user gekoppelt werden (Neubewertung).
def set_user_profile(conn, user_id, profile_text, now):
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET profile_text = excluded.profile_text, updated_ts = excluded.updated_ts",
        (user_id, profile_text, now),
    )
    conn.commit()


# --- Feed-Gruppen -----------------------------------------------------------

def create_group(conn, user_id, name, now, priority=3, position=0, profile_text=None):
    cur = conn.execute(
        "INSERT INTO feed_groups (user_id, name, priority, position, profile_text, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, name, priority, position, profile_text, now),
    )
    conn.commit()
    return cur.lastrowid


def list_groups(conn, user_id):
    return conn.execute(
        "SELECT * FROM feed_groups WHERE user_id = ? ORDER BY position, id", (user_id,)
    ).fetchall()


# --- Feeds ------------------------------------------------------------------

def create_feed(conn, group_id, connector_type, name, config_enc, now, priority=3,
                poll_interval=None, llm_scoring=True):
    cur = conn.execute(
        "INSERT INTO feeds (group_id, connector_type, name, priority, config_enc, enabled, "
        "poll_interval, next_poll_ts, status, error_count, created_ts, llm_scoring) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'new', 0, ?, ?)",
        (group_id, connector_type, name, priority, config_enc, poll_interval, now, now,
         1 if llm_scoring else 0),
    )
    conn.commit()
    return cur.lastrowid


def get_feed(conn, feed_id):
    return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()


# Liefert faellige, aktive Feeds (next_poll_ts <= now oder noch nie gepollt).
def list_due_feeds(conn, now):
    return conn.execute(
        "SELECT * FROM feeds WHERE enabled = 1 AND (next_poll_ts IS NULL OR next_poll_ts <= ?) "
        "ORDER BY next_poll_ts",
        (now,),
    ).fetchall()


# Schreibt Poll-Ergebnis (Status + naechster Pollzeitpunkt) zurueck. Fehlertext ohne Secrets.
def update_feed_status(conn, feed_id, status, next_poll_ts, status_detail=None, last_poll_ts=None, error_count=None):
    fields = ["status = ?", "next_poll_ts = ?", "status_detail = ?"]
    args = [status, next_poll_ts, status_detail]
    if last_poll_ts is not None:
        fields.append("last_poll_ts = ?")
        args.append(last_poll_ts)
    if error_count is not None:
        fields.append("error_count = ?")
        args.append(error_count)
    args.append(feed_id)
    conn.execute(f"UPDATE feeds SET {', '.join(fields)} WHERE id = ?", args)
    conn.commit()


# --- Items ------------------------------------------------------------------

# Legt ein Item an oder aktualisiert es bei Inhaltsaenderung.
# Rueckgabe: (item_id, is_new, content_changed).
def upsert_item(conn, item, now):
    row = conn.execute(
        "SELECT id, content_hash FROM items WHERE dedup_key = ?", (item.dedup_key,)
    ).fetchone()
    content_hash = item.content_hash
    raw_json = json.dumps(item.raw) if item.raw is not None else None
    if row is None:
        cur = conn.execute(
            "INSERT INTO items (feed_id, user_id, dedup_key, source_type, external_id, title, body, "
            "sender, url, ts_source, ts_due, ts_fetched, content_hash, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.feed_id, item.user_id, item.dedup_key, item.source_type, item.external_id,
             item.title, item.body, item.sender, item.url, item.ts_source, item.ts_due,
             now, content_hash, raw_json),
        )
        conn.commit()
        return cur.lastrowid, True, True
    item_id = row["id"]
    if row["content_hash"] != content_hash:
        conn.execute(
            "UPDATE items SET title = ?, body = ?, sender = ?, url = ?, ts_source = ?, ts_due = ?, "
            "content_hash = ?, raw_json = ? WHERE id = ?",
            (item.title, item.body, item.sender, item.url, item.ts_source, item.ts_due,
             content_hash, raw_json, item_id),
        )
        conn.commit()
        return item_id, False, True
    return item_id, False, False


# --- Bewertung --------------------------------------------------------------

# Schreibt/aktualisiert die Bewertung eines Items (1:1, Upsert ueber item_id).
def set_score(conn, item_id, urgency, reason, override, group_prio, feed_prio, base, final, model, now):
    conn.execute(
        "INSERT INTO item_scores (item_id, urgency, reason, override, group_prio, feed_prio, "
        "base_score, final_score, model, scored_ts, decay_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET urgency = excluded.urgency, reason = excluded.reason, "
        "override = excluded.override, group_prio = excluded.group_prio, feed_prio = excluded.feed_prio, "
        "base_score = excluded.base_score, final_score = excluded.final_score, model = excluded.model, "
        "scored_ts = excluded.scored_ts, decay_ts = excluded.decay_ts",
        (item_id, urgency, reason, 1 if override else 0, group_prio, feed_prio, base, final, model, now, now),
    )
    conn.commit()


# Aktualisiert nur den deterministischen Decay-Anteil (ohne erneuten LLM-Aufruf).
def update_final_score(conn, item_id, final, now):
    conn.execute(
        "UPDATE item_scores SET final_score = ?, decay_ts = ? WHERE item_id = ?",
        (final, now, item_id),
    )
    conn.commit()


# Verwirft die Bewertung eines Items (z.B. nach Inhaltsaenderung) -> wird neu bewertet.
def clear_score(conn, item_id):
    conn.execute("DELETE FROM item_scores WHERE item_id = ?", (item_id,))
    conn.commit()


# Aktuell gespeicherte dedup_keys eines Feeds (damit Connectoren nur Neues laden).
def feed_dedup_keys(conn, feed_id):
    return set(r[0] for r in conn.execute("SELECT dedup_key FROM items WHERE feed_id = ?", (feed_id,)))


# Entfernt Items eines Feeds, die in der Quelle nicht mehr vorhanden sind (Abgleich).
# present_keys = aktuell in der Quelle vorhandene dedup_keys. Rueckgabe: Anzahl entfernt.
def reconcile_feed(conn, feed_id, present_keys):
    rows = conn.execute("SELECT id, dedup_key FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    stale = [r["id"] for r in rows if r["dedup_key"] not in present_keys]
    for item_id in stale:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return len(stale)


# --- Status -----------------------------------------------------------------

def set_state(conn, item_id, now, seen=None, snoozed_until=None, done=None):
    existing = conn.execute("SELECT item_id FROM item_state WHERE item_id = ?", (item_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO item_state (item_id, seen, snoozed_until, done, updated_ts) VALUES (?, ?, ?, ?, ?)",
            (item_id, 1 if seen else 0, snoozed_until, 1 if done else 0, now),
        )
    else:
        sets, args = [], []
        if seen is not None:
            sets.append("seen = ?")
            args.append(1 if seen else 0)
        if snoozed_until is not None:
            sets.append("snoozed_until = ?")
            args.append(snoozed_until)
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
        sets.append("updated_ts = ?")
        args.append(now)
        args.append(item_id)
        conn.execute(f"UPDATE item_state SET {', '.join(sets)} WHERE item_id = ?", args)
    conn.commit()


# --- Ranking ----------------------------------------------------------------

# Offene Items eines Nutzers nach final_score absteigend. Optional auf eine Gruppe gefiltert.
def get_ranking(conn, user_id, now, group_id=None, limit=200):
    query = (
        "SELECT i.*, s.final_score, s.urgency, s.reason, s.override, s.rank_score, "
        "g.id AS group_id, g.name AS group_name, "
        "COALESCE(st.seen, 0) AS seen "
        "FROM items i "
        "JOIN item_scores s ON s.item_id = i.id "
        "JOIN feeds f ON f.id = i.feed_id "
        "JOIN feed_groups g ON g.id = f.group_id "
        "LEFT JOIN item_state st ON st.item_id = i.id "
        "WHERE i.user_id = ? AND COALESCE(st.done, 0) = 0 "
        "AND (st.snoozed_until IS NULL OR st.snoozed_until < ?) "
    )
    args = [user_id, now]
    if group_id is not None:
        query += "AND g.id = ? "
        args.append(group_id)
    # Sortierung: vergleichender rank_sort hat Vorrang (Top-N-Reihenfolge aus dem Re-Rank),
    # sonst final_score. Beide auf derselben Skala -> neue, noch nicht re-gerankte Items
    # ordnen sich per final_score korrekt ein. Tie-Breaker: neuestes Item, dann ID.
    query += "ORDER BY COALESCE(s.rank_sort, s.final_score) DESC, i.ts_source DESC, i.id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(query, args).fetchall()


# --- Vergleichender Re-Rank (LLM sieht die Top-N gemeinsam) ------------------

# Aktive Nutzer fuer den periodischen Re-Rank-Pass = Nutzer mit mindestens einer
# Feed-Gruppe (nur diese haben ueberhaupt Items). Profiltext aus user_profiles.
def list_active_users(conn):
    return conn.execute(
        "SELECT DISTINCT g.user_id AS id, "
        "  (SELECT p.profile_text FROM user_profiles p WHERE p.user_id = g.user_id) AS profile_text "
        "FROM feed_groups g ORDER BY g.user_id"
    ).fetchall()


# Setzt rank_score/rank_sort aller Items eines Nutzers zurueck (vor dem Neuschreiben).
def clear_rank_scores(conn, user_id):
    conn.execute(
        "UPDATE item_scores SET rank_score = NULL, rank_sort = NULL "
        "WHERE item_id IN (SELECT id FROM items WHERE user_id = ?)",
        (user_id,),
    )


# Schreibt das Re-Rank-Ergebnis eines Items: Anzeige-Score (0-100) und Sortierschluessel.
def set_rank_score(conn, item_id, rank_score, rank_sort):
    conn.execute(
        "UPDATE item_scores SET rank_score = ?, rank_sort = ? WHERE item_id = ?",
        (rank_score, rank_sort, item_id),
    )


# Items, die (neu) bewertet werden muessen, inkl. Scoring-Kontext (Prioritaeten + Profile).
# rescore_age <= 0: NUR noch nicht bewertete Items. rescore_age > 0: zusaetzlich bereits
# bewertete offene Items, deren Bewertung aelter als rescore_age ist.
def get_items_to_score(conn, now, rescore_age, limit=200):
    select = (
        "SELECT i.*, f.priority AS feed_prio, g.priority AS group_prio, "
        "f.llm_scoring AS llm_scoring, "
        "g.profile_text AS group_profile, u.profile_text AS user_profile "
        "FROM items i "
        "JOIN feeds f ON f.id = i.feed_id "
        "JOIN feed_groups g ON g.id = f.group_id "
        "LEFT JOIN user_profiles u ON u.user_id = i.user_id "
        "LEFT JOIN item_scores s ON s.item_id = i.id "
        "LEFT JOIN item_state st ON st.item_id = i.id "
        "WHERE COALESCE(st.done, 0) = 0 "
        "AND (st.snoozed_until IS NULL OR st.snoozed_until < ?) "
    )
    if rescore_age and rescore_age > 0:
        query = select + "AND (s.item_id IS NULL OR s.scored_ts < ?) ORDER BY s.scored_ts IS NOT NULL, s.scored_ts LIMIT ?"
        args = (now, now - rescore_age, limit)
    else:
        query = select + "AND s.item_id IS NULL ORDER BY i.id LIMIT ?"
        args = (now, limit)
    return conn.execute(query, args).fetchall()


# Verwirft alle Bewertungen offener Items eines Nutzers -> Neubewertung mit aktuellem Profil.
def clear_scores_for_user(conn, user_id):
    conn.execute("DELETE FROM item_scores WHERE item_id IN (SELECT id FROM items WHERE user_id = ?)", (user_id,))
    conn.commit()


# Verwirft die Bewertungen eines Feeds -> Neubewertung (z.B. nach Prioritaetsaenderung).
def clear_scores_for_feed(conn, feed_id):
    conn.execute("DELETE FROM item_scores WHERE item_id IN (SELECT id FROM items WHERE feed_id = ?)", (feed_id,))
    conn.commit()


# Bewertete, offene Items fuer die deterministische Decay-Aktualisierung (ohne LLM).
def get_decay_batch(conn, now, limit=2000):
    return conn.execute(
        "SELECT s.item_id, s.urgency, s.override, s.base_score, i.ts_source, i.ts_due "
        "FROM item_scores s "
        "JOIN items i ON i.id = s.item_id "
        "LEFT JOIN item_state st ON st.item_id = i.id "
        "WHERE COALESCE(st.done, 0) = 0 "
        "AND (st.snoozed_until IS NULL OR st.snoozed_until < ?) "
        "LIMIT ?",
        (now, limit),
    ).fetchall()


# Setzt den Poll-Cursor (hoechster gesehener Quell-Zeitstempel) eines Feeds.
def set_poll_cursor(conn, feed_id, cursor):
    conn.execute("UPDATE feeds SET poll_cursor = ? WHERE id = ?", (cursor, feed_id))
    conn.commit()


# --- Feed-Bearbeitung -------------------------------------------------------

# Aktualisiert Prioritaet/Poll-Intervall/enabled eines Feeds (nur gesetzte Felder).
def update_feed_settings(conn, feed_id, priority=None, poll_interval=None, enabled=None, llm_scoring=None):
    sets, args = [], []
    if priority is not None:
        sets.append("priority = ?")
        args.append(priority)
    if poll_interval is not None:
        # poll_interval == 0 -> NULL (Default aus config.yaml).
        sets.append("poll_interval = ?")
        args.append(poll_interval or None)
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if llm_scoring is not None:
        sets.append("llm_scoring = ?")
        args.append(1 if llm_scoring else 0)
    if not sets:
        return
    args.append(feed_id)
    conn.execute(f"UPDATE feeds SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()


def delete_feed(conn, feed_id):
    conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()


# Setzt next_poll_ts eines Feeds auf "jetzt faellig" (fuer manuellen Sofort-Poll).
def mark_feed_due(conn, feed_id, now):
    conn.execute("UPDATE feeds SET next_poll_ts = ? WHERE id = ?", (now, feed_id))
    conn.commit()


# --- Sync-Requests (manueller Anstoss aus der Web-UI) -----------------------

def create_sync_request(conn, feed_id, now):
    cur = conn.execute(
        "INSERT INTO sync_requests (feed_id, status, created_ts) VALUES (?, 'pending', ?)",
        (feed_id, now),
    )
    conn.commit()
    return cur.lastrowid


def take_pending_sync_requests(conn):
    return conn.execute(
        "SELECT s.id, s.feed_id FROM sync_requests s WHERE s.status = 'pending' ORDER BY s.id"
    ).fetchall()


def finish_sync_request(conn, req_id, now, status, detail=None):
    conn.execute(
        "UPDATE sync_requests SET status = ?, detail = ?, done_ts = ? WHERE id = ?",
        (status, detail, now, req_id),
    )
    conn.commit()


# --- Debug-Ereignisse -------------------------------------------------------

# Schreibt ein Debug-Ereignis und trimmt die Tabelle auf die letzten `keep` Eintraege.
def add_debug_event(conn, ts, kind, summary, detail=None, feed_id=None, user_id=None, keep=1000):
    conn.execute(
        "INSERT INTO debug_events (ts, feed_id, user_id, kind, summary, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, feed_id, user_id, kind, summary, detail),
    )
    conn.execute(
        "DELETE FROM debug_events WHERE id <= (SELECT MAX(id) FROM debug_events) - ?",
        (keep,),
    )
    conn.commit()


# Live-Statusuebersicht eines Nutzers: je Feed Item-/Bewertungszahlen + Gesamtsummen,
# letzter Bewertungszeitpunkt und letzte Daemon-Aktivitaet (Heartbeat).
def get_status(conn, user_id):
    feeds = conn.execute(
        "SELECT f.id, f.name, f.connector_type, f.enabled, f.status, f.status_detail, "
        "f.last_poll_ts, f.next_poll_ts, f.poll_interval, f.error_count, "
        "(SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id) AS items, "
        "(SELECT COUNT(*) FROM items i JOIN item_scores s ON s.item_id = i.id WHERE i.feed_id = f.id) AS scored "
        "FROM feeds f JOIN feed_groups g ON g.id = f.group_id "
        "WHERE g.user_id = ? ORDER BY g.position, f.id",
        (user_id,),
    ).fetchall()
    one = lambda q, a=(): conn.execute(q, a).fetchone()[0]
    totals = {
        "items": one("SELECT COUNT(*) FROM items WHERE user_id = ?", (user_id,)),
        "scored": one("SELECT COUNT(*) FROM items i JOIN item_scores s ON s.item_id = i.id WHERE i.user_id = ?", (user_id,)),
        "open_unscored": one(
            "SELECT COUNT(*) FROM items i LEFT JOIN item_scores s ON s.item_id = i.id "
            "LEFT JOIN item_state st ON st.item_id = i.id "
            "WHERE i.user_id = ? AND s.item_id IS NULL AND COALESCE(st.done, 0) = 0", (user_id,)),
        "done": one("SELECT COUNT(*) FROM items i JOIN item_state st ON st.item_id = i.id WHERE i.user_id = ? AND st.done = 1", (user_id,)),
        "pending_sync": one(
            "SELECT COUNT(*) FROM sync_requests sr JOIN feeds f ON f.id = sr.feed_id "
            "JOIN feed_groups g ON g.id = f.group_id WHERE g.user_id = ? AND sr.status = 'pending'", (user_id,)),
    }
    last_score = conn.execute(
        "SELECT s.scored_ts, i.title FROM item_scores s JOIN items i ON i.id = s.item_id "
        "WHERE i.user_id = ? ORDER BY s.scored_ts DESC LIMIT 1", (user_id,)).fetchone()
    last_event = conn.execute(
        "SELECT MAX(ts) FROM debug_events WHERE user_id = ? OR user_id IS NULL", (user_id,)).fetchone()[0]
    return {"feeds": feeds, "totals": totals, "last_score": last_score, "last_event": last_event}


# Debug-Ereignisse eines Nutzers (neueste zuerst), optional auf einen Feed gefiltert.
def get_debug_events(conn, user_id, limit=100, feed_id=None, kind=None):
    query = "SELECT * FROM debug_events WHERE (user_id = ? OR user_id IS NULL) "
    args = [user_id]
    if feed_id is not None:
        query += "AND feed_id = ? "
        args.append(feed_id)
    if kind:
        query += "AND kind = ? "
        args.append(kind)
    query += "ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(query, args).fetchall()


# --- Grafana-Panels (Einbettung im Dashboard) -------------------------------

def create_grafana_panel(conn, user_id, title, embed_url, position, width, height, auth_enc, mode, now,
                         color=None):
    cur = conn.execute(
        "INSERT INTO grafana_panels (user_id, title, embed_url, position, width, height, auth_enc, mode, color, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, title, embed_url, int(position), width, int(height), auth_enc, mode, color, now),
    )
    conn.commit()
    return cur.lastrowid


def get_grafana_panel(conn, user_id, panel_id):
    return conn.execute(
        "SELECT * FROM grafana_panels WHERE id = ? AND user_id = ?", (panel_id, user_id)
    ).fetchone()


def list_grafana_panels(conn, user_id):
    return conn.execute(
        "SELECT * FROM grafana_panels WHERE user_id = ? ORDER BY position, id", (user_id,)
    ).fetchall()


def delete_grafana_panel(conn, user_id, panel_id):
    conn.execute("DELETE FROM grafana_panels WHERE id = ? AND user_id = ?", (panel_id, user_id))
    conn.commit()


# Aktualisiert ein Panel. embed_url/auth_enc nur, wenn nicht None (leere Eingabe = unveraendert).
def update_grafana_panel(conn, user_id, panel_id, title, position, width, height, mode,
                         embed_url=None, auth_enc=None, color=None):
    sets = ["title = ?", "position = ?", "width = ?", "height = ?", "mode = ?", "color = ?"]
    args = [title, int(position), width, int(height), mode, color]
    if embed_url is not None:
        sets.append("embed_url = ?")
        args.append(embed_url)
    if auth_enc is not None:
        sets.append("auth_enc = ?")
        args.append(auth_enc)
    args += [panel_id, user_id]
    conn.execute("UPDATE grafana_panels SET %s WHERE id = ? AND user_id = ?" % ", ".join(sets), args)
    conn.commit()


# --- News-Ticker (Odoo-HelpDesk-Teams) ---------------------------------------

def create_ticker_team(conn, user_id, team_id, team_name, config_enc, now, position=0):
    cur = conn.execute(
        "INSERT INTO ticker_teams (user_id, team_id, team_name, config_enc, position, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, int(team_id), team_name, config_enc, int(position), now),
    )
    conn.commit()
    return cur.lastrowid


def list_ticker_teams(conn, user_id):
    return conn.execute(
        "SELECT * FROM ticker_teams WHERE user_id = ? ORDER BY position, id", (user_id,)
    ).fetchall()


# Fuer den Daemon: alle aktiven Ticker-Teams ueber alle Nutzer.
def list_enabled_ticker_teams(conn):
    return conn.execute(
        "SELECT * FROM ticker_teams WHERE enabled = 1 ORDER BY user_id, position, id"
    ).fetchall()


def delete_ticker_team(conn, user_id, ticker_id):
    conn.execute("DELETE FROM ticker_teams WHERE id = ? AND user_id = ?", (ticker_id, user_id))
    conn.commit()


def set_ticker_headline(conn, ticker_id, headline, ticket_count, now):
    conn.execute(
        "UPDATE ticker_teams SET headline = ?, ticket_count = ?, headline_ts = ?, "
        "status = 'ok', status_detail = NULL WHERE id = ?",
        (headline, int(ticket_count), now, ticker_id),
    )
    conn.commit()


# Fehler merken, letzte gute Schlagzeile aber stehen lassen (Laufband bleibt gefuellt).
def set_ticker_error(conn, ticker_id, detail, now):
    conn.execute(
        "UPDATE ticker_teams SET status = 'error', status_detail = ? WHERE id = ?",
        ((detail or "")[:300], ticker_id),
    )
    conn.commit()


# --- Laufzeit-Einstellungen (Settings-Seite, ueberschreiben config.yaml) ------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value, now):
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
        (key, str(value), now),
    )
    conn.commit()


def get_setting_int(conn, key, default):
    try:
        return int(get_setting(conn, key, default))
    except (TypeError, ValueError):
        return default


# --- Dashboard-Kacheln (Briefing/Team/Betrieb/Rueckblick) -----------------------

def list_tiles(conn, user_id):
    return conn.execute(
        "SELECT * FROM dashboard_tiles WHERE user_id = ? ORDER BY position, id", (user_id,)
    ).fetchall()


def tile_kinds_in_use(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT kind FROM dashboard_tiles")]


def create_tile(conn, user_id, kind, title, position, width, color, now):
    conn.execute(
        "INSERT INTO dashboard_tiles (user_id, kind, title, position, width, color, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, title, int(position), width, color, now),
    )
    conn.commit()


def update_tile(conn, user_id, tile_id, title, position, width, color):
    conn.execute(
        "UPDATE dashboard_tiles SET title = ?, position = ?, width = ?, color = ? "
        "WHERE id = ? AND user_id = ?",
        (title, int(position), width, color, tile_id, user_id),
    )
    conn.commit()


def delete_tile(conn, user_id, tile_id):
    conn.execute("DELETE FROM dashboard_tiles WHERE id = ? AND user_id = ?", (tile_id, user_id))
    conn.commit()


def get_tile_content(conn, kind):
    return conn.execute("SELECT payload, updated_ts FROM tile_content WHERE kind = ?", (kind,)).fetchone()


def set_tile_content(conn, kind, payload_json, now):
    conn.execute(
        "INSERT INTO tile_content (kind, payload, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET payload = excluded.payload, updated_ts = excluded.updated_ts",
        (kind, payload_json, now),
    )
    conn.commit()


# --- Kalender-Modul (Zuordnung + globaler Snapshot) -----------------------------

def list_calendar_feeds(conn):
    return conn.execute("SELECT * FROM calendar_feeds ORDER BY name").fetchall()


# Ersetzt die DAV-Zuordnung (Formular sendet den Gesamtstand der SoGo-Sammlungen);
# direkte iCal-Feeds (http…) bleiben unberuehrt.
def replace_calendar_feeds(conn, entries, now):
    conn.execute("DELETE FROM calendar_feeds WHERE path NOT LIKE 'http%'")
    for e in entries:
        conn.execute(
            "INSERT INTO calendar_feeds (path, name, role, created_ts) VALUES (?, ?, ?, ?)",
            (e["path"], e["name"], e["role"], now),
        )
    conn.commit()


def create_direct_calendar_feed(conn, url, name, role, config_enc, now):
    conn.execute(
        "INSERT INTO calendar_feeds (path, name, role, config_enc, created_ts) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET name = excluded.name, "
        "role = excluded.role, config_enc = COALESCE(excluded.config_enc, config_enc)",
        (url, name, role, config_enc, now),
    )
    conn.commit()


def delete_calendar_feed(conn, feed_id):
    conn.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,))
    conn.commit()


def get_calendar_status(conn):
    return conn.execute("SELECT payload, updated_ts FROM calendar_status WHERE id = 1").fetchone()


def set_calendar_status(conn, payload_json, now):
    conn.execute(
        "INSERT INTO calendar_status (id, payload, updated_ts) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_ts = excluded.updated_ts",
        (payload_json, now),
    )
    conn.commit()


# --- Arbeitszeit-Laufband (globaler Snapshot der Zeiterfassung) ---------------

def get_worktime_status(conn):
    return conn.execute("SELECT payload, updated_ts FROM worktime_status WHERE id = 1").fetchone()


def set_worktime_status(conn, payload_json, now):
    conn.execute(
        "INSERT INTO worktime_status (id, payload, updated_ts) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_ts = excluded.updated_ts",
        (payload_json, now),
    )
    conn.commit()


# --- Retention --------------------------------------------------------------

# Loescht abgelaufene Items gemaess Aufbewahrungsregeln. Rueckgabe: Anzahl geloeschter Items.
def cleanup_retention(conn, now, done_age, due_grace, news_age, default_age):
    deleted = 0
    # Erledigte Items nach Ablauf der Frist.
    cur = conn.execute(
        "DELETE FROM items WHERE id IN (SELECT i.id FROM items i JOIN item_state st ON st.item_id = i.id "
        "WHERE st.done = 1 AND st.updated_ts < ?)",
        (now - done_age,),
    )
    deleted += cur.rowcount
    # Terminbezogene Items nach Faelligkeit + Karenz.
    cur = conn.execute("DELETE FROM items WHERE ts_due IS NOT NULL AND ts_due < ?", (now - due_grace,))
    deleted += cur.rowcount
    # News/RSS nach Maximalalter.
    cur = conn.execute(
        "DELETE FROM items WHERE source_type = 'rss' AND ts_fetched < ?", (now - news_age,)
    )
    deleted += cur.rowcount
    # Sonstige offene Items nach Maximalalter.
    cur = conn.execute(
        "DELETE FROM items WHERE ts_due IS NULL AND source_type != 'rss' AND ts_fetched < ?",
        (now - default_age,),
    )
    deleted += cur.rowcount
    conn.commit()
    return deleted
