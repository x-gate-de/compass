# -----------------------------------------------------------------------------
# Skript: src/connectors/imap.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Read-only IMAP-Connector: liest neue Mails aus den vom Nutzer ausgewaehlten Ordnern.
# Ablauf:
# - discover(): listet die Ordner des Postfachs zur Auswahl in der UI.
# - validate_config(): prueft Verbindung/Login.
# - poll(since): holt je gewaehltem Ordner Mails seit `since` und mappt sie auf Items.
# Betriebs- und Wartungshinweise:
# - STRIKT READ-ONLY: Ordner werden mit EXAMINE geoeffnet (readonly), Inhalte mit
#   BODY.PEEK[] geholt -> es wird KEIN \Seen-Flag gesetzt und nichts veraendert.
# - imaplib ist blockierend; alle Netzwerkschritte laufen via asyncio.to_thread.
# - TLS-Verifikation ist Standard (IMAP4_SSL). Passwort niemals loggen.
# -----------------------------------------------------------------------------

import asyncio
import base64
import calendar
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import ssl
import time

from ..models import Item
from .base import Connector

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PER_POLL = 50
_DEFAULT_WINDOW_DAYS = 30
# Parst eine LIST-Antwortzeile: (flags) "delim" name.
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.*)$')


# Dekodiert modifiziertes UTF-7 (IMAP-Ordnernamen) fuer die Anzeige.
def decode_mutf7(name):
    res = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "&":
            end = name.find("-", i + 1)
            if end == -1:
                end = len(name)
            chunk = name[i + 1:end]
            if chunk == "":
                res.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    res.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    res.append(name[i:end + 1])
            i = end + 1
        else:
            res.append(ch)
            i += 1
    return "".join(res)


