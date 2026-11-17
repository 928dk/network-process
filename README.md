# network-process

A per-process network monitor for Linux desktops. It reads `nethogs` output, shows live rates
per process, and keeps a local SQLite history of daily and per-process usage so a transfer that
already finished can still be explained.

Current release: **0.5.0** (2026-11-17).

## What it does

- **Live processes**: current sent/received rate per process, with totals for the session. The
  capture follows the devices as they come and go: when the device set changes (a VPN tunnel
  appears, a NIC is recreated) `nethogs` is restarted, so its handles cover the devices actually
  carrying traffic.
- **Usage history**: daily totals, per-process totals per day, and an **Instance history** tab
  that logs every observed run with command line, user, parent process and start time.
- **Usage over time**: a day shaped by the minute — sent and received traffic across the selected
  day, with 1 h / 3 h / whole-day presets, an optional process filter and a 1 / 5 / 15 / 60-minute
  granularity control. Days logged before minute sampling began show their day totals and a line
  naming the first sampled day instead of an empty graph.
- **Connections / peers**: the stored sockets of all logged processes in one table — peer, name,
  port, protocol, state, local endpoint, owning process, pid, user and when it was last seen.
  Names resolve on request, never on tab open. **Connection activity, not traffic: bytes are
  recorded per process per day and are not attributed to peers. No column here is a byte count.**
- **Details report**: for any PID, the full command line, the parent chain up to five levels
  (`sync-home.sh` → `rsync` → `ssh`) and the sockets carrying the traffic.
- **Peer names**: reverse DNS for the remote end of each socket, with `-> 1.2.3.4:443 (dns.google)`
  in the report. Names are cached in SQLite, so a process that has since exited still names the
  peers it talked to. Off by default; turn it on under **Settings → Network**.
- **Settings**: which summary lines to show, binary/decimal/bit units, a colour scheme, the
  interface, reverse DNS and its cache TTL, and the interface language. Stored in `QSettings` and
  applied at startup.
- **Local data**: usage and names are stored on this machine, with no telemetry. The only network
  traffic is the system resolver, and only when reverse DNS is switched on.

