# -----------------------------------------------------------------------------
# Skript: src/xmpp/daemon.py
# Autor: Torben <github@x-gate.de>
# Version: 1.1.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Always-Online XMPP-Client: empfaengt/entschluesselt 1:1-OMEMO-Nachrichten,
#   nimmt an oeffentlichen (unverschluesselten) Gruppenraeumen teil, archiviert
#   alles und versendet aus der Outbox (1:1 OMEMO, Gruppe als Klartext).
# Ablauf:
# - Anmeldung; Roster persistieren; MUC-Dienste/Raeume entdecken; beigetretene
#   Raeume betreten; Carbons aktivieren; eingehende Nachrichten archivieren;
#   Empfangsbestaetigungen (XEP-0184) auswerten; Outbox abarbeiten.
# Betriebs- und Wartungshinweise:
# - Entschluesselung sofort beim Empfang (Forward Secrecy).
# - Trust-Politik fuer den unbeaufsichtigten Archivierer: TOAKAFA.
# - Gruppenchats sind serverseitig unverschluesselt; kein OMEMO im MUC.
# - Es werden keine Nachrichteninhalte geloggt.
# -----------------------------------------------------------------------------

import asyncio
import base64
import datetime
import hashlib
import io
import json
import logging
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
try:
    from pywebpush import webpush, WebPushException
except ImportError:  # Push optional -- ohne pywebpush bleibt es deaktiviert.
    webpush = None

    class WebPushException(Exception):
        pass
from slixmpp import ClientXMPP, JID
from slixmpp.plugins import register_plugin
from slixmpp_omemo import TrustLevel, XEP_0384

from .archive import MessageArchive
from .omemo_storage import SqliteOmemoStorage

logger = logging.getLogger(__name__)


# Uebersetzt technische Sende-/Verschluesselungsfehler in eine verstaendliche Meldung
# fuer die Outbox-Anzeige (ohne sensible Details).
def _human_send_error(exc):
    name = type(exc).__name__
    if name == "NoEligibleDevices":
        return "Empfaenger hat kein vertrautes OMEMO-Geraet (verschluesselt nicht moeglich)"
    return name


