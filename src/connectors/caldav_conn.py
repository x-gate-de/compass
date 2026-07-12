# -----------------------------------------------------------------------------
# Skript: src/connectors/caldav_conn.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.1
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Read-only CalDAV-Connector (z.B. SoGo): liest anstehende Termine aus den vom Nutzer
#   ausgewaehlten Kalendern in einem konfigurierbaren Zeitfenster.
# Ablauf:
# - discover(): listet die Kalender des Principals zur Auswahl in der UI.
# - validate_config(): prueft Verbindung/Login.
# - poll(): holt VEVENTs im Fenster [jetzt; jetzt+window_days] und mappt sie auf Items.
#   ts_due = Terminbeginn -> treibt den Zeit-Decay (Boost bei Annaeherung).
# Betriebs- und Wartungshinweise:
# - Read-only: es werden ausschliesslich Kalenderabfragen (REPORT) ausgefuehrt.
# - caldav/requests sind blockierend -> alle Netzwerkschritte via asyncio.to_thread.
# - Termine werden je Poll fuer das Fenster neu geholt; dedup_key (UID+Beginn) haelt das
#   idempotent. Vergangene Termine fallen ueber die Retention (ts_due + Karenz) weg.
# -----------------------------------------------------------------------------

import asyncio
import datetime
import logging
import time

import icalendar

from ..models import Item
from .base import Connector

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 14


class CalDavConnector(Connector):
    connector_type = "caldav"

    def _client(self, config):
        # Import lokal halten, damit das Modul auch ohne installiertes caldav importierbar
        # bleibt (z.B. fuer den reinen Mapping-Test).
        from caldav import DAVClient
        return DAVClient(
            url=config["url"],
            username=config["username"],
            password=config["password"],
            ssl_verify_cert=bool(config.get("tls_verify", True)),
        )

    # --- discover -------------------------------------------------------------

    async def discover(self, config):
        return await asyncio.to_thread(self._discover_sync, config)

    def _discover_sync(self, config):
        client = self._client(config)
        principal = client.principal()
        out = []
        for cal in principal.calendars():
            try:
                label = cal.get_display_name()
            except Exception:
                label = None
            out.append({"id": str(cal.url), "label": label or str(cal.url)})
        return out

    # --- validate -------------------------------------------------------------

    async def validate_config(self, config):
        for key in ("url", "username", "password"):
            if not config.get(key):
                return False, f"{key} ist erforderlich"
        try:
            await asyncio.to_thread(self._validate_sync, config)
            return True, "ok"
        except Exception as exc:
            return False, f"Verbindung fehlgeschlagen: {type(exc).__name__}"

    def _validate_sync(self, config):
        # Principal abrufen reicht als Verbindungs-/Login-Test.
        self._client(config).principal()

    # --- poll -----------------------------------------------------------------

    async def poll(self, ctx, config, since, known_keys=None):
        items, cursor = await asyncio.to_thread(self._poll_sync, ctx, config, since)
        # Aufraeumen ueber die Termin-Retention (ts_due) -> keine Reconciliation noetig.
        return items, cursor, None

    def _poll_sync(self, ctx, config, since):
        import caldav
        client = self._client(config)
        now = time.time()
        start = datetime.datetime.now(datetime.timezone.utc)
        window = int(config.get("window_days", _DEFAULT_WINDOW_DAYS))
        end = start + datetime.timedelta(days=window)

        items = []
        for cal_url in config.get("calendars", []):
            try:
                cal = caldav.Calendar(client=client, url=cal_url)
                results = cal.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                # Ein Kalender-Fehler darf die anderen nicht reissen. Kalender-URL
                # mitloggen, sonst ist der Verursacher nicht auffindbar.
                logger.warning("CalDAV-Kalender %s nicht lesbar (%s) - uebersprungen",
                               cal_url, type(exc).__name__)
                continue
            for ev in results:
                for vevent in self._iter_vevents(ev):
                    item = self._event_to_item(ctx, vevent, now)
                    if item:
                        items.append(item)
        # Kein Vorwaerts-Cursor: das Fenster wird je Poll neu abgefragt.
        return items, None

    def _iter_vevents(self, ev):
        try:
            cal = icalendar.Calendar.from_ical(ev.data)
        except Exception:
            return []
        return list(cal.walk("vevent"))

    # Mappt einen VEVENT auf ein Item. Rein (ohne I/O) -> isoliert testbar.
    def _event_to_item(self, ctx, vevent, now):
        dtstart = vevent.get("dtstart")
        if dtstart is None:
            return None
        ts = self._to_ts(dtstart.dt)
        if ts is None:
            return None
        uid = str(vevent.get("uid") or "")
        summary = str(vevent.get("summary") or "(ohne Titel)")
        location = str(vevent.get("location") or "").strip()
        description = str(vevent.get("description") or "").strip()
        organizer = vevent.get("organizer")
        sender = None
        if organizer is not None:
            sender = str(organizer)
            if sender.lower().startswith("mailto:"):
                sender = sender[7:]

        # Beginn in den dedup_key, damit Einzeltermine einer Serie unterscheidbar bleiben.
        external_id = f"{uid}:{int(ts)}"
        body_parts = []
        if location:
            body_parts.append("Ort: " + location)
        if description:
            body_parts.append(description)
        body = ("\n".join(body_parts))[:500] or None

        return Item(
            feed_id=ctx.feed_id,
            user_id=ctx.user_id,
            source_type="calendar",
            external_id=external_id,
            title=summary,
            body=body,
            sender=sender,
            url=None,
            ts_source=ts,
            ts_due=ts,
            raw=None,
        )

    # Wandelt date/datetime (auch all-day) in einen Unix-Zeitstempel.
    def _to_ts(self, dt):
        if isinstance(dt, datetime.datetime):
            if dt.tzinfo is not None:
                return dt.timestamp()
            # Naive Zeit als lokale Zeit interpretieren.
            return time.mktime(dt.timetuple())
        if isinstance(dt, datetime.date):
            midnight = datetime.datetime(dt.year, dt.month, dt.day)
            return time.mktime(midnight.timetuple())
        return None
