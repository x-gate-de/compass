#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Skript: scripts/selftest.py
# Autor: Torben <github@x-gate.de>
# Version: 1.0.0
# Lizenz: AGPL-3.0-or-later — siehe LICENSE.
# Zweck:
# - Offline-Selbsttest der compass-Datenpipeline als Regressions-Referenz (SPEC =
#   Soll-Verhalten): Konfiguration, Schema, Account-Registry, Chat-Archiv, In-Process-
#   Chat-Connector, Aggregator-Store, Scoring/Ranking und Cross-DB-Aufraeumen.
# Ablauf:
# - Aufruf: ./venv/bin/python scripts/selftest.py  (bzw. python -m scripts.selftest)
# - Nutzt ausschliesslich temporaere Verzeichnisse; KEIN Netzwerk, kein XMPP, kein LLM.
# - Exit-Code 0 = bestanden, 1 = Abweichung (Bug).
# Betriebs- und Wartungshinweise:
# - Benoetigt die Basis-Abhaengigkeiten (cryptography, PyYAML); keine slixmpp/fastapi.
# - Bewusst dependency-arm (importiert den Chat-Connector direkt, nicht die Registry,
#   damit caldav/icalendar nicht erforderlich sind).
# -----------------------------------------------------------------------------

import asyncio
import os
import stat
import sys
import tempfile
import time
from types import SimpleNamespace

# Repo-Wurzel in den Pfad (Aufruf sowohl als Datei als auch als Modul moeglich).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402
import secrets  # noqa: E402

from src.config import load_config  # noqa: E402
from src import db, store, schema  # noqa: E402
from src.accounts import AccountRegistry, account_slug  # noqa: E402
from src.xmpp.archive import MessageArchive  # noqa: E402
from src.connectors.chat import ChatConnector  # noqa: E402
from src.models import Item  # noqa: E402
from src.scoring import ScoringParams, compute_scores  # noqa: E402

_passed = 0


def check(label, cond):
    global _passed
    if not cond:
        raise AssertionError(label)
    _passed += 1
    print("OK  " + label)


