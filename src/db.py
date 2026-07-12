# -----------------------------------------------------------------------------
# Skript: src/db.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Datenbankverbindung (SQLite/WAL) fuer die zentrale Aggregator-DB (compass.db)
#   sowie Hilfsfunktionen fuer die Fernet-Verschluesselung der Feed-Credentials.
# Betriebs- und Wartungshinweise:
# - Feed-Credentials werden ausschliesslich verschluesselt (feeds.config_enc) abgelegt.
#   Verlust des fernet_key = gespeicherte Feed-Zugangsdaten nicht mehr entschluesselbar.
# - XMPP-Login-Passwoerter liegen NICHT hier, sondern in der Account-Registry
#   (accounts.db, ebenfalls Fernet); der Login validiert per XMPP-Bind.
# -----------------------------------------------------------------------------

import json
import logging
import os
import sqlite3

from cryptography.fernet import Fernet

from .schema import ensure_schema

logger = logging.getLogger(__name__)


# Oeffnet die Aggregator-DB, setzt Row-Factory und stellt das Schema sicher.
def connect(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("Konnte Dateirechte fuer %s nicht setzen", path)
    return conn


def _fernet(key):
    return Fernet(key.encode() if isinstance(key, str) else key)


# Verschluesselt die komplette Feed-Konfiguration (inkl. Secrets) zu einem Text-Token.
def encrypt_config(key, data):
    return _fernet(key).encrypt(json.dumps(data).encode("utf-8")).decode("ascii")


# Entschluesselt ein config_enc-Token zurueck zum Konfigurations-Dict.
def decrypt_config(key, token):
    return json.loads(_fernet(key).decrypt(token.encode("ascii")).decode("utf-8"))
