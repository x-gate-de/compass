# -----------------------------------------------------------------------------
# Skript: src/worktime.py
# Autor: Torben <github@x-gate.de>
# Version: 1.5.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Client fuer die Zeiterfassung (phatman-kompatible API, Header X-PHATMAN-AUTH): liest je
#   Mitarbeiter die Tagesarbeitszeit (daily_stats) und die Fehltage-Jahresstatistik
#   (absences/statistics) und baut daraus den Snapshot fuer das Arbeitszeit-Laufband.
# Ablauf:
# - snapshot(prev): /users (nur enabled) -> je Nutzer daily_stats(heute) + Fehltage
#   (Jahres-Cache 1h) + Wochensumme (daily_stats Mo..heute; vergangene Tage 1h-Cache,
#   der accounting-Endpunkt deckt die laufende Woche nicht ab). "Gerade angemeldet"
#   gibt die API nicht her -> Heuristik: time_worked seit dem letzten Lauf gewachsen.
# - build_segments(payload, opts): formatiert den Snapshot als Laufband-Segmente
#   (JETZT / HEUTE / NICHT DA / WOCHE / FEHLTAGE <Jahr>). opts steuert Schwellwerte:
#   max_vacation/max_sick faerben ueberschrittene Fehltage-Zahlen (Farbe waehlbar).
# - "Heute nicht da": NUR aus einem zugeordneten Abwesenheits-Kalender (echter
#   Grund). Ohne solchen Kalender wird der Abschnitt weggelassen.
# Betriebs- und Wartungshinweise:
# - Read-only; API-Key kommt aus config.yaml (worktime.api_key, Rechte 0600).
# - Maessige Parallelitaet (Semaphore 5), um die Zeiterfassung nicht zu fluten.
# - Erreichbarkeit: die Zeiterfassung ist ggf. nur intern erreichbar; Fehler werden
#   im Snapshot vermerkt, die letzte gute Anzeige bleibt bestehen (Aufrufer).
# -----------------------------------------------------------------------------

import asyncio
import datetime
import logging
import re
import time

import httpx

from .calendar_feed import active_now, classify, covering_today, match_users

logger = logging.getLogger(__name__)

_CAL_KIND_LABEL = {"vacation": "Urlaub", "sick": "krank", "school": "Schule"}


# Betreff ohne die bereits als Label gezeigten Mitarbeiter-Kuerzel; dient als
# Grund-Text, wenn keine Kategorie (Urlaub/krank/...) erkannt wurde. Verhindert
# die Kuerzel-Dopplung ("tzi tzi"), wenn der Betreff nur aus dem Kuerzel besteht.
def _absence_reason(summary, hits):
    rest = summary or ""
    for h in hits:
        rest = re.sub(r"(?i)\b%s\b" % re.escape(h), "", rest)
    return re.sub(r"\s+", " ", rest).strip(" -+,/").strip()

# Fehltage-Kategorien der Zeiterfassung -> kurze Anzeigenamen fuers Laufband.
_ABSENCE_LABELS = {
    "Urlaub": "Urlaub",
    "Sonderurlaub": "Sonderurlaub",
    "Krankheit mit AU": "Krank",
    "Krankheit ohne AU": "Krank o. AU",
    "Krankheit - Selbstdiagnose": "Krank (selbst)",
    "Abbauen von Überstunden": "Ueberstd.-Abbau",
    "Schule": "Schule",
}


# Unbekannte (teils sehr lange) Kategorienamen fuers Laufband einkuerzen.
def _absence_label(name):
    if name in _ABSENCE_LABELS:
        return _ABSENCE_LABELS[name]
    if name.startswith("Krankheit (Kind)"):
        return "Krank (Kind)"
    return name if len(name) <= 20 else name[:18] + "…"

# Jahresstatistik aendert sich selten -> 1h-Cache je (user_id, jahr).
_ABS_CACHE_TTL = 3600
_abs_cache = {}

# Tageswerte VERGANGENER Tage der laufenden Woche aendern sich kaum -> 1h-Cache
# je (user_id, datum). Der heutige Wert wird nie gecacht (waechst laufend).
_DAY_CACHE_TTL = 3600
_day_cache = {}


