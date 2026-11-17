# Per-Process Network Monitor — Todo

## Done

- [x] Plasma 6 panel widget: sparkline plus per-process table (0.1.0)
- [x] Desktop app (`network-process.py`): live per-process rates, daily and per-process totals (0.2.0)
- [x] Process details report: command line, parent chain, sockets (0.2.0)
- [x] Process instance history: every observed run logged with user, parent and start time (0.3.0)
- [x] `pkexec`-free capture: nethogs runs as a direct child with file capabilities (0.3.0)
- [x] Persistent settings: summary lines, units, colour scheme, interface, language (0.4.0)
- [x] Reverse DNS for peer addresses in the details report, live and post-mortem (0.4.0)
- [x] `dns_cache` and `process_sockets` tables, schema version 2 (0.4.0)
- [x] Capture-device tracking: restart `nethogs` when the device set changes, so a VPN tunnel
      that comes up after Start is captured (0.4.0)
- [x] `(unattributed)` row for nethogs' PID-0 traffic, instead of inventing processes (0.4.0)
- [x] Language plumbing: every string in `tr()`, Danish and German catalogues committed
      untranslated, `build-translations.sh` builds the `.qm` the Language tab lists (0.4.0)
- [x] Minute-resolution usage history: `traffic_samples` at schema v3, written from the accounting
      path, with 30-day retention and day totals for older days (0.5.0)
- [x] Usage over time tab: range presets, process filter and 1 / 5 / 15 / 60-minute granularity (0.5.0)
- [x] Connections / peers tab over the stored sockets, labelled connection activity (0.5.0)

## Backlog

- [ ] **1.0.0** — Release-ready: polkit rules, shipped translations, KDE Store packaging
- [ ] Multiple pinned interfaces in the UI (the default already captures all devices, and the
      capture now follows device changes on its own)
- [ ] Sortable columns in process table
- [ ] Dark/light theme awareness via the Plasma theme
- [ ] Bandwidth history export (CSV)
- [ ] Per-process alert thresholds
- [ ] systemd user service for backend auto-start
