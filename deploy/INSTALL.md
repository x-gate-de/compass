# compass — Installation und Betrieb

Zielsystem: `compass.internal.example.com` (192.0.2.10), Debian 13 (trixie), Python 3.13.
Extern erreichbar ueber `compass.example.com` via traefik (`traefik.example.com`).

## Komponenten

- **Daemon** (`compass.service`): XMPP/OMEMO-Account-Manager (Chat) + Aggregator-Schleifen
  (Poll/Scoring/Decay/Retention/Re-Rank).
- **Web-UI** (`compass-web.service`): uvicorn auf `127.0.0.1:8100` (Chat / NextUp / Grafana).
- **nginx**: Reverse-Proxy `:80` -> `127.0.0.1:8100`, setzt CSP (inkl. Grafana-`frame-src`).
- **traefik** (separater Host): TLS-Terminierung + Routing `compass.example.com` -> `192.0.2.10:80`,
  intern beschraenkt (ipAllowList + crowdsec).

## Server-Setup (192.0.2.10, als root)

```bash
apt-get update && apt-get install -y nginx git
id compass || useradd --system --home /opt/compass --shell /usr/sbin/nologin compass
install -d -o compass -g compass /opt/compass /var/lib/compass /var/log/compass

# Code holen (oder rsync vom Arbeitsplatz)
git clone <repo> /opt/compass            # github.com/x-gate-de/compass.git
cd /opt/compass
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Konfiguration
cp config.yaml.example config.yaml
# Secrets erzeugen und eintragen:
./venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"   # security.fernet_key
./venv/bin/python -c "import secrets;print(secrets.token_urlsafe(48))"                                  # security.session_secret
# web.session_https_only: true (laeuft hinter TLS); llm.base_url/model auf das eigene Ollama;
# grafana.base_url auf die interne Grafana-Instanz (identisch zur CSP frame-src im nginx).
chmod 600 config.yaml && chown compass:compass config.yaml
chown -R compass:compass /opt/compass

# Dienste
cp deploy/compass.service deploy/compass-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now compass-web.service compass.service

# nginx
cp deploy/nginx-compass.example.com.conf /etc/nginx/sites-available/compass.example.com.conf
ln -sf /etc/nginx/sites-available/compass.example.com.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Ersten Nutzer anlegen (Passwort wird interaktiv abgefragt)
sudo -u compass ./venv/bin/python -m scripts.create_user --login <login>
```

## traefik-Setup (traefik.example.com, als root)

```bash
cp deploy/traefik-compass.yml /etc/traefik/dynamic/compass.yml
# traefik (File-Provider) laedt die Datei automatisch; LE-Zertifikat wird beim
# ersten HTTPS-Zugriff ueber den httpChallenge (entryPoint web) ausgestellt.
# Voraussetzung: externer DNS compass.example.com zeigt auf traefik.example.com.
```

## Updates

```bash
cd /opt/compass && git pull       # oder rsync vom Arbeitsplatz
./venv/bin/pip install -r requirements.txt
systemctl restart compass.service compass-web.service
```

## Migration aus x-gate_chat / x-gate_nextup (nur ein Nutzer, z.B. tbe)

Uebernimmt Struktur/Konfiguration, NICHT die Nachrichteninhalte. `scripts/migrate.py`
laeuft auf der compass-VM; die alten DBs/Configs vorher read-only herkopieren.

**Was migriert wird:** Account-Credential (Umschluesselung alt->neu fernet_key), OMEMO-State
(gleiches Geraet, kein Neu-Trust), Chat-Archiv-Struktur (Kontakte/Raeume/Devices/Read-State/
Push-Prefs/Avatare), NextUp-Profil/Gruppen/Feeds (Feed-Config umgeschluesselt; Chat-Feeds
von HTTP auf in-process umgestellt). **Nicht** migriert: Nachrichteninhalte, Items/Bewertungen
(compass pollt neu).

```bash
# 1) Alte Daten read-only auf die compass-VM holen (Slug = Account-Verzeichnis in x-gate_chat)
mkdir -p /root/mig/chat /root/mig/nextup
rsync -a root@192.0.2.11:/opt/x-gate-chat/config.yaml            /root/mig/chat/chat-config.yaml
rsync -a root@192.0.2.11:/var/lib/x-gate-chat/accounts.db        /root/mig/chat/accounts.db
rsync -a root@192.0.2.11:/var/lib/x-gate-chat/users/<SLUG>/      /root/mig/chat/account/
rsync -a root@192.0.2.12:/opt/nextup/config.yaml                 /root/mig/nextup/nextup-config.yaml
rsync -a root@192.0.2.12:/var/lib/nextup/nextup.db               /root/mig/nextup/nextup.db

# 2) WICHTIG: den alten chat-Daemon fuer diesen Account stoppen, BEVOR der OMEMO-State
#    uebernommen wird (sonst laufen zwei Instanzen desselben OMEMO-Geraets -> Ratchet-Desync).

# 3) Erst Trockenlauf, dann echt (als Systembenutzer compass):
sudo -u compass /opt/compass/venv/bin/python scripts/migrate.py \
  --config /opt/compass/config.yaml --jid user@example.com \
  --chat-config /root/mig/chat/chat-config.yaml --chat-accounts-db /root/mig/chat/accounts.db \
  --chat-account-dir /root/mig/chat/account \
  --nextup-config /root/mig/nextup/nextup-config.yaml --nextup-db /root/mig/nextup/nextup.db \
  --nextup-login tbe --dry-run
# Ausgabe pruefen, dann denselben Befehl ohne --dry-run ausfuehren.

# 4) Kopierte Alt-Daten wieder entfernen (enthalten Secrets): rm -rf /root/mig
```

Den `<SLUG>` des Account-Verzeichnisses zeigt `ls /var/lib/x-gate-chat/users/` auf dem
chat-Server (Format `tbe_x_gate_de_<hash>`). Einzelne Quellen sind optional: nur die
angegebenen Argumente werden migriert.
