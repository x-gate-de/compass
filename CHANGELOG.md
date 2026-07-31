# Changelog

## 1.2.2

- **My Day**: each task now links directly to its source record (Odoo ticket or project
  task) when a deep-link exists. Implemented without leaking URLs to the external
  provider: each context line is numbered (#N), the LLM returns the reference per task,
  and compass resolves it to the URL server-side. Mail/appointments have no deep-links
  and stay unlinked. Anthropic timeout raised to 90s.

## 1.2.1

- **My Day**: the assigned Odoo helpdesk tickets and project tasks are now passed to
  the planner as their own blocks (no longer only the mixed top-15 ranking), where they
  used to be crowded out by many similar mail notifications. The prompt also asks for a
  balanced treatment of all blocks and to bundle recurring notifications. Raised
  max_tokens so a longer list is not truncated.

## 1.2.0

- **New feature "My Day" (Mein Tag)**: a prioritised task list for today, built from
  the already-aggregated/scored sources (top items, appointments, on-call). Own page
  in the navigation, a daily automatic run plus a *recompute* button. The
  prioritisation uses the Anthropic API (model `claude-sonnet-5`) — unlike the internal
  Ollama scoring. Data minimisation: only condensed metadata is transmitted (source,
  internally computed urgency, due date, sender, shortened title), no raw content. The
  web process never calls out; the daemon computes, the page only displays. Disabled by
  default (`myday:` block, needs `enabled` + `api_key`).

## 1.1.8

- **News ticker**: when the access check fails, the error now shows the concrete Odoo
  reason (last line of the fault, e.g. "database ... does not exist") instead of just
  "Fault" — making a wrong database name immediately obvious.

## 1.1.7

- **News ticker**: the Odoo access of the ticker teams (URL, database, login, API key)
  can now be updated in settings — for all teams at once, since they point at the same
  Odoo instance. Previously the access could only be set when adding a team, so a
  changed database name made the teams (and thus the HelpDesk ticker and the review
  tile) fail with "Fault". Leave the API key empty to keep it unchanged.

## 1.1.6

- **Odoo feeds**: the source configuration of existing Odoo feeds (URL, database,
  login, API key, TLS/open-only) is now editable in the edit form. Previously the
  source section was missing for Odoo, so e.g. a changed database name could only be
  fixed by recreating the feed. Leave the API key empty to keep it unchanged.

## 1.1.5

- **Kiosk**: new "border (TV overscan)" setting (0-5 % per side, default 3 %). Many
  TVs crop the edges of the picture, which cut off e.g. the top ticker; the safe-area
  border keeps the display inside the visible region.

## 1.1.4

- **Kiosk readability**: a new "font size (distance)" setting (100-250 %) for the
  wall display. As all sizes are rem-based, this scales the whole UI (tickers,
  cards, gauges, panels) together. Default is now 150 %; from 200 % the card body
  text/reason is hidden (title + score stay). The kiosk page is also no longer
  cached (Cache-Control: no-store) so a display never keeps showing a stale version.

## 1.1.3

- **Kiosk/Grafana**: office displays without their own Grafana access now get the
  panels as server-rendered images. compass fetches each panel via the Grafana
  image-renderer, caches it (for the display's reload interval) and serves it over
  a token-authenticated route /kiosk/<token>/panel/<id>/img. The display needs
  neither a Grafana connection nor a session; the iframe mode is dropped in kiosk.
  During a brief Grafana outage the last good image is served instead of an error
  placeholder. Requires the grafana-image-renderer plugin and a panel access token.

## 1.1.2

- **Worktime "away" band**: an employee's initials are no longer shown twice
  ("tzi tzi"). When the calendar subject is just the initials, the reason now
  reads "abwesend"; any other subject (e.g. "Homeoffice") is shown as the reason
  without the initials. Combined entries ("cde+jhe") are listed per person.

## 1.1.1

- **Calendar**: a SoGo/CalDAV collection link entered as a direct feed (a URL
  containing /dav/ and not ending in .ics) is now loaded via a CalDAV REPORT
  instead of GET. SoGo answers GET on a collection with HTTP 501, which made
  such feeds fail; plain .ics subscriptions are unchanged and still use GET.

## 1.1.0

- **NOC design**: the dark theme is now a control-room terminal look —
  blue-black ground, phosphor-green accent with a signal-cyan secondary,
  hairline rules, tight radii, a faint scanline texture and glowing escalation
  cards. Applies system-wide; the light theme stays as a calm twin.
- **Settings redesign**: the settings page is now a two-pane layout — a grouped
  module list (Appearance / Dashboard / Data sources / Output / Account) on the
  left with per-module status hints, content on the right. Replaces the long
  accordion stack; the active module is remembered across saves.
- **Kiosk mode for office displays**: a token-protected URL (`/kiosk/<token>`)
  renders the dashboard read-only — tickers, Grafana panels, function tiles and
  the NextUp list, without chat and without controls, with periodic reload. Since
  the display has no input, its appearance (theme, accent, view, boxes per row,
  max items, tickers on/off, refresh interval) is configured entirely server-side.

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
