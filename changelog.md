# Changelog

## 0.5.0 — 2026-11-17
- Add **Usage over time**: a day's sent and received traffic drawn from minute samples, with
  1 h / 3 h / whole-day ranges, an optional process filter and 1 / 5 / 15 / 60-minute granularity.
  Missing minutes are zero rather than interpolated; a day with no minute detail shows its day
  total and the date detail starts from
- Add **Connections / peers**: every stored socket with its peer, cached name, port, protocol,
  state, local endpoint, owning process, pid, user and last-seen time. The tab is explicitly
  **connection activity, not traffic** — bytes are recorded per process per day and are not
  attributed to peers, and no column is a byte count
- Add `Only running processes` to hide sockets of exited instances, and `Resolve names` to fetch
  peer names on request through the existing resolver and cache; opening the tab never resolves
- Upgrade the database to schema version 3 with additive migration and a `.bak-<stamp>` copy:
  `traffic_samples` records one row per day, minute and process name from the accounting path that
  already writes `daily_totals`, so both agree. Minute rows are pruned after 30 days and older
  days keep their day totals
- Fix a multi-second GUI freeze on a return visit to **Connections / peers**: every column now has
  a fixed, draggable width. `ResizeToContents` rescanned every row for each `setItem` on a table
  rebuilt in place, which grew quadratically with the socket count; the other timer-rebuilt
  tables use fixed widths for the same reason

## 0.4.0 — 2026-11-13
- Make both day totals agree: one `_day_totals(day)` helper feeds the `Today` line and the line
  under `History month:`, both render on every tick, and only the two tables behind them are
  throttled to 2 s. A selection that pointed at the newest day follows the midnight rollover
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
- Track the capture devices: re-read the device set every 5 s and restart `nethogs` when it
  changes, so a VPN tunnel that appears after Start is captured instead of silently reading zero
- Fold nethogs' unattributable PID-0 rows (`unknown TCP`, bare `src:dst` pairs) into one
  `(unattributed)` process instead of storing them as processes that never existed
- Wrap every user-visible string in `tr()` and add the language plumbing: Danish and German
  catalogues committed untranslated, `build-translations.sh` (pylupdate6 + lrelease6), and a
  Language tab that lists only locales with a compiled `.qm`

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
