# compass

**compass** is a self-hosted information & communication hub for small ops/hosting
teams. It merges a **secure company chat** (XMPP/OMEMO archive + web client), a
**"what to do next" dashboard** (LLM-ranked items from mail, chat, calendars and
helpdesk), live **Grafana panels**, **news tickers** and **team/on-call widgets** —
one login, one database, one deployment.

compass is the successor and merger of two earlier projects:
[NextUp](https://github.com/x-gate-de/NextUp) (LLM-ranked aggregation dashboard) and
[xmpp-omemo-web-client](https://github.com/x-gate-de/xmpp-omemo-web-client)
(always-online OMEMO chat archive with web UI).

> Status: working, actively used. Daemon (XMPP/OMEMO + poll/score/decay/retention
> loops), web UI, connectors and dashboard widgets are implemented and covered by
> self-tests.

---

## Highlights

### One dashboard for everything
- **Signal field** (bento heatmap): card size and color temperature follow LLM-scored
  importance; escalations glow. List and grid views included. Boxes per row, preview
  lines, max items, density, light/dark and accent color are all configurable per browser.
- **Positionable blocks**: Grafana panels and function tiles (see below) flow into the
  item stream at configurable slots, with per-block width and frame color.
- **Soft refresh**: the item list updates in the background without page reloads —
  typed text (chat sidebar, inline replies) is never lost.

### Secure chat, fully integrated
- Always-online XMPP client as an **extra OMEMO device**: decrypts and archives 1:1
  messages server-side, so the web UI has your full history — even for messages
  received while your browser was closed.
- **Multi-user chat** (MUC): discover public rooms, join (history backfills
  automatically via MAM), leave; room messages archived in plain text.
- Web client with quote/reply, encrypted attachments (XEP-0454 aesgcm over HTTP
  upload) with lightbox and drag & drop, delivery receipts, avatars, full-text
  archive search, per-conversation web push (VAPID) and OMEMO device verification.
- **Chat sidebar on the dashboard**: collapsible conversation list with unread
  counters and a full reply-capable thread view; chat items in the stream carry
  inline reply fields.

### LLM-ranked "NextUp" stream
- Connectors: IMAP, CalDAV, Odoo helpdesk/project tickets and the built-in chat
  archive (in-process — no HTTP hop).
- Every item is scored by an LLM (Ollama by default, or any OpenAI-compatible
  endpoint), steered by a free-text importance profile; deterministic time decay and
  a comparative re-rank pass keep the top of the list meaningful.
- Items disappear automatically when handled at the source; a manual *confirm*
  button hides and un-scores an item without touching the source.

### Tickers & function tiles
- **News ticker**: pick up to three Odoo helpdesk teams; the LLM condenses each
  team's open tickets into a single running headline (interval configurable up to 12 h).
- **Staff ticker**: live data from a time-tracking API (phatman-compatible) — who is
  clocked in, hours today and this week, yearly absence stats with configurable
  color thresholds, plus on-call shifts and "out today (vacation/sick)" from a
  calendar module (SOGo/CalDAV collections or plain iCal feeds, role-assignable).
- **Function tiles**: morning briefing (daily LLM digest of top items, overnight
  chat volume, today's appointments, on-call and absences), team & planning
  (availability, upcoming absences, on-call gap warning), operations (monitoring
  room message volume with top offenders) and review (solved tickets, noise teams
  excludable, period configurable).

### Settings, not config files
A central settings page groups everything by module: appearance, scoring profile,
feeds, Grafana panels, news ticker, staff ticker, calendar mapping, dashboard tiles
and account. Runtime settings (intervals, thresholds, colors) are stored in the
database and picked up by the daemon **without restarts**. Credentials are stored
Fernet-encrypted, never in YAML.

## Architecture

- **Daemon** (`run_daemon.py`): one asyncio process — XMPP/OMEMO account manager
  plus aggregator loops (poll, score, decay, re-rank, retention, tickers, calendar,
  briefing/review). All LLM calls serialized through a single lock.
- **Web UI** (`run_web.py`): FastAPI + uvicorn, signed session cookies, binds to
  `127.0.0.1` only (reverse proxy in front).
- **Storage**: SQLite (WAL). `accounts.db` (XMPP account registry; the XMPP account
  *is* the identity — login validates against your XMPP server), `compass.db`
  (aggregator, settings, tiles) and one archive per account
  (`users/<slug>/messages.sqlite` + separate OMEMO state).
- **Security**: no secrets in the repo or logs; Fernet-encrypted credentials; strict
  CSP; TLS verification by default; API tokens stored hashed; per-user data isolation.

## Requirements

- Python ≥ 3.11 (target 3.13), Debian 12/13 or similar
- An XMPP server (with MAM/carbons/HTTP-upload for the full feature set)
- Optional: Ollama (or OpenAI-compatible endpoint), IMAP/CalDAV/Odoo/Grafana,
  a phatman-compatible time-tracking API, SOGo/CalDAV calendars
- nginx (or any reverse proxy) in front; systemd units included

## Install

See [`deploy/INSTALL.md`](deploy/INSTALL.md). Short version:

```bash
git clone https://github.com/x-gate-de/compass.git /opt/compass
cd /opt/compass
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml && chmod 600 config.yaml   # fill in secrets
./venv/bin/python scripts/selftest.py                          # offline self-test
cp deploy/compass.service deploy/compass-web.service /etc/systemd/system/
systemctl enable --now compass compass-web
```

Log in with your XMPP credentials — the first login creates the account, connects
the daemon and starts archiving.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/selftest.py` | offline self-test of the data pipeline (exit 0/1) |
| `scripts/gen_vapid.py` | generate VAPID keys for web push |
| `scripts/migrate.py` | migrate accounts/feeds from NextUp + xmpp-omemo-web-client installations |

## License

[AGPL-3.0-or-later](LICENSE) — © x-gate GmbH.
