# Per-Process Network Monitor — Spec

A per-process network monitor for Linux: a PySide6 desktop application (the primary surface) and a
KDE Plasma 6 panel widget, both showing per-process bandwidth usage in the spirit of `nethogs-qt`.

Current release: **0.5.0** (2026-11-17).

## Architecture

Two surfaces over the same nethogs output: the desktop app parses `nethogs -t` directly, and the
plasmoid polls a runner's output file.

```
nethogs -t -d 1 -c 0  →  /tmp/nethogs-raw.txt  →  QML file polling + XMLHttpRequest
```

### Backend
- Shell script `start-monitoring.sh` launches `nethogs -t` directly; the binary carries the
  capture capabilities, so no `pkexec` is involved
- Output redirected to `/tmp/nethogs-raw.txt`

### Plasmoid
- Pure QML + JavaScript (no C++ plugin)
- Timer-based polling reads the nethogs output file
- Compact representation: sparkline + total bandwidth in panel
- Full representation: bandwidth chart + process list in popup

## Features
- Real-time per-process sent/receive rates
- Sparkline chart of total bandwidth (last 60s)
- Process table with program name, PID, sent/recv rates
- Process details for any PID: full command line, parent chain and sockets
- Reverse DNS for peer addresses in the details report and the peers tab (live and logged), cached on disk
- Process instance history: every observed run logged with user, parent and start time
- Minute-resolution traffic samples for the selected day, with range, process and granularity controls
- A connections/peers view of the stored sockets, labelled connection activity rather than traffic
- Configurable refresh interval
- Configurable network interface
- Persisted settings: summary lines, units, colour scheme, interface, DNS, language
- Capability-based capture: `nethogs` runs as a direct child with file capabilities (no `pkexec`)
- Capture-device tracking: the device set is re-read every 5 s and `nethogs` is restarted when it
  changes, so a VPN tunnel that appears after Start is not silently invisible

## Desktop application (`network-process.py`)

The PySide6 desktop app is the current primary surface; the plasmoid package remains for
panel use. It runs `nethogs -t -d 1 -c 0` as a direct child through `QProcess` (no `pkexec`),
parses the tab-separated rows, and integrates the sampled rates into `network-usage.sqlite3`
(daily totals plus per-process totals, split at local midnight).

### Settings

`Settings` is the single owner of every `QSettings` read and write, so a missing, partial or
corrupt file degrades to a documented default in one place. The dialog writes them on OK; the
window reapplies them at startup and after a change.

| Key | Default | Effect |
|-----|---------|--------|
| `summary/visible` | all six lines | which information lines the main window shows |
| `units/rate` | `binary` | `binary`, `decimal` or `bits` for rates |
| `units/totals` | `binary` | `binary` or `decimal` for totals |
| `display/scheme` | `system` | `system`, `dark`, `light`, `contrast` palette |
| `network/interface` | empty | interface to pin; empty captures all devices |
| `network/dns` | `false` | reverse DNS in the details report |
| `network/dns_ttl_minutes` | `30` | how long a positive DNS answer is reused |
| `language/locale` | empty | translation catalogue; empty means the system default |

Hiding a summary line is presentation only: the refresh path still computes and stores every
number. Units change display only; persisted usage stays binary KiB. The `summary/visible` key is
stored as one comma-separated string because INI has no list type and an absent key (all visible)
must stay distinguishable from an explicit empty selection.

### Translations

Every user-visible string goes through `tr()` (widgets) or
`QCoreApplication.translate(context, …)` (module-level helpers), so `pylupdate6` sees the whole
surface. Two catalogues are committed, both untranslated on purpose:

```
translations/network-process_da.ts   # Danish scaffold
translations/network-process_de.ts   # German scaffold
build-translations.sh                # pylupdate6, then lrelease6
```

