#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Skript: scripts/migrate.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Einmalige Migration der Daten EINES Nutzers (Default: tbe) aus x-gate_chat und
#   x-gate_nextup in compass. Uebernimmt Struktur/Konfiguration, NICHT die
#   Nachrichteninhalte (messages werden bewusst ausgelassen).
# Ablauf (jeweils optional, je nach uebergebenen Quellen):
# - Chat-Account: Passwort aus der alten Registry mit dem ALTEN fernet_key entschluesseln
#   und mit dem NEUEN compass-fernet_key neu speichern (Daemon kann sofort verbinden).
# - OMEMO-State: omemo_state.sqlite 1:1 uebernehmen (gleiches OMEMO-Geraet -> kein Neu-Trust).
# - Chat-Archiv-Struktur: contacts, mucs, muc_available, omemo_devices, read_state,
#   push_prefs, avatars uebernehmen. KEINE messages/outbox (Inhalte nicht benoetigt).
# - NextUp: user_profiles, feed_groups und feeds fuer den Nutzer. feeds.config_enc wird
#   mit dem ALTEN nextup-fernet_key entschluesselt und mit dem NEUEN Key neu verschluesselt.
#   Chat-Feeds werden von HTTP (base_url/token) auf IN-PROCESS (archive_path) umgestellt.
#   Items/Bewertungen werden NICHT migriert (compass pollt sie neu).
# Betriebs- und Wartungshinweise:
# - Auf der compass-VM ausfuehren; alte DBs/Configs vorher dorthin rsyncen (read-only).
# - WICHTIG: Den alten chat-Daemon fuer diesen Account VOR der OMEMO-State-Uebernahme
#   stoppen (sonst laufen zwei Instanzen desselben OMEMO-Geraets -> Double-Ratchet-Desync).
# - --dry-run zeigt nur, was passieren wuerde (keine Schreibzugriffe).
# - Aufruf-Beispiel siehe --help.
# -----------------------------------------------------------------------------

import argparse
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from src import db, store  # noqa: E402
from src.accounts import AccountRegistry, account_slug  # noqa: E402
from src.xmpp.chat_schema import ensure_chat_schema  # noqa: E402


# Tabellen des Chat-Archivs, deren STRUKTUR uebernommen wird (ohne Nachrichteninhalte).
_CHAT_COPY_TABLES = ("contacts", "mucs", "muc_available", "omemo_devices",
                     "read_state", "push_prefs", "avatars")


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _fernet_key_of(cfg):
    key = (cfg.get("security") or {}).get("fernet_key")
    if not key:
        raise SystemExit("fernet_key fehlt in %s" % cfg)
    return key


def _ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


