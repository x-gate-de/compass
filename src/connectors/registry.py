# -----------------------------------------------------------------------------
# Skript: src/connectors/registry.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Registry der verfuegbaren Connector-Typen. Neue Connectoren werden hier eingetragen.
# Betriebs- und Wartungshinweise:
# - get_connector(connector_type) liefert eine frische Connector-Instanz.
# -----------------------------------------------------------------------------

from .caldav_conn import CalDavConnector
from .chat import ChatConnector
from .imap import ImapConnector
from .odoo import OdooHelpdeskConnector, OdooProjectConnector

# connector_type -> Connector-Klasse. Grafana ist in Phase 1 KEIN Connector (nur
# iframe-Embed als Web-Ansicht, siehe SPEC F4). Roadmap weiter: rss, Grafana-Alerts.
_CONNECTORS = {
    ChatConnector.connector_type: ChatConnector,
    ImapConnector.connector_type: ImapConnector,
    CalDavConnector.connector_type: CalDavConnector,
    OdooHelpdeskConnector.connector_type: OdooHelpdeskConnector,
    OdooProjectConnector.connector_type: OdooProjectConnector,
}


def available_types():
    return sorted(_CONNECTORS.keys())


def get_connector(connector_type):
    cls = _CONNECTORS.get(connector_type)
    if cls is None:
        raise ValueError(f"Unbekannter Connector-Typ: {connector_type}")
    return cls()
