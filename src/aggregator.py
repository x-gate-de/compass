# -----------------------------------------------------------------------------
# Skript: src/aggregator.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Aggregator-Hintergrunddienst (aus x-gate_nextup): pollt Feeds, normalisiert/
#   dedupliziert neue Items, bewertet sie per LLM, fuehrt den deterministischen
#   Zeit-Decay nach, ordnet die Spitze vergleichend um und raeumt abgelaufene Items auf.
# Ablauf:
# - Periodische Schleifen: Poll, Scoring, Commands (manuelle Syncs), Decay, Retention,
#   optional Re-Rank. Intervalle aus config.yaml.
# - Netzwerkarbeit (Connector-Poll, LLM) laeuft ausserhalb des DB-Locks; DB-Zugriffe
#   sind durch einen asyncio.Lock serialisiert (eine SQLite-Verbindung, WAL).
# Betriebs- und Wartungshinweise:
# - Fehler bleiben pro Feed/Item isoliert; ein Feed-Fehler erhoeht error_count und
#   verschiebt den naechsten Poll per exponentiellem Backoff.
# - Secrets/Item-Klartext erscheinen nicht in Logs.
# - Laeuft im selben asyncio-Loop wie der Chat-Manager (siehe src/manager.py).
# -----------------------------------------------------------------------------

import asyncio
import json
import logging
import re
import time
from types import SimpleNamespace

from . import store
from .config import cfg_get
from .connectors.odoo import fetch_team_tickets
from .connectors.registry import get_connector
from .db import decrypt_config
from .calendar_feed import (active_now, classify, covering_today,
                            fetch_collection_events, fetch_events)
from .connectors.odoo import fetch_closed_tickets
from .scoring import ScoringParams, compute_scores, decay_factor
from .worktime import WorktimeClient

logger = logging.getLogger(__name__)