class WorktimeClient:
    def __init__(self, base_url, api_key, tls_verify=True, timeout=20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tls_verify = tls_verify
        self.timeout = timeout

    async def _get(self, client, path, params=None):
        resp = await client.get(self.base_url + path, params=params,
                                headers={"X-PHATMAN-AUTH": self.api_key})
        resp.raise_for_status()
        return resp.json()

    async def _day_worked(self, client, user_id, day):
        key = (user_id, day.isoformat())
        ent = _day_cache.get(key)
        if ent and time.time() - ent[0] < _DAY_CACHE_TTL:
            return ent[1]
        ds = await self._get(client, "/users/%d/timeslots/daily_stats" % user_id,
                             {"date": day.isoformat()})
        worked = int(ds.get("time_worked") or 0)
        _day_cache[key] = (time.time(), worked)
        return worked

    async def _absences(self, client, user_id, year):
        key = (user_id, year)
        ent = _abs_cache.get(key)
        if ent and time.time() - ent[0] < _ABS_CACHE_TTL:
            return ent[1]
        data = await self._get(client, "/users/%d/absences/statistics" % user_id,
                               {"year": year})
        stats = data.get("statistics") or {}
        _abs_cache[key] = (time.time(), stats)
        return stats

    # Liest den aktuellen Stand aller aktiven Mitarbeiter. prev: dict user_id(str) ->
    # time_worked des letzten Laufs (Aktiv-Heuristik). Rueckgabe: Snapshot-Dict.
    async def snapshot(self, prev=None):
        prev = prev or {}
        today = datetime.date.today()
        async with httpx.AsyncClient(verify=self.tls_verify, timeout=self.timeout) as client:
            data = await self._get(client, "/users", {"per_page": 200})
            users = [u for u in (data.get("users") or []) if u.get("enabled")]
            sem = asyncio.Semaphore(5)

            monday = today - datetime.timedelta(days=today.weekday())
            past_days = [monday + datetime.timedelta(days=i)
                         for i in range((today - monday).days)]

            async def one(u):
                async with sem:
                    ds = await self._get(client, "/users/%d/timeslots/daily_stats" % u["id"],
                                         {"date": today.isoformat()})
                    absences = await self._absences(client, u["id"], today.year)
                    # Wochensumme: vergangene Tage (gecacht) + heutiger Live-Wert.
                    week = 0
                    for day in past_days:
                        week += await self._day_worked(client, u["id"], day)
                worked = int(ds.get("time_worked") or 0)
                return {
                    "id": u["id"], "name": u.get("name") or str(u["id"]),
                    "worked_s": worked,
                    "quota_s": int(ds.get("time_quota") or 0),
                    "week_s": week + worked,
                    # aktiv = Tagesarbeitszeit ist seit dem letzten Lauf gewachsen.
                    "active": worked > int(prev.get(str(u["id"]), worked)),
                    "absences": {k: v for k, v in absences.items() if v},
                }

            entries = await asyncio.gather(*(one(u) for u in users))
        entries.sort(key=lambda e: e["name"])
        return {"ts": time.time(), "date": today.isoformat(), "year": today.year,
                "users": entries, "error": None}


def _fmt_h(seconds):
    seconds = int(seconds or 0)
    return "%d:%02d" % (seconds // 3600, seconds % 3600 // 60)


# Fehltage eines Nutzers als Anzeige-Teile [{t, c?}]: Zahlen ueber dem Schwellwert
# werden in der konfigurierten Farbe hervorgehoben. Urlaub = Kategorie "Urlaub";
# Krank = Summe aller "Krank…"-Kategorien.
def _absence_parts(stats, opts):
    opts = opts or {}
    total_sick = sum(v for k, v in stats.items() if k.startswith("Krank"))
    vacation_over = bool(opts.get("max_vacation")) and stats.get("Urlaub", 0) > opts["max_vacation"]
    sick_over = bool(opts.get("max_sick")) and total_sick > opts["max_sick"]
    parts = []
    for i, (name, days) in enumerate(sorted(stats.items(), key=lambda kv: (-kv[1], kv[0]))):
        if i:
            parts.append({"t": " / "})
        p = {"t": "%s %d" % (_absence_label(name), days)}
        if name == "Urlaub" and vacation_over:
            p["c"] = opts.get("vacation_color") or "#f85149"
        elif name.startswith("Krank") and sick_over:
            p["c"] = opts.get("sick_color") or "#f85149"
        parts.append(p)
    return parts


# Baut die Laufband-Segmente aus einem Snapshot: [{label, text|parts, kind}].
# kind: head (Abschnittsueberschrift), active (gerade angemeldet), plain.
# cal: optionaler Kalender-Snapshot (calendar_feed) -> RUFBEREITSCHAFT + NICHT DA
# mit echtem Grund (Abwesenheits-Kalender); ohne solchen entfaellt NICHT DA.
def build_segments(payload, opts=None, cal=None):
    if not payload or not payload.get("users"):
        if payload and payload.get("error"):
            return [{"label": "Zeiterfassung", "text": "nicht erreichbar (%s)" % payload["error"],
                     "kind": "head"}]
        return [{"label": "Zeiterfassung", "text": "sammle Daten …", "kind": "head"}]
    users = payload["users"]
    segs = []
    if payload.get("error"):
        segs.append({"label": "Zeiterfassung", "text": "Stand aelter (%s)" % payload["error"],
                     "kind": "head"})
    names = [u["name"] for u in users]
    cal_events = (cal or {}).get("events") or []
    cal_feeds = (cal or {}).get("feeds") or []
    # Rufbereitschaft: zugeordneter Kalender (role=oncall); ohne Zuordnung
    # faellt die Erkennung auf das Betreff-Schluesselwort zurueck.
    oncall = [e for e in active_now(cal_events)
              if e.get("role") == "oncall"
              or (not e.get("role") and classify(e["summary"]) == "oncall")]
    if oncall:
        segs.append({"label": "RUFBEREITSCHAFT", "text": "", "kind": "head"})
        for e in oncall:
            # Schichten koennen mehrere Personen tragen ("cde+jhe").
            who = match_users(e["summary"], names)
            segs.append({"label": " + ".join(who) if who else "-",
                         "text": "" if who else e["summary"],
                         "kind": "active"})
    active = [u for u in users if u.get("active")]
    worked = [u for u in users if not u.get("active") and (u.get("worked_s") or 0) > 0]
    segs.append({"label": "JETZT", "text": "", "kind": "head"})
    if active:
        for u in active:
            quota = ("/" + _fmt_h(u["quota_s"])) if u.get("quota_s") else ""
            segs.append({"label": u["name"], "text": "● " + _fmt_h(u["worked_s"]) + quota,
                         "kind": "active"})
    else:
        segs.append({"label": "-", "text": "niemand angemeldet", "kind": "plain"})
    if worked:
        segs.append({"label": "HEUTE", "text": "", "kind": "head"})
        for u in worked:
            segs.append({"label": u["name"], "text": _fmt_h(u["worked_s"]), "kind": "plain"})
    # Heute nicht da: NUR aus einem zugeordneten Abwesenheits-Kalender (echter Grund).
    # Die fruehere Soll-0-Heuristik ist raus -- sie hat Teilzeit-/Nicht-Arbeitstage
    # (Tages-Soll 0) faelschlich als "abwesend" markiert. Ohne Abwesenheits-Kalender
    # wird der Abschnitt weggelassen (kein Raten).
    has_absence_cal = any(f.get("role") == "absence" for f in cal_feeds) \
        or (cal_events and not cal_feeds)
    if has_absence_cal:
        segs.append({"label": "NICHT DA", "text": "", "kind": "head"})
        entries = []
        for e in covering_today(cal_events):
            role = e.get("role")
            kind = classify(e["summary"])
            if role == "oncall" or (not role and kind == "oncall"):
                continue
            if role and role != "absence":
                continue  # "other"-Kalender nicht als Abwesenheit werten
            hits = match_users(e["summary"], names)
            reason = _CAL_KIND_LABEL.get(kind)
            if hits:
                # Grund: erkannte Kategorie (Urlaub/krank/Schule), sonst der Rest
                # des Betreffs ohne die bereits als Label gezeigten Kuerzel.
                # Besteht der Betreff nur aus dem Kuerzel, "abwesend" statt Dopplung.
                text = reason or _absence_reason(e["summary"], hits) or "abwesend"
                for who in hits:
                    entries.append({"label": who, "text": text, "kind": "plain"})
            else:
                entries.append({"label": "-", "text": e["summary"], "kind": "plain"})
        segs.extend(entries or [{"label": "-", "text": "alle da", "kind": "plain"}])
    # Wochensumme (Mo..heute) fuer alle mit Arbeitszeit in dieser Woche.
    week_users = [u for u in users if (u.get("week_s") or 0) > 0]
    if week_users:
        segs.append({"label": "WOCHE", "text": "", "kind": "head"})
        for u in sorted(week_users, key=lambda x: -x["week_s"]):
            segs.append({"label": u["name"], "text": _fmt_h(u["week_s"]), "kind": "plain"})
    abs_users = [u for u in users if u.get("absences")]
    if abs_users:
        segs.append({"label": "FEHLTAGE %s" % payload.get("year", ""), "text": "", "kind": "head"})
        for u in abs_users:
            segs.append({"label": u["name"], "kind": "plain",
                         "parts": _absence_parts(u["absences"], opts)})
    return segs
