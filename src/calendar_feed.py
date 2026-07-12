# -----------------------------------------------------------------------------
# Skript: src/calendar_feed.py
# Autor: Torben <github@x-gate.de>
# Version: 1.1.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Kalender-Modul: laedt einen iCal-Feed (z.B. Abwesenheits-/Rufbereitschafts-
#   Kalender der Zeiterfassung, timetracking.example.com/ical) mit Basic-Auth und parst die
#   VEVENTs in ein kompaktes JSON-Format fuer weitere Module (Arbeitszeit-Band:
#   NICHT DA mit Grund, RUFBEREITSCHAFT; spaeter mehr).
# Ablauf:
# - fetch_events(config): HTTP-GET (Basic-Auth) -> parse_ics -> Ereignisfenster
#   (7 Tage zurueck bis 62 Tage voraus), gedeckelt auf 500 Ereignisse.
# - classify(summary): Kategorie aus dem Betreff (urlaub/krank/schule/
#   rufbereitschaft/sonstiges). match_user(): ordnet ein Ereignis einem
#   Mitarbeiter-Kuerzel zu (Wort-Treffer im Betreff).
# Betriebs- und Wartungshinweise:
# - Eigener minimaler ICS-Parser (RFC-5545-Zeilenentfaltung, DTSTART/DTEND/SUMMARY);
#   keine zusaetzliche Abhaengigkeit. Zeitzonen: UTC-Zeiten (Z) exakt, lokale
#   Zeiten in Server-Zeitzone, reine DATE-Werte als lokale Mitternacht.
# - Zugang liegt Fernet-verschluesselt in app_settings (calendar.config_enc).
# - Synchron (httpx.Client); Aufruf im Daemon via asyncio.to_thread.
# -----------------------------------------------------------------------------

import datetime
import logging
import re
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

_WINDOW_PAST = 7 * 86400
_WINDOW_FUTURE = 62 * 86400
_MAX_EVENTS = 500


def _parse_dt(prop, value):
    value = value.strip()
    if "VALUE=DATE" in prop.upper() or (len(value) == 8 and value.isdigit()):
        d = datetime.datetime.strptime(value, "%Y%m%d")
        return d.timestamp(), True
    v = value.rstrip("Z")
    dt = datetime.datetime.strptime(v, "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp(), False
    return dt.timestamp(), False


# Minimaler ICS-Parser: liefert [{summary, start, end, all_day}]. DTEND ist bei
# Ganztags-Ereignissen exklusiv (RFC 5545); fehlt DTEND, gilt 1 Tag bzw. 1 Stunde.
def parse_ics(text):
    lines = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw.rstrip("\r"))
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None and cur.get("start") is not None:
                if cur.get("end") is None:
                    cur["end"] = cur["start"] + (86400 if cur.get("all_day") else 3600)
                events.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            prop, _, val = ln.partition(":")
            name = prop.split(";")[0].upper()
            try:
                if name == "SUMMARY":
                    cur["summary"] = val.strip()[:200]
                elif name == "DTSTART":
                    cur["start"], cur["all_day"] = _parse_dt(prop, val)
                elif name == "DTEND":
                    cur["end"], _ = _parse_dt(prop, val)
            except ValueError:
                # Unbekanntes Datumsformat: Ereignis lieber ueberspringen als raten.
                cur["start"] = None
    return events


def _client(config):
    auth = None
    if config.get("username"):
        auth = (config["username"], config.get("password", ""))
    return httpx.Client(verify=bool(config.get("tls_verify", True)), timeout=30,
                        follow_redirects=True, auth=auth)


# Listet die Kalender-Sammlungen eines SoGo-/CalDAV-Kontos (PROPFIND Depth 1).
# config.url darf die DAV-Wurzel (…/SOGo/dav/<user>/) oder …/Calendar/ sein.
# Rueckgabe: [{path, name}] — path ist der DAV-Pfad der Sammlung.
def discover_calendars(config):
    base = config["url"].rstrip("/")
    if not base.endswith("/Calendar"):
        base += "/Calendar"
    body = ('<?xml version="1.0"?><propfind xmlns="DAV:">'
            '<prop><displayname/><resourcetype/></prop></propfind>')
    with _client(config) as client:
        resp = client.request("PROPFIND", base + "/",
                              headers={"Depth": "1", "Content-Type": "application/xml"},
                              content=body)
        resp.raise_for_status()
    ns = {"d": "DAV:"}
    out = []
    for r in ElementTree.fromstring(resp.content).findall("d:response", ns):
        href = r.findtext("d:href", "", ns)
        if r.find(".//d:resourcetype/d:collection", ns) is None:
            continue
        if href.rstrip("/").endswith("/Calendar"):
            continue  # die Wurzel selbst
        name = (r.findtext(".//d:displayname", "", ns) or href).strip()
        out.append({"path": href, "name": name})
    return out


