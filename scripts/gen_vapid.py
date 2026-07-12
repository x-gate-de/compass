# -----------------------------------------------------------------------------
# Skript: scripts/gen_vapid.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Erzeugt ein VAPID-Schluesselpaar fuer Web Push und schreibt es in config.yaml.
# Ablauf:
# - Aufruf: ./venv/bin/python scripts/gen_vapid.py "mailto:admin@example.com"
# - Der private Schluessel wird NICHT auf stdout ausgegeben, nur der oeffentliche
#   Application Server Key (nicht geheim, fuer den Browser).
# Betriebs- und Wartungshinweise:
# - Idempotent: ist push.vapid_private_key bereits gesetzt, wird nichts geaendert.
# - config.yaml danach mit Rechten 0600 belassen.
# - Konfigurationspfad ueber Umgebungsvariable COMPASS_CONFIG (Default /opt/compass/config.yaml).
# -----------------------------------------------------------------------------

import base64
import os
import sys

import yaml
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def main():
    cfg_path = os.environ.get("COMPASS_CONFIG", "/opt/compass/config.yaml")
    subject = sys.argv[1] if len(sys.argv) > 1 else "mailto:admin@example.com"

    with open(cfg_path, encoding="utf-8") as f:
        raw = f.read()
    data = yaml.safe_load(raw) or {}
    push = data.get("push") or {}
    if push.get("vapid_private_key"):
        print("Push ist bereits konfiguriert. Public key:")
        print(push.get("vapid_public_key", ""))
        return
    # Existiert ein push-Block ohne privaten Schluessel, nicht doppelt anlegen.
    if "\npush:" in raw or raw.startswith("push:"):
        print("FEHLER: 'push:'-Block existiert bereits (ohne privaten Schluessel).", file=sys.stderr)
        print("Bitte den Block manuell ausfuellen oder leeren push-Block entfernen.", file=sys.stderr)
        sys.exit(1)

    v = Vapid01()
    v.generate_keys()
    priv = v.private_pem().decode()
    rawpub = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    pub = base64.urlsafe_b64encode(rawpub).rstrip(b"=").decode()

    indented = "\n".join("    " + ln for ln in priv.strip().splitlines())
    block = (
        "\npush:\n"
        '  vapid_subject: "%s"\n'
        '  vapid_public_key: "%s"\n'
        "  vapid_private_key: |\n"
        "%s\n" % (subject, pub, indented)
    )
    with open(cfg_path, "a", encoding="utf-8") as f:
        f.write(block)
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    print("VAPID-Schluessel in %s geschrieben." % cfg_path)
    print("Application Server Key (public):")
    print(pub)


if __name__ == "__main__":
    main()