`build-translations.sh [locale…]` refreshes the `<source>` strings in each `.ts` without dropping
translations already filled in, then writes `network-process_<locale>.qm` beside them. The `.qm`
files are build artefacts and stay out of git. `available_translations()` scans that folder, so the
**Language** tab offers `System default` plus exactly the locales that have a compiled catalogue;
`install_translation()` loads the stored one at startup, which means a language change applies on
the next start rather than mid-session. With no `.qm` present the tab holds one entry and the app
behaves as it always did.

### Day totals

Both day totals come from one helper, `_day_totals(day)`, which is the only reader of a
`daily_totals` row. The `Today (…)` line reads today's row and the line under **History month:**
reads the selected day's row, and both render on every one-second tick, so for the same day they
always print the same numbers.

The two tables behind the history line (process-name totals and instance history) are re-rendered
at most every 2 s, and immediately on a day change, a month change, a tab change and Stop. The
throttle covers only the tables: a line that lags a tick is exactly the disagreement this design
exists to prevent.

The month and day selectors are rebuilt only when the set of logged days changes, which is a
different operation from rendering a day. After midnight a selection that pointed at the newest day
follows the new day; an explicit older selection stays where the user put it, and today's row is
labelled `(today)`.

### Process details

Double-clicking a row in **Live processes** (or right-click → "Process details…") opens a
read-only report for that PID, built from `ps` and `ss`:

| Section | Source | Answers |
|---------|--------|---------|
| Command | `ps -ww -o pid=,ppid=,user=,etimes=,stat=,args= -p PID` | what the process actually is |
| Parent chain | same query, walking PPIDs up to 5 levels | which job spawned it (`sync-home.sh` → `rsync` → `ssh`) |
| Sockets | one `ss -tunap` call, parsed once and grouped by PID | which remote endpoint carries the traffic, and its name |

The parent chain is the part that matters: nethogs attributes a tunnel's bytes to the socket
owner, so an `ssh` row alone says nothing about what is being copied — the parent `rsync` row
carries the source, destination and flags (`--bwlimit=20`, excludes, `--timeout`).

Degradation is deliberate: PID 0 (`unknown TCP`) has no process to inspect, PIDs owned by
another user expose no sockets to `ss -p`, and a PID that exits between refreshes reports as
gone rather than showing stale data.

### Reverse DNS

The **Network** tab holds a reverse-DNS switch (default off) and a positive-answer cache TTL
(default 30 minutes). When it is on, each peer address in the report gains its PTR name:

```
tcp  ESTAB  203.0.113.10:56021 -> 199.232.43.82:443 (cache10-fra1.steamcontent.com)
```

- **Only peers.** Loopback, unspecified, multicast, link-local and wildcard addresses are skipped;
  local and bind addresses are never looked up.
- **Never on the refresh path.** The details report paints from the cache, then hands unresolved
  addresses to a `DnsResolver` (a `QThreadPool` capped at four workers). Answers arrive through a
  queued signal; a 250 ms single-shot timer coalesces a burst into one repaint, and the reader's
  scroll position is kept.
- **Cached twice.** In memory for the session, and in the `dns_cache` table so a restart does not
  re-query. A positive answer is reused for `dns_ttl_minutes`; a negative one for at most five
  minutes. Rows older than seven days are pruned on open.
- **One thread for SQLite.** Pool threads only call `socket.gethostbyaddr` and emit; the window
  writes the answer on the GUI thread, so the connection stays single-threaded.
- **Closed dialogs forget.** The dialog filters answers to its own address set and disconnects the
  signal in `done()`, so a stale name cannot attach to a different report.

Socket parsing is line-oriented off the same `ss -tunap` output the app already ran: an
`address:port` token is split with IPv6 brackets and scope zones kept intact
(`[fd7a:...]%tailscale0:64218` → address `fd7a:...%tailscale0`, port `64218`), and a rendering
helper reproduces the previous text for every line without a name.

### Process instance log

