# -----------------------------------------------------------------------------
# Skript: src/connectors/odoo.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Read-only Odoo-Connectoren (XML-RPC) fuer mir zugewiesene HelpDesk-Tickets
#   (helpdesk.ticket) und Projekt-Aufgaben (project.task).
# - News-Ticker-Helfer: HelpDesk-Teams auflisten und alle offenen/in-Bearbeitung-
#   Tickets EINES Teams lesen (Basis fuer die LLM-Schlagzeile).
# Ablauf:
# - validate_config(): authentifiziert gegen Odoo (common.authenticate -> uid).
# - poll(): liest die dem konfigurierten Nutzer zugewiesenen, offenen Datensaetze und
#   mappt sie auf Items. date_deadline -> ts_due (treibt Decay).
# Betriebs- und Wartungshinweise:
# - Read-only: ausschliesslich authenticate/fields_get/search_read. Kein write.
# - Auth ueber access_token (API-Key) oder Passwort; beides als Passwort an XML-RPC.
# - Modell-/Feldunterschiede (user_ids vs user_id, Stage-Schliessfeld) werden per
#   fields_get erkannt. xmlrpc ist blockierend -> Aufrufe via asyncio.to_thread.
# -----------------------------------------------------------------------------

import asyncio
import calendar
import datetime
import html
import logging
import re
import ssl
import xmlrpc.client

from ..models import Item
from .base import Connector

logger = logging.getLogger(__name__)

_MAX_PER_POLL = 100
_TAG_RE = re.compile(r"<[^>]+>")


# Wandelt einen Odoo-Datetime/Date-String (UTC) in Unix-Sekunden.
def odoo_dt_to_ts(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(datetime.datetime.strptime(value, fmt).timetuple()))
        except ValueError:
            continue
    return None


# Liefert bei einem many2one-Wert ([id, "Name"] oder False) den Namen, sonst None.
def m2o_name(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1]
    return None


# Entfernt HTML aus Beschreibungen und kuerzt fuer den Snippet.
def strip_html(value):
    if not value:
        return None
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500] or None


# Schlanker, synchroner XML-RPC-Client (blockierend; wird im Thread aufgerufen).
class OdooClient:
    def __init__(self, url, database, username, secret, tls_verify=True):
        self.url = url.rstrip("/")
        self.database = database
        self.username = username
        self.secret = secret
        context = ssl.create_default_context()
        if not tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", context=context)
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", context=context)
        self.uid = None

    def authenticate(self):
        self.uid = self._common.authenticate(self.database, self.username, self.secret, {})
        if not self.uid:
            raise PermissionError("Odoo-Authentifizierung fehlgeschlagen")
        return self.uid

    def execute(self, model, method, *args, **kw):
        return self._models.execute_kw(self.database, self.uid, self.secret, model, method, list(args), kw)

    def field_names(self, model):
        return set(self.execute(model, "fields_get", [], {"attributes": ["type"]}).keys())


# IDs der als "geschlossen" geltenden Stages. Das Schliessfeld heisst je Odoo-Version
# anders (fold/is_close/closed) und wird per fields_get erkannt.
def closed_stage_ids(client, stage_model):
    stage_fields = client.field_names(stage_model)
    for candidate in ("fold", "is_close", "closed"):
        if candidate in stage_fields:
            try:
                return client.execute(stage_model, "search", [(candidate, "=", True)])
            except Exception:
                return []
    return []


# --- News-Ticker-Helfer (synchron; Aufruf via asyncio.to_thread) --------------

def _ticker_client(config):
    return OdooClient(
        url=config["url"], database=config["database"], username=config["username"],
        secret=config.get("access_token") or config.get("password", ""),
        tls_verify=bool(config.get("tls_verify", True)),
    )


# Alle HelpDesk-Teams zur Auswahl in der UI.
def list_helpdesk_teams(config):
    client = _ticker_client(config)
    client.authenticate()
    return client.execute("helpdesk.team", "search_read", [],
                          fields=["id", "name"], order="name")


