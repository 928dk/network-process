# Changelog

## 0.4.0 — 2026-11-13
- Build the **Settings** dialog that persists to `QSettings`: which summary lines show, rate and
  total units, colour scheme, interface, reverse DNS, and interface language
- Add display units — binary (KiB/MiB), decimal (kB/MB) and bits (kbit/Mbit) — without changing
  what is stored
- Add colour schemes (system, dark, light, high contrast) applied to the tables, chart and text
- Resolve peer addresses in the details report, live and after the fact, with a memory cache and a
  new `dns_cache` table; add the `network/dns` switch and `network/dns_ttl_minutes` cache TTL
- Store the last known socket set per logged instance in a new `process_sockets` table, so a
  post-mortem report still names the remote peers of an exited transfer
- Capture all sockets from a single `ss -tunap` call and parse them into structured records
  (zones, bracketed IPv6 and wildcard listeners included) instead of matching text per PID
- Upgrade the database to schema version 2, additively and with the same `.bak-<stamp>` backup

## 0.3.0 — 2026-11-10
- Log every observed process run to a new `process_instances` table: command line, user, parent command, start time, first/last seen
- Add an "Instance history" tab (day-filtered) and a post-mortem details fallback, so an exited transfer can still be explained
- Migrate the existing SQLite database additively, with a `network-usage.sqlite3.bak-<stamp>` backup on first open
- Add a `[Details]` button row under the live table, acting on the selected row
- Run `nethogs` as a direct child with file capabilities instead of `pkexec`, fixing the immortal root `nethogs` left behind on every Stop

## 0.2.0 — 2026-11-09
- Live process rows open a details report: full command line, parent chain and sockets (`ps` + `ss`)
- Parent chain walks up to 5 ancestors, so a tunnel PID (`ssh`) reveals the `rsync` job that spawned it
- Graceful degradation for PID 0 (`unknown TCP`), root-owned PIDs and PIDs that exited since the last refresh

## 0.1.0 — 2026-05-27
- Initial plasmoid scaffold with nethogs backend
- Compact panel view with total bandwidth sparkline
- Full popup view with per-process table
- File-based IPC between nethogs and QML