The live table forgets a process the moment it exits, so a finished transfer can no longer be
explained. Every observed run is therefore written to `process_instances` in the same SQLite
database, keyed by `(pid, started_at)` — a recycled PID becomes a second instance instead of
overwriting the first.

| Column | Meaning |
|--------|---------|
| `pid`, `started_at` | identity of this run (start time survives PID reuse) |
| `first_seen`, `last_seen` | when the monitor first and most recently observed it |
| `process_name` | nethogs label, so it joins to `process_usage` |
| `user_name` | owner resolved from `/proc/<pid>/status` |
| `command_line` | argv joined by spaces; kernel threads fall back to `[comm]` |
| `parent_pid`, `parent_command` | one level up, so an `ssh` tunnel still names its `rsync` job |

Collection reads `/proc` directly (no fork) and is budgeted to at most 20 new instances per
one-second refresh, so a restart with many processes does not stall the GUI. `last_seen` is
refreshed at most once a minute and flushed on close; exited PIDs are dropped, and a changed
start time under the same PID is logged as a new instance. PID 0 is skipped — nethogs uses it
for traffic it could not attribute, and those rows are per-flow labels, not processes; their
bytes land in the `(unattributed)` usage row instead (see **Usage attribution**).

The schema is additive (`CREATE TABLE IF NOT EXISTS`), and the first open of an older database
writes a `network-usage.sqlite3.bak-<stamp>` copy through the SQLite backup API before adding
tables and stamping `PRAGMA user_version = 2` (version 2 adds `dns_cache` and `process_sockets`).

### Logged sockets

A logged instance is only half the story without its connections, so `process_sockets` keeps the
last known socket set per `(pid, started_at)`:

| Column | Meaning |
|--------|---------|
| `pid`, `started_at` | the instance this socket belonged to |
| `netid`, `state` | `tcp`/`udp`, `ESTAB`/`LISTEN`/… |
| `local_address`, `local_port` | this end |
| `peer_address`, `peer_port` | the remote end, port `*` for a listener |
| `peer_hostname` | cached PTR name at capture time, NULL when unknown |
| `captured_at` | when the snapshot was taken |

The set is *replaced*, not appended: each capture deletes the instance's previous rows and inserts
the current ones, so a long-running process does not grow the table without bound. Capture happens
when the instance is first logged and on the 60 s `last_seen` flush, from a single `ss -tunap`
call shared by every instance in that pass. `peer_hostname` is read from the DNS cache, never
resolved inline; names requested in the background land on the next flush. A report for an exited
process therefore lists the sockets it held and the peers it named.

**Instance history** (third tab) lists the logged instances for the day selected in the
month/day selector — rows whose start *or* last-seen day matches. Double-clicking one opens the
stored command line, user, parent, timestamps and sockets. A live details report for a PID that
has since exited falls back to its most recent stored instance instead of reporting "No running
process".

### Schema version 3 and retention

The current `user_version` is **3**. Opening an older database takes a `.bak-<stamp>` copy and adds
the new table and index additively; no pre-existing row is deleted or rewritten.

| Table | Retention | Role |
|-------|-----------|------|
| `daily_totals` | indefinite | the day's sent and received KiB |
| `process_usage` | indefinite | per day, process name and pid |
| `process_instances` | indefinite | one row per observed run |
| `process_sockets` | replaced per instance | last known socket set, not byte counters |
| `dns_cache` | positive answers 7 days | reverse-DNS answers |
| `traffic_samples` | **30 days** | day, minute, process name, sent/received KiB |

`traffic_samples` is written by `_record_interval`, the same accounting path that writes
`daily_totals`: a segment is split at the next minute boundary as well as at midnight, each slice
is accumulated into a buffer keyed by `(day, minute, process_name)`, and the buffer is flushed in
one transaction on the tick that crosses the minute, as well as on Stop, capture exit and window
close. A read of the samples flushes first, so a query never sees a partly accumulated minute. The
columns are `REAL` KiB, like `daily_totals`, so a day's sample sums reconcile with its day total
without an integer rounding step.

