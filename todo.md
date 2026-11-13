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

## Backlog

- [ ] **1.0.0** — Release-ready: polkit rules, shipped translations, KDE Store packaging
- [ ] Multiple interface support (auto-switch)
- [ ] Sortable columns in process table
- [ ] Dark/light theme awareness via the Plasma theme
- [ ] Bandwidth history export (CSV)
- [ ] Per-process alert thresholds
- [ ] systemd user service for backend auto-start
