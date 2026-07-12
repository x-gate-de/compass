#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Skript: run_daemon.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Einstiegspunkt fuer den compass-Daemon: vereint den XMPP/OMEMO-Account-Manager
#   (Chat) und die Aggregator-Schleifen (Poll/Scoring/Decay/Retention/Re-Rank).
# Ablauf:
# - CLI-Argumente lesen, Konfiguration laden, Logging einrichten, Daemon starten.
# Betriebs- und Wartungshinweise:
# - Betrieb ueber systemd (deploy/compass.service). Laeuft als Systembenutzer compass.
# - Der eigentliche Daemon (manager + aggregator) wird in Etappe 2/3 angebunden.
# -----------------------------------------------------------------------------

import argparse
import logging
import sys

from src.config import ConfigError, load_config


# Richtet Root-Logging gemaess Konfiguration ein (Level + optionale Datei).
def _setup_logging(cfg):
    level = getattr(logging, str(cfg["logging"]["level"]).upper(), logging.INFO)
    handlers = [logging.StreamHandler()]
    logfile = cfg["logging"].get("file")
    if logfile:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main():
    parser = argparse.ArgumentParser(description="compass Daemon (Chat + Aggregator)")
    parser.add_argument("--config", required=True, help="Pfad zur config.yaml")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    _setup_logging(cfg)
    logger = logging.getLogger("compass.daemon")

    # Manager (XMPP/OMEMO, Etappe 2) starten; die Aggregator-Schleifen (Etappe 3)
    # werden im selben Loop ergaenzt. Ein ImportError deutet auf fehlende
    # Abhaengigkeiten hin (slixmpp/slixmpp-omemo -> requirements.txt / venv).
    try:
        from src.manager import run_daemon
    except ImportError as exc:
        logger.error("Daemon-Abhaengigkeiten fehlen (venv/requirements.txt?): %s", exc)
        return 1

    return run_daemon(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