class Migration:
    def __init__(self, compass_cfg_path, jid, dry_run):
        self.dry = dry_run
        self.jid = jid
        self.cfg = _load_yaml(compass_cfg_path)
        self.new_key = _fernet_key_of(self.cfg)
        self.acc_db = (self.cfg.get("accounts") or {}).get("db_path")
        self.users_dir = (self.cfg.get("accounts") or {}).get("users_dir")
        self.db_path = (self.cfg.get("database") or {}).get("path")
        if not (self.acc_db and self.users_dir and self.db_path):
            raise SystemExit("compass-config unvollstaendig (accounts/database).")
        self.registry = AccountRegistry(self.acc_db, self.new_key, self.users_dir)
        self.uid = None  # accounts.id = user_id (nach Account-Migration bekannt)

    def log(self, msg):
        print(("[dry-run] " if self.dry else "") + msg)

    # --- 1) Account-Credential -------------------------------------------------
    def migrate_account(self, chat_config_path, chat_accounts_db):
        old_key = _fernet_key_of(_load_yaml(chat_config_path))
        conn = _ro(chat_accounts_db)
        try:
            row = conn.execute("SELECT * FROM accounts WHERE jid = ?", (self.jid,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise SystemExit("Account %s nicht in %s gefunden." % (self.jid, chat_accounts_db))
        try:
            password = Fernet(old_key).decrypt(row["password_enc"].encode("ascii")).decode("utf-8")
        except Exception:
            raise SystemExit("Passwort nicht entschluesselbar (falscher chat-fernet_key?).")
        self.log("Account %s: Credential uebernehmen (Umschluesselung alt->neu)" % self.jid)
        if not self.dry:
            self.registry.upsert(self.jid, password, host=row["host"], port=row["port"] or 5222,
                                 resource=self.cfg["xmpp"].get("resource", "compass"),
                                 muc_nick=row["muc_nick"] or self.jid.split("@")[0])
        # user_id fuer den Rest der Migration.
        self.uid = self.registry.account_id(self.jid) if not self.dry else -1
        self.log("  -> accounts.id (user_id) = %s" % self.uid)

    # Falls kein Account migriert wird: sicherstellen, dass der Account existiert.
    def ensure_uid(self):
        if self.uid is not None:
            return
        uid = self.registry.account_id(self.jid)
        if uid is None and not self.dry:
            raise SystemExit(
                "Kein compass-Account fuer %s. Erst --chat-config/--chat-accounts-db "
                "angeben (Credential-Migration) oder den Nutzer einmal per Web anmelden." % self.jid)
        self.uid = uid if uid is not None else -1

    # --- 2) OMEMO-State + Chat-Archiv-Struktur --------------------------------
    def migrate_chat_archive(self, old_account_dir, skip_omemo_state=False):
        old_msgs = os.path.join(old_account_dir, "messages.sqlite")
        old_state = os.path.join(old_account_dir, "omemo_state.sqlite")
        target_dir = self.registry.account_dir(self.jid)

        # OMEMO-State bewusst NICHT uebernehmen -> compass wird ein NEUES OMEMO-Geraet und
        # kann gefahrlos neben dem alten chat-Archivierer laufen (kein Ratchet-Konflikt).
        if skip_omemo_state:
            self.log("OMEMO-State uebersprungen (--skip-omemo-state): compass wird neues Geraet.")
        # OMEMO-State 1:1 uebernehmen (gleiches Geraet). Nur wenn alter Daemon gestoppt ist!
        elif os.path.isfile(old_state):
            self.log("OMEMO-State uebernehmen: %s -> %s" % (old_state, os.path.join(target_dir, "omemo_state.sqlite")))
            if not self.dry:
                os.makedirs(target_dir, exist_ok=True)
                shutil.copy2(old_state, os.path.join(target_dir, "omemo_state.sqlite"))
                try:
                    os.chmod(os.path.join(target_dir, "omemo_state.sqlite"), 0o600)
                except OSError:
                    pass
        else:
            self.log("Kein omemo_state.sqlite gefunden (uebersprungen).")

        if not os.path.isfile(old_msgs):
            self.log("Kein messages.sqlite gefunden (Archiv-Struktur uebersprungen).")
            return
        target_msgs = self.registry.archive_path(self.jid)
        src = _ro(old_msgs)
        try:
            counts = {}
            if not self.dry:
                os.makedirs(target_dir, exist_ok=True)
                dst = sqlite3.connect(target_msgs, timeout=5)
                ensure_chat_schema(dst)
            for table in _CHAT_COPY_TABLES:
                try:
                    rows = src.execute("SELECT * FROM %s" % table).fetchall()
                except sqlite3.OperationalError:
                    continue  # Tabelle im alten Archiv nicht vorhanden
                counts[table] = len(rows)
                if self.dry or not rows:
                    continue
                cols = _table_columns(dst, table)
                common = [c for c in rows[0].keys() if c in cols]
                ph = ",".join("?" * len(common))
                sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (table, ",".join(common), ph)
                dst.executemany(sql, [[r[c] for c in common] for r in rows])
            if not self.dry:
                dst.commit()
                dst.close()
                try:
                    os.chmod(target_msgs, 0o600)
                except OSError:
                    pass
        finally:
            src.close()
        self.log("Chat-Archiv-Struktur uebernommen (ohne Nachrichten): %s" %
                 ", ".join("%s=%d" % (t, counts.get(t, 0)) for t in _CHAT_COPY_TABLES))

    # --- 3) NextUp: Profil, Gruppen, Feeds ------------------------------------
    def migrate_nextup(self, nextup_config_path, nextup_db, login):
        self.ensure_uid()
        old_key = _fernet_key_of(_load_yaml(nextup_config_path))
        src = _ro(nextup_db)
        try:
            user = src.execute("SELECT id, profile_text FROM users WHERE login = ?", (login,)).fetchone()
            if not user:
                raise SystemExit("NextUp-Login '%s' nicht in %s gefunden." % (login, nextup_db))
            old_uid = user["id"]
            groups = src.execute(
                "SELECT * FROM feed_groups WHERE user_id = ? ORDER BY position, id", (old_uid,)).fetchall()
            feeds_by_group = {}
            for g in groups:
                feeds_by_group[g["id"]] = src.execute(
                    "SELECT * FROM feeds WHERE group_id = ? ORDER BY id", (g["id"],)).fetchall()
        finally:
            src.close()

        now = time.time()
        dst = None if self.dry else db.connect(self.db_path)
        try:
            # Wichtigkeits-Profil
            if user["profile_text"]:
                self.log("Nutzerprofil uebernehmen (%d Zeichen)" % len(user["profile_text"]))
                if not self.dry:
                    store.set_user_profile(dst, self.uid, user["profile_text"], now)

            gcount = fcount = skipped = 0
            for g in groups:
                self.log("Gruppe '%s' (P%s) uebernehmen" % (g["name"], g["priority"]))
                gcount += 1
                new_gid = None
                if not self.dry:
                    new_gid = store.create_group(dst, self.uid, g["name"], now, priority=g["priority"],
                                                 position=g["position"] or 0, profile_text=g["profile_text"])
                for feed in feeds_by_group.get(g["id"], []):
                    new_cfg = self._convert_feed_config(feed, old_key)
                    if new_cfg is None:
                        skipped += 1
                        self.log("  ! Feed '%s' (%s) uebersprungen (nicht konvertierbar)"
                                 % (feed["name"], feed["connector_type"]))
                        continue
                    self.log("  Feed '%s' (%s) uebernehmen" % (feed["name"], feed["connector_type"]))
                    fcount += 1
                    if not self.dry:
                        enc = db.encrypt_config(self.new_key, new_cfg)
                        store.create_feed(dst, new_gid, feed["connector_type"], feed["name"], enc, now,
                                          priority=feed["priority"],
                                          poll_interval=feed["poll_interval"],
                                          llm_scoring=bool(feed["llm_scoring"]) if "llm_scoring" in feed.keys() else True)
            self.log("NextUp: %d Gruppen, %d Feeds uebernommen, %d uebersprungen." % (gcount, fcount, skipped))
        finally:
            if dst is not None:
                dst.close()

    # Entschluesselt die alte Feed-Config und bereitet sie fuer compass auf.
    # Chat-Feeds: HTTP (base_url/token) -> IN-PROCESS (archive_path). Rest: unveraendert.
    def _convert_feed_config(self, feed, old_key):
        try:
            cfg = db.decrypt_config(old_key, feed["config_enc"])
        except Exception:
            return None
        if feed["connector_type"] == "chat":
            partner = cfg.get("partner")
            if not partner:
                return None  # Legacy /api/feed ohne partner -> nicht in-process abbildbar
            return {
                "archive_path": self.registry.archive_path(self.jid),
                "partner": partner,
                "is_room": cfg.get("is_room", False),
                "max_age_hours": int(cfg.get("max_age_hours", 24)),
                "include_outgoing": cfg.get("include_outgoing", False),
            }
        return cfg  # imap/caldav/odoo unveraendert (nur Umschluesselung)


def main():
    p = argparse.ArgumentParser(
        description="Migration eines Nutzers aus x-gate_chat/x-gate_nextup nach compass "
                    "(Struktur/Config, ohne Nachrichteninhalte).",
        epilog="Beispiel:\n"
               "  ./venv/bin/python scripts/migrate.py --config /opt/compass/config.yaml \\\n"
               "     --jid user@example.com \\\n"
               "     --chat-config ./old/chat-config.yaml --chat-accounts-db ./old/accounts.db \\\n"
               "     --chat-account-dir ./old/users/tbe_x_gate_de_xxxx \\\n"
               "     --nextup-config ./old/nextup-config.yaml --nextup-db ./old/nextup.db --nextup-login tbe",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True, help="compass config.yaml (Ziel)")
    p.add_argument("--jid", required=True, help="XMPP-JID des Nutzers (z.B. user@example.com)")
    p.add_argument("--chat-config", help="Alte x-gate_chat config.yaml (fuer alten fernet_key)")
    p.add_argument("--chat-accounts-db", help="Alte accounts.db aus x-gate_chat")
    p.add_argument("--chat-account-dir", help="Altes Account-Verzeichnis (messages.sqlite + omemo_state.sqlite)")
    p.add_argument("--nextup-config", help="Alte x-gate_nextup config.yaml (fuer alten fernet_key)")
    p.add_argument("--nextup-db", help="Alte nextup.db")
    p.add_argument("--nextup-login", default="tbe", help="Login des Nutzers in NextUp (Default tbe)")
    p.add_argument("--skip-omemo-state", action="store_true",
                   help="OMEMO-State NICHT uebernehmen -> compass wird neues Geraet (neben chat betreibbar)")
    p.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben")
    args = p.parse_args()

    m = Migration(args.config, args.jid, args.dry_run)

    if args.chat_config and args.chat_accounts_db:
        m.migrate_account(args.chat_config, args.chat_accounts_db)
    if args.chat_account_dir:
        m.ensure_uid()
        m.migrate_chat_archive(args.chat_account_dir, skip_omemo_state=args.skip_omemo_state)
    if args.nextup_config and args.nextup_db:
        m.migrate_nextup(args.nextup_config, args.nextup_db, args.nextup_login)

    print("\nMigration %s." % ("simuliert (dry-run)" if args.dry_run else "abgeschlossen"))
    if not any([args.chat_accounts_db, args.chat_account_dir, args.nextup_db]):
        print("Hinweis: keine Quelle angegeben -- nichts zu tun. Siehe --help.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
