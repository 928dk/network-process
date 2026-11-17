# Per-Process Network Monitor — Version

Current: **0.5.0** — Minute-resolution traffic history, a Usage over time day graph and a Connections / peers tab with fixed-width, draggable table columns

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 0.5.0 | 2026-11-17 | Minute samples and schema v3 with 30-day retention, the Usage over time day graph with range, process and granularity controls, the Connections / peers tab labelled connection activity, and fixed draggable widths on the timer-rebuilt tables to prevent a GUI freeze |
| 0.4.0 | 2026-11-13 | Persistent settings, units and colour schemes, reverse DNS for peers, schema v2 (`dns_cache`, `process_sockets`), capture-device tracking with an automatic `nethogs` restart, `(unattributed)` usage row, every user-visible string in `tr()` with Danish and German catalogues and `build-translations.sh` |
| 0.3.0 | 2026-11-10 | Desktop app: logged process instances, Instance history tab, direct-child nethogs with file capabilities |
| 0.2.0 | 2026-11-09 | Desktop app: per-PID details report (command line, parent chain, sockets) |
| 0.1.0 | 2026-05-27 | Initial QML-only plasmoid with nethogs backend |
