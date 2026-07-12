# -----------------------------------------------------------------------------
# Skript: src/connectors/base.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Gemeinsame Schnittstelle aller Connectoren (wiederverwendbare Importmodule).
# Ablauf:
# - validate_config(): Verbindungstest beim Anlegen eines Feeds.
# - discover():        waehlbare Unterobjekte (z.B. IMAP-Ordner, Kalender) fuer die UI.
# - poll():            normalisierte Items ab Zeitpunkt `since` liefern.
# Betriebs- und Wartungshinweise:
# - Connectoren kennen weder Scoring noch Web-UI; sie liefern ausschliesslich Items.
# - Fehler eines Connectors bleiben auf seinen Feed begrenzt (Daemon faengt sie ab).
# -----------------------------------------------------------------------------

from dataclasses import dataclass


# Minimaler Kontext, den der Daemon je Feed an den Connector reicht (fuer dedup_key etc.).
@dataclass
class FeedContext:
    feed_id: int
    user_id: int


class Connector:
    connector_type = ""

    # Prueft die Konfiguration/Verbindung. Rueckgabe: (ok: bool, detail: str ohne Secrets).
    async def validate_config(self, config):
        raise NotImplementedError

    # Liefert waehlbare Unterobjekte als Liste von Dicts ({"id":..., "label":...}).
    async def discover(self, config):
        raise NotImplementedError

    # Liefert (items, next_since, present_keys) ab `since` (Unix-Sekunden, None=alles).
    # - items: neue/geaenderte normalisierte Items.
    # - next_since: Cursor fuer den naechsten Aufruf (oder None).
    # - present_keys: Menge der dedup_keys, die aktuell in der Quelle vorhanden sind ->
    #   der Daemon entfernt Items dieses Feeds, die nicht mehr enthalten sind. None =
    #   append-only (keine Reconciliation; Aufraeumen nur ueber Retention/Status).
    # known_keys: bereits gespeicherte dedup_keys dieses Feeds. Darf ignoriert werden.
    async def poll(self, ctx, config, since, known_keys=None):
        raise NotImplementedError
