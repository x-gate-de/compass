# -----------------------------------------------------------------------------
# Skript: src/xmpp/omemo_storage.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Persistente Storage-Implementierung fuer python-omemo (OMEMO-Zustand:
#   Identity-Key, Device-ID, Double-Ratchet-Sessions, Trust).
# Ablauf:
# - Schluessel-Wert-Ablage in SQLite. _store/_delete committen sofort, weil der
#   OMEMO-State vor Rueckkehr dauerhaft sein muss (Forward Secrecy).
# Betriebs- und Wartungshinweise:
# - Diese Datei enthaelt Schluesselmaterial. Dateirechte 0600.
# - NICHT aus Backups zurueckspielen (zerstoert Double-Ratchet-Konsistenz).
# - Geht der State verloren, ist der Daemon ein neues OMEMO-Geraet.
# -----------------------------------------------------------------------------

import json
import logging
import os
import sqlite3

from omemo import Just, Nothing, Storage

logger = logging.getLogger(__name__)


# SQLite-gestuetzte Storage fuer den OMEMO-Zustand. Implementiert die drei
# abstrakten Primitivoperationen von omemo.Storage; Caching/Serialisierung
# uebernimmt die Basisklasse.
class SqliteOmemoStorage(Storage):
    def __init__(self, path):
        super().__init__()
        # Verzeichnis sicherstellen und State-Datei nur fuer den Owner lesbar halten.
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS omemo_kv ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        self._conn.commit()
        # Seiteneffekt: Dateirechte haerten, falls neu angelegt.
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Konnte Dateirechte fuer OMEMO-State nicht setzen: %s", path)

    async def _load(self, key):
        cur = self._conn.execute("SELECT value FROM omemo_kv WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return Nothing()
        return Just(json.loads(row[0]))

    async def _store(self, key, value):
        # Sofortiges Commit: der OMEMO-State muss vor Rueckkehr persistent sein.
        self._conn.execute(
            "INSERT INTO omemo_kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    async def _delete(self, key):
        self._conn.execute("DELETE FROM omemo_kv WHERE key = ?", (key,))
        self._conn.commit()