class ImapConnector(Connector):
    connector_type = "imap"

    # --- Verbindung (blockierend, laeuft im Thread) ---------------------------

    def _connect(self, config):
        host = config["host"]
        port = int(config.get("port", 993))
        if config.get("ssl", True):
            context = ssl.create_default_context()
            # Verifikation nur auf ausdruecklichen Wunsch abschalten (z.B. Self-Signed).
            if not config.get("tls_verify", True):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=context)
        else:
            conn = imaplib.IMAP4(host, port)
        conn.login(config["username"], config["password"])
        return conn

    # --- discover -------------------------------------------------------------

    async def discover(self, config):
        return await asyncio.to_thread(self._discover_sync, config)

    def _discover_sync(self, config):
        conn = self._connect(config)
        try:
            typ, data = conn.list()
            if typ != "OK":
                return []
            folders = []
            for line in data or []:
                if not isinstance(line, (bytes, bytearray)):
                    continue
                match = _LIST_RE.match(line)
                if not match:
                    continue
                # Nicht auswaehlbare Container ueberspringen.
                if b"\\Noselect" in match.group("flags"):
                    continue
                raw = match.group("name").strip()
                if raw.startswith(b'"') and raw.endswith(b'"'):
                    raw = raw[1:-1]
                name = raw.decode("ascii", "replace")
                folders.append({"id": name, "label": decode_mutf7(name)})
            return folders
        finally:
            self._safe_logout(conn)

    # --- validate -------------------------------------------------------------

    async def validate_config(self, config):
        for key in ("host", "username", "password"):
            if not config.get(key):
                return False, f"{key} ist erforderlich"
        try:
            await asyncio.to_thread(self._validate_sync, config)
            return True, "ok"
        except Exception as exc:
            return False, f"Verbindung fehlgeschlagen: {type(exc).__name__}"

    def _validate_sync(self, config):
        conn = self._connect(config)
        self._safe_logout(conn)

    # --- poll -----------------------------------------------------------------

    async def poll(self, ctx, config, since, known_keys=None):
        return await asyncio.to_thread(self._poll_sync, ctx, config, known_keys or set())

    # Liefert (neue Items, None, present_keys). present_keys = dedup_keys aller Mails, die
    # aktuell in den beobachteten Ordnern (im Zeitfenster) liegen -> verschwindet eine Mail
    # aus dem Ordner (bearbeitet/verschoben/geloescht), entfernt der Daemon sie aus compass.
    # Nur noch nicht bekannte Mails werden im Volltext geladen (known_keys).
    def _poll_sync(self, ctx, config, known_keys):
        items = []
        present = set()
        failed = False
        window_days = int(config.get("window_days", _DEFAULT_WINDOW_DAYS))
        max_new = int(config.get("max_per_poll", _DEFAULT_MAX_PER_POLL))
        conn = self._connect(config)
        try:
            for folder in config.get("folders", []):
                try:
                    self._poll_folder(conn, ctx, folder, window_days, max_new, known_keys, items, present)
                except Exception:
                    # Ein defekter Ordner darf die anderen nicht reissen UND nicht zur
                    # faelschlichen Loeschung fuehren -> Reconciliation in dieser Runde aus.
                    logger.warning("IMAP-Ordner konnte nicht gelesen werden (uebersprungen)")
                    failed = True
                    continue
        finally:
            self._safe_logout(conn)
        # Bei Teil-Fehlern keine present-Menge zurueckgeben (kein Abgleich -> kein Loeschen).
        return items, None, (None if failed else present)

    def _poll_folder(self, conn, ctx, folder, window_days, max_new, known_keys, items, present):
        # readonly=True -> EXAMINE: kein Veraendern des Ordnerzustands.
        typ, _ = conn.select(self._mailbox_arg(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError("EXAMINE fehlgeschlagen")
        uidvalidity = self._uidvalidity(conn)
        # Zeitfenster: nur Mails der letzten window_days beruecksichtigen.
        typ, data = conn.uid("SEARCH", None, "SINCE", self._since_date(time.time() - window_days * 86400))
        if typ != "OK":
            raise RuntimeError("SEARCH fehlgeschlagen")
        uids = data[0].split() if data and data[0] else []
        new = []
        for raw in uids:
            uid = raw.decode("ascii", "replace")
            external_id = f"{folder}:{uidvalidity}:{uid}"
            key = f"{ctx.feed_id}:mail:{external_id}"
            present.add(key)
            if key not in known_keys:
                new.append((raw, external_id))
        # Nur neue Mails laden, neueste zuerst, auf max_new begrenzt.
        for raw, external_id in new[-max_new:]:
            # BODY.PEEK[] holt den Inhalt OHNE \Seen zu setzen; INTERNALDATE fuer den Zeitbezug.
            typ, fetched = conn.uid("FETCH", raw, "(INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                continue
            for part in fetched:
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                ts = self._internaldate(part[0])
                item = self._message_to_item(ctx, external_id, part[1], ts)
                if item:
                    items.append(item)

    def _uidvalidity(self, conn):
        # UIDVALIDITY aus der untagged-Antwort des EXAMINE/SELECT.
        try:
            typ, data = conn.response("UIDVALIDITY")
            if data and data[0]:
                return int(data[0])
        except Exception:
            pass
        return 0

    # Mappt eine Roh-Mail auf ein Item (external_id = Ordner:UIDVALIDITY:UID).
    # Rein (ohne I/O) -> isoliert testbar.
    def _message_to_item(self, ctx, external_id, raw_bytes, ts_source):
        if not raw_bytes:
            return None
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        subject = self._header_text(msg, "subject")
        from_name, from_addr = email.utils.parseaddr(self._header_text(msg, "from") or "")
        sender = from_name or from_addr or None
        return Item(
            feed_id=ctx.feed_id,
            user_id=ctx.user_id,
            source_type="mail",
            external_id=external_id,
            title=subject or "(ohne Betreff)",
            body=self._snippet(msg),
            sender=sender,
            url=None,
            ts_source=ts_source,
            ts_due=None,
            raw=None,
        )

    # --- Hilfen ---------------------------------------------------------------

    def _header_text(self, msg, name):
        value = msg.get(name)
        return str(value) if value is not None else None

    def _snippet(self, msg):
        try:
            body = msg.get_body(preferencelist=("plain",))
        except Exception:
            body = None
        if body is None:
            return None
        try:
            text = body.get_content()
        except Exception:
            payload = body.get_payload(decode=True) or b""
            text = payload.decode(body.get_content_charset() or "utf-8", "replace")
        return text.strip()[:500] or None

    def _mailbox_arg(self, folder):
        # Ordnernamen mit Sonderzeichen fuer SELECT/EXAMINE quoten.
        if any(c in folder for c in ' "()'):
            return '"' + folder.replace('"', '\\"') + '"'
        return folder

    def _since_date(self, since):
        return time.strftime("%d-%b-%Y", time.gmtime(since))

    def _internaldate(self, meta):
        try:
            parsed = imaplib.Internaldate2tuple(meta)
            if parsed:
                return float(calendar.timegm(parsed))
        except Exception:
            pass
        return None

    def _safe_logout(self, conn):
        try:
            conn.logout()
        except Exception:
            pass