Rows older than 30 days are deleted on open, in the same block that prunes `dns_cache`, not on a
timer. There is no hourly rollup: a day past retention keeps its day totals, and the graph says so
rather than drawing an empty series. Files, columns and tables added here carry no new personal
data — process names, day keys and byte counts were already stored.

### Usage over time

The second tab plots the selected day from `traffic_samples`. Sent and received are two
distinguishable lines with a light fill; no per-flow or per-peer figure is plotted, because the
samples are per process name.

| Control | Behaviour |
|---------|-----------|
| Range | 1 h, 3 h or the whole day; the window is snapped to the bucket grid |
| Process | `All processes` or one process name present in the selected day's samples |
| Granularity | 1, 5, 15 or 60 minutes; changes resolution, not the range total |
| Day | shares the month/day selector with the history tabs |

Missing buckets are drawn as zero, not interpolated: a gap in capture is a gap in traffic. A single
sample is drawn as a dot. A day with no surviving samples renders its `daily_totals` figure plus a
line naming the first sampled day. The chart is a hand-painted `QWidget` (`DayUsageChart`), so no
charting dependency was added; at 1440 one-minute points it paints in roughly 14 ms on the app
host. The tab queries only while visible: a 60 s timer runs when it is current and stops
otherwise, and switching to it renders immediately.

### Connections and peers

The third tab answers "what is this machine talking to" from rows that already existed. One
`SELECT` joins `process_sockets` to `process_instances` on `(pid, started_at)` for the owning
process, and left-joins `dns_cache` for the peer name. A scope zone on an IPv6 peer address is
stripped for the join because the cache is keyed by the bare address. A socket whose instance row
is gone keeps empty owner fields rather than disappearing, and a peer with no cached name shows
its bare address.

| Column | Source | Meaning |
|--------|--------|---------|
| Peer, Port | `process_sockets` | remote end; `*` for a listener |
| Hostname | `dns_cache`, then stored `peer_hostname` | cached name, empty when unknown |
| Protocol, State | `process_sockets` | `tcp`/`udp` and `ESTAB`/`LISTEN`/… |
| Local endpoint | `process_sockets` | this end |
| Process, PID, User | `process_instances` | owner; empty for an unlogged instance |
| Seen | `process_instances.last_seen` | when the owner was last observed; the tooltip gives the socket capture time |

**Connection activity, not traffic: bytes are recorded per process per day and are not attributed to peers. No column here is a byte count.**

That sentence is the caveat in the tab itself, and the same words appear in the README. Per-peer
bandwidth is impossible from this data and is not approximated. The stored socket set is a
snapshot that capture replaces, and the **Seen** column exposes its age; no row claims to be a
live connection.

`Only running processes` filters on the instances still tracked in this session, so a row for an
exited process hides and returns when the box is cleared. `Resolve names` is the only trigger for
a lookup: opening the tab queues nothing, already cached, pending, loopback, link-local,
unspecified and listening addresses are skipped, and the existing resolver's TTL decides when a
name may be refreshed. Answers land in `dns_cache` and in the table; a burst is coalesced into one
rebuild after 400 ms. The table rides the existing 2 s day-view refresh and issues no query while
its tab is hidden. Every column has a fixed, draggable width: a full rebuild replaces items in an
already-populated table, and `ResizeToContents` would rescan all rows on each `setItem`, so a
return visit with hundreds of sockets stalled the GUI for tens of seconds. The live, name-totals
and instance-history tables are rebuilt on timers too and use fixed widths for the same reason.

### nethogs lifecycle

