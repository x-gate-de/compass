# Changelog

## 1.0.0 — Initial public release

First public release of compass — the merger of
[NextUp](https://github.com/x-gate-de/NextUp) and
[xmpp-omemo-web-client](https://github.com/x-gate-de/xmpp-omemo-web-client)
into one application, extended by:

- Grafana panel embedding (server-side rendered or live iframe) with per-panel
  position, width and frame color
- News ticker: LLM headlines per Odoo helpdesk team
- Staff ticker: time-tracking integration (today/week hours, absence stats with
  color thresholds, on-call shifts, "out today")
- Calendar module: SOGo/CalDAV collection discovery with per-calendar role
  assignment, plus plain iCal feeds with own credentials
- Function tiles: morning briefing (LLM), team & planning, operations
  (monitoring room analysis) and solved-ticket review
- Chat sidebar on the dashboard, inline replies on chat cards
- Central settings page; runtime-configurable intervals, thresholds and colors
  (picked up by the daemon without restarts)