# Alle offenen + in Bearbeitung befindlichen Tickets eines Teams (Stage nicht
# geschlossen). Bewusst NUR die Betreffzeilen (plus Sortier-Metadaten): der
# LLM-Prompt des Tickers bleibt so klein wie moeglich (langsames Ollama).
def fetch_team_tickets(config, team_id, limit=120):
    client = _ticker_client(config)
    client.authenticate()
    fields_available = client.field_names("helpdesk.ticket")
    domain = [("team_id", "=", int(team_id))]
    closed = closed_stage_ids(client, "helpdesk.stage")
    if closed:
        domain.append(("stage_id", "not in", closed))
    fields = [f for f in ("id", "name", "priority", "write_date")
              if f in fields_available]
    records = client.execute(
        "helpdesk.ticket", "search_read", domain,
        fields=fields, limit=int(limit), order="priority desc, write_date desc",
    )
    return [{
        "id": rec["id"],
        "title": rec.get("name") or "(ohne Betreff)",
        "priority": rec.get("priority"),
        "updated_ts": odoo_dt_to_ts(rec.get("write_date")),
    } for rec in records]


# Geloeste HelpDesk-Tickets der letzten Tage (Rueckblick-Kachel): Stage geschlossen,
# zuletzt geaendert im Zeitfenster. Rueckgabe kompakt (Titel/Team/Datum).
def fetch_closed_tickets(config, days=7, limit=200):
    client = _ticker_client(config)
    client.authenticate()
    closed = closed_stage_ids(client, "helpdesk.stage")
    if not closed:
        return []
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    records = client.execute(
        "helpdesk.ticket", "search_read",
        [("stage_id", "in", closed), ("write_date", ">=", since.strftime("%Y-%m-%d %H:%M:%S"))],
        fields=["id", "name", "team_id", "write_date"],
        limit=int(limit), order="write_date desc",
    )
    return [{
        "id": rec["id"],
        "title": rec.get("name") or "(ohne Betreff)",
        "team": m2o_name(rec.get("team_id")) or "-",
        "closed_ts": odoo_dt_to_ts(rec.get("write_date")),
    } for rec in records]


class _OdooBase(Connector):
    model = ""
    source_type = ""

    def _client(self, config):
        return OdooClient(
            url=config["url"],
            database=config["database"],
            username=config["username"],
            secret=config.get("access_token") or config.get("password", ""),
            tls_verify=bool(config.get("tls_verify", True)),
        )

    async def validate_config(self, config):
        for key in ("url", "database", "username"):
            if not config.get(key):
                return False, f"{key} ist erforderlich"
        if not (config.get("access_token") or config.get("password")):
            return False, "access_token oder password ist erforderlich"
        try:
            await asyncio.to_thread(self._validate_sync, config)
            return True, "ok"
        except PermissionError:
            return False, "Login abgelehnt - Login (meist E-Mail), Datenbank und API-Key pruefen"
        except Exception as exc:
            return False, f"Verbindung fehlgeschlagen: {type(exc).__name__}"

    def _validate_sync(self, config):
        self._client(config).authenticate()

    # Odoo-Feeds liefern "mir zugewiesen" -> keine waehlbaren Unterobjekte.
    async def discover(self, config):
        return []

    async def poll(self, ctx, config, since, known_keys=None):
        items, present = await asyncio.to_thread(self._poll_sync, ctx, config)
        # present_keys = aktuelle "mir zugewiesen/offen"-Menge -> der Daemon entfernt
        # Items zu Tickets/Aufgaben, die nicht mehr zugewiesen oder geschlossen sind.
        return items, None, present

    # Liest die volle aktuelle Menge zugewiesener, offener Datensaetze (fuer Reconciliation).
    def _poll_sync(self, ctx, config):
        client = self._client(config)
        client.authenticate()
        fields_available = client.field_names(self.model)
        # since=None -> keine write_date-Einschraenkung, also der vollstaendige aktuelle Stand.
        domain = self._domain(client, config, None, fields_available)
        fields = [f for f in self._read_fields() if f in fields_available]
        # Optionen als Keyword-Argumente (fields/limit/order) - nicht positional, sonst
        # interpretiert Odoo das Dict als Feldliste ("Invalid field 'fields'").
        records = client.execute(
            self.model, "search_read", domain,
            fields=fields,
            limit=int(config.get("max_per_poll", _MAX_PER_POLL)),
            order="write_date desc",
        )
        base_url = client.url
        items = []
        present = set()
        for rec in records:
            item = self._record_to_item(ctx, rec, base_url)
            if item:
                items.append(item)
                present.add(item.dedup_key)
        return items, present

    def _read_fields(self):
        raise NotImplementedError

    def _domain(self, client, config, since, fields_available):
        raise NotImplementedError

    def _record_to_item(self, ctx, rec, base_url):
        raise NotImplementedError

    # Gemeinsamer Deep-Link in die Odoo-Weboberflaeche.
    def _record_url(self, base_url, rec_id):
        return f"{base_url}/web#id={rec_id}&model={self.model}&view_type=form"