class AggregatorDaemon:
    def __init__(self, conn, cfg, llm_client):
        self.conn = conn
        self.cfg = cfg
        self.llm = llm_client
        self.fernet_key = cfg_get(cfg, "security.fernet_key")
        self.db_lock = asyncio.Lock()
        # Ollama verarbeitet Anfragen seriell: mehrere gleichzeitige Calls (Scoring,
        # Re-Rank, Ticker) stauen sich sonst gegenseitig in den Timeout. Eine Sperre
        # stellt sicher, dass der Daemon immer nur EINE LLM-Anfrage offen hat.
        self.llm_lock = asyncio.Lock()

        # Intervalle/Parameter mit Defaults aus SPEC.
        self.poll_default = int(cfg_get(cfg, "polling.default_interval", 3600))
        self.backoff_start = int(cfg_get(cfg, "polling.error_backoff_start", 60))
        self.backoff_max = int(cfg_get(cfg, "polling.error_backoff_max", 3600))
        self.rescore_interval = int(cfg_get(cfg, "scoring.rescore_interval", 0))
        # Takt der Score-Schleife: wie oft nach unbewerteten/faelligen Items gesucht wird.
        self.score_tick = int(cfg_get(cfg, "scoring.score_tick", 60))
        self.decay_interval = int(cfg_get(cfg, "scoring.decay_tick", 300))
        self.retention_interval = 24 * 3600
        self.decay_enabled = bool(cfg_get(cfg, "scoring.time_decay_enabled", True))
        # Takt der Befehls-Schleife (manuelle Sync-Anstoesse aus der Web-UI).
        self.commands_tick = int(cfg_get(cfg, "polling.commands_tick", 5))
        # Debug-Log fuer die Web-UI: LLM-Ein/Ausgabe mitschreiben? (Entwicklung)
        self.debug_capture = bool(cfg_get(cfg, "debug.capture_llm", True))
        self.debug_keep = int(cfg_get(cfg, "debug.max_events", 1000))

        self.params = ScoringParams(
            override_threshold=int(cfg_get(cfg, "scoring.override_threshold", 90)),
            override_floor=float(cfg_get(cfg, "scoring.override_floor", 1000.0)),
        )
        self.model_name = cfg_get(cfg, "llm.model", "")

        # Vergleichender Re-Rank-Pass: das LLM sieht die Top-N offenen Items gemeinsam.
        self.rerank_enabled = bool(cfg_get(cfg, "scoring.rerank_enabled", True))
        self.rerank_top_n = int(cfg_get(cfg, "scoring.rerank_top_n", 10))
        self.rerank_interval = int(cfg_get(cfg, "scoring.rerank_interval", 600))
        self.rerank_timeout = int(cfg_get(cfg, "scoring.rerank_timeout", 240))

        # News-Ticker: Zyklus + Prompt-Deckel (Tickets je Team) + LLM-Timeout.
        # Das interne Ollama schafft nur wenige Token/s -> Prompt klein halten
        # (Top-30 nach Prioritaet/Aktualitaet) und grosszuegiger Timeout.
        self.ticker_interval = int(cfg_get(cfg, "ticker.interval", 600))
        self.ticker_max_tickets = int(cfg_get(cfg, "ticker.max_tickets", 30))
        self.ticker_timeout = int(cfg_get(cfg, "ticker.timeout", 480))

        # Arbeitszeit-Laufband: Zeiterfassung (phatman) nur, wenn Zugang konfiguriert.
        wt_url = cfg_get(cfg, "worktime.base_url", "") or ""
        wt_key = cfg_get(cfg, "worktime.api_key", "") or ""
        self.worktime = WorktimeClient(
            wt_url, wt_key,
            tls_verify=bool(cfg_get(cfg, "worktime.tls_verify", True)),
            timeout=int(cfg_get(cfg, "worktime.timeout", 30)),
        ) if (wt_url and wt_key) else None
        self.worktime_interval = int(cfg_get(cfg, "worktime.interval", 300))

        # Kalender-Modul (iCal/SoGo): Zugang kommt zur Laufzeit aus app_settings
        # (Settings-Seite) -> Schleife laeuft immer und prueft je Zyklus.
        self.calendar_interval = int(cfg_get(cfg, "calendar.interval", 900))

        # Dashboard-Kacheln: Briefing (1x taeglich ab konfigurierter Stunde) und
        # Rueckblick (geloeste Tickets, alle 6h). Nur aktiv, wenn Kacheln existieren.
        self.briefing_check = 300
        self.review_interval = int(cfg_get(cfg, "review.interval", 6 * 3600))

    # --- Poll -----------------------------------------------------------------

    async def poll_once(self):
        now = time.time()
        async with self.db_lock:
            feeds = [dict(r) for r in store.list_due_feeds(self.conn, now)]
        for feed in feeds:
            await self._poll_feed(feed)

    async def _poll_feed(self, feed):
        feed_id = feed["id"]
        interval = feed["poll_interval"] or self.poll_default
        try:
            config = decrypt_config(self.fernet_key, feed["config_enc"])
            connector = get_connector(feed["connector_type"])
            ctx = SimpleNamespace(feed_id=feed_id, user_id=None)
            # user_id des Feeds und bereits bekannte dedup_keys ermitteln.
            async with self.db_lock:
                ctx.user_id = self._feed_user_id(feed_id)
                known_keys = store.feed_dedup_keys(self.conn, feed_id)
            # Netzwerk-/IO-Poll ausserhalb des Locks.
            items, next_since, present_keys = await connector.poll(
                ctx, config, feed.get("poll_cursor"), known_keys)
        except Exception as exc:
            await self._mark_feed_error(feed, exc)
            return

        now = time.time()
        async with self.db_lock:
            for item in items:
                _, _, changed = store.upsert_item(self.conn, item, now)
                # Inhaltsaenderung verwirft die alte Bewertung -> Re-Scoring.
                if changed:
                    existing = self.conn.execute(
                        "SELECT id FROM items WHERE dedup_key = ?", (item.dedup_key,)
                    ).fetchone()
                    if existing is not None:
                        store.clear_score(self.conn, existing["id"])
            # Abgleich: in der Quelle nicht mehr vorhandene Items entfernen.
            removed = store.reconcile_feed(self.conn, feed_id, present_keys) if present_keys is not None else 0
            if next_since is not None:
                store.set_poll_cursor(self.conn, feed_id, next_since)
            store.update_feed_status(
                self.conn, feed_id, "ok", now + interval,
                status_detail=None, last_poll_ts=now, error_count=0,
            )
            summary = f"{len(items)} neu" + (f", {removed} entfernt" if removed else "")
            store.add_debug_event(
                self.conn, now, "poll", summary,
                feed_id=feed_id, user_id=ctx.user_id, keep=self.debug_keep,
            )
        logger.info("Feed %s gepollt: %d neu, %d entfernt", feed_id, len(items), removed)

    def _feed_user_id(self, feed_id):
        row = self.conn.execute(
            "SELECT g.user_id AS uid FROM feeds f JOIN feed_groups g ON g.id = f.group_id "
            "WHERE f.id = ?",
            (feed_id,),
        ).fetchone()
        return row["uid"] if row else None

    async def _mark_feed_error(self, feed, exc):
        now = time.time()
        error_count = (feed["error_count"] or 0) + 1
        backoff = min(self.backoff_start * (2 ** (error_count - 1)), self.backoff_max)
        # Nur den Fehlertyp speichern/loggen, keine Secrets oder Antwortinhalte.
        detail = type(exc).__name__
        async with self.db_lock:
            store.update_feed_status(
                self.conn, feed["id"], "error", now + backoff,
                status_detail=detail, last_poll_ts=now, error_count=error_count,
            )
            store.add_debug_event(
                self.conn, now, "error", f"Poll-Fehler: {detail}",
                feed_id=feed["id"], user_id=self._feed_user_id(feed["id"]), keep=self.debug_keep,
            )
        logger.warning("Feed %s Fehler (%s), naechster Versuch in %ds", feed["id"], detail, backoff)

    # --- Scoring --------------------------------------------------------------

    async def score_once(self):
        now = time.time()
        async with self.db_lock:
            rows = [dict(r) for r in store.get_items_to_score(self.conn, now, self.rescore_interval)]
        for row in rows:
            await self._score_item(row)

    async def _score_item(self, row):
        # Feeds ohne LLM-Bewertung (z.B. Syslog-Raum): deterministisch, keine Ollama-Last.
        if not row.get("llm_scoring", 1):
            now = time.time()
            base, final = compute_scores(50, False, row["group_prio"], row["feed_prio"],
                                         now, row.get("ts_source"), row.get("ts_due"), self.params)
            async with self.db_lock:
                store.set_score(self.conn, row["id"], 50, "(ohne LLM-Bewertung)", False,
                                row["group_prio"], row["feed_prio"], base, final, "-", now)
            return

        item_obj = SimpleNamespace(**row)
        # Optional LLM-Ein/Ausgabe fuer das Debug-Log mitschneiden.
        debug = {} if self.debug_capture else None
        try:
            async with self.llm_lock:
                result = await self.llm.score(item_obj, row.get("user_profile"), row.get("group_profile"), debug=debug)
        except Exception as exc:
            logger.warning("Scoring Item %s fehlgeschlagen: %s", row["id"], type(exc).__name__)
            return
        now = time.time()
        base, final = compute_scores(
            result.urgency, result.override, row["group_prio"], row["feed_prio"],
            now, row.get("ts_source"), row.get("ts_due"), self.params,
        )
        async with self.db_lock:
            store.set_score(
                self.conn, row["id"], result.urgency, result.reason, result.override,
                row["group_prio"], row["feed_prio"], base, final, self.model_name, now,
            )
            if debug is not None:
                detail = "SYSTEM:\n{}\n\nUSER:\n{}\n\nANTWORT:\n{}".format(
                    debug.get("system", ""), debug.get("user", ""),
                    debug.get("raw") or debug.get("error") or "",
                )
                store.add_debug_event(
                    self.conn, now, "llm",
                    f"u={result.urgency} override={result.override} | {row.get('title') or ''}"[:200],
                    detail=detail, feed_id=row.get("feed_id"), user_id=row.get("user_id"),
                    keep=self.debug_keep,
                )

    # --- Decay ----------------------------------------------------------------

    async def decay_once(self):
        if not self.decay_enabled:
            return
        now = time.time()
        async with self.db_lock:
            rows = [dict(r) for r in store.get_decay_batch(self.conn, now)]
            for row in rows:
                final = self._decayed_final(row, now)
                store.update_final_score(self.conn, row["item_id"], final, now)

    def _decayed_final(self, row, now):
        # Identische Formel wie scoring.final_score: Decay auf den Basisscore, Override
        # als additiver Bonus (kein Plateau).
        score = row["base_score"] * decay_factor(now, row["ts_source"], row["ts_due"], self.params)
        if row["override"] and row["urgency"] >= self.params.override_threshold:
            score += self.params.override_floor
        return score

    # --- Vergleichender Re-Rank -----------------------------------------------

    async def rerank_once(self):
        if not self.rerank_enabled:
            return
        now = time.time()
        async with self.db_lock:
            users = [dict(r) for r in store.list_active_users(self.conn)]
        for u in users:
            async with self.db_lock:
                rows = [dict(r) for r in store.get_ranking(self.conn, u["id"], now, None, self.rerank_top_n)]
            # Unter zwei Items gibt es nichts zu vergleichen.
            if len(rows) < 2:
                continue
            objs = [SimpleNamespace(**r) for r in rows]
            try:
                async with self.llm_lock:
                    mapping = await self.llm.rerank(objs, u.get("profile_text"), timeout=self.rerank_timeout)
            except Exception as exc:
                logger.warning("Re-Rank Nutzer %s fehlgeschlagen: %s", u["id"], type(exc).__name__)
                continue
            # Nur Items mit gueltigem Vergleichs-Score, nach diesem absteigend.
            order = sorted((i for i in range(1, len(rows) + 1) if i in mapping),
                           key=lambda i: mapping[i], reverse=True)
            if not order:
                continue
            # rank_sort: die vorhandenen final_scores der Top-N gemaess Vergleich permutieren.
            finals_desc = sorted((r["final_score"] or 0.0) for r in rows)[::-1]
            async with self.db_lock:
                store.clear_rank_scores(self.conn, u["id"])
                for pos, idx in enumerate(order):
                    item = rows[idx - 1]
                    store.set_rank_score(self.conn, item["id"], mapping[idx], finals_desc[pos])
                # Wichtig: committen, sonst bleibt die Schreibtransaktion offen und haelt den
                # Schreib-Lock bis zum naechsten Commit -> blockiert die Web-App ("database is locked").
                self.conn.commit()
            logger.info("Re-Rank Nutzer %s: %d Items neu geordnet", u["id"], len(order))

    # --- Befehle (manuelle Syncs aus der Web-UI) ------------------------------

    async def commands_once(self):
        async with self.db_lock:
            reqs = [dict(r) for r in store.take_pending_sync_requests(self.conn)]
        for req in reqs:
            await self._handle_sync(req)

    async def _handle_sync(self, req):
        async with self.db_lock:
            feed = self.conn.execute("SELECT * FROM feeds WHERE id = ?", (req["feed_id"],)).fetchone()
            feed = dict(feed) if feed else None
        if not feed:
            async with self.db_lock:
                store.finish_sync_request(self.conn, req["id"], time.time(), "error", "Feed nicht gefunden")
            return
        # Sofort pollen (unabhaengig vom Intervall) und direkt bewerten.
        await self._poll_feed(feed)
        await self.score_once()
        async with self.db_lock:
            row = self.conn.execute("SELECT status, status_detail FROM feeds WHERE id = ?", (req["feed_id"],)).fetchone()
            status = row["status"] if row else "unbekannt"
            detail = row["status_detail"] if row and row["status_detail"] else "ok"
            store.finish_sync_request(self.conn, req["id"], time.time(), "done", f"{status}: {detail}")

    # --- Retention ------------------------------------------------------------

    async def retention_once(self):
        now = time.time()
        async with self.db_lock:
            deleted = store.cleanup_retention(
                self.conn, now,
                done_age=int(cfg_get(self.cfg, "retention.done_age", 7 * 24 * 3600)),
                due_grace=int(cfg_get(self.cfg, "retention.due_grace", 24 * 3600)),
                news_age=int(cfg_get(self.cfg, "retention.news_age", 14 * 24 * 3600)),
                default_age=int(cfg_get(self.cfg, "retention.default_age", 30 * 24 * 3600)),
            )
        if deleted:
            logger.info("Retention: %d Items entfernt", deleted)

    # --- News-Ticker ------------------------------------------------------------

    # Holt je konfiguriertem HelpDesk-Team alle offenen/in-Bearbeitung-Tickets aus Odoo
    # und laesst das LLM EINE Top-Schlagzeile formulieren. Fehler bleiben pro Team
    # isoliert; die letzte gute Schlagzeile bleibt bei Fehlern stehen.
    async def ticker_once(self):
        async with self.db_lock:
            # Laufzeit-Override aus den Settings (Web) je Zyklus neu lesen ->
            # Intervall-Aenderungen wirken ohne Daemon-Neustart.
            self.ticker_interval_current = store.get_setting_int(
                self.conn, "ticker.interval", self.ticker_interval)
            teams = store.list_enabled_ticker_teams(self.conn)
        for team in teams:
            now = time.time()
            try:
                config = decrypt_config(self.fernet_key, team["config_enc"])
                # XML-RPC ist blockierend -> Thread; LLM-Call ist async.
                tickets = await asyncio.to_thread(
                    fetch_team_tickets, config, team["team_id"], self.ticker_max_tickets)
                async with self.llm_lock:
                    headline = await self.llm.ticker_headline(
                        team["team_name"], tickets, timeout=self.ticker_timeout)
                if headline is None:
                    raise RuntimeError("LLM lieferte keine Schlagzeile")
                async with self.db_lock:
                    store.set_ticker_headline(self.conn, team["id"], headline, len(tickets), now)
                logger.info("Ticker %s: %d Tickets -> Schlagzeile aktualisiert",
                            team["team_name"], len(tickets))
            except Exception as exc:
                # Kein Klartext/Secret im Log; Detail (ohne Secrets) fuer die UI merken.
                logger.warning("Ticker %s fehlgeschlagen: %s", team["team_name"], type(exc).__name__)
                async with self.db_lock:
                    store.set_ticker_error(self.conn, team["id"],
                                           "%s: %s" % (type(exc).__name__, exc), now)

    # --- Arbeitszeit-Laufband ----------------------------------------------------

    # Snapshot der Zeiterfassung ziehen. Bei Fehler bleibt der letzte gute Snapshot
    # erhalten und wird nur mit dem Fehlerkennzeichen versehen (Anzeige "Stand aelter").
    async def worktime_once(self):
        if not self.worktime:
            return
        async with self.db_lock:
            self.worktime_interval_current = store.get_setting_int(
                self.conn, "worktime.interval", self.worktime_interval)
            enabled = store.get_setting(self.conn, "worktime.enabled", "1") != "0"
            row = store.get_worktime_status(self.conn)
        if not enabled:
            return
        prev_payload = {}
        if row and row["payload"]:
            try:
                prev_payload = json.loads(row["payload"])
            except ValueError:
                prev_payload = {}
        prev_worked = {str(u["id"]): u.get("worked_s") or 0
                       for u in prev_payload.get("users") or []}
        now = time.time()
        try:
            payload = await self.worktime.snapshot(prev_worked)
            logger.info("Zeiterfassung: %d Mitarbeiter, %d aktiv",
                        len(payload["users"]),
                        sum(1 for u in payload["users"] if u["active"]))
        except Exception as exc:
            logger.warning("Zeiterfassung nicht erreichbar: %s", type(exc).__name__)
            payload = prev_payload or {"users": []}
            payload["error"] = type(exc).__name__
        async with self.db_lock:
            store.set_worktime_status(self.conn, json.dumps(payload), now)

    # --- Kalender-Modul (iCal/SoGo) ----------------------------------------------

    # Holt den konfigurierten iCal-Feed (Zugang Fernet-verschluesselt in app_settings).
    # Ohne Konfiguration ist der Lauf ein No-Op -> Einrichtung im Web wirkt ohne
    # Daemon-Neustart. Bei Fehlern bleibt der letzte gute Snapshot (mit Fehlermarke).
    async def calendar_once(self):
        async with self.db_lock:
            enc = store.get_setting(self.conn, "calendar.config_enc", "")
            self.calendar_interval_current = store.get_setting_int(
                self.conn, "calendar.interval", self.calendar_interval)
            feeds = store.list_calendar_feeds(self.conn)
            row = store.get_calendar_status(self.conn)
        if not enc or not feeds:
            return
        try:
            prev = json.loads(row["payload"]) if (row and row["payload"]) else {}
        except ValueError:
            prev = {}
        prev_by_cal = {}
        for e in prev.get("events") or []:
            prev_by_cal.setdefault(e.get("cal"), []).append(e)
        config = decrypt_config(self.fernet_key, enc)
        now = time.time()
        events, feed_states = [], []
        for f in feeds:
            try:
                # Direkte iCal-Feeds (http…) ggf. mit eigenen Zugangsdaten; sonst
                # CalDAV-REPORT auf die SoGo-Sammlung mit dem Modul-Zugang.
                if f["path"].startswith("http"):
                    fcfg = dict(config)
                    if f["config_enc"]:
                        fcfg.update(decrypt_config(self.fernet_key, f["config_enc"]))
                    evs = await asyncio.to_thread(fetch_events, fcfg, f["path"])
                else:
                    evs = await asyncio.to_thread(fetch_collection_events, config, f["path"])
                # Rolle/Kalendername an jedes Ereignis heften (Auswertung im Band).
                for e in evs:
                    e["role"] = f["role"]
                    e["cal"] = f["name"]
                events.extend(evs)
                feed_states.append({"name": f["name"], "role": f["role"],
                                    "count": len(evs), "error": None})
            except Exception as exc:
                # Fehler pro Kalender isolieren; letzten guten Stand behalten.
                logger.warning("Kalender %s fehlgeschlagen: %s", f["name"], type(exc).__name__)
                events.extend(prev_by_cal.get(f["name"], []))
                feed_states.append({"name": f["name"], "role": f["role"],
                                    "count": len(prev_by_cal.get(f["name"], [])),
                                    "error": type(exc).__name__})
        payload = {"ts": now, "error": None, "events": events, "feeds": feed_states}
        logger.info("Kalender: %d Ereignisse aus %d Kalendern", len(events), len(feeds))
        async with self.db_lock:
            store.set_calendar_status(self.conn, json.dumps(payload), now)

    # --- Dashboard-Kacheln: Morgen-Briefing + Rueckblick -------------------------

    # Erzeugt einmal pro Tag (ab briefing.hour) ein LLM-Briefing aus: Top-Items,
    # Chat-Aufkommen der Nacht, Terminen heute sowie Abwesenheit/Rufbereitschaft.
    async def briefing_once(self):
        now = time.time()
        lt = time.localtime(now)
        async with self.db_lock:
            if "briefing" not in store.tile_kinds_in_use(self.conn):
                return
            hour = store.get_setting_int(self.conn, "briefing.hour", 7)
            row = store.get_tile_content(self.conn, "briefing")
        if lt.tm_hour < hour:
            return
        if row and row["updated_ts"] and \
                time.localtime(row["updated_ts"]).tm_yday == lt.tm_yday:
            return
        async with self.db_lock:
            user = self.conn.execute(
                "SELECT user_id FROM dashboard_tiles WHERE kind = 'briefing' LIMIT 1"
            ).fetchone()["user_id"]
            top = store.get_ranking(self.conn, user, now)[:5]
            cutoff = now - 14 * 3600
            chat = self.conn.execute(
                "SELECT title, COUNT(*) n FROM items WHERE user_id = ? AND ts_source >= ? "
                "AND source_type IN ('chat','groupchat') GROUP BY title ORDER BY n DESC LIMIT 5",
                (user, cutoff)).fetchall()
            day_end = now + (24 - lt.tm_hour) * 3600
            appts = self.conn.execute(
                "SELECT title, ts_due FROM items WHERE user_id = ? AND source_type = 'calendar' "
                "AND ts_due BETWEEN ? AND ? ORDER BY ts_due LIMIT 8",
                (user, now - 3600, day_end)).fetchall()
            wrow = store.get_worktime_status(self.conn)
            crow = store.get_calendar_status(self.conn)
        cal = {}
        try:
            cal = json.loads(crow["payload"]) if (crow and crow["payload"]) else {}
        except ValueError:
            pass
        oncall = [e["summary"] for e in active_now(cal.get("events") or [])
                  if e.get("role") == "oncall"]
        absent = [e["summary"] for e in covering_today(cal.get("events") or [])
                  if e.get("role") == "absence" and classify(e["summary"]) != "oncall"]
        lines = ["Top-Eintraege:"]
        lines += ["- [%s] %s" % (r["source_type"], (r["title"] or "-")[:80]) for r in top]
        lines.append("Chat-Aufkommen letzte 14h:")
        lines += ["- %s: %d Nachrichten" % ((r["title"] or "-")[:40], r["n"]) for r in chat]
        lines.append("Termine heute:")
        lines += ["- %s %s" % (time.strftime("%H:%M", time.localtime(r["ts_due"])),
                               (r["title"] or "-")[:60]) for r in appts]
        lines.append("Rufbereitschaft: " + (", ".join(oncall) or "-"))
        lines.append("Heute abwesend: " + (", ".join(absent) or "-"))
        system = ("Du schreibst ein knappes internes Morgen-Briefing (deutsch) fuer den "
                  "Leiter eines Hosting-Betriebs. Maximal 6 kurze Zeilen, je Zeile ein "
                  "Punkt mit '- ' beginnend, wichtigstes zuerst (Ausfaelle > Fristen > "
                  "Termine > Personal). Kein Markdown, keine Anrede, keine Floskeln.")
        async with self.llm_lock:
            text = await self.llm.summarize(system, "\n".join(lines), timeout=self.ticker_timeout)
        if not text:
            return
        # Modelle liefern die Punkte oft in EINER Zeile (" - ...") -> normalisieren,
        # damit die Kachel saubere Aufzaehlungszeilen zeigt.
        text = re.sub(r"\s+-\s+(?=[A-ZÄÖÜ0-9])", "\n- ", text).strip()
        async with self.db_lock:
            store.set_tile_content(self.conn, "briefing",
                                   json.dumps({"text": text[:1200]}), now)
        logger.info("Morgen-Briefing erzeugt (%d Zeichen)", len(text))

    # Rueckblick: geloeste HelpDesk-Tickets der letzten 7 Tage (Zugang aus den
    # News-Ticker-Teams; ein Team-Zugang reicht, Abfrage laeuft teamuebergreifend).
    async def review_once(self):
        now = time.time()
        async with self.db_lock:
            if "review" not in store.tile_kinds_in_use(self.conn):
                return
            teams = store.list_enabled_ticker_teams(self.conn)
            days = store.get_setting_int(self.conn, "review.days", 7)
            exclude_raw = store.get_setting(
                self.conn, "review.exclude", "vermutlich SPAM, Systemmeldungen")
            row = store.get_tile_content(self.conn, "review")
        if not teams:
            return
        exclude = {t.strip().lower() for t in (exclude_raw or "").split(",") if t.strip()}
        # Nur neu abfragen, wenn Stand fehlt, veraltet ist oder sich Zeitraum/
        # Ausschlussliste geaendert haben (Schleife 15min, Odoo-Abfrage alle 6h).
        if row and row["payload"]:
            try:
                old = json.loads(row["payload"])
                if old.get("days") == days and old.get("exclude") == sorted(exclude) \
                        and row["updated_ts"] \
                        and now - row["updated_ts"] < self.review_interval:
                    return
            except ValueError:
                pass
        try:
            config = decrypt_config(self.fernet_key, teams[0]["config_enc"])
            tickets = await asyncio.to_thread(fetch_closed_tickets, config, days, 500)
        except Exception as exc:
            logger.warning("Rueckblick fehlgeschlagen: %s", type(exc).__name__)
            return
        # Rausch-Teams (Spam-/Systemmeldungs-Auffangbecken) ausblenden.
        tickets = [t for t in tickets if t["team"].strip().lower() not in exclude]
        per_team = {}
        for t in tickets:
            per_team[t["team"]] = per_team.get(t["team"], 0) + 1
        payload = {"days": days, "exclude": sorted(exclude), "total": len(tickets),
                   "per_team": sorted(per_team.items(), key=lambda kv: -kv[1]),
                   "recent": tickets[:12]}
        async with self.db_lock:
            store.set_tile_content(self.conn, "review", json.dumps(payload), now)
        logger.info("Rueckblick: %d geloeste Tickets (%d Tage)", len(tickets), days)

    # --- Schleifen ------------------------------------------------------------

    # interval darf ein Callable sein (dynamisch aus den Settings, je Zyklus neu).
    async def _loop(self, coro_factory, interval, name):
        while True:
            try:
                await coro_factory()
            except Exception as exc:
                logger.error("Schleife %s Fehler: %s", name, type(exc).__name__)
            await asyncio.sleep(interval() if callable(interval) else interval)

    async def run(self):
        logger.info("Aggregator-Daemon startet")
        loops = [
            self._loop(self.poll_once, self.poll_default, "poll"),
            self._loop(self.score_once, self.score_tick, "score"),
            self._loop(self.commands_once, self.commands_tick, "commands"),
            self._loop(self.decay_once, self.decay_interval, "decay"),
            self._loop(self.retention_once, self.retention_interval, "retention"),
        ]
        if self.rerank_enabled:
            loops.append(self._loop(self.rerank_once, self.rerank_interval, "rerank"))
        # Ticker-Intervalle sind zur Laufzeit einstellbar (Settings-Seite).
        self.ticker_interval_current = self.ticker_interval
        self.worktime_interval_current = self.worktime_interval
        self.calendar_interval_current = self.calendar_interval
        loops.append(self._loop(self.ticker_once,
                                lambda: self.ticker_interval_current, "ticker"))
        if self.worktime:
            loops.append(self._loop(self.worktime_once,
                                    lambda: self.worktime_interval_current, "worktime"))
        loops.append(self._loop(self.calendar_once,
                                lambda: self.calendar_interval_current, "calendar"))
        loops.append(self._loop(self.briefing_once, self.briefing_check, "briefing"))
        loops.append(self._loop(self.review_once, 900, "review"))
        await asyncio.gather(*loops)
