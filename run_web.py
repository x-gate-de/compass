#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Skript: run_web.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Einstiegspunkt fuer die compass-Web-UI (FastAPI/uvicorn): ein Login, Navigation
#   zwischen Chat, NextUp und Grafana.
# Ablauf:
# - CLI-Argumente/Env lesen, Konfiguration laden, uvicorn mit der App starten.
# Betriebs- und Wartungshinweise:
# - Betrieb ueber systemd (deploy/compass-web.service). Bindet nur an 127.0.0.1;
#   externe Erreichbarkeit ueber nginx -> traefik.
# - Konfigurationspfad ueber --config oder Umgebungsvariable COMPASS_CONFIG.
# - Die FastAPI-App (src.web.app) wird in Etappe 5 angebunden.
# -----------------------------------------------------------------------------

import argparse
import logging
import os
import sys

from src.config import ConfigError, load_config


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
    parser = argparse.ArgumentParser(description="compass Web-UI")
    parser.add_argument(
        "--config",
        default=os.environ.get("COMPASS_CONFIG"),
        help="Pfad zur config.yaml (alternativ Umgebungsvariable COMPASS_CONFIG)",
    )
    args = parser.parse_args()
    if not args.config:
        print("Kein Konfigurationspfad (--config oder COMPASS_CONFIG)", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    _setup_logging(cfg)
    logger = logging.getLogger("compass.web")

    # Die FastAPI-App (Etappe 5) stellt eine Factory bereit, die uvicorn hier startet.
    try:
        from src.web.app import serve  # noqa: F401  (Anbindung in Etappe 5)
    except ImportError:
        logger.error("Web-App noch nicht implementiert (Etappe 5). Abbruch.")
        return 1

    return serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
