# Per-Process Network Monitor — Spec

A KDE Plasma 6 panel widget showing per-process network bandwidth usage, similar to `nethogs-qt`.

## Architecture

Two-tier: backend runner + QML plasmoid.

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
- Reverse DNS for peer addresses in the details report (live and logged), cached on disk
- Process instance history: every observed run logged with user, parent and start time
- Configurable refresh interval
- Configurable network interface
- Persisted settings: summary lines, units, colour scheme, interface, DNS, language
- Capability-based capture: `nethogs` runs as a direct child with file capabilities (no `pkexec`)

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
| `network/interface` | empty | interface to pin; empty follows the default route |
| `network/dns` | `false` | reverse DNS in the details report |
| `network/dns_ttl_minutes` | `30` | how long a positive DNS answer is reused |
| `language/locale` | empty | translation catalogue; empty means the system default |

Hiding a summary line is presentation only: the refresh path still computes and stores every
number. Units change display only; persisted usage stays binary KiB. The `summary/visible` key is
stored as one comma-separated string because INI has no list type and an absent key (all visible)
must stay distinguishable from an explicit empty selection.

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
for traffic it could not attribute, and those rows are per-flow labels, not processes.

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

### nethogs lifecycle

`nethogs` runs as a direct child of the app, never through `pkexec`. `pkexec` put the child in
its own session and exited immediately, so Stop signalled `pkexec` while a root `nethogs` kept
running and reparented to `systemd --user` — every Start added another immortal process. The
binary carries `cap_net_admin`, `cap_net_raw`, `cap_dac_read_search` and `cap_sys_ptrace`
instead; the app checks them before starting and refuses with the exact `setcap` command when
they are missing (a package upgrade replaces the binary and drops them).

## File Structure
```
network-process/
├── network-process.py          # PySide6 desktop app (primary surface)
├── README.md                   # install, run, credits
├── start-monitoring.sh         # launches nethogs for the plasmoid backend
├── install.sh                  # installs the plasmoid package
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

## Non-Goals
- No C++ compilation
- No system-monitor-dbus integration
- No libpcap integration (relies on nethogs)
