#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Skript: deploy/bootstrap.sh
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Idempotentes Einrichten von compass auf der Ziel-VM (Debian 13): Systempakete,
#   Benutzer, venv, Abhaengigkeiten, Konfiguration, Selbsttest, systemd-Dienste, nginx.
# Ablauf:
# - Phase 1 (ohne config.yaml): Pakete/User/venv/Deps + config.yaml aus Vorlage mit
#   frisch erzeugten Server-Keys anlegen. Danach xmpp/llm/grafana ausfuellen.
# - Phase 2 (mit ausgefuellter config.yaml): Selbsttest, Dienste + nginx aktivieren.
# Betriebs- und Wartungshinweise:
# - AUSFUEHRUNG DURCH DEN FREIGABEVERANTWORTLICHEN (Mensch), NICHT durch KI (ISO 27001).
# - Vorher den Code nach /opt/compass bringen (rsync vom Arbeitsplatz).
# - Aufruf als root:  bash /opt/compass/deploy/bootstrap.sh
# - Skript ist idempotent: mehrfaches Ausfuehren ist unschaedlich.
# -----------------------------------------------------------------------------
set -euo pipefail
# Neue Dateien nicht weltweit lesbar (Haertung).
umask 0027

APP=/opt/compass
USER=compass
CFG="$APP/config.yaml"

if [ "$(id -u)" -ne 0 ]; then echo "Bitte als root ausfuehren." >&2; exit 1; fi
if [ ! -f "$APP/requirements.txt" ]; then
  echo "Code fehlt unter $APP (erst per rsync vom Arbeitsplatz bringen)." >&2; exit 1
fi

# Systempakete (idempotent).
apt-get update
apt-get install -y nginx git python3-venv python3-dev build-essential

# Systembenutzer + Verzeichnisse.
id "$USER" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$USER"
install -d -o "$USER" -g "$USER" "$APP" /var/lib/compass /var/log/compass

# venv + Abhaengigkeiten.
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"

# Phase 1: Konfiguration erstmalig anlegen (Server-Keys erzeugen), dann Abbruch zum Ausfuellen.
if [ ! -f "$CFG" ]; then
  cp "$APP/config.yaml.example" "$CFG"
  FKEY="$("$APP/venv/bin/python" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
  SKEY="$("$APP/venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(48))')"
  # Platzhalter der Vorlage durch frische Schluessel ersetzen.
  sed -i "s|HIER-FERNET-KEY-EINTRAGEN|$FKEY|; s|HIER-SESSION-SECRET-EINTRAGEN|$SKEY|" "$CFG"
  chmod 600 "$CFG"; chown "$USER:$USER" "$CFG"
  chown -R "$USER:$USER" "$APP"
  echo
  echo ">> config.yaml angelegt (fernet_key/session_secret gesetzt)."
  echo ">> Jetzt anpassen: xmpp.default_host, llm.base_url (Ollama), grafana.base_url"
  echo "   (muss zur frame-src in deploy/nginx-compass.example.com.conf passen),"
  echo "   web.session_https_only: true. Danach dieses Skript ERNEUT ausfuehren."
  exit 0
fi

chown -R "$USER:$USER" "$APP"

# Phase 2: Selbsttest vor Dienststart (Abweichung = Abbruch).
echo ">> Selbsttest ..."
sudo -u "$USER" "$APP/venv/bin/python" "$APP/scripts/selftest.py"

# systemd-Dienste installieren/aktivieren.
cp "$APP/deploy/compass.service" "$APP/deploy/compass-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now compass-web.service compass.service

# nginx-Reverse-Proxy.
cp "$APP/deploy/nginx-compass.example.com.conf" /etc/nginx/sites-available/compass.example.com.conf
ln -sf /etc/nginx/sites-available/compass.example.com.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

sleep 2
echo ">> Healthcheck:"
curl -fsS http://127.0.0.1/healthz && echo
echo
echo ">> Fertig. Externer Zugriff erst nach Ausrollen von deploy/traefik-compass.yml"
echo "   auf traefik.example.com. Danach: curl -sI https://compass.example.com  (erwartet 303 -> /login)."