`nethogs` runs as a direct child of the app, never through `pkexec`. `pkexec` put the child in
its own session and exited immediately, so Stop signalled `pkexec` while a root `nethogs` kept
running and reparented to `systemd --user` — every Start added another immortal process. The
binary carries `cap_net_admin`, `cap_net_raw`, `cap_dac_read_search` and `cap_sys_ptrace`
instead; the app checks them before starting and refuses with the exact `setcap` command when
they are missing (a package upgrade replaces the binary and drops them).

#### Capture-device tracking

`nethogs` resolves the devices it captures when it starts and never follows one that appears
later. A VPN tunnel that comes up after Start is therefore invisible: the tunnel carries traffic,
the processes inside it read `0 B/s`, and nothing in the UI explains the silence. The app
therefore reads the device set from `/sys/class/net` every five seconds — the `(name, ifindex)`
pairs `nethogs` would bind if started now — and restarts the child when it differs from the set
it was started with.

- **The index is part of the identity.** A tunnel that is torn down and recreated keeps its name
  and gets a new `ifindex`; the old handle is dead although the name still matches.
- **Loopback, bridges and veth peers are excluded.** They carry container and VM traffic the
  uplink already shows, and they appear and disappear with every container, which would restart
  the capture continuously.
- **A pinned interface is compared by its own identity**, not ignored: pinning `wlan0` is still a
  dead handle if `wlan0` is recreated.
- **The restart takes the path Stop takes.** The interval is accounted with the rates still
  displayed before they are cleared, so the gap is neither lost nor counted twice, and the
  status line names the change (`Capture restarted (added proton0)`).

### Usage attribution

A flow `nethogs` cannot map to a process is reported with PID 0 under a label such as
`unknown TCP` or a bare `address:port-address:port` pair. Those rows are not processes: stored
under their raw labels they invented programs that never existed and pushed the real ones down
the history list. The accounting folds every PID-0 row into a single `(unattributed)` row
(PID `0`), so the history shows real programs while the amount stays visible next to the day
total. Bytes recorded under the old labels are left as they were written.

## File Structure
```
network-process/
├── network-process.py          # PySide6 desktop app (primary surface)
├── README.md                   # install, run, credits
├── start-monitoring.sh         # launches nethogs for the plasmoid backend
├── install.sh                  # installs the plasmoid package
├── build-translations.sh       # pylupdate6 + lrelease6 over translations/*.ts
├── translations/
│   ├── network-process_da.ts   # Danish scaffold (untranslated)
│   └── network-process_de.ts   # German scaffold (untranslated)
├── spec.md / changelog.md / version.md / todo.md
├── package/
│   ├── metadata.json
│   ├── contents/
│   │   ├── ui/
│   │   │   ├── main.qml
│   │   │   ├── CompactRepresentation.qml
│   │   │   └── FullRepresentation.qml
│   │   ├── code/
│   │   │   └── backend.js
│   │   └── config/
│   │       ├── main.xml
│   │       └── config.qml
│   └── icons/
│       └── icon.svg
```

Runtime data (not in the repo): `~/.local/share/network-process/network-process/network-usage.sqlite3`
holds the daily totals, the per-process totals, the `process_instances` log, the last socket set
per instance (`process_sockets`) and cached reverse-DNS answers (`dns_cache`). Preferences live in
`~/.config/network-process/network-process.conf` (`QSettings`).

## Credits

The design follows **[nethogs-qt](https://slist.lilotux.net/linux/nethogs-qt/index_en.html)** by
**Stéphane List**: a Qt front end that shows nethogs traffic per process, with a connection list and
reverse lookups. This project is an independent implementation and shares no code or assets with it
— the credit is an acknowledgement of the concept, and `Help → About` repeats it.

The capture itself is **[nethogs](https://github.com/raboof/nethogs)** (GPL-2.0); the UI toolkit is
**Qt 6 / PySide6** (LGPL-3.0). This project is GPL-3.0-only, as declared in
`package/metadata.json`.

## Non-Goals
- No C++ compilation
- No system-monitor-dbus integration
- No libpcap integration (relies on nethogs)
