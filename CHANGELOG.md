# Changelog

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
