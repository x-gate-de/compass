# -----------------------------------------------------------------------------
# Skript: src/myday.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Funktion "Mein Tag": erzeugt aus verdichteten Item-Metadaten (Top-Eintraege,
#   Termine, offene Tickets/Aufgaben) eine priorisierte Tages-Aufgabenliste.
# - ABWEICHEND vom internen Ollama-Scoring nutzt diese Funktion die Anthropic-API
#   (externer Anbieter; freigegeben 2026-07-31, siehe SPEC/BLUEPRINT/CLAUDE).
# Ablauf:
# - plan(context): POST /v1/messages an die Anthropic-API. System-Prompt fordert
#   striktes JSON {tasks:[{task,source,why,urgency,deadline}]}. Antwort wird
#   validiert/geklemmt; ungueltiges JSON -> leere Liste (kein Absturz).
# Betriebs- und Wartungshinweise:
# - Datensparsamkeit: der Aufrufer uebergibt NUR verdichtete Metadaten (Titel/Betreff,
#   Quelle, Absender, Faelligkeit, intern berechnete Dringlichkeit), keine Roh-Inhalte.
# - Secrets: der API-Key wird als Header x-api-key gesetzt, nie geloggt.
# - Synchron blockierend? Nein: httpx.AsyncClient; Aufruf im Daemon-Loop via await.
# -----------------------------------------------------------------------------

import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class MyDayPlanner:
    def __init__(self, base_url, model, api_key, anthropic_version="2023-06-01",
                 max_tasks=12, timeout=60, tls_verify=True, temperature=0.2):
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.anthropic_version = anthropic_version
        self.max_tasks = max_tasks
        self.timeout = timeout
        self.tls_verify = tls_verify
        self.temperature = temperature

    def _system(self):
        return (
            "Du bist der persoenliche Assistent des Leiters eines Hosting-Betriebs. "
            "Aus den folgenden verdichteten Informationen (offene Vorgaenge, Termine, "
            "Tickets, Mails, Chats - jeweils nur Kurzangaben) erstellst du eine "
            "priorisierte Aufgabenliste fuer HEUTE. Regeln: nur real ableitbare "
            "Aufgaben, nichts erfinden; Wichtigstes zuerst (Ausfaelle/Stoerungen > "
            "Fristen > Termine > Routine); fasse zusammengehoerige Punkte zusammen. "
            "Antworte AUSSCHLIESSLICH mit JSON in genau dieser Form: "
            '{"tasks":[{"task":"kurze Handlungsanweisung","source":"mail|chat|ticket|'
            'project|calendar|sonst","why":"knappe Begruendung","urgency":1-100,'
            '"deadline":"HH:MM oder Datum oder null"}]}. '
            "Maximal %d Aufgaben. Kein Markdown, kein Text ausserhalb des JSON."
            % self.max_tasks
        )

    async def plan(self, context):
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }
        # Kein "temperature": bei neueren Claude-Modellen (z.B. sonnet-5) ist der
        # Parameter deprecated und fuehrt zu HTTP 400. Assistant-Prefill wird von
        # sonnet-5 ebenfalls nicht unterstuetzt -> reines robustes Parsing (Fences/
        # Prosa werden in _parse entfernt). max_tokens grosszuegig gegen Abbruch.
        payload = {
            "model": self.model,
            "max_tokens": 3000,
            "system": self._system(),
            "messages": [{"role": "user", "content": context}],
        }
        async with httpx.AsyncClient(verify=self.tls_verify, timeout=self.timeout) as client:
            resp = await client.post(self.base_url + "/v1/messages", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        if data.get("stop_reason") == "max_tokens":
            logger.warning("Mein Tag: Antwort in max_tokens gelaufen (evtl. unvollstaendig)")
        # Anthropic liefert content als Liste von Bloecken; wir wollen den Text.
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return self._parse("".join(parts))

    # Robustes Parsen: Markdown-Fences entfernen, JSON lesen, Felder klemmen.
    def _parse(self, content):
        raw = _FENCE.sub("", (content or "").strip())
        try:
            data = json.loads(raw)
        except ValueError:
            # Modell hat evtl. Text drumherum gesetzt -> erstes {...} herausschneiden.
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                logger.warning("Mein Tag: Antwort ist kein JSON")
                return []
            try:
                data = json.loads(m.group(0))
            except ValueError:
                logger.warning("Mein Tag: JSON nicht lesbar")
                return []
        tasks = []
        for t in (data.get("tasks") or [])[: self.max_tasks]:
            if not isinstance(t, dict):
                continue
            task = str(t.get("task", "")).strip()[:200]
            if not task:
                continue
            try:
                urgency = int(round(float(t.get("urgency", 50))))
            except (TypeError, ValueError):
                urgency = 50
            urgency = max(1, min(100, urgency))
            deadline = t.get("deadline")
            deadline = str(deadline).strip()[:40] if deadline not in (None, "", "null") else None
            tasks.append({
                "task": task,
                "source": str(t.get("source", "sonst")).strip().lower()[:20] or "sonst",
                "why": str(t.get("why", "")).strip()[:200],
                "urgency": urgency,
                "deadline": deadline,
            })
        tasks.sort(key=lambda x: -x["urgency"])
        return tasks
