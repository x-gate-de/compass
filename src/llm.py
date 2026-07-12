# -----------------------------------------------------------------------------
# Skript: src/llm.py
# Autor: Torben <github@x-gate.de>
# Version: 1.2.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Pluggable LLM-Client zur Dringlichkeitsbewertung eines Items. Default-Backend ist
#   ein eigenes, internes Ollama; OpenAI-kompatible Endpunkte werden ebenfalls unterstuetzt.
# - News-Ticker: formuliert aus allen offenen Tickets eines HelpDesk-Teams EINE
#   Top-Schlagzeile (ticker_headline).
# Ablauf:
# - System-Prompt traegt Rolle + Nutzer-/Gruppen-Profiltext + Override-Regel.
# - User-Prompt traegt die Item-Felder. Antwort ist striktes JSON {urgency,reason,override}.
# Betriebs- und Wartungshinweise:
# - Antwort wird validiert/geklemmt; ungueltiges JSON -> ein Retry, danach neutraler Default.
# - Item-Inhalte gehen an das INTERNE LLM -> keine externen Anbieter. Keine Secrets im Prompt.
# -----------------------------------------------------------------------------

import json
import logging
import time

import httpx

from .models import ScoreResult

logger = logging.getLogger(__name__)


class LLMClient:
    # backend: "ollama" (default) oder "openai_compatible".
    def __init__(self, backend, base_url, model, api_key="", tls_verify=True,
                 timeout=60, temperature=0.2, override_threshold=90):
        self.backend = backend
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.tls_verify = tls_verify
        self.timeout = timeout
        self.temperature = temperature
        self.override_threshold = override_threshold

    # Bewertet ein Item. Liefert immer ein gueltiges ScoreResult (Default bei Fehlern).
    # Wird ein dict `debug` uebergeben, fuellt die Methode es mit system-/user-Prompt und
    # der Roh-Antwort (fuer das Debug-Log der Web-UI).
    async def score(self, item, user_profile=None, group_profile=None, debug=None):
        system = self._build_system(user_profile, group_profile)
        user = self._build_user(item)
        if debug is not None:
            debug["system"] = system
            debug["user"] = user
            debug["raw"] = None
            debug["error"] = None
        # Ein Retry bei Netz-/Parsefehlern, danach neutraler Default (urgency=50).
        for attempt in range(2):
            try:
                content = await self._call(system, user)
                if debug is not None:
                    debug["raw"] = content
                return self._parse(content)
            except Exception as exc:
                if debug is not None:
                    debug["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("LLM-Bewertung fehlgeschlagen (Versuch %d/2): %s", attempt + 1, exc)
        return ScoreResult(urgency=50, reason="", override=False)

    def _build_system(self, user_profile, group_profile):
        parts = [
            "Du bewertest die Dringlichkeit eines eingehenden Eintrags fuer den Nutzer.",
            "Gib NUR ein JSON-Objekt zurueck: "
            '{"urgency": <0-100>, "reason": "<kurz, deutsch, <=200 Zeichen>", "override": <true|false>}.',
            "Maszstab fuer urgency (nutze die VOLLE Skala und vergib moeglichst DISTINKTE Werte,"
            " vermeide Haeufungen auf Rundzahlen wie 95):",
            "- 96-100: absolute Prioritaet, laufender kritischer Ausfall / Sicherheitsvorfall /"
            " Frist in Minuten.",
            "- 90-95: sehr dringend, aber (noch) kein laufender Totalausfall; heute unbedingt.",
            "- 70-89: heute zu erledigen.",
            "- 40-69: diese Woche relevant.",
            "- 10-39: niedrige Relevanz, kann warten.",
            "- 0-9: reine Information/Automatik (Newsletter, Werbung, Erfolgs-/Statusmeldung,"
            " Routine-Benachrichtigung, Einladung ohne Bezug).",
            "Differenziere auch innerhalb eines Bandes nach Schwere, Fristnaehe und Reichweite.",
            "Die meisten Eintraege sind NICHT dringend. Vergib 90+ nur bei echten Notfaellen,"
            " nicht fuer Routine, Reports, Einladungen oder Newsletter.",
            f"override = true NUR wenn urgency >= {self.override_threshold} UND ein echter Notfall"
            " mit sofortigem Handlungsbedarf vorliegt. Im Zweifel override = false.",
        ]
        if user_profile:
            parts.append("Profil/Prioritaeten des Nutzers (maszgeblich): " + user_profile)
        if group_profile:
            parts.append("Kontext dieser Feed-Gruppe: " + group_profile)
        return "\n".join(parts)

    def _build_user(self, item):
        # Body bewusst begrenzen, um Prompt-Groesse und Latenz zu deckeln.
        body = (item.body or "")[:2000]
        lines = [
            f"Quelle: {item.source_type}",
            f"Absender: {item.sender or '-'}",
            f"Titel: {item.title or '-'}",
        ]
        if item.ts_due:
            lines.append(f"Faellig (Unix): {item.ts_due}")
        lines.append("Inhalt:")
        lines.append(body)
        return "\n".join(lines)

    # Freitext-Auftrag (z.B. Morgen-Briefing): liefert reinen Text oder None.
    async def summarize(self, system, user, timeout=None):
        for attempt in range(2):
            try:
                text = (await self._call(system, user, timeout=timeout, json_format=False)).strip()
                if text:
                    return text.replace("**", "").replace("*", "")
            except Exception as exc:
                logger.warning("LLM-Zusammenfassung fehlgeschlagen (Versuch %d/2): %s %s",
                               attempt + 1, type(exc).__name__, exc)
        return None

    # timeout optional: der vergleichende Re-Rank-Call (grosser Prompt) braucht mehr Zeit.
    async def _call(self, system, user, timeout=None, json_format=True):
        if self.backend == "ollama":
            return await self._call_ollama(system, user, timeout, json_format)
        if self.backend == "openai_compatible":
            return await self._call_openai(system, user, timeout)
        raise ValueError(f"Unbekanntes LLM-Backend: {self.backend}")

    async def _call_ollama(self, system, user, timeout=None, json_format=True):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if json_format:
            payload["format"] = "json"
        async with httpx.AsyncClient(verify=self.tls_verify, timeout=timeout or self.timeout) as client:
            resp = await client.post(self.base_url + "/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    async def _call_openai(self, system, user, timeout=None):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
        }
        async with httpx.AsyncClient(verify=self.tls_verify, timeout=timeout or self.timeout) as client:
            resp = await client.post(self.base_url + "/v1/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    # Validiert und klemmt die Modellantwort auf das erwartete Format.
    def _parse(self, content):
        data = json.loads(content)
        urgency = int(round(float(data.get("urgency", 50))))
        urgency = 0 if urgency < 0 else 100 if urgency > 100 else urgency
        reason = str(data.get("reason", ""))[:200]
        # Override nur akzeptieren, wenn die Schwelle erreicht ist.
        override = bool(data.get("override", False)) and urgency >= self.override_threshold
        return ScoreResult(urgency=urgency, reason=reason, override=override)

    # --- News-Ticker ----------------------------------------------------------
    # Formuliert aus den offenen Tickets eines Teams EINE Top-Schlagzeile (deutsch,
    # max. ~140 Zeichen). Rueckgabe: String oder None bei Fehler (Aufrufer behaelt
    # dann die letzte gute Schlagzeile).
    async def ticker_headline(self, team_name, tickets, timeout=None):
        system = "\n".join([
            "Du bist Redakteur eines internen IT-News-Tickers eines Hosting-/Netzbetreibers.",
            "Du erhaeltst ALLE offenen und in Bearbeitung befindlichen HelpDesk-Tickets"
            " eines Teams und formulierst daraus EINE Ticker-Schlagzeile.",
            'Gib NUR ein JSON-Objekt zurueck: {"headline": "<Schlagzeile>"}.',
            "Regeln fuer die Schlagzeile:",
            "- Deutsch, maximal 140 Zeichen, Telegrammstil wie ein Boersenticker.",
            "- Nenne das WICHTIGSTE Thema (laufender Ausfall > Frist > Haeufung > Einzelfall).",
            "- Wenn mehrere Tickets dasselbe Thema betreffen, fasse zusammen und nenne die Anzahl.",
            "- Keine Ticket-IDs, keine Floskeln, keine Bewertung des Teams.",
            "- Gibt es nichts Dringendes, fasse die Lage neutral zusammen"
            ' (z.B. "12 offene Tickets, nichts Kritisches").',
        ])
        # Bewusst NUR die Betreffzeilen: haelt den Prompt klein (das interne Ollama
        # ist langsam) — die Titel reichen fuer eine Lage-Schlagzeile aus.
        lines = ["Team: %s" % team_name,
                 "Offene Tickets (Betreffzeilen, nach Prioritaet/Aktualitaet sortiert): %d" % len(tickets), ""]
        for t in tickets:
            lines.append("- %s" % (t.get("title") or "-"))
        user = "\n".join(lines)
        # Ein Retry wie beim Scoring; danach None (alte Schlagzeile bleibt stehen).
        for attempt in range(2):
            try:
                content = await self._call(system, user, timeout=timeout)
                data = json.loads(content)
                # Manche Modelle liefern Markdown-Auszeichnung -> fuers Laufband entfernen.
                headline = str(data.get("headline", "")).replace("**", "").replace("*", "").strip()
                if headline:
                    return headline[:200]
            except Exception as exc:
                # str(exc) ist z.B. bei httpx-Timeouts leer -> Typ immer mitloggen.
                logger.warning("Ticker-Schlagzeile fehlgeschlagen (Versuch %d/2): %s %s",
                               attempt + 1, type(exc).__name__, exc)
        return None

    # --- Vergleichender Re-Rank ---------------------------------------------
    # Bewertet eine LISTE von Items IM VERGLEICH zueinander und liefert verteilte,
    # distinkte Scores. Rueckgabe: dict {laufende_nummer (1-basiert): score 0-100}.
    async def rerank(self, items, user_profile=None, timeout=None):
        system = self._build_rerank_system(user_profile)
        user = self._build_rerank_user(items)
        content = await self._call(system, user, timeout=timeout)
        data = json.loads(content)
        out = {}
        for row in data.get("ranking", []):
            try:
                rid = int(row.get("id"))
                score = int(round(float(row.get("score", 0))))
            except (TypeError, ValueError):
                continue
            out[rid] = 0 if score < 0 else 100 if score > 100 else score
        return out

    def _build_rerank_system(self, user_profile):
        parts = [
            "Du ordnest eine LISTE eingehender Eintraege nach Dringlichkeit fuer den Nutzer.",
            "Betrachte die Eintraege IM VERGLEICH zueinander: was muss wirklich zuerst erledigt"
            " werden? Ein laufender Ausfall schlaegt eine Routine-/Terminmeldung.",
            "Gib NUR ein JSON-Objekt zurueck:"
            ' {"ranking": [{"id": <nummer>, "score": <0-100>}]}.',
            "Verteile die Scores ueber die VOLLE Skala und vergib DISTINKTE Werte (kein"
            " Gleichstand); genau ein Eintrag ist der wichtigste. Jede id genau einmal.",
        ]
        if user_profile:
            parts.append("Profil/Prioritaeten des Nutzers (maszgeblich): " + user_profile)
        return "\n".join(parts)

    def _build_rerank_user(self, items):
        lines = ["Eintraege (id = laufende Nummer):", ""]
        for idx, it in enumerate(items, 1):
            body = " ".join((getattr(it, "body", "") or "").split())[:280]
            due = ("  faellig(unix)=%s" % it.ts_due) if getattr(it, "ts_due", None) else ""
            lines.append("[%d] quelle=%s absender=%s%s" % (
                idx, getattr(it, "source_type", "-"), getattr(it, "sender", None) or "-", due))
            lines.append("    titel: %s" % (getattr(it, "title", None) or "-"))
            lines.append("    inhalt: %s" % body)
        return "\n".join(lines)