def main():
    tmp = tempfile.mkdtemp(prefix="compass_selftest_")
    fkey = Fernet.generate_key().decode()
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(
            "security: {fernet_key: \"%s\", session_secret: \"%s\"}\n"
            "accounts: {db_path: \"%s/accounts.db\", users_dir: \"%s/users\"}\n"
            "database: {path: \"%s/compass.db\"}\n"
            "grafana: {base_url: \"https://grafana.example.com\"}\n"
            % (fkey, secrets.token_urlsafe(48), tmp, tmp, tmp)
        )

    # 1) Konfiguration + Defaults
    cfg = load_config(cfg_path)
    check("config: web.bind_port Default 8100", cfg["web"]["bind_port"] == 8100)
    check("config: xmpp.resource Default compass", cfg["xmpp"]["resource"] == "compass")
    check("config: llm.model Default llama3.1", cfg["llm"]["model"] == "llama3.1")

    # 2) Aggregator-Schema
    conn = db.connect(cfg["database"]["path"])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("schema: alle Aggregator-Tabellen",
          {"user_profiles", "feed_groups", "feeds", "items", "item_scores",
           "item_state", "sync_requests", "debug_events"} <= tables)
    check("db: compass.db Rechte 0600",
          stat.S_IMODE(os.stat(cfg["database"]["path"]).st_mode) == 0o600)
    tok = db.encrypt_config(fkey, {"user": "a", "pw": "geheim"})
    check("db: Fernet-Config Round-Trip", db.decrypt_config(fkey, tok) == {"user": "a", "pw": "geheim"})

    # 3) Account-Registry (Identitaet; accounts.id = user_id)
    reg = AccountRegistry(cfg["accounts"]["db_path"], fkey, cfg["accounts"]["users_dir"])
    reg.upsert("user@example.com", "pw123", host="xmpp.example.com")
    uid = reg.account_id("user@example.com")
    check("registry: account_id ist int", isinstance(uid, int) and uid > 0)
    check("registry: jid_for_id Rueckabbildung", reg.jid_for_id(uid) == "user@example.com")
    reg.set_auth_state("user@example.com", "ok")
    check("registry: verified_match nach ok", reg.verified_match("user@example.com", "pw123"))
    check("registry: falsches Passwort abgelehnt", not reg.verified_match("user@example.com", "falsch"))
    t = reg.create_api_token("user@example.com", "monitoring")
    check("registry: API-Token aufloesbar", reg.account_for_token(t) == "user@example.com")
    check("registry: falscher Token None", reg.account_for_token("nope") is None)
    check("registry: accounts.db Rechte 0600",
          stat.S_IMODE(os.stat(cfg["accounts"]["db_path"]).st_mode) == 0o600)

    # 4) Chat-Archiv (Etappe 2) fuellen
    arc = MessageArchive(reg.archive_path("user@example.com"))
    arc.upsert_contact("alice@example.com", "Alice", "both")
    check("archive: store neu", arc.store("alice@example.com", "in", "Hallo", "m1") is True)
    check("archive: store idempotent", arc.store("alice@example.com", "in", "Hallo", "m1") is False)
    for i in range(2):
        arc.store("alice@example.com", "in", "Nachricht %d" % i, "x%d" % i)
    check("archive: slug stabil", account_slug("user@example.com").startswith("tbe_x_gate_de_"))

    # 5) In-Process Chat-Connector (Etappe 3) liest das Archiv
    cfg_enc = db.encrypt_config(fkey, {"archive_path": reg.archive_path("user@example.com"),
                                       "partner": "alice@example.com", "is_room": False,
                                       "max_age_hours": 24})
    now = time.time()
    gid = store.create_group(conn, uid, "X-Gate", now, priority=4)
    fid = store.create_feed(conn, gid, "chat", "Alice", cfg_enc, now, priority=3)
    ctx = SimpleNamespace(feed_id=fid, user_id=uid)
    items, _, present = asyncio.run(
        ChatConnector().poll(ctx, db.decrypt_config(fkey, cfg_enc), None, set()))
    check("chat-connector: 3 Items aus Archiv", len(items) == 3)
    check("chat-connector: present_keys gesetzt", present is not None and len(present) == 3)

    # 6) Store: Upsert idempotent + Scoring-Kontext + Ranking
    store.set_user_profile(conn, uid, "Ausfaelle zuerst.", now)
    for it in items:
        store.upsert_item(conn, it, now)
    for it in items:
        _, is_new, _ = store.upsert_item(conn, it, now)
        check("store: upsert idempotent (%s)" % it.external_id, is_new is False)
    rows = [dict(r) for r in store.get_items_to_score(conn, now, 0)]
    check("store: get_items_to_score liefert Profil aus user_profiles",
          rows and rows[0]["user_profile"] == "Ausfaelle zuerst.")
    check("store: Prioritaeten verknuepft", rows[0]["group_prio"] == 4 and rows[0]["feed_prio"] == 3)

    p = ScoringParams()
    for r in rows:
        base, final = compute_scores(60, False, r["group_prio"], r["feed_prio"],
                                     now, r.get("ts_source"), None, p)
        store.set_score(conn, r["id"], 60, "test", False, r["group_prio"], r["feed_prio"],
                        base, final, "-", now)
    rank = store.get_ranking(conn, uid, now)
    check("store: Ranking enthaelt alle Items", len(rank) == 3)
    check("store: list_active_users aus feed_groups+user_profiles",
          [dict(x) for x in store.list_active_users(conn)]
          == [{"id": uid, "profile_text": "Ausfaelle zuerst."}])

    # 7) Reconciliation + Retention + purge_user
    removed = store.reconcile_feed(conn, fid, {items[0].dedup_key})
    check("store: reconcile entfernt fehlende (2)", removed == 2)
    schema.purge_user(conn, uid)
    check("schema: purge_user leert Gruppen",
          conn.execute("SELECT COUNT(*) FROM feed_groups WHERE user_id=?", (uid,)).fetchone()[0] == 0)
    check("schema: purge_user leert Items",
          conn.execute("SELECT COUNT(*) FROM items WHERE user_id=?", (uid,)).fetchone()[0] == 0)

    conn.close()
    print("\n%d Pruefungen bestanden -- compass-Selbsttest OK" % _passed)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("\nFEHLGESCHLAGEN: %s" % exc, file=sys.stderr)
        sys.exit(1)