# Absolute URL einer DAV-Sammlung aus dem konfigurierten Konto.
def collection_url(config, path):
    parts = urlsplit(config["url"])
    return "%s://%s%s/" % (parts.scheme, parts.netloc, path.rstrip("/"))


# Ereignisse EINER Sammlung per CalDAV-REPORT (calendar-query mit Zeitfenster).
# Noetig, weil SoGo den .ics-Export nur fuer eigene Kalender anbietet; REPORT
# funktioniert auch fuer abonnierte Sammlungen (z.B. Rufbereitschaft).
def fetch_collection_events(config, path):
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=_WINDOW_PAST)
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=_WINDOW_FUTURE)
    body = ('<?xml version="1.0"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            '<c:time-range start="%s" end="%s"/>'
            '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'
            % (start.strftime("%Y%m%dT%H%M%SZ"), end.strftime("%Y%m%dT%H%M%SZ")))
    with _client(config) as client:
        resp = client.request("REPORT", collection_url(config, path),
                              headers={"Depth": "1", "Content-Type": "application/xml"},
                              content=body)
        resp.raise_for_status()
    out = []
    for el in ElementTree.fromstring(resp.content).iter(
            "{urn:ietf:params:xml:ns:caldav}calendar-data"):
        if not el.text:
            continue
        for ev in parse_ics(el.text):
            out.append({"summary": ev.get("summary") or "(ohne Titel)",
                        "start": ev["start"], "end": ev["end"],
                        "all_day": bool(ev.get("all_day"))})
    out.sort(key=lambda e: e["start"])
    return out[:_MAX_EVENTS]


# Laedt und parst einen Feed. config: {url, username, password, tls_verify};
# url_override holt eine bestimmte Sammlung (Zuordnungs-Betrieb).
def fetch_events(config, url_override=None):
    with _client(config) as client:
        resp = client.get(url_override or config["url"])
        resp.raise_for_status()
        text = resp.text
    now = time.time()
    out = []
    for ev in parse_ics(text):
        if ev["end"] < now - _WINDOW_PAST or ev["start"] > now + _WINDOW_FUTURE:
            continue
        out.append({"summary": ev.get("summary") or "(ohne Titel)",
                    "start": ev["start"], "end": ev["end"],
                    "all_day": bool(ev.get("all_day"))})
    out.sort(key=lambda e: e["start"])
    return out[:_MAX_EVENTS]


# Kategorie aus dem Betreff (deutschsprachige Zeiterfassungs-Begriffe).
def classify(summary):
    s = (summary or "").lower()
    if "rufbereitschaft" in s or "on-call" in s or "oncall" in s:
        return "oncall"
    if "urlaub" in s:
        return "vacation"
    if "krank" in s:
        return "sick"
    if "schule" in s or "berufsschule" in s:
        return "school"
    return "other"


# Ordnet ein Ereignis Mitarbeitern zu: Kuerzel/Name als eigenes Wort im Betreff
# (auch "cde+jhe"-Kombinationen). Rueckgabe: Liste der Treffer.
def match_users(summary, names):
    tokens = set(re.split(r"[^a-z0-9@.\-]+", (summary or "").lower()))
    return [n for n in names if n.lower() in tokens]


def match_user(summary, names):
    hits = match_users(summary, names)
    return hits[0] if hits else None


# Ereignisse, die JETZT laufen (z.B. aktuelle Rufbereitschaftsschicht).
def active_now(events, now=None):
    now = now or time.time()
    return [e for e in events if e["start"] <= now < e["end"]]


# Ganztags-/Mehrtages-Ereignisse, die HEUTE beruehren (Abwesenheiten).
def covering_today(events, now=None):
    now = now or time.time()
    day = datetime.date.fromtimestamp(now)
    day0 = datetime.datetime.combine(day, datetime.time.min).timestamp()
    day1 = day0 + 86400
    return [e for e in events if e["start"] < day1 and e["end"] > day0]
