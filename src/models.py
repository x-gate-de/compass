# -----------------------------------------------------------------------------
# Skript: src/models.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Einheitliches Item-Modell, in das alle Connectoren (inkl. der internen Chat-Quelle)
#   ihre Rohdaten normalisieren, plus das Ergebnisobjekt der LLM-Bewertung.
# Betriebs- und Wartungshinweise:
# - dedup_key macht das Einspielen idempotent (eindeutig je Feed + Quelle + externe ID).
# - content_hash erkennt inhaltliche Aenderungen und loest dadurch ein Re-Scoring aus.
# - user_id ist die accounts.id aus der Account-Registry (accounts.db).
# -----------------------------------------------------------------------------

import hashlib
from dataclasses import dataclass
from typing import Optional


# Ein normalisierter Eintrag aus einer Quelle. ts_due nur bei terminbezogenen Quellen.
@dataclass
class Item:
    feed_id: int
    user_id: int
    source_type: str
    external_id: str
    title: Optional[str] = None
    body: Optional[str] = None
    sender: Optional[str] = None
    url: Optional[str] = None
    ts_source: Optional[float] = None
    ts_due: Optional[float] = None
    raw: Optional[dict] = None

    # Global eindeutiger Schluessel je Feed + Quelle + externer ID -> Dedup beim Upsert.
    @property
    def dedup_key(self):
        return f"{self.feed_id}:{self.source_type}:{self.external_id}"

    # Hash ueber die inhaltlich relevanten Felder; aendert er sich, wird neu bewertet.
    @property
    def content_hash(self):
        h = hashlib.sha256()
        for value in (self.title, self.body, self.sender, self.url, self.ts_due):
            h.update(b"" if value is None else str(value).encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


# Ergebnis der LLM-Bewertung eines Items (vor Verrechnung mit Prioritaeten/Decay).
@dataclass
class ScoreResult:
    urgency: int
    reason: str = ""
    override: bool = False
