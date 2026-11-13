# network-process

A per-process network monitor for Linux desktops. It reads `nethogs` output, shows live rates
per process, and keeps a local SQLite history of daily and per-process usage so a transfer that
already finished can still be explained.

## What it does

- **Live processes**: current sent/received rate per process, with totals for the session.
- **Usage history**: daily totals, per-process totals per day, and an **Instance history** tab
  that logs every observed run with command line, user, parent process and start time.
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

Press **Start** to launch `nethogs -t -d 1 -c 0` as a child process, leave **Interface** empty
to use the default route, or type an interface name (`wlan0`, `enp7s0`) to pin one.

Data is written to:

```
~/.local/share/network-process/network-process/network-usage.sqlite3
```

The database schema is additive. The first open of an older database writes a
`network-usage.sqlite3.bak-<stamp>` copy before it adds new tables.

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
| Interface | main window field, saved on OK; empty means the default route | empty |
| Reverse DNS | **Network** tab — resolve peer addresses in the details report | off |
| DNS cache TTL | **Network** tab — minutes a positive answer is reused | 30 min |
| Language | **Language** tab — only the system default until translations are built | system |

Hiding a summary line is presentation only: the refresh path still computes and stores every
number. Units change display only; stored usage stays binary KiB. A negative DNS answer is retried
after 5 minutes regardless of the TTL, and `dns_cache` rows older than seven days are pruned on
open.

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