class OdooHelpdeskConnector(_OdooBase):
    connector_type = "odoo_helpdesk"
    model = "helpdesk.ticket"
    source_type = "helpdesk"

    def _read_fields(self):
        return ["id", "name", "description", "stage_id", "partner_id", "user_id",
                "team_id", "priority", "create_date", "write_date", "date_deadline"]

    def _domain(self, client, config, since, fields_available):
        domain = [("user_id", "=", client.uid)]
        if config.get("open_only", True):
            closed = self._closed_stage_ids(client, "helpdesk.stage", fields_available)
            if closed:
                domain.append(("stage_id", "not in", closed))
        if since:
            domain.append(("write_date", ">", _odoo_dt(since)))
        return domain

    def _closed_stage_ids(self, client, stage_model, fields_available):
        # Delegiert an den gemeinsamen Helfer (auch vom News-Ticker genutzt).
        return closed_stage_ids(client, stage_model)

    def _record_to_item(self, ctx, rec, base_url):
        ts = odoo_dt_to_ts(rec.get("write_date"))
        body_bits = []
        stage = m2o_name(rec.get("stage_id"))
        if stage:
            body_bits.append("Stage: " + stage)
        desc = strip_html(rec.get("description"))
        if desc:
            body_bits.append(desc)
        return Item(
            feed_id=ctx.feed_id, user_id=ctx.user_id, source_type=self.source_type,
            external_id=str(rec["id"]),
            title=rec.get("name") or "(ohne Betreff)",
            body=("\n".join(body_bits))[:500] or None,
            sender=m2o_name(rec.get("partner_id")),
            url=self._record_url(base_url, rec["id"]),
            ts_source=ts,
            ts_due=odoo_dt_to_ts(rec.get("date_deadline")),
            raw=None,
        )


class OdooProjectConnector(_OdooBase):
    connector_type = "odoo_project"
    model = "project.task"
    source_type = "project_task"

    def _read_fields(self):
        return ["id", "name", "description", "stage_id", "project_id", "user_ids", "user_id",
                "priority", "create_date", "write_date", "date_deadline"]

    def _domain(self, client, config, since, fields_available):
        # Assignee-Feld haengt von der Odoo-Version ab (user_ids many2many vs user_id).
        if "user_ids" in fields_available:
            domain = [("user_ids", "in", [client.uid])]
        elif "user_id" in fields_available:
            domain = [("user_id", "=", client.uid)]
        else:
            raise RuntimeError("project.task hat weder user_ids noch user_id")
        if config.get("open_only", True):
            closed = self._closed_stage_ids(client, "project.task.type", fields_available)
            if closed:
                domain.append(("stage_id", "not in", closed))
        if since:
            domain.append(("write_date", ">", _odoo_dt(since)))
        return domain

    def _closed_stage_ids(self, client, stage_model, fields_available):
        stage_fields = client.field_names(stage_model)
        for candidate in ("fold", "is_closed", "closed"):
            if candidate in stage_fields:
                try:
                    return client.execute(stage_model, "search", [(candidate, "=", True)])
                except Exception:
                    return []
        return []

    def _record_to_item(self, ctx, rec, base_url):
        ts = odoo_dt_to_ts(rec.get("write_date"))
        body_bits = []
        project = m2o_name(rec.get("project_id"))
        if project:
            body_bits.append("Projekt: " + project)
        desc = strip_html(rec.get("description"))
        if desc:
            body_bits.append(desc)
        return Item(
            feed_id=ctx.feed_id, user_id=ctx.user_id, source_type=self.source_type,
            external_id=str(rec["id"]),
            title=rec.get("name") or "(ohne Titel)",
            body=("\n".join(body_bits))[:500] or None,
            sender=project,
            url=self._record_url(base_url, rec["id"]),
            ts_source=ts,
            ts_due=odoo_dt_to_ts(rec.get("date_deadline")),
            raw=None,
        )


# Formatiert einen Unix-Zeitstempel als Odoo-UTC-Datetime fuer Domain-Filter.
def _odoo_dt(ts):
    return datetime.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