# Konkrete OMEMO-Plugin-Implementierung (Storage + Trust-Politik).
class XEP_0384Impl(XEP_0384):
    default_config = {
        "state_db_path": None,
        "fallback_message": "Diese Nachricht ist OMEMO-verschluesselt.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__storage = None

    def plugin_init(self):
        if not self.state_db_path:
            raise ValueError("state_db_path fuer das OMEMO-Plugin nicht gesetzt")
        self.__storage = SqliteOmemoStorage(self.state_db_path)
        super().plugin_init()

    @property
    def storage(self):
        return self.__storage

    @property
    def _btbv_enabled(self):
        return True

    async def _devices_blindly_trusted(self, blindly_trusted, identifier):
        logger.info("OMEMO: Geraete blind vertraut (%s): %d", identifier, len(blindly_trusted))

    async def _prompt_manual_trust(self, manually_trusted, identifier):
        # Unbeaufsichtigter Archivierer: TOAKAFA (alle Geraete vertrauen).
        session_manager = await self.get_session_manager()
        for device in manually_trusted:
            await session_manager.set_trust(
                device.bare_jid, device.identity_key, TrustLevel.TRUSTED.value
            )
            logger.warning("OMEMO: Geraet automatisch vertraut (TOAKAFA): %s", device.bare_jid)


register_plugin(XEP_0384Impl)


# Der Always-Online-Client.
class ArchiverBot(ClientXMPP):
    def __init__(self, config, archive):
        xmpp_cfg = config["xmpp"]
        jid = f"{xmpp_cfg['jid']}/{xmpp_cfg['resource']}"
        super().__init__(jid, xmpp_cfg["password"])

        self._config = config
        self._archive = archive
        self._own_bare = self.boundjid.bare
        # Nick fuer Gruppenraeume (Default: lokaler Teil der JID).
        self._muc_nick = xmpp_cfg.get("muc_nick") or self.boundjid.local
        self._joined_rooms = set()
        self._loops_started = False
        # True erst nach session_start (Sitzung steht). Verhindert Senden in eine
        # tote/halboffene Verbindung -> ausgehende Nachrichten bleiben in der Outbox.
        self._session_ready = False
        # slixmpp darf selbst neu verbinden; der Manager ist der Backstop-Watchdog.
        self.auto_reconnect = True

        # Web Push (optional): nur aktiv, wenn pywebpush vorhanden und VAPID konfiguriert.
        push = config.get("push") or {}
        self._vapid_subject = push.get("vapid_subject") or "mailto:admin@example.com"
        self._vapid_key = ""
        self._push_enabled = bool(webpush and (push.get("vapid_private_key") or ""))
        if self._push_enabled:
            # py_vapid/pywebpush erwarten den privaten Schluessel als RAW (32 Byte,
            # base64url) -- nicht als PEM. Daher den PEM-Schluessel einmal umwandeln.
            try:
                k = serialization.load_pem_private_key(
                    push["vapid_private_key"].encode(), password=None)
                raw = k.private_numbers().private_value.to_bytes(32, "big")
                self._vapid_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            except Exception as e:
                logger.error("VAPID-Schluessel ungueltig, Push deaktiviert: %s", type(e).__name__)
                self._push_enabled = False

        self.register_plugin("xep_0030")  # Service Discovery
        self.register_plugin("xep_0060")  # PubSub
        self.register_plugin("xep_0199")  # Ping
        self.register_plugin("xep_0280")  # Message Carbons
        self.register_plugin("xep_0334")  # Message Processing Hints
        self.register_plugin("xep_0045")  # Multi-User Chat
        self.register_plugin("xep_0184")  # Empfangsbestaetigungen
        self.register_plugin("xep_0313")  # Message Archive Management (MAM)
        self.register_plugin("xep_0363")  # HTTP File Upload (Anhaenge)
        self.register_plugin("xep_0066")  # Out of Band Data (oob)
        self.register_plugin("xep_0054")  # vCard-temp (Kontakt-Fotos)
        self.register_plugin("xep_0153")  # vCard-basierte Avatare (Presence-Hash)
        self.register_plugin(
            "xep_0384",
            {"state_db_path": config["omemo"]["state_path"]},
            module=sys.modules[__name__],
        )

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("carbon_received", self._on_carbon_received)
        self.add_event_handler("carbon_sent", self._on_carbon_sent)
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self.add_event_handler("receipt_received", self._on_receipt)
        self.add_event_handler("roster_update", lambda _e: self._persist_roster())
        self.add_event_handler("disconnected", self._on_disconnected)
        # Avatar-Aenderung eines Kontakts (Foto-Hash in dessen Presence, XEP-0153).
        self.add_event_handler("vcard_avatar_update", self._on_vcard_avatar)

    async def _on_session_start(self, _event):
        self.send_presence()
        await self.get_roster()
        self._persist_roster()
        try:
            await self["xep_0280"].enable()
            logger.info("Message Carbons aktiviert")
        except Exception as e:
            logger.warning("Carbons konnten nicht aktiviert werden: %s", type(e).__name__)
        try:
            await self["xep_0384"].get_session_manager()
            logger.info("OMEMO-Geraet veroeffentlicht/aktiv")
        except Exception as e:
            logger.error("OMEMO-Initialisierung fehlgeschlagen: %s", type(e).__name__)
        # Aktiver Keepalive (XEP-0199): erkennt tote/halboffene Verbindungen in ~1-2 Min
        # und loest einen Reconnect aus (statt erst nach einem langen TCP-Timeout).
        try:
            self["xep_0199"].enable_keepalive(interval=60, timeout=30)
        except Exception as e:
            logger.warning("Keepalive konnte nicht aktiviert werden: %s", type(e).__name__)
        # Sitzung steht -> Senden ist jetzt erlaubt, Outbox wird abgearbeitet.
        self._session_ready = True
        logger.info("Daemon online als %s", self.boundjid.full)

        # Hintergrundaufgaben nur einmal starten (session_start feuert bei Reconnect).
        if not self._loops_started:
            self._loops_started = True
            asyncio.create_task(self._discover_rooms())
            asyncio.create_task(self._outbox_loop())
            asyncio.create_task(self._avatar_sweep())

    # Verbindung verloren -> kein Senden mehr, bis die Sitzung wieder steht.
    def _on_disconnected(self, _event):
        self._session_ready = False
        logger.info("Verbindung getrennt -- warte auf Reconnect")

    # Roster in die contacts-Tabelle schreiben (Quelle der Userliste in der UI).
    def _persist_roster(self):
        try:
            roster = self.client_roster
            for jid in roster.keys():
                if jid == self._own_bare:
                    continue
                item = roster[jid]
                self._archive.upsert_contact(jid, item["name"] or "", item["subscription"] or "")
        except Exception as e:
            logger.warning("Roster-Persistenz fehlgeschlagen: %s", type(e).__name__)

    # --- Avatare (vCard-Foto, XEP-0153/0054) --------------------------------

    # Loest das vCard-Foto eines Kontakts auf und speichert es (oder einen
    # Negativ-Marker, wenn kein Foto vorhanden ist).
    async def _fetch_avatar(self, jid):
        try:
            iq = await self["xep_0054"].get_vcard(jid=jid, timeout=10)
            vc = iq["vcard_temp"]
            data = bytes(vc["PHOTO"]["BINVAL"] or b"")
            mime = vc["PHOTO"]["TYPE"] or "image/png"
        except Exception as e:
            logger.debug("vCard-Abruf %s fehlgeschlagen: %s", jid, type(e).__name__)
            return
        if data:
            self._archive.store_avatar(jid, mime, data, hashlib.sha1(data).hexdigest())
            logger.info("Avatar gespeichert: %s (%d Bytes)", jid, len(data))
        else:
            # Kein Foto -> Negativ-Marker (nicht bei jeder Presence neu laden).
            self._archive.store_avatar(jid, "", b"", "none")

    # Presence eines Kontakts trug einen (geaenderten) Foto-Hash -> Avatar holen.
    async def _on_vcard_avatar(self, pres):
        try:
            jid = JID(pres["from"]).bare
            if jid == self._own_bare:
                return
            photo_hash = pres["vcard_temp_update"]["photo"]
            if not photo_hash:
                return
            if self._archive.avatar_hash(jid) == photo_hash:
                return  # unveraendert
            await self._fetch_avatar(jid)
        except Exception as e:
            logger.debug("Avatar-Update %s: %s", pres["from"], type(e).__name__)

    # Einmaliger Hintergrund-Sweep: Avatare fuer Roster-Kontakte holen, die wir noch
    # nicht kennen (rate-limitiert). Presence-Updates halten sie danach aktuell.
    async def _avatar_sweep(self):
        await asyncio.sleep(8)  # Roster/Presence erst ankommen lassen
        try:
            jids = [j for j in self.client_roster.keys() if j != self._own_bare]
        except Exception:
            return
        for jid in jids:
            try:
                if self._archive.avatar_hash(jid) is None:
                    await self._fetch_avatar(jid)
            except Exception:
                pass
            await asyncio.sleep(0.4)  # den XMPP-Server nicht fluten

    # Oeffentliche MUC-Raeume des Servers entdecken und speichern.
    async def _discover_rooms(self):
        try:
            domain = self.boundjid.domain
            services = await self["xep_0030"].get_items(jid=domain)
            available = []
            for svc in services["disco_items"]["items"]:
                svc_jid = svc[0]
                try:
                    info = await self["xep_0030"].get_info(jid=svc_jid)
                    identities = info["disco_info"]["identities"]
                    is_muc = any(cat == "conference" for cat, _typ, _lang, _name in identities)
                    if not is_muc:
                        continue
                    rooms = await self["xep_0030"].get_items(jid=svc_jid)
                    for r in rooms["disco_items"]["items"]:
                        available.append((r[0], r[2] or r[0]))
                except Exception:
                    continue
            self._archive.set_available_rooms(available)
            logger.info("MUC-Raeume entdeckt: %d", len(available))
        except Exception as e:
            logger.warning("MUC-Discovery fehlgeschlagen: %s", type(e).__name__)

    # Betritt alle als beigetreten markierten Raeume, die noch nicht aktiv sind.
    async def _join_pending_rooms(self):
        for room_jid, nick in self._archive.joined_rooms():
            if room_jid in self._joined_rooms:
                continue
            try:
                # maxhistory=0: keine Verlaufswiederholung -> ab jetzt archivieren.
                await self["xep_0045"].join_muc(JID(room_jid), nick or self._muc_nick, maxhistory="0")
                self._joined_rooms.add(room_jid)
                logger.info("Raum betreten: %s", room_jid)
            except Exception as e:
                logger.warning("Raum %s konnte nicht betreten werden: %s", room_jid, type(e).__name__)

    # Verlaesst Raeume, deren joined-Markierung entfernt wurde (Web: "Verlassen").
    async def _leave_stale_rooms(self):
        wanted = {room for room, _nick in self._archive.joined_rooms()}
        for room in list(self._joined_rooms):
            if room in wanted:
                continue
            try:
                self["xep_0045"].leave_muc(JID(room), self._muc_nick)
            except Exception as e:
                logger.warning("Raum %s konnte nicht verlassen werden: %s", room, type(e).__name__)
            self._joined_rooms.discard(room)
            logger.info("Raum verlassen: %s", room)

    # --- Empfang ------------------------------------------------------------

    async def _archive_stanza(self, stanza, partner_jid, direction):
        if stanza["type"] not in ("chat", "normal"):
            return
        stanza_id = stanza["id"] or ""
        xep_0384 = self["xep_0384"]
        namespaces = xep_0384.is_encrypted(stanza)

        if not namespaces:
            body = stanza["body"]
            # Leere Nachrichten (Chat-States/Marker ohne Inhalt) nicht archivieren.
            if not body or not body.strip():
                return
            if self._archive.store(partner_jid, direction, body, stanza_id, decrypted=True):
                logger.info("Archiviert (plain, %s) %s", direction, partner_jid)
                if direction == "in":
                    asyncio.create_task(self._maybe_push(partner_jid, in_room=False))
            return

        namespace = next(iter(namespaces))
        try:
            message, _info = await xep_0384.decrypt_message(stanza)
            body = message["body"] or ""
            # OMEMO-verschluesselte Chat-States/Marker entschluesseln zu leerem Body
            # (von anderen eigenen Clients gesendet) -> nicht archivieren.
            if not body.strip():
                return
            if self._archive.store(partner_jid, direction, body, stanza_id, decrypted=True, namespace=namespace):
                logger.info("Archiviert (omemo, %s) %s", direction, partner_jid)
                if direction == "in":
                    asyncio.create_task(self._maybe_push(partner_jid, in_room=False))
        except Exception as e:
            logger.warning("Entschluesselung fehlgeschlagen (%s) %s: %s", direction, partner_jid, type(e).__name__)
            self._archive.store(partner_jid, direction, None, stanza_id, decrypted=False, namespace=namespace)

    async def _on_message(self, stanza):
        # Gruppenchat laeuft ueber groupchat_message; Carbons ueber eigene Handler.
        if stanza["type"] == "groupchat":
            return
        partner = stanza["from"].bare
        if partner == self._own_bare:
            return
        await self._archive_stanza(stanza, partner, "in")

    async def _on_carbon_received(self, stanza):
        inner = stanza["carbon_received"]
        partner = inner["from"].bare
        if partner == self._own_bare:
            return
        await self._archive_stanza(inner, partner, "in")

    async def _on_carbon_sent(self, stanza):
        inner = stanza["carbon_sent"]
        partner = inner["to"].bare
        await self._archive_stanza(inner, partner, "out")

    # Gruppennachricht (unverschluesselt) archivieren.
    async def _on_groupchat(self, stanza):
        room = stanza["from"].bare
        nick = stanza["from"].resource
        body = stanza["body"]
        if not body or not nick:
            return
        # Eigene (reflektierte) Nachrichten als 'out' markieren.
        direction = "out" if nick == self._muc_nick else "in"
        stanza_id = stanza["id"] or ""
        if self._archive.store(room, direction, body, stanza_id, decrypted=True, sender=nick):
            if direction == "in":
                asyncio.create_task(self._maybe_push(room, in_room=True))

    # Empfangsbestaetigung (XEP-0184) -> Nachricht als zugestellt markieren.
    async def _on_receipt(self, stanza):
        try:
            receipt_id = stanza["receipt"]
            if receipt_id:
                self._archive.mark_delivered(receipt_id)
        except Exception:
            pass

    # --- Senden -------------------------------------------------------------

    async def _outbox_loop(self):
        while True:
            try:
                # Nur arbeiten, wenn die Sitzung wirklich steht. Sonst wuerden Sends
                # in eine tote Verbindung laufen und als "gesendet" verbucht, obwohl
                # nichts ankommt. So bleiben sie in der Outbox und gehen nach dem
                # Reconnect automatisch raus (kein stiller Verlust).
                if self._session_ready and self.is_connected():
                    await self._join_pending_rooms()
                    await self._leave_stale_rooms()
                    for (outbox_id, recipient, body, kind,
                         media_path, media_name, media_mime) in self._archive.claim_pending_outbox():
                        if kind == "media":
                            await self._send_media(outbox_id, recipient, media_path, media_name, media_mime)
                        elif kind == "groupchat":
                            await self._send_groupchat(outbox_id, recipient, body)
                        else:
                            await self._send_chat(outbox_id, recipient, body)
                    # Anfragen, aeltere Nachrichten per MAM nachzuladen, abarbeiten.
                    for req_id, target, kind in self._archive.claim_pending_mam():
                        await self._backfill(req_id, target, kind)
                    # OMEMO-Geraete-/Verifizierungs-Anfragen abarbeiten.
                    for req_id, action, jid, ihex, tval in self._archive.claim_pending_omemo():
                        await self._omemo_request(req_id, action, jid, ihex, tval)
            except Exception as e:
                logger.error("Outbox-Verarbeitung fehlgeschlagen: %s", type(e).__name__)
            await asyncio.sleep(2)

    # OMEMO-Verschluesselung + Versand eines 1:1-Bodys. Gibt die Message-ID zurueck
    # (oder None bei fehlgeschlagener Verschluesselung).
    async def _omemo_send(self, recipient, body):
        recipient_jid = JID(recipient)
        xep_0384 = self["xep_0384"]
        await xep_0384.refresh_device_lists({recipient_jid})
        plain = self.make_message(mto=recipient_jid, mbody=body, mtype="chat")
        encrypted, errors = await xep_0384.encrypt_message(plain, {recipient_jid})
        if errors:
            logger.warning("OMEMO-Verschluesselung an %s mit %d Fehlern", recipient, len(errors))
        if encrypted is None:
            return None
        msg_id = self.new_id()
        encrypted["id"] = msg_id
        encrypted["request_receipt"] = True
        encrypted.send()
        return msg_id

    # Verschluesselt (OMEMO) und sendet eine 1:1-Nachricht, archiviert als 'out'.
    async def _send_chat(self, outbox_id, recipient, body):
        try:
            msg_id = await self._omemo_send(recipient, body)
            if msg_id is None:
                self._archive.mark_outbox_error(outbox_id, "Verschluesselung fehlgeschlagen")
                return
            # stanza_id = echte Message-ID, damit ein spaeteres MAM-Ergebnis dedupliziert.
            self._archive.store(recipient, "out", body, msg_id,
                                decrypted=True, namespace="send", status="sent", msg_id=msg_id)
            self._archive.mark_outbox_sent(outbox_id)
            logger.info("Gesendet (1:1) an %s", recipient)
        except Exception as e:
            logger.warning("Senden (1:1) an %s fehlgeschlagen: %s", recipient, type(e).__name__, exc_info=True)
            self._archive.mark_outbox_error(outbox_id, _human_send_error(e))

    # Sendet einen Anhang als OMEMO-Media (XEP-0454): Datei AES-256-GCM-verschluesseln,
    # per HTTP File Upload (XEP-0363) hochladen, die aesgcm://-URL (Schluessel+IV im
    # Fragment) NUR im OMEMO-verschluesselten Body verschicken (kein Klartext-OOB, sonst
    # laege der Schluessel offen) und als 'out' archivieren. Die Spool-Datei wird geloescht.
    async def _send_media(self, outbox_id, recipient, media_path, media_name, media_mime):
        try:
            with open(media_path, "rb") as f:
                data = f.read()
            key = AESGCM.generate_key(bit_length=256)
            iv = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(iv, data, None)  # GCM-Tag haengt am Ciphertext
            get_url = await self["xep_0363"].upload_file(
                media_name, size=len(ciphertext),
                content_type=media_mime or "application/octet-stream",
                input_file=io.BytesIO(ciphertext), timeout=180,
            )
            # https://host/pfad -> aesgcm://host/pfad#<iv-hex><key-hex>
            aesgcm_url = "aesgcm://" + str(get_url).split("://", 1)[-1] + "#" + iv.hex() + key.hex()
            msg_id = await self._omemo_send(recipient, aesgcm_url)
            if msg_id is None:
                self._archive.mark_outbox_error(outbox_id, "Verschluesselung fehlgeschlagen")
                return
            self._archive.store(recipient, "out", aesgcm_url, msg_id,
                                decrypted=True, namespace="send", status="sent", msg_id=msg_id)
            self._archive.mark_outbox_sent(outbox_id)
            logger.info("Anhang gesendet (1:1) an %s (%s)", recipient, media_name)
        except Exception as e:
            logger.warning("Anhang-Senden an %s fehlgeschlagen: %s", recipient, type(e).__name__, exc_info=True)
            self._archive.mark_outbox_error(outbox_id, _human_send_error(e))
        finally:
            try:
                os.remove(media_path)
            except OSError:
                pass

    # --- Web Push -----------------------------------------------------------

    # Sendet (falls fuer diese Konversation aktiviert) eine inhaltslose Push-Notiz
    # an alle Geraete-Abos. Inhalt bleibt aus Datenschutzgruenden draussen -- nur
    # ein Hinweis "Neue Nachricht von/in <Name>" + Link zum Chat.
    async def _maybe_push(self, partner, in_room):
        if not self._push_enabled:
            return
        try:
            if not self._archive.push_pref_enabled(partner):
                return
            subs = self._archive.push_subscriptions()
            if not subs:
                return
            # iOS haengt automatisch "from <App-Name>" an -> Titel = Name des Chats
            # (statt nochmal "Chat"), damit es nicht doppelt "Chat from Chat" heisst.
            name = self._archive.display_name(partner)
            body = "Neue Nachricht im Raum" if in_room else "Neue Nachricht"
            payload = json.dumps({"title": name, "body": body, "url": "/c/" + partner})
            loop = asyncio.get_event_loop()
            for sub in subs:
                gone = await loop.run_in_executor(None, self._send_one_push, sub, payload)
                if gone:
                    # Abgelaufenes Abo im Hauptthread entfernen (sqlite ist threadgebunden).
                    self._archive.delete_push_subscription(sub["endpoint"])
                    logger.info("Push-Abo entfernt (abgelaufen)")
        except Exception as e:
            logger.warning("Push fehlgeschlagen: %s", type(e).__name__)

    # Blockierender Versand eines einzelnen Push (laeuft im Executor-Thread).
    # Rueckgabe True = Abo ist abgelaufen (404/410) und soll entfernt werden.
    def _send_one_push(self, sub, payload):
        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(subscription_info=info, data=payload,
                    vapid_private_key=self._vapid_key, vapid_claims={"sub": self._vapid_subject})
            return False
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                return True
            logger.warning("Push-Versand fehlgeschlagen: HTTP %s", code)
            return False
        except Exception as e:
            logger.warning("Push-Versand fehlgeschlagen: %s", type(e).__name__)
            return False

    # Sendet eine Gruppennachricht (unverschluesselt). Die Reflexion wird archiviert.
    async def _send_groupchat(self, outbox_id, room, body):
        try:
            if room not in self._joined_rooms:
                await self["xep_0045"].join_muc(JID(room), self._muc_nick, maxhistory="0")
                self._joined_rooms.add(room)
            self.send_message(mto=JID(room), mbody=body, mtype="groupchat")
            self._archive.mark_outbox_sent(outbox_id)
            logger.info("Gesendet (Gruppe) an %s", room)
        except Exception as e:
            logger.warning("Senden (Gruppe) an %s fehlgeschlagen: %s", room, type(e).__name__)
            self._archive.mark_outbox_error(outbox_id, _human_send_error(e))

    # --- MAM: aeltere Nachrichten nachladen ---------------------------------

    # Laedt ein 30-Tage-Fenster vor dem bisher aeltesten Stand des Ziels nach.
    async def _backfill(self, req_id, target, kind):
        try:
            oldest = self._archive.mam_oldest(target)
            if oldest is None:
                oldest = self._archive.oldest_message_ts(target) or time.time()
            end_dt = datetime.datetime.fromtimestamp(oldest, datetime.timezone.utc)
            start_dt = end_dt - datetime.timedelta(days=30)
            mam = self["xep_0313"]
            if kind == "muc":
                # MUC-Archiv des Raums (unverschluesselt -> voll lesbar).
                iterator = mam.iterate(jid=JID(target), start=start_dt, end=end_dt)
            else:
                # Eigenes Archiv, gefiltert auf den Gespraechspartner.
                iterator = mam.iterate(with_jid=JID(target), start=start_dt, end=end_dt)
            count = 0
            async for result_msg in iterator:
                try:
                    if await self._archive_mam_result(result_msg, target, kind):
                        count += 1
                except Exception:
                    continue
            self._archive.set_mam_oldest(target, start_dt.timestamp())
            self._archive.mark_mam_done(req_id, True)
            logger.info("MAM-Backfill %s (%s): %d neue Nachrichten", target, kind, count)
        except Exception as e:
            logger.warning("MAM-Backfill %s fehlgeschlagen: %s", target, type(e).__name__)
            self._archive.mark_mam_done(req_id, False)

    # Verarbeitet ein einzelnes MAM-Ergebnis; Rueckgabe True wenn neu archiviert.
    async def _archive_mam_result(self, result_msg, target, kind):
        result = result_msg["mam_result"]
        forwarded = result["forwarded"]
        inner = forwarded["stanza"]
        ts = None
        try:
            stamp = forwarded["delay"]["stamp"]
            if stamp:
                ts = stamp.timestamp()
        except Exception:
            ts = None
        stanza_id = inner["id"] or result["id"] or ""

        if kind == "muc":
            nick = inner["from"].resource
            body = inner["body"]
            if not body or not nick:
                return False
            direction = "out" if nick == self._muc_nick else "in"
            return self._archive.store(target, direction, body, stanza_id, decrypted=True, sender=nick, ts=ts)

        # 1:1
        direction = "out" if inner["from"].bare == self._own_bare else "in"
        # Dedup VOR der Entschluesselung: bereits archivierte Nachrichten nicht erneut
        # OMEMO-entschluesseln (wuerde den Double-Ratchet-Zustand stoeren).
        if self._archive.has(target, direction, "", stanza_id):
            return False
        namespaces = self["xep_0384"].is_encrypted(inner)
        if namespaces:
            ns = next(iter(namespaces))
            try:
                decrypted, _info = await self["xep_0384"].decrypt_message(inner)
                body = decrypted["body"] or ""
                # Leere Nachrichten (Chat-States/Marker ohne Inhalt) nicht archivieren.
                if not body.strip():
                    return False
                return self._archive.store(target, direction, body, stanza_id,
                                           decrypted=True, namespace=ns, ts=ts)
            except Exception:
                # Vor unserer Geraete-Existenz gesendet / Schluessel weg -> unlesbar.
                return self._archive.store(target, direction, None, stanza_id,
                                           decrypted=False, namespace=ns, ts=ts)
        body = inner["body"]
        if not body or not body.strip():
            return False
        return self._archive.store(target, direction, body, stanza_id, decrypted=True, ts=ts)

    # --- OMEMO-Geraete / Verifizierung --------------------------------------

    def _trust_simple(self, name):
        if name == TrustLevel.TRUSTED.value:
            return "verified"
        if name == TrustLevel.BLINDLY_TRUSTED.value:
            return "trusted"
        if name == TrustLevel.DISTRUSTED.value:
            return "distrusted"
        return "undecided"

    def _dev_row(self, d, is_own):
        fp = d.identity_key.hex()
        grouped = " ".join(fp[i:i + 8] for i in range(0, len(fp), 8))
        return {"jid": d.bare_jid, "device_id": d.device_id, "fingerprint": grouped,
                "identity_hex": fp, "trust": self._trust_simple(d.trust_level_name),
                "is_own": is_own, "label": d.label or ""}

    # Holt eigene + Kontakt-Geraete (Fingerprint + Trust) und legt sie ab.
    async def _omemo_refresh(self, jid):
        sm = await self["xep_0384"].get_session_manager()
        rows = []
        try:
            own, own_others = await sm.get_own_device_information()
            rows.append(self._dev_row(own, True))
            for d in own_others:
                rows.append(self._dev_row(d, True))
        except Exception:
            pass
        try:
            await self["xep_0384"].refresh_device_lists({JID(jid)})
            for d in await sm.get_device_information(jid):
                rows.append(self._dev_row(d, False))
        except Exception:
            pass
        if rows:
            self._archive.set_omemo_devices(rows)

    async def _omemo_request(self, req_id, action, jid, identity_hex, trust_value):
        try:
            if action == "trust" and identity_hex and trust_value:
                level = TrustLevel.TRUSTED.value if trust_value == "verify" else TrustLevel.DISTRUSTED.value
                sm = await self["xep_0384"].get_session_manager()
                await sm.set_trust(jid, bytes.fromhex(identity_hex), level)
                logger.info("OMEMO-Trust gesetzt fuer %s -> %s", jid, trust_value)
            await self._omemo_refresh(jid)
            self._archive.mark_omemo_done(req_id, True)
        except Exception as e:
            logger.warning("OMEMO-Anfrage (%s) fehlgeschlagen: %s", action, type(e).__name__)
            self._archive.mark_omemo_done(req_id, False)


# Baut den Daemon aus der Konfiguration und liefert die laufbereite Instanz.
def build_daemon(config):
    archive = MessageArchive(config["archive"]["db_path"])
    bot = ArchiverBot(config, archive)
    return bot