A Plasma 6 panel widget is included as a second surface for the same data source. See
[Plasma widget](#plasma-widget).

## Requirements

| Component | Version | Why |
|-----------|---------|-----|
| Linux | any with `/proc` and `ss` | process and socket inspection |
| Python | 3.11 or newer | the version the app declares and is tested on (3.14 here) |
| PySide6 | 6.x | Qt 6 widgets |
| nethogs | 0.8.x | the packet capture source |

On Arch and Garuda:

```bash
sudo pacman -S nethogs pyside6
```

### nethogs capabilities

`nethogs` needs packet capture rights. Do not run the app through `sudo` or `pkexec`: grant the
binary file capabilities once and it captures as your own user, which also means the app can
always stop the process it started.

```bash
sudo setcap cap_net_admin,cap_net_raw,cap_dac_read_search,cap_sys_ptrace=ep "$(command -v nethogs)"
```

A nethogs package upgrade replaces the binary and drops the capabilities. The app checks them
before every start and shows the exact command again if they are missing, so the failure is
visible instead of silent.

## Run

```bash
python3 network-process.py
```

Press **Start** to launch `nethogs -t -d 1 -c 0` as a child process. Leave **Interface** empty
to capture **all devices** (the nethogs default), or type an interface name (`wlan0`, `enp7s0`)
to pin one. `nethogs` binds its capture handles once, when it starts, so the app re-reads the
device set every five seconds and restarts the child when it changed — a VPN tunnel that comes
up after Start is picked up instead of silently reading zero.

Data is written to:

```
~/.local/share/network-process/network-process/network-usage.sqlite3
```

The database schema is additive. The first open of an older database writes a
`network-usage.sqlite3.bak-<stamp>` copy before it adds new tables.

### Schema version 3 and retention

The current schema is **version 3**. It adds `traffic_samples`, one row per day, minute and process
name with the KiB sent and received in that minute, written by the same accounting interval that
updates `daily_totals` — so a day's samples and its day total cannot drift apart. The minute rows
are **kept for 30 days** and pruned on open; older days keep their `daily_totals` and per-process
rows. There is no hourly rollup, so "Usage over time" shows day totals for a day whose minute
detail has aged out.

`Connections / peers` reads `process_sockets`, which stores the last socket *set* seen for each
logged instance, not a live flow table and not byte counters. A row says a process was talking to a
peer; it cannot say how much traffic went there. The **Seen** column shows the age of the instance
and the cell tooltip the moment the sockets were captured.

## Plasma widget

The original panel widget lives in `package/` and polls a nethogs dump instead of running the
desktop app. It is independent of `network-process.py` and useful when you want the numbers in
the panel rather than a window.

```bash
./install.sh                      # install the widget
./start-monitoring.sh [interface] # start the backend
plasmawindowed network-perprocess-monitor  # try it without installing
```

## Configuration

**Settings** (the gear button in the main window) edits the persisted preferences:

| Setting | Where | Default |
|---------|-------|---------|
| Summary lines | **Summary lines** tab — which of the six information lines show | all visible |
| Rate units | **Units** tab — binary, decimal or bits | binary |
| Total units | **Units** tab — binary or decimal | binary |
| Colour scheme | **Colours** tab — system, dark, light, high contrast | system |
| Interface | main window field, saved on OK; empty captures all devices | empty |
| Reverse DNS | **Network** tab — resolve peer addresses in the details report and on the peers tab's **Resolve names** button | off |
| DNS cache TTL | **Network** tab — minutes a positive answer is reused | 30 min |
| Language | **Language** tab — system default plus every built catalogue; applies on restart | system |

Hiding a summary line is presentation only: the refresh path still computes and stores every
number. Units change display only; stored usage stays binary KiB. A negative DNS answer is retried
after 5 minutes regardless of the TTL, and `dns_cache` rows older than seven days are pruned on
open.

### Translations

The interface is wrapped in `tr()` throughout, but no translation ships: the Danish and German
catalogues are committed untranslated on purpose.

```
translations/network-process_da.ts   # Danish scaffold, every message unfinished
translations/network-process_de.ts   # German scaffold, every message unfinished
./build-translations.sh              # pylupdate6 + lrelease6 for every .ts
./build-translations.sh da           # one locale only
```

`build-translations.sh` refreshes the source strings in each `.ts` (keeping anything already
translated) and writes `network-process_<locale>.qm` beside them. The `.qm` files are build
artefacts and are not tracked. The **Language** tab lists a locale only when its `.qm` exists, and a
chosen language takes effect the next time the app starts. Building needs `pylupdate6`
(`pyside6-tools`) and `lrelease6` (`qt6-tools`).

The plasmoid's refresh interval still lives in `package/contents/config/main.xml`, and the database
location is fixed by the application name (see above).

## Credits

This project follows the design of **[nethogs-qt](https://slist.lilotux.net/linux/nethogs-qt/index_en.html)**
by **Stéphane List**: a Qt front end that shows nethogs traffic per process, with a connection
list, hostname lookup and translated UI. The debt is conceptual. No code, assets or licence text
from that project are used here; this is a Python and PySide6 rewrite on top of `nethogs`
itself, and the per-process graph, pause/zoom and CSV export it offers are still to come.

It also stands on:

- **[nethogs](https://github.com/raboof/nethogs)** (GPL-2.0), which does the actual capture.
- **Qt 6 and PySide6** (LGPL-3.0), the UI toolkit.

## Licence

GPL-3.0-only, matching the licence declared in `package/metadata.json`.

## Documentation

| File | Content |
|------|---------|
| `spec.md` | architecture, data model, behaviour contracts |
| `changelog.md` | what changed per release |
| `version.md` | current version and history |
| `todo.md` | backlog |
