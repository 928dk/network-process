#!/usr/bin/env python3
"""Per-process network monitor for KDE Plasma 6 / Wayland.

Requires Python 3.11+, PySide6 6.x, and nethogs. nethogs must carry the
cap_net_admin, cap_net_raw, cap_dac_read_search and cap_sys_ptrace file
capabilities so it can capture as this user: the monitor then runs it as a
direct child and can always stop it. Usage is stored locally in SQLite and can
be viewed by day, both per PID and grouped by name.
Selecting a row opens a details report built from ps and ss: the full command
line, the parent chain, and the sockets that carry the traffic.
"""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QPointF,
    QProcess,
    QRectF,
    QRunnable,
    QSettings,
    QStandardPaths,
    QThreadPool,
    QTimer,
    QTranslator,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class ProcessRate:
    name: str
    pid: int
    sent_kib_s: float
    received_kib_s: float


@dataclass
class ProcessInstance:
    """One observed run of a process, keyed by (pid, started_at)."""

    pid: int
    started_at: str
    first_seen: str
    last_seen: str
    process_name: str
    user_name: str
    command_line: str
    parent_pid: int | None
    parent_command: str | None


def parse_nethogs_line(line: str) -> ProcessRate | None:
    """Parse one nethogs text-mode process row, ignoring headers/status lines."""
    line = line.strip()
    if not line or line.lower().startswith("refreshing:"):
        return None
    fields = [part.strip() for part in line.split("\t") if part.strip()]
    if len(fields) < 3:
        fields = re.split(r"\s+", line)
    if len(fields) < 3:
        return None
    identity = re.search(r"/(\d+)/(\d+)$", fields[0])
    if not identity:
        return None
    try:
        sent = float(fields[1].replace(",", ""))
        received = float(fields[2].replace(",", ""))
    except ValueError:
        return None
    if sent < 0 or received < 0:
        return None
    return ProcessRate(fields[0][: identity.start()] or fields[0], int(identity.group(1)), sent, received)


NETHOGS_CAPABILITIES = (
    ("cap_net_admin", 12),
    ("cap_net_raw", 13),
    ("cap_dac_read_search", 2),
    ("cap_sys_ptrace", 19),
)
VFS_CAP_FLAGS_EFFECTIVE = 0x000001
VFS_CAP_REVISION_3 = 0x03000000


def missing_nethogs_capabilities(path: str) -> list[str]:
    """Return the file capabilities nethogs still lacks, or [] when it may sniff.

    nethogs supports non-root capture when its binary carries those four
    capabilities. Package upgrades replace the binary and drop the extended
    attributes, so check before starting instead of showing an empty table.
    """
    names = [name for name, _bit in NETHOGS_CAPABILITIES]
    try:
        blob = os.getxattr(path, "security.capability")
    except OSError:
        return names
    if len(blob) < 8:
        return names
    magic = int.from_bytes(blob[0:4], "little")
    if magic & VFS_CAP_REVISION_3 == VFS_CAP_REVISION_3 and len(blob) < 12:
        return names
    if not magic & VFS_CAP_FLAGS_EFFECTIVE:
        return names
    permitted = int.from_bytes(blob[4:8], "little")
    return [name for name, bit in NETHOGS_CAPABILITIES if not permitted >> bit & 1]


APP_VERSION = "0.5.0"

# Units. The module-level defaults are set from the persisted settings once at
# startup (and again whenever the dialog changes them), so every existing call
# site keeps working without threading a setting through the refresh path.
DEFAULT_RATE_UNITS = "binary"
DEFAULT_TOTAL_UNITS = "binary"
RATE_UNIT_MODES = ("binary", "decimal", "bits")
TOTAL_UNIT_MODES = ("binary", "decimal")

# The six information lines in the main window. Hiding one is presentation
# only: the refresh path still computes and stores every number.
SUMMARY_LINES = ("status", "total", "tx", "rx", "today", "history")


def summary_line_labels() -> dict[str, str]:
    """Translatable labels for the six summary lines.

    A function rather than a constant: the labels must follow a language change
    made in the settings dialog, and ``pylupdate6`` still finds every source
    string because they are written out in full.
    """
    return {
        "status": QCoreApplication.translate("SummaryLine", "Status line"),
        "total": QCoreApplication.translate("SummaryLine", "Total rate"),
        "tx": QCoreApplication.translate("SummaryLine", "Total TX rate"),
        "rx": QCoreApplication.translate("SummaryLine", "Total RX rate"),
        "today": QCoreApplication.translate("SummaryLine", "Today's totals"),
        "history": QCoreApplication.translate(
            "SummaryLine", "Selected day total (under History month)"
        ),
    }

COLOR_SCHEMES = ("system", "dark", "light", "contrast")

# Presets only: `system` is an empty mapping, meaning "leave the desktop
# palette alone". Each other entry replaces the palette and adds the row
# alternation the tables need to read as a theme rather than a recolour.
THEMES: dict[str, dict[str, str]] = {
    "system": {},
    "dark": {
        "window": "#1e1f22",
        "text": "#e6e6e6",
        "base": "#2b2d30",
        "alternate": "#33363a",
        "highlight": "#4c8bf5",
        "highlighted_text": "#ffffff",
        "chart_line": "#5ac8fa",
        "grid": "#3c3f44",
        "tx": "#f5a623",
        "rx": "#50c878",
    },
    "light": {
        "window": "#f5f6f7",
        "text": "#1c1d1f",
        "base": "#ffffff",
        "alternate": "#eef1f4",
        "highlight": "#2f6fdd",
        "highlighted_text": "#ffffff",
        "chart_line": "#1f7ae0",
        "grid": "#c9ced6",
        "tx": "#a85b00",
        "rx": "#1f7a3d",
    },
    "contrast": {
        "window": "#000000",
        "text": "#ffffff",
        "base": "#000000",
        "alternate": "#1a1a1a",
        "highlight": "#ffff00",
        "highlighted_text": "#000000",
        "chart_line": "#ffff00",
        "grid": "#7f7f7f",
        "tx": "#ffb000",
        "rx": "#00e000",
    },
}


def set_unit_defaults(rate_units: str, total_units: str) -> None:
    """Apply the persisted unit choice to every formatter call site."""
    global DEFAULT_RATE_UNITS, DEFAULT_TOTAL_UNITS
    DEFAULT_RATE_UNITS = rate_units if rate_units in RATE_UNIT_MODES else "binary"
    DEFAULT_TOTAL_UNITS = total_units if total_units in TOTAL_UNIT_MODES else "binary"


def format_rate(kib_s: float, units: str | None = None) -> str:
    """Render a rate in binary, decimal or bit units.

    ``units`` defaults to the module-level choice so the one-second refresh
    path does not have to read settings on every tick.
    """
    units = units or DEFAULT_RATE_UNITS
    value = max(0.0, kib_s)
    if units == "bits":
        bits = value * 1024 * 8
        for divisor, suffix in ((1.0, "bit/s"), (1e3, "kbit/s"), (1e6, "Mbit/s"), (1e9, "Gbit/s")):
            scaled = bits / divisor
            if scaled < 1000 or suffix == "Gbit/s":
                precision = 0 if suffix == "bit/s" else 2
                return f"{scaled:.{precision}f} {suffix}"
    if units == "decimal":
        value *= 1024 / 1000  # KiB -> kB
        base = 1000.0
        ladder = ("kB/s", "MB/s", "GB/s", "TB/s")
    else:
        base = 1024.0
        ladder = ("KiB/s", "MiB/s", "GiB/s", "TiB/s")
    for unit in ladder:
        if value < base or unit == ladder[-1]:
            precision = 1 if unit == ladder[0] else 2
            return f"{value:.{precision}f} {unit}"
        value /= base
    return f"{value:.2f} {ladder[-1]}"  # pragma: no cover - ladder ends in the loop


def format_data(kib: float, units: str | None = None) -> str:
    """Render a stored KiB total in binary or decimal byte units."""
    units = units or DEFAULT_TOTAL_UNITS
    value = max(0.0, kib)
    if units == "decimal":
        value *= 1024 / 1000  # KiB -> kB
        base = 1000.0
        ladder = ("kB", "MB", "GB", "TB", "PB")
    else:
        base = 1024.0
        ladder = ("KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in ladder:
        if value < base or unit == ladder[-1]:
            return f"{value:.2f} {unit}"
        value /= base
    return f"{value:.2f} {ladder[-1]}"  # pragma: no cover - ladder ends in the loop


class Settings:
    """Typed access to the persisted preferences.

    One object owns every ``QSettings`` read and write, so a missing, partial
    or corrupt file degrades to the documented defaults in one place. Keys are
    namespaced and unknown keys are simply ignored.
    """

    def __init__(self, store: QSettings | None = None) -> None:
        self._store = store if store is not None else QSettings()

    # -- summary lines -----------------------------------------------------
    def visible_lines(self) -> list[str]:
        # Stored as one comma-separated string: INI has no real list type, and
        # an absent key (all visible) must stay distinguishable from an
        # explicitly empty selection (nothing visible).
        if not self._store.contains("summary/visible"):
            return list(SUMMARY_LINES)
        raw = self._store.value("summary/visible", "")
        if isinstance(raw, (list, tuple)):
            items = [str(part).strip() for part in raw]
        else:
            items = [part.strip() for part in str(raw).split(",")]
        return [name for name in SUMMARY_LINES if name in items]

    def set_visible_lines(self, names) -> None:
        chosen = [name for name in SUMMARY_LINES if name in set(names)]
        self._store.setValue("summary/visible", ",".join(chosen))
        self._flush()

    def is_line_visible(self, name: str) -> bool:
        return name in self.visible_lines()

    def set_line_visible(self, name: str, visible: bool) -> None:
        lines = set(self.visible_lines())
        if visible:
            lines.add(name)
        else:
            lines.discard(name)
        self.set_visible_lines(lines)

    # -- units -------------------------------------------------------------
    def rate_units(self) -> str:
        value = str(self._store.value("units/rate", "binary"))
        return value if value in RATE_UNIT_MODES else "binary"

    def set_rate_units(self, value: str) -> None:
        self._store.setValue("units/rate", value if value in RATE_UNIT_MODES else "binary")
        self._flush()

    def total_units(self) -> str:
        value = str(self._store.value("units/totals", "binary"))
        return value if value in TOTAL_UNIT_MODES else "binary"

    def set_total_units(self, value: str) -> None:
        self._store.setValue("units/totals", value if value in TOTAL_UNIT_MODES else "binary")
        self._flush()

    # -- appearance --------------------------------------------------------
    def scheme(self) -> str:
        value = str(self._store.value("display/scheme", "system"))
        return value if value in COLOR_SCHEMES else "system"

    def set_scheme(self, value: str) -> None:
        self._store.setValue("display/scheme", value if value in COLOR_SCHEMES else "system")
        self._flush()

    # -- network -----------------------------------------------------------
    def interface(self) -> str:
        return str(self._store.value("network/interface", "")).strip()

    def set_interface(self, value: str) -> None:
        self._store.setValue("network/interface", value.strip())
        self._flush()

    def dns_enabled(self) -> bool:
        return self._as_bool(self._store.value("network/dns", False))

    def set_dns_enabled(self, value: bool) -> None:
        self._store.setValue("network/dns", bool(value))
        self._flush()

    def dns_ttl_minutes(self) -> int:
        return self._as_int(self._store.value("network/dns_ttl_minutes", 30), 30, 1, 1440)

    def set_dns_ttl_minutes(self, value: int) -> None:
        self._store.setValue("network/dns_ttl_minutes", self._as_int(value, 30, 1, 1440))
        self._flush()

    # -- language ----------------------------------------------------------
    def locale(self) -> str:
        return str(self._store.value("language/locale", "")).strip()

    def set_locale(self, value: str) -> None:
        self._store.setValue("language/locale", value.strip())
        self._flush()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _as_int(value, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))

    def _flush(self) -> None:
        """Write now, then keep the file readable by this user only."""
        self._store.sync()
        try:
            path = self._store.fileName()
            if path and os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:
            pass


def available_translations() -> list[str]:
    """Locale codes with a built catalogue beside the script, sorted.

    Nothing ships compiled, so this is empty until ``build-translations.sh``
    produces a ``.qm``; the language combo then offers only the system default
    instead of a list of dead options.
    """
    folder = Path(__file__).resolve().parent / "translations"
    if not folder.is_dir():
        return []
    return sorted(path.stem.rsplit("_", 1)[-1] for path in folder.glob("network-process_*.qm"))


def apply_color_scheme(app: QApplication, scheme: str, base_palette: QPalette) -> dict[str, str]:
    """Apply a preset palette and return its colours for the chart and labels."""
    theme = THEMES.get(scheme) or {}
    if not theme:
        app.setPalette(base_palette)
        app.setStyleSheet("")
        return {}
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme["base"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme["alternate"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme["base"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme["highlight"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme["highlighted_text"]))
    app.setPalette(palette)
    app.setStyleSheet(
        f"QTableWidget {{ alternate-background-color: {theme['alternate']}; }}"
        f"QHeaderView::section {{ background-color: {theme['base']}; color: {theme['text']}; }}"
    )
    return theme


PS_FIELDS = "pid=,ppid=,user=,etimes=,stat=,args="
PS_FIELD_NAMES = ("pid", "ppid", "user", "elapsed", "state", "args")
MAX_ANCESTORS = 5
ANCESTOR_ARGS_LIMIT = 200

SCHEMA_VERSION = 3
INSTANCE_FIELDS = (
    "pid",
    "started_at",
    "first_seen",
    "last_seen",
    "process_name",
    "user_name",
    "command_line",
    "parent_pid",
    "parent_command",
)
MAX_NEW_INSTANCES_PER_REFRESH = 20
LAST_SEEN_FLUSH_SECONDS = 60

# Reverse DNS. Lookups run on a small pool, always off the one-second refresh
# path, and answers are cached twice: in memory for this run, and in SQLite so a
# restart does not re-query addresses that were resolved minutes ago.
MAX_DNS_WORKERS = 4
NEGATIVE_DNS_TTL_SECONDS = 300
DNS_HOSTNAME_LIMIT = 60
DNS_CACHE_RETENTION_DAYS = 7
def dns_disabled_hint() -> str:
    """The line a details report carries while reverse DNS is switched off."""
    return QCoreApplication.translate(
        "ProcessReport", "DNS resolution is off. Turn it on under Settings > Network."
    )

# The selected day's view (totals line, name totals, instance history) is
# rebuilt on this cadence. The live table keeps its one-second refresh; this
# only governs the history panes, which change slowly and cost a table rebuild.
DAY_VIEW_REFRESH_SECONDS = 2.0

# The usage graph reads an aggregate over the visible window, so it is refreshed
# on a slow cadence and only while its tab is the visible one. The one-second
# tick must not pay for a query whose answer changes at most once a minute, and
# a hidden tab must issue no query at all.
USAGE_REFRESH_SECONDS = 60
USAGE_RANGE_HOURS = (1, 3, 6)
USAGE_GRANULARITIES = (1, 5, 15, 60)
MINUTES_PER_DAY = 1440

# Column order of one peers-tab row, in the order `_peers_query` selects them.
PEER_FIELDS = (
    "peer_address",
    "peer_port",
    "netid",
    "state",
    "local_address",
    "local_port",
    "hostname",
    "process_name",
    "pid",
    "user_name",
    "started_at",
    "last_seen",
    "captured_at",
)

# Resolver answers arrive one signal per address. A burst of them would rebuild
# the peers table once per answer, so the rebuild waits for the burst to quiet.
PEER_NAME_REFRESH_MS = 400

# nethogs binds its capture handles once, when it starts. A device created
# afterwards (a VPN tunnel that comes up a minute later) is never seen, and
# every byte inside it is invisible: the processes look idle and nothing in the
# log says why. The monitor therefore watches the capture set and restarts the
# child when it changes.
INTERFACE_SCAN_SECONDS = 5

# nethogs cannot always map a flow to a process. It reports those as "unknown
# TCP" or as a bare "address:port-address:port" pair, both with pid 0. Storing
# them under their raw names invented processes that never existed, so they are
# accumulated into one labelled row instead.
UNATTRIBUTED_PROCESS = "(unattributed)"

# Minute-resolution samples. `daily_totals` and `process_usage` are per-day
# aggregates with no intra-day column, and the live chart keeps its points in a
# deque that dies with the window, so nothing on disk could draw a day.
# A sample is a slice of the same interval the day total is computed from, taken
# in `_record_interval` rather than by a second sampler, so the two cannot drift.
# The buffer is flushed at most once a minute: a write in the one-second path
# would buy resolution the tick does not have.
SAMPLE_RETENTION_DAYS = 30
SAMPLE_FLUSH_SECONDS = 60

try:
    SC_CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):  # pragma: no cover - non-Linux fallback
    SC_CLK_TCK = 100
_boot_time_cache: float | None = None


def _run_helper(command: list[str]) -> str:
    """Run a short-lived read-only helper command, returning stdout or ''."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _ps_process(pid: int) -> dict[str, str] | None:
    """Return ps fields for one PID, or None when it is not running."""
    line = _run_helper(["ps", "-ww", "-o", PS_FIELDS, "-p", str(pid)]).strip()
    if not line:
        return None
    parts = line.split(None, len(PS_FIELD_NAMES) - 1)
    if len(parts) != len(PS_FIELD_NAMES):
        return None
    return dict(zip(PS_FIELD_NAMES, parts))


def _elapsed_seconds(info: dict[str, str]) -> int:
    try:
        return int(info["elapsed"])
    except (KeyError, ValueError):
        return 0


def _format_elapsed(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _started_at(elapsed_seconds: int) -> str:
    started = datetime.now() - timedelta(seconds=max(0, elapsed_seconds))
    return started.strftime("%Y-%m-%d %H:%M:%S")


def _parent_chain(pid: int) -> list[dict[str, str]]:
    """Walk PPIDs upwards so a tunnel process reveals the command that spawned it."""
    ancestors: list[dict[str, str]] = []
    seen = {pid}
    current = _ps_process(pid)
    while current is not None and len(ancestors) < MAX_ANCESTORS:
        try:
            ppid = int(current["ppid"])
        except (KeyError, ValueError):
            break
        if ppid <= 1 or ppid in seen:
            break
        seen.add(ppid)
        parent = _ps_process(ppid)
        if parent is None:
            break
        ancestors.append(parent)
        current = parent
    return ancestors


@dataclass
class SocketState:
    """One socket, parsed from a single ss line."""

    netid: str
    state: str
    local_address: str
    local_port: str
    peer_address: str
    peer_port: str

    @property
    def listening(self) -> bool:
        return self.peer_port in ("", "*")


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    """Split an ss ``address:port`` token, keeping IPv6 brackets and scope zones intact."""
    if endpoint.startswith("["):
        closing = endpoint.find("]")
        if closing != -1:
            address = endpoint[1:closing]
            rest = endpoint[closing + 1:]
            if rest.startswith("%"):
                zone, _, port = rest.partition(":")
                return f"{address}{zone}", port or "*"
            return address, rest.lstrip(":") or "*"
    address, _, port = endpoint.rpartition(":")
    if not address:
        return endpoint, "*"
    return address, port or "*"


def _format_endpoint(address: str, port: str) -> str:
    """Render an endpoint the way ss does, bracketing IPv6 addresses."""
    if ":" in address:
        return f"[{address}]:{port}"
    return f"{address}:{port}"


def _bare_address(address: str) -> str:
    """Drop a scope suffix so the address can be handed to the resolver."""
    return address.split("%", 1)[0]


def read_socket_table() -> dict[int, list[SocketState]]:
    """Group every socket ``ss`` can attribute by PID, using one ss invocation."""
    table: dict[int, list[SocketState]] = {}
    seen: set[tuple[int, str, str, str, str, str]] = set()
    for line in _run_helper(["ss", "-tunap"]).splitlines():
        pids = {int(value) for value in re.findall(r"pid=(\d+)", line)}
        if not pids:
            continue
        fields = line.split()
        if len(fields) < 6:
            continue
        local_address, local_port = _split_endpoint(fields[4])
        peer_address, peer_port = _split_endpoint(fields[5])
        socket = SocketState(
            fields[0], fields[1], local_address, local_port, peer_address, peer_port
        )
        for pid in pids:
            key = (
                pid,
                socket.netid,
                socket.local_address,
                socket.local_port,
                socket.peer_address,
                socket.peer_port,
            )
            if key in seen:
                continue
            seen.add(key)
            table.setdefault(pid, []).append(socket)
    return table


def read_process_sockets(pid: int) -> list[SocketState]:
    """Return the sockets ss attributes to this PID."""
    return read_socket_table().get(pid, [])


def capture_interfaces(root: str = "/sys/class/net") -> tuple[tuple[str, int], ...]:
    """Return the (name, ifindex) pairs nethogs would bind to if started now.

    ``ifindex`` is part of the identity on purpose: a VPN interface that is torn
    down and recreated keeps its name but gets a new index, and the old capture
    handle is dead even though the name still matches.

    Loopback, bridges and veth peers are left out. They carry container and VM
    traffic that the uplink shows as well, and they appear and disappear with
    every container, so watching them would restart the capture constantly.
    """
    devices: list[tuple[str, int]] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return ()
    for entry in entries:
        name = entry.name
        if name == "lo":
            continue
        base = os.path.join(root, name)
        if os.path.exists(os.path.join(base, "bridge")):
            continue
        try:
            ifindex = int(Path(base, "ifindex").read_text(encoding="ascii").strip())
            iflink = int(Path(base, "iflink").read_text(encoding="ascii").strip())
            state = Path(base, "operstate").read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            continue
        if iflink != ifindex or state == "down":
            continue
        devices.append((name, ifindex))
    return tuple(sorted(devices))


def _short_hostname(hostname: str) -> str:
    """Trim a PTR name so one long Akamai-style name cannot wrap the report."""
    name = hostname.rstrip(".")
    if len(name) <= DNS_HOSTNAME_LIMIT:
        return name
    return name[: DNS_HOSTNAME_LIMIT - 1] + "…"


def _render_socket(socket: SocketState, hostname: str | None = None) -> str:
    """Render one socket line, naming the peer when a name is known."""
    local = _format_endpoint(socket.local_address, socket.local_port)
    if socket.listening:
        listening = QCoreApplication.translate("ProcessReport", "(listening)")
        return f"{socket.netid:<4} {socket.state:<10} {local} {listening}"
    peer = _format_endpoint(socket.peer_address, socket.peer_port)
    if hostname:
        peer = f"{peer} ({_short_hostname(hostname)})"
    return f"{socket.netid:<4} {socket.state:<10} {local} -> {peer}"


def _needs_resolution(address: str) -> bool:
    """True when an address is a real peer worth a reverse lookup."""
    bare = _bare_address(address)
    if not bare:
        return False
    try:
        parsed = ipaddress.ip_address(bare)
    except ValueError:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_unspecified
        or parsed.is_multicast
        or parsed.is_link_local
    )


def reverse_lookup(address: str) -> str | None:
    """Return the PTR name for an address, or None when it has none."""
    try:
        name = socket.gethostbyaddr(address)[0].rstrip(".")
    except OSError:
        return None
    return name or None


class DnsSignals(QObject):
    """Signal carrier for a QRunnable, which cannot own signals itself."""

    resolved = Signal(str, str)


class DnsTask(QRunnable):
    """Resolve one address in a pool thread; never touches SQLite or widgets."""

    def __init__(self, address: str, signals: DnsSignals) -> None:
        super().__init__()
        self.address = address
        self.signals = signals

    def run(self) -> None:  # noqa: D401 - QRunnable contract
        self.signals.resolved.emit(self.address, reverse_lookup(self.address) or "")


class DnsResolver(QObject):
    """Resolve peer addresses off the GUI thread, with memory and SQLite caching.

    Only the GUI thread reads or writes the caches. Pool threads resolve and
    emit; the queued signal brings the answer back to this object, and the
    window persists it, so the SQLite connection stays single-threaded.
    """

    resolved = Signal(str, str)  # address, hostname (empty when there is no name)

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._pending: set[str] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_DNS_WORKERS)
        self._signals = DnsSignals(self)
        self._signals.resolved.connect(self._on_resolved)

    @property
    def enabled(self) -> bool:
        return self._settings.dns_enabled()

    def load(self, rows: Iterable[tuple[str, str | None, str]]) -> None:
        """Seed the memory cache from stored (address, hostname, resolved_at) rows."""
        ttl = self._settings.dns_ttl_minutes() * 60
        now = monotonic()
        for address, hostname, resolved_at in rows:
            try:
                age = (datetime.now() - datetime.fromisoformat(resolved_at)).total_seconds()
            except ValueError:
                continue
            lifetime = min(ttl, NEGATIVE_DNS_TTL_SECONDS) if hostname is None else ttl
            remaining = lifetime - age
            if remaining > 0:
                self._cache[address] = (hostname, now + remaining)

    def lookup(self, address: str) -> str | None:
        """Return a cached name for painting; never starts a lookup."""
        entry = self._cache.get(_bare_address(address))
        if entry is None:
            return None
        hostname, expires_at = entry
        if expires_at < monotonic():
            return None
        return hostname

    def request(self, sockets: Iterable[SocketState]) -> int:
        """Queue a lookup for every unresolved peer among these sockets.

        Returns how many lookups were actually started, so a caller can say
        something true about what pressing a button did: an address that is
        already cached, already pending, loopback or link-local is skipped.
        """
        if not self.enabled:
            return 0
        queued = 0
        now = monotonic()
        for socket in sockets:
            address = _bare_address(socket.peer_address)
            if not _needs_resolution(address) or address in self._pending:
                continue
            entry = self._cache.get(address)
            if entry is not None and entry[1] >= now:
                continue
            self._pending.add(address)
            self._pool.start(DnsTask(address, self._signals))
            queued += 1
        return queued

    def shutdown(self) -> None:
        """Drop queued work; lookups already running finish on their own."""
        self._pool.clear()

    def _on_resolved(self, address: str, hostname: str) -> None:
        self._pending.discard(address)
        name = hostname or None
        ttl = self._settings.dns_ttl_minutes() * 60
        if name is None:
            ttl = min(ttl, NEGATIVE_DNS_TTL_SECONDS)
        self._cache[address] = (name, monotonic() + ttl)
        self.resolved.emit(address, hostname)


def _boot_time() -> float | None:
    """Return the kernel boot epoch from /proc/stat, cached for the session."""
    global _boot_time_cache
    if _boot_time_cache is None:
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    _boot_time_cache = float(line.split()[1])
                    break
        except OSError:
            return None
    return _boot_time_cache


def _read_proc_stat(pid: int) -> tuple[str, int, int] | None:
    """Return (comm, ppid, starttime_ticks) from /proc/<pid>/stat, or None."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = data.rfind(")")
    open_ = data.find("(")
    if open_ < 0 or close < open_:
        return None
    comm = data[open_ + 1 : close]
    rest = data[close + 1 :].split()
    # rest[0] is state (field 3), rest[1] is ppid (field 4), rest[19] is starttime (field 22).
    if len(rest) < 20:
        return None
    try:
        return comm, int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def _read_proc_cmdline(pid: int) -> str:
    """Return the NUL-separated argv joined by spaces, or '' when unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _read_proc_user(pid: int) -> str:
    """Resolve the owner of a PID from /proc/<pid>/status, falling back to the uid."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                try:
                    return pwd.getpwuid(uid).pw_name
                except KeyError:
                    return str(uid)
    except (OSError, ValueError, IndexError):
        pass
    return "unknown"


def _format_start_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def read_process_instance(pid: int, name: str = "") -> ProcessInstance | None:
    """Collect a process instance from /proc without forking; None if it is gone."""
    stat = _read_proc_stat(pid)
    if stat is None:
        return None
    comm, ppid, starttime = stat
    boot = _boot_time()
    started_at = _format_start_epoch(boot + starttime / SC_CLK_TCK) if boot is not None else ""
    command = _read_proc_cmdline(pid) or f"[{comm}]"
    parent_pid = ppid if ppid > 0 else None
    parent_command: str | None = None
    if parent_pid is not None:
        parent_command = _read_proc_cmdline(parent_pid) or None
        if parent_command is None:
            parent_stat = _read_proc_stat(parent_pid)
            if parent_stat is not None:
                parent_command = f"[{parent_stat[0]}]"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return ProcessInstance(
        pid=pid,
        started_at=started_at,
        first_seen=now,
        last_seen=now,
        process_name=name or (command.split()[0] if command else comm),
        user_name=_read_proc_user(pid),
        command_line=command,
        parent_pid=parent_pid,
        parent_command=parent_command,
    )


def describe_logged_instance(record: dict) -> str:
    """Render a stored process_instances row as a copyable post-mortem report."""
    pid = record.get("pid")
    name = record.get("process_name") or "unknown"
    parent = record.get("parent_command") or ""
    parent_pid = record.get("parent_pid")
    unknown = QCoreApplication.translate("ProcessReport", "unknown")
    parent_line = (
        f"{parent_pid}  {parent}".strip()
        if parent_pid
        else QCoreApplication.translate("ProcessReport", "(none recorded)")
    )
    lines = [
        QCoreApplication.translate("ProcessReport", "PID {pid} — {name} (logged instance)").format(
            pid=pid, name=name
        ),
        "",
        QCoreApplication.translate(
            "ProcessReport",
            "This process is no longer running. The record below was captured while it was active.",
        ),
        "",
        QCoreApplication.translate("ProcessReport", "LOGGED INSTANCE"),
        QCoreApplication.translate("ProcessReport", "  STARTED     {value}").format(
            value=record.get("started_at") or unknown
        ),
        QCoreApplication.translate("ProcessReport", "  FIRST SEEN  {value}").format(
            value=record.get("first_seen") or unknown
        ),
        QCoreApplication.translate("ProcessReport", "  LAST SEEN   {value}").format(
            value=record.get("last_seen") or unknown
        ),
        QCoreApplication.translate("ProcessReport", "  USER        {value}").format(
            value=record.get("user_name") or unknown
        ),
        QCoreApplication.translate("ProcessReport", "  PARENT      {value}").format(
            value=parent_line
        ),
        "",
        QCoreApplication.translate("ProcessReport", "COMMAND LINE"),
        f"  {record.get('command_line') or QCoreApplication.translate('ProcessReport', '(not recorded)')}",
    ]
    sockets = record.get("sockets") or []
    lines.extend(
        [
            "",
            QCoreApplication.translate(
                "ProcessReport", "SOCKETS (captured while it was running)"
            ),
        ]
    )
    if sockets:
        for socket in sockets:
            lines.append("  " + _render_socket(_socket_from_row(socket), socket.get("peer_hostname")))
    else:
        lines.append(QCoreApplication.translate("ProcessReport", "  none recorded"))
    return "\n".join(lines)


def _socket_from_row(row: dict) -> SocketState:
    """Rebuild a SocketState from a stored process_sockets row."""
    return SocketState(
        row["netid"],
        row["state"],
        row["local_address"],
        row["local_port"],
        row["peer_address"],
        row["peer_port"],
    )


@dataclass
class ProcessReport:
    """A rendered details report plus the sockets it was built from."""

    text: str
    sockets: list[SocketState]
    # False when the PID could not be described at all, so the caller can fall
    # back to a stored instance without matching on translated report text.
    found: bool = True


def build_process_report(pid: int, hostnames=None) -> ProcessReport:
    """Collect and render the details report for one PID.

    ``hostnames`` is a cache-only lookup (address to name) so rendering never
    waits on the resolver; unresolved peers print as a bare address.
    """
    if pid <= 0:
        return ProcessReport(
            QCoreApplication.translate(
                "ProcessReport",
                "PID 0 is not a real process. nethogs reports traffic it could not attribute "
                "to a process (for example packets that belong to no socket) under PID 0.",
            ),
            [],
            False,
        )
    info = _ps_process(pid)
    if info is None:
        return ProcessReport(
            QCoreApplication.translate(
                "ProcessReport",
                "No running process with PID {pid}. It may have exited since the last "
                "table refresh.",
            ).format(pid=pid),
            [],
            False,
        )
    elapsed = _elapsed_seconds(info)
    program = info["args"].split()[0] if info["args"] else QCoreApplication.translate(
        "ProcessReport", "unknown"
    )
    lines = [
        QCoreApplication.translate("ProcessReport", "PID {pid} — {program}").format(
            pid=pid, program=program
        ),
        "",
        f"ps -ww -o {PS_FIELDS} -p {pid}",
        "",
        "  PID    PPID   USER       ELAPSED     STARTED              STAT  COMMAND",
        f"  {info['pid']:<6} {info['ppid']:<6} {info['user']:<10} "
        f"{_format_elapsed(elapsed):<11} {_started_at(elapsed):<20} "
        f"{info['state']:<5} {info['args']}",
        "",
        QCoreApplication.translate("ProcessReport", "PARENT CHAIN"),
    ]
    ancestors = _parent_chain(pid)
    if not ancestors:
        lines.append(
            QCoreApplication.translate("ProcessReport", "  (no parent process above PID 1)")
        )
    for ancestor in ancestors:
        args = ancestor["args"]
        if len(args) > ANCESTOR_ARGS_LIMIT:
            args = args[:ANCESTOR_ARGS_LIMIT] + "…"
        lines.append(
            f"  {ancestor['pid']:<7} {ancestor['user']:<10} "
            f"{_format_elapsed(_elapsed_seconds(ancestor)):<11} {args}"
        )
    lines.extend(
        ["", QCoreApplication.translate("ProcessReport", "SOCKETS (ss -tunap, filtered on this PID)")]
    )
    sockets = read_process_sockets(pid)
    if sockets:
        for socket in sockets:
            hostname = None
            if hostnames is not None:
                hostname = hostnames(_bare_address(socket.peer_address))
            lines.append(f"  {_render_socket(socket, hostname)}")
    else:
        lines.append(
            QCoreApplication.translate(
                "ProcessReport",
                "  none reported (no open sockets, or the process belongs to another user)",
            )
        )
    if hostnames is None:
        lines.extend(["", dns_disabled_hint()])
    return ProcessReport("\n".join(lines), sockets)


class NumericTableWidgetItem(QTableWidgetItem):
    """A table cell that sorts numerically using its UserRole value."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            return float(left) < float(right)
        return super().__lt__(other)


class HistoryChart(QWidget):
    """Small live chart of aggregate sent + received bandwidth."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setMaximumHeight(120)
        self._samples: deque[float] = deque(maxlen=60)
        self._theme: dict[str, str] = {}

    def set_sample(self, value: float) -> None:
        self._samples.append(max(0.0, value))
        self.update()

    def apply_theme(self, theme: dict[str, str]) -> None:
        """Store the active preset's colours; empty means "use the palette"."""
        self._theme = dict(theme or {})
        self.update()

    def _color(self, key: str, role: QPalette.ColorRole) -> QColor:
        value = self._theme.get(key)
        return QColor(value) if value else self.palette().color(role)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(8, 8, -8, -8)
        painter.setPen(self._color("grid", QPalette.ColorRole.Mid))
        painter.drawRoundedRect(area, 5, 5)
        if len(self._samples) < 2:
            painter.setPen(self._color("text", QPalette.ColorRole.Text))
            painter.drawText(
                area,
                Qt.AlignmentFlag.AlignCenter,
                self.tr("Bandwidth history (last 60 samples)"),
            )
            return
        maximum = max(max(self._samples), 0.1)
        points = []
        for index, value in enumerate(self._samples):
            x = area.left() + index * area.width() / (len(self._samples) - 1)
            y = area.bottom() - value / maximum * (area.height() - 12) - 6
            points.append((x, y))
        painter.setPen(QPen(self._color("chart_line", QPalette.ColorRole.Highlight), 2))
        for start, end in zip(points, points[1:]):
            painter.drawLine(int(start[0]), int(start[1]), int(end[0]), int(end[1]))
        painter.setPen(self._color("text", QPalette.ColorRole.Text))
        painter.drawText(
            area.adjusted(6, 4, -6, -4),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            self.tr("Peak: {}").format(format_rate(maximum)),
        )


class DayUsageChart(QWidget):
    """Hand-painted chart of one day's sent and received KiB per bucket.

    Samples are keyed by minute of the day, so x is time and not the index of a
    point: a quiet stretch is drawn on the baseline instead of being compressed
    away. The rows arrive already aggregated to the chosen granularity, and are
    expanded back onto that grid so a missing bucket is a zero rather than an
    interpolated line between two distant points.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self._sent: list[tuple[int, float]] = []
        self._received: list[tuple[int, float]] = []
        self._day = ""
        self._span = (0, MINUTES_PER_DAY)
        self._granularity = 1
        self._theme: dict[str, str] = {}

    def set_series(
        self,
        sent: list[tuple[int, float]],
        received: list[tuple[int, float]],
        day: str,
        span: tuple[int, int],
        granularity: int,
    ) -> None:
        """Store the buckets and repaint.

        ``sent``/``received`` are ``(minute of day, KiB)`` pairs for buckets
        that carried traffic, ``span`` is the visible minute window and
        ``granularity`` the bucket width those minutes were aggregated at.
        """
        self._sent = list(sent)
        self._received = list(received)
        self._day = day
        self._granularity = max(1, int(granularity))
        start, end = int(span[0]), int(span[1])
        start -= start % self._granularity
        self._span = (start, max(end, start + self._granularity))
        self.update()

    def apply_theme(self, theme: dict[str, str]) -> None:
        """Store the active preset's colours; empty means "use the palette"."""
        self._theme = dict(theme or {})
        self.update()

    def _color(self, key: str, role: QPalette.ColorRole) -> QColor:
        value = self._theme.get(key)
        return QColor(value) if value else self.palette().color(role)

    def _dense(self, points: list[tuple[int, float]]) -> list[tuple[int, float]]:
        """One entry per bucket in the span, zero where no sample exists."""
        lookup = dict(points)
        start, end = self._span
        step = self._granularity
        buckets = []
        minute = start
        while minute < end:
            buckets.append((minute, lookup.get(minute, 0.0)))
            minute += step
        return buckets

    def _x(self, minute: int, plot: QRectF) -> float:
        start, end = self._span
        return plot.left() + (minute - start) / max(1, end - start) * plot.width()

    def _y(self, value: float, peak: float, plot: QRectF) -> float:
        return plot.bottom() - min(value, peak) / peak * plot.height()

    def _points(self, series: list[tuple[int, float]], peak: float, plot: QRectF) -> list[QPointF]:
        return [QPointF(self._x(minute, plot), self._y(value, peak, plot)) for minute, value in series]

    def _paint_axes(self, painter: QPainter, area: QRect, plot: QRectF, peak: float) -> None:
        grid_pen = QPen(self._color("grid", QPalette.ColorRole.Mid), 1)
        muted = self._color("text", QPalette.ColorRole.Mid)
        metrics = painter.fontMetrics()
        for fraction in (0.0, 0.5, 1.0):
            y = plot.bottom() - fraction * plot.height()
            painter.setPen(grid_pen)
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(muted)
            painter.drawText(
                QRectF(area.left() + 4, y - 9, 60, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                format_data(peak * fraction),
            )
        start, end = self._span
        span_minutes = end - start
        step = 60 if span_minutes <= 360 else 180 if span_minutes <= 720 else 360
        minute = start - start % step
        while minute <= end:
            if minute >= start:
                x = self._x(minute, plot)
                painter.setPen(grid_pen)
                painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
                painter.setPen(muted)
                painter.drawText(
                    QRectF(x - 24, plot.bottom() + 4, 48, 16),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{minute // 60 % 24:02d}:00",
                )
            minute += step
        x = plot.left()
        y = plot.top() - 14
        for key, role, label in (
            ("tx", QPalette.ColorRole.Highlight, self.tr("Sent")),
            ("rx", QPalette.ColorRole.Text, self.tr("Received")),
        ):
            painter.setPen(QPen(self._color(key, role), 2))
            painter.drawLine(int(x), int(y), int(x) + 16, int(y))
            painter.setPen(self._color("text", QPalette.ColorRole.Text))
            painter.drawText(
                QRectF(x + 22, y - 9, metrics.horizontalAdvance(label) + 8, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            x += 22 + metrics.horizontalAdvance(label) + 16
        if self._day:
            painter.setPen(muted)
            painter.drawText(
                QRectF(plot.right() - 180, y - 9, 180, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self._day,
            )

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(8, 8, -8, -8)
        painter.setPen(self._color("grid", QPalette.ColorRole.Mid))
        painter.drawRoundedRect(area, 5, 5)
        plot = QRectF(area.left() + 68, area.top() + 26, area.width() - 76, area.height() - 50)
        if plot.width() < 40 or plot.height() < 30:
            return
        sent = self._dense(self._sent)
        received = self._dense(self._received)
        peak = max([value for _minute, value in sent + received] + [0.0])
        self._paint_axes(painter, area, plot, peak)
        if peak <= 0:
            painter.setPen(self._color("text", QPalette.ColorRole.Text))
            painter.drawText(
                plot, Qt.AlignmentFlag.AlignCenter, self.tr("No traffic in this range.")
            )
            return
        for series, source, key, role in (
            (sent, self._sent, "tx", QPalette.ColorRole.Highlight),
            (received, self._received, "rx", QPalette.ColorRole.Text),
        ):
            colour = self._color(key, role)
            if len(source) < 2:
                # One bucket has no line to draw. A dot at its own point is the
                # honest mark, and it keeps a one-point series from being
                # stretched across the full width of the chart.
                painter.setPen(QPen(colour, 2))
                painter.setBrush(colour)
                for minute, value in source:
                    painter.drawEllipse(
                        QPointF(self._x(minute, plot), self._y(value, peak, plot)), 3.0, 3.0
                    )
                painter.setBrush(Qt.BrushStyle.NoBrush)
                continue
            points = self._points(series, peak, plot)
            fill = QPainterPath()
            fill.moveTo(points[0].x(), plot.bottom())
            for point in points:
                fill.lineTo(point)
            fill.lineTo(points[-1].x(), plot.bottom())
            fill.closeSubpath()
            shade = QColor(colour)
            shade.setAlpha(48)
            # The fill's top edge is covered by the line drawn over it, so its
            # own edge smoothing buys nothing and costs raster time.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shade)
            painter.drawPath(fill)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # A day at one-minute granularity is denser than the pixels it
            # lands on, so smoothing changes nothing visible but dominates the
            # paint time; the coarser views keep it because their corners show.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, len(points) <= 720)
            painter.setPen(QPen(colour, 2))
            painter.drawPolyline(points)


class ProcessDetailsDialog(QDialog):
    """Read-only report for one PID: command line, parent chain and sockets.

    When ``logged`` is given the report is rendered from the stored instance
    snapshot. Otherwise the live ``ps``/``ss`` report is used, ``lookup`` is
    consulted when the process has already exited, and ``resolver`` names the
    peers, filling them in after the first paint.
    """

    def __init__(
        self,
        pid: int,
        name: str = "",
        parent: QWidget | None = None,
        logged: dict | None = None,
        lookup=None,
        resolver: DnsResolver | None = None,
    ) -> None:
        super().__init__(parent)
        self._pid = pid
        self._logged = logged
        self._lookup = lookup
        self._resolver = resolver
        self._name_timer = QTimer(self)
        self._name_timer.setSingleShot(True)
        self._name_timer.setInterval(250)
        self._name_timer.timeout.connect(self.reload)
        if resolver is not None:
            resolver.resolved.connect(self._on_hostname)
        label = (
            self.tr("{} (PID {})").format(name, pid) if name else self.tr("PID {}").format(pid)
        )
        self.setWindowTitle(self.tr("Process details — {}").format(label))
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.report.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.report, 1)
        buttons = QHBoxLayout()
        copy_button = QPushButton(self.tr("Copy"))
        copy_button.clicked.connect(self._copy_report)
        buttons.addWidget(copy_button)
        reload_button = QPushButton(self.tr("Refresh"))
        reload_button.clicked.connect(self.reload)
        buttons.addWidget(reload_button)
        buttons.addStretch(1)
        close_button = QPushButton(self.tr("Close"))
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        """Collect a fresh report, paint it from cache, then resolve the gaps."""
        if self._logged is not None:
            self._set_text(describe_logged_instance(self._logged))
            return
        hostnames = None
        if self._resolver is not None and self._resolver.enabled:
            hostnames = self._resolver.lookup
        report = build_process_report(self._pid, hostnames)
        text, sockets = report.text, report.sockets
        if self._lookup is not None and not report.found:
            record = self._lookup(self._pid)
            if record is not None:
                text, sockets = describe_logged_instance(record), []
        self._set_text(text)
        if self._resolver is not None and sockets:
            self._resolver.request(sockets)

    def _set_text(self, text: str) -> None:
        """Replace the report without throwing away the reader's scroll position."""
        bar = self.report.verticalScrollBar()
        position = bar.value()
        self.report.setPlainText(text)
        bar.setValue(position)

    def _on_hostname(self, _address: str, _hostname: str) -> None:
        """Coalesce a burst of answers into one re-render."""
        if self.isVisible() and not self._name_timer.isActive():
            self._name_timer.start()

    def done(self, result: int) -> None:  # noqa: N802 - Qt event naming
        if self._resolver is not None:
            try:
                self._resolver.resolved.disconnect(self._on_hostname)
            except (RuntimeError, TypeError):
                pass
        self._name_timer.stop()
        super().done(result)

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self.report.toPlainText())


class SettingsDialog(QDialog):
    """Edit the persisted preferences; writes through ``Settings`` on OK."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle(self.tr("Settings"))
        self.resize(560, 460)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._summary_tab(), self.tr("Summary lines"))
        tabs.addTab(self._units_tab(), self.tr("Units"))
        tabs.addTab(self._appearance_tab(), self.tr("Colours"))
        tabs.addTab(self._network_tab(), self.tr("Network"))
        tabs.addTab(self._language_tab(), self.tr("Language"))
        layout.addWidget(tabs, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _summary_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(self.tr(
            "Choose which information lines the main window shows. Hiding a line only "
            "removes it from view: its numbers keep being computed and stored."
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        visible = set(self._settings.visible_lines())
        self._summary_boxes: dict[str, QCheckBox] = {}
        for name in SUMMARY_LINES:
            box = QCheckBox(summary_line_labels()[name])
            box.setChecked(name in visible)
            self._summary_boxes[name] = box
            layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _units_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self._rate_units = QComboBox()
        for label, value in (
            (self.tr("Binary — KiB/s, MiB/s"), "binary"),
            (self.tr("Decimal — kB/s, MB/s"), "decimal"),
            (self.tr("Bits — kbit/s, Mbit/s"), "bits"),
        ):
            self._rate_units.addItem(label, value)
        self._rate_units.setCurrentIndex(max(0, self._rate_units.findData(self._settings.rate_units())))
        form.addRow(self.tr("Rates:"), self._rate_units)
        self._total_units = QComboBox()
        for label, value in (
            (self.tr("Binary — KiB, MiB"), "binary"),
            (self.tr("Decimal — kB, MB"), "decimal"),
        ):
            self._total_units.addItem(label, value)
        self._total_units.setCurrentIndex(max(0, self._total_units.findData(self._settings.total_units())))
        form.addRow(self.tr("Totals:"), self._total_units)
        hint = QLabel(self.tr(
            "Links are sold in bits, storage in bytes. Stored usage is always binary KiB; "
            "this only changes how it is displayed."
        ))
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self._scheme = QComboBox()
        for label, value in (
            (self.tr("System (desktop colours)"), "system"),
            (self.tr("Dark"), "dark"),
            (self.tr("Light"), "light"),
            (self.tr("High contrast"), "contrast"),
        ):
            self._scheme.addItem(label, value)
        self._scheme.setCurrentIndex(max(0, self._scheme.findData(self._settings.scheme())))
        form.addRow(self.tr("Colour scheme:"), self._scheme)
        hint = QLabel(self.tr(
            "System keeps whatever palette the desktop provides. The presets recolour "
            "the chart, the tables and the TX/RX lines."
        ))
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _network_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self._interface = QLineEdit(self._settings.interface())
        self._interface.setPlaceholderText(self.tr("Default interface (auto-detect)"))
        self._interface.setClearButtonEnabled(True)
        form.addRow(self.tr("Interface:"), self._interface)
        self._dns = QCheckBox(self.tr("Resolve peer addresses in the details report"))
        self._dns.setChecked(self._settings.dns_enabled())
        form.addRow(self._dns)
        self._ttl = QSpinBox()
        self._ttl.setRange(1, 1440)
        self._ttl.setSuffix(self.tr(" min"))
        self._ttl.setValue(self._settings.dns_ttl_minutes())
        self._ttl.setEnabled(self._dns.isChecked())
        self._dns.toggled.connect(self._ttl.setEnabled)
        form.addRow(self.tr("Cache lookups for:"), self._ttl)
        hint = QLabel(self.tr(
            "Lookups run in the background, at most four at a time, and never on the "
            "one-second refresh path."
        ))
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _language_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self._language = QComboBox()
        self._language.addItem(self.tr("System default"), "")
        for locale in available_translations():
            self._language.addItem(locale, locale)
        current = self._settings.locale()
        self._language.setCurrentIndex(max(0, self._language.findData(current)))
        form.addRow(self.tr("Interface language:"), self._language)
        hint = QLabel(self.tr(
            "A language takes effect the next time the app starts. Catalogues are scaffolded "
            "but not shipped translated yet, so only the system default is offered until "
            "someone builds one with build-translations.sh."
        ))
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _save(self) -> None:
        self._settings.set_visible_lines(
            [name for name, box in self._summary_boxes.items() if box.isChecked()]
        )
        self._settings.set_rate_units(self._rate_units.currentData())
        self._settings.set_total_units(self._total_units.currentData())
        self._settings.set_scheme(self._scheme.currentData())
        self._settings.set_interface(self._interface.text())
        self._settings.set_dns_enabled(self._dns.isChecked())
        self._settings.set_dns_ttl_minutes(self._ttl.value())
        self._settings.set_locale(self._language.currentData() or "")
        self.accept()


def _ensure_usage_schema(connection: sqlite3.Connection) -> None:
    """Create every table the app needs and stamp the schema version (idempotent)."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS daily_totals (
               day TEXT PRIMARY KEY,
               sent_kib REAL NOT NULL DEFAULT 0,
               received_kib REAL NOT NULL DEFAULT 0
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS process_usage (
               day TEXT NOT NULL,
               process_name TEXT NOT NULL,
               pid INTEGER NOT NULL,
               sent_kib REAL NOT NULL DEFAULT 0,
               received_kib REAL NOT NULL DEFAULT 0,
               PRIMARY KEY (day, process_name, pid)
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS process_instances (
               pid            INTEGER NOT NULL,
               started_at     TEXT    NOT NULL,
               first_seen     TEXT    NOT NULL,
               last_seen      TEXT    NOT NULL,
               process_name   TEXT    NOT NULL,
               user_name      TEXT    NOT NULL,
               command_line   TEXT    NOT NULL,
               parent_pid     INTEGER,
               parent_command TEXT,
               PRIMARY KEY (pid, started_at)
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS process_instances_started ON process_instances(started_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dns_cache (
               address     TEXT PRIMARY KEY,
               hostname    TEXT,
               resolved_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS process_sockets (
               pid           INTEGER NOT NULL,
               started_at    TEXT    NOT NULL,
               netid         TEXT    NOT NULL,
               state         TEXT    NOT NULL,
               local_address TEXT    NOT NULL,
               local_port    TEXT    NOT NULL,
               peer_address  TEXT    NOT NULL,
               peer_port     TEXT    NOT NULL,
               peer_hostname TEXT,
               captured_at   TEXT    NOT NULL,
               PRIMARY KEY (pid, started_at, netid, local_address, local_port,
                            peer_address, peer_port)
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS traffic_samples (
               day          TEXT    NOT NULL,
               minute       INTEGER NOT NULL,
               process_name TEXT    NOT NULL,
               sent_kib     REAL    NOT NULL DEFAULT 0,
               received_kib REAL    NOT NULL DEFAULT 0,
               PRIMARY KEY (day, minute, process_name)
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS traffic_samples_day_process"
        " ON traffic_samples(day, process_name)"
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


class NetworkProcessWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle(self.tr("Per-Process Network Monitor"))
        self.resize(950, 680)
        self.settings = settings if settings is not None else Settings()
        app = QApplication.instance()
        self._system_palette = app.palette() if app is not None else QPalette()
        self._theme: dict[str, str] = {}
        self._usage_db: sqlite3.Connection | None = self._open_usage_database()
        self._resolver = DnsResolver(self.settings, self)
        self._resolver.resolved.connect(self._store_dns_answer)
        self._resolver.load(self._read_dns_cache())
        self._last_accounted_at: datetime | None = None
        # Minute samples waiting for the rollover. Keyed by day and minute of
        # the day, then process name, so two pids that happen to share a name
        # land in the same row, which is what the day-wide sum needs.
        self._sample_buffer: dict[tuple[str, int], dict[str, list[float]]] = {}
        self._last_sample_flush: datetime | None = None
        self._sample_flush_error: str | None = None
        self._available_months: list[str] = []
        self._available_days: tuple[str, list[str]] | None = None
        self._stdout_buffer = b""
        self._in_refresh_cycle = False
        self._pending_rows: dict[tuple[int, str], ProcessRate] = {}
        self._rows: dict[tuple[int, str], ProcessRate] = {}
        self._tracked_instances: dict[int, ProcessInstance] = {}
        self._last_seen_flush_at: datetime | None = None
        self._instance_records: list[dict] = []
        self._instance_tab_index = -1
        self._name_totals_tab_index = -1
        self._usage_tab_index = -1
        self._peers_tab_index = -1
        # The day and its distinct process names the usage filter was filled
        # from, so the combo is rebuilt only when the selection actually moves.
        self._usage_filter_state: tuple[str, list[str]] | None = None
        self._last_day_view_at: datetime | None = None
        # The capture set nethogs was started with, and a guard so the restart
        # path can stop the child without looking like a user-initiated stop.
        self._captured_interfaces: tuple[tuple[str, int], ...] = ()
        self._restarting = False
        # True between Start and the first parsed line, so the finished handler
        # can tell "never started" from "ran and stopped" without reading the
        # status text back, which is translated.
        self._awaiting_first_output = False

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        controls = QHBoxLayout()
        controls.addWidget(QLabel(self.tr("Interface:")))
        self.interface_input = QLineEdit()
        self.interface_input.setPlaceholderText(self.tr("Default interface (auto-detect)"))
        self.interface_input.setClearButtonEnabled(True)
        controls.addWidget(self.interface_input, 1)
        self.start_button = QPushButton(self.tr("Start"))
        self.start_button.clicked.connect(self.start_monitoring)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton(self.tr("Stop"))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_monitoring)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)

        self.status_label = QLabel(self.tr("Stopped."))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.total_label = QLabel(self.tr("Total: {}").format(format_rate(0.0)))
        self.total_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(self.total_label)
        self.tx_label = QLabel(self.tr("Total TX: {}").format(format_rate(0.0)))
        self.rx_label = QLabel(self.tr("Total RX: {}").format(format_rate(0.0)))
        layout.addWidget(self.tx_label)
        layout.addWidget(self.rx_label)
        self.today_label = QLabel()
        self.today_label.setWordWrap(True)
        layout.addWidget(self.today_label)

        history_controls = QHBoxLayout()
        history_controls.addWidget(QLabel(self.tr("History month:")))
        self.month_combo = QComboBox()
        self.month_combo.currentIndexChanged.connect(self._on_history_month_changed)
        history_controls.addWidget(self.month_combo)
        history_controls.addWidget(QLabel(self.tr("Day:")))
        self.day_combo = QComboBox()
        self.day_combo.currentIndexChanged.connect(self._show_selected_day)
        history_controls.addWidget(self.day_combo, 1)
        self.history_label = QLabel(self.tr("Select a logged day to view totals."))
        self.history_label.setWordWrap(True)
        layout.addLayout(history_controls)
        layout.addWidget(self.history_label)

        self.tabs = QTabWidget()
        live_page = QWidget()
        live_layout = QVBoxLayout(live_page)
        self.chart = HistoryChart()
        live_layout.addWidget(self.chart)
        self.live_table = self._make_table(
            [
                self.tr("Process"),
                self.tr("PID"),
                self.tr("Sent rate"),
                self.tr("Received rate"),
                self.tr("Today TX"),
                self.tr("Today RX"),
            ]
        )
        self.live_table.cellDoubleClicked.connect(self._show_process_details)
        self.live_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.live_table.customContextMenuRequested.connect(self._live_table_menu)
        self.live_table.itemSelectionChanged.connect(self._update_details_button)
        live_layout.addWidget(self.live_table)
        details_row = QHBoxLayout()
        self.details_button = QPushButton(self.tr("Details"))
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(self._show_selected_details)
        details_row.addWidget(self.details_button)
        self.details_hint = QLabel(
            self.tr(
                "Select a row and press Details (or double-click / right-click) for the full "
                "command line, parent chain and sockets."
            )
        )
        self.details_hint.setWordWrap(True)
        details_row.addWidget(self.details_hint, 1)
        live_layout.addLayout(details_row)
        self.tabs.addTab(live_page, self.tr("Live processes"))

        usage_page = QWidget()
        usage_layout = QVBoxLayout(usage_page)
        usage_controls = QHBoxLayout()
        usage_controls.addWidget(QLabel(self.tr("Range:")))
        self.usage_range_combo = QComboBox()
        self.usage_range_combo.addItem(self.tr("Whole day"), None)
        for hours in USAGE_RANGE_HOURS:
            self.usage_range_combo.addItem(
                self.tr("Last {hours} h").format(hours=hours), hours
            )
        self.usage_range_combo.currentIndexChanged.connect(self._refresh_usage_tab)
        usage_controls.addWidget(self.usage_range_combo)
        usage_controls.addWidget(QLabel(self.tr("Process:")))
        self.usage_process_combo = QComboBox()
        self.usage_process_combo.addItem(self.tr("All processes"), None)
        self.usage_process_combo.currentIndexChanged.connect(self._refresh_usage_tab)
        usage_controls.addWidget(self.usage_process_combo, 1)
        usage_controls.addWidget(QLabel(self.tr("Granularity:")))
        self.usage_granularity_combo = QComboBox()
        for minutes in USAGE_GRANULARITIES:
            self.usage_granularity_combo.addItem(
                self.tr("{minutes} min").format(minutes=minutes), minutes
            )
        self.usage_granularity_combo.currentIndexChanged.connect(self._refresh_usage_tab)
        usage_controls.addWidget(self.usage_granularity_combo)
        usage_layout.addLayout(usage_controls)
        self.usage_chart = DayUsageChart()
        usage_layout.addWidget(self.usage_chart, 1)
        self.usage_stats_label = QLabel()
        self.usage_stats_label.setWordWrap(True)
        usage_layout.addWidget(self.usage_stats_label)
        self.usage_fallback_label = QLabel()
        self.usage_fallback_label.setWordWrap(True)
        self.usage_fallback_label.setVisible(False)
        usage_layout.addWidget(self.usage_fallback_label)
        self._usage_tab_index = self.tabs.addTab(usage_page, self.tr("Usage over time"))

        peers_page = QWidget()
        peers_layout = QVBoxLayout(peers_page)
        # The caveat sits above the table on purpose: the one wrong reading of
        # this tab is that a row is a bandwidth figure, and a reader who is
        # looking for that figure should meet the correction first.
        self.peers_caveat_label = QLabel(
            self.tr(
                "Connection activity, not traffic: bytes are recorded per process per day "
                "and are not attributed to peers. No column here is a byte count."
            )
        )
        self.peers_caveat_label.setWordWrap(True)
        peers_layout.addWidget(self.peers_caveat_label)
        peers_controls = QHBoxLayout()
        self.peers_running_check = QCheckBox(self.tr("Only running processes"))
        self.peers_running_check.toggled.connect(self._refresh_peers_tab)
        peers_controls.addWidget(self.peers_running_check)
        self.peers_resolve_button = QPushButton(self.tr("Resolve names"))
        self.peers_resolve_button.clicked.connect(self._resolve_peer_names)
        peers_controls.addWidget(self.peers_resolve_button)
        self.peers_status_label = QLabel()
        self.peers_status_label.setWordWrap(True)
        peers_controls.addWidget(self.peers_status_label, 1)
        peers_layout.addLayout(peers_controls)
        self.peers_table = self._make_table(
            [
                self.tr("Peer"),
                self.tr("Hostname"),
                self.tr("Port"),
                self.tr("Protocol"),
                self.tr("State"),
                self.tr("Local endpoint"),
                self.tr("Process"),
                self.tr("PID"),
                self.tr("User"),
                self.tr("Seen"),
            ]
        )
        peers_layout.addWidget(self.peers_table, 1)
        # Ten columns of real addresses do not fit the window at once, so every
        # width is set rather than derived. ResizeToContents on a table rebuilt
        # in place rescans all rows for every setItem: a return visit to this
        # tab froze the GUI for tens of seconds with a few hundred sockets.
        # Fixed widths the user can drag keep each rebuild linear. Measured at
        # 1100 px the whole table fits; narrower windows scroll.
        peers_header = self.peers_table.horizontalHeader()
        for column, width in (
            (0, 200), (1, 125), (2, 80), (3, 90), (4, 100),
            (5, 125), (6, 125), (7, 80), (8, 120), (9, 160),
        ):
            peers_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.peers_table.setColumnWidth(column, width)
        self.peers_summary_label = QLabel()
        self.peers_summary_label.setWordWrap(True)
        peers_layout.addWidget(self.peers_summary_label)
        self._peers_tab_index = self.tabs.addTab(peers_page, self.tr("Connections / peers"))
        # Newest first until the user picks a column. The table sorts itself, so
        # a rebuild keeps whatever order the header indicator shows.
        self.peers_table.sortByColumn(9, Qt.SortOrder.DescendingOrder)
        # Answers trickle in one address at a time; coalesce them.
        self.peer_name_timer = QTimer(self)
        self.peer_name_timer.setSingleShot(True)
        self.peer_name_timer.setInterval(PEER_NAME_REFRESH_MS)
        self.peer_name_timer.timeout.connect(self._refresh_peers_tab)
        self._resolver.resolved.connect(self._on_peer_name_resolved)

        totals_page = QWidget()
        totals_layout = QVBoxLayout(totals_page)
        self.process_totals_hint = QLabel(
            self.tr("Daily totals grouped by process name (all PIDs). Select a day above.")
        )
        self.process_totals_hint.setWordWrap(True)
        totals_layout.addWidget(self.process_totals_hint)
        self.name_totals_table = self._make_table(
            [self.tr("Process name"), self.tr("Total TX"), self.tr("Total RX"), self.tr("Combined")]
        )
        totals_layout.addWidget(self.name_totals_table)
        self._name_totals_tab_index = self.tabs.addTab(totals_page, self.tr("Process name totals"))

        instances_page = QWidget()
        instances_layout = QVBoxLayout(instances_page)
        self.instances_hint = QLabel(
            self.tr(
                "Logged process instances for the selected day. Double-click a row for the stored "
                "command line and parent, even after the process has exited."
            )
        )
        self.instances_hint.setWordWrap(True)
        instances_layout.addWidget(self.instances_hint)
        self.instance_table = self._make_table(
            [
                self.tr("Day"),
                self.tr("Started"),
                self.tr("PID"),
                self.tr("Process"),
                self.tr("User"),
                self.tr("Parent"),
                self.tr("Command"),
            ]
        )
        instance_header = self.instance_table.horizontalHeader()
        instance_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.instance_table.setColumnWidth(0, 100)
        instance_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.instance_table.setColumnWidth(1, 160)
        instance_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.instance_table.cellDoubleClicked.connect(self._show_logged_instance)
        instances_layout.addWidget(self.instance_table)
        self._instance_tab_index = self.tabs.addTab(instances_page, self.tr("Instance history"))
        # The usage query changes at most once a minute and only while its tab
        # is the visible one, so it has its own slower timer instead of riding
        # the one-second tick.
        self.usage_timer = QTimer(self)
        self.usage_timer.setInterval(USAGE_REFRESH_SECONDS * 1000)
        self.usage_timer.timeout.connect(self._refresh_usage_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._summary_widgets = {
            "status": self.status_label,
            "total": self.total_label,
            "tx": self.tx_label,
            "rx": self.rx_label,
            "today": self.today_label,
            "history": self.history_label,
        }
        self._summary_actions: dict[str, QAction] = {}
        self._build_menus()
        self._apply_settings()

        self._refresh_history_selectors()
        self._refresh_selected_day_view()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self._refresh_view)
        self.refresh_timer.start()
        self.interface_timer = QTimer(self)
        self.interface_timer.setInterval(INTERFACE_SCAN_SECONDS * 1000)
        self.interface_timer.timeout.connect(self._check_interfaces)
        self.interface_timer.start()
        self._update_today_total()

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # Fixed widths, never ResizeToContents: these tables are rebuilt in
        # place on a timer, and content-sized sections rescan every row on each
        # setItem, which grows quadratically with row count. Callers override
        # individual widths where a field needs more room.
        for column in range(1, len(headers)):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(column, 120)
        return table

    def _build_menus(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu(self.tr("&File"))
        quit_action = file_menu.addAction(self.tr("&Quit"))
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        view_menu = menu.addMenu(self.tr("&View"))
        visible = set(self.settings.visible_lines())
        for name in SUMMARY_LINES:
            action = view_menu.addAction(summary_line_labels()[name])
            action.setCheckable(True)
            action.setChecked(name in visible)
            action.toggled.connect(lambda checked, key=name: self._toggle_summary_line(key, checked))
            self._summary_actions[name] = action

        settings_menu = menu.addMenu(self.tr("&Settings"))
        preferences = settings_menu.addAction(self.tr("&Preferences…"))
        preferences.setShortcut("Ctrl+,")
        preferences.triggered.connect(self.open_settings)

        help_menu = menu.addMenu(self.tr("&Help"))
        about = help_menu.addAction(self.tr("&About"))
        about.triggered.connect(self.show_about)

    def _toggle_summary_line(self, name: str, visible: bool) -> None:
        self.settings.set_line_visible(name, visible)
        self._apply_summary_visibility()

    def _apply_summary_visibility(self) -> None:
        """Show or hide the six information lines; never skip their computation."""
        visible = set(self.settings.visible_lines())
        for name, widget in self._summary_widgets.items():
            widget.setVisible(name in visible)
        for name, action in self._summary_actions.items():
            action.blockSignals(True)
            action.setChecked(name in visible)
            action.blockSignals(False)

    def _apply_settings(self) -> None:
        """Push the persisted preferences into the widgets and the formatters."""
        set_unit_defaults(self.settings.rate_units(), self.settings.total_units())
        app = QApplication.instance()
        if app is not None:
            self._theme = apply_color_scheme(app, self.settings.scheme(), self._system_palette)
        self.chart.apply_theme(self._theme)
        self.usage_chart.apply_theme(self._theme)
        accent = self._theme.get("tx", "")
        self.tx_label.setStyleSheet(f"color: {accent};" if accent else "")
        accent = self._theme.get("rx", "")
        self.rx_label.setStyleSheet(f"color: {accent};" if accent else "")
        total_style = "font-size: 16px; font-weight: 600;"
        if self._theme.get("text"):
            total_style += f" color: {self._theme['text']};"
        self.total_label.setStyleSheet(total_style)
        self._apply_summary_visibility()
        self.interface_input.setText(self.settings.interface())

    def open_settings(self) -> None:
        # Keep whatever is typed in the field: the dialog is the surface that
        # owns the interface value, and opening it should not discard text.
        self.settings.set_interface(self.interface_input.text())
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings()
            self._refresh_view()
            self._refresh_selected_day_view()

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            self.tr("About Per-Process Network Monitor"),
            self.tr(
                "<h3>Per-Process Network Monitor {version}</h3>"
                "<p>Per-process bandwidth monitor driven by <code>nethogs</code>.</p>"
                "<p>Inspired by <b>nethogs-qt</b> by Stéphane List — "
                '<a href="https://slist.lilotux.net/linux/nethogs-qt/index_en.html">'
                "slist.lilotux.net/linux/nethogs-qt</a>.<br>"
                "Concept and acknowledgement only; no code or assets are shared.</p>"
                "<p>Built with Qt for Python (PySide6); traffic capture by nethogs.</p>"
            ).format(version=APP_VERSION),
        )

    @staticmethod
    def _open_usage_database() -> sqlite3.Connection:
        """Open persistent usage data, migrating it forward with a backup first."""
        data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "network-usage.sqlite3"
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA busy_timeout = 5000")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION and db_path.exists() and db_path.stat().st_size > 0:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")
            with sqlite3.connect(backup_path) as backup:
                connection.backup(backup)
        _ensure_usage_schema(connection)
        cutoff = (datetime.now() - timedelta(days=DNS_CACHE_RETENTION_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        # Minute samples are detail, not history: past the retention window the
        # day keeps its `daily_totals` row and loses the minute rows, so growth
        # is bounded by a month of detail instead of every byte ever counted.
        sample_cutoff = (date.today() - timedelta(days=SAMPLE_RETENTION_DAYS)).isoformat()
        with connection:
            connection.execute("DELETE FROM dns_cache WHERE resolved_at < ?", (cutoff,))
            connection.execute("DELETE FROM traffic_samples WHERE day < ?", (sample_cutoff,))
        return connection

    def _record_interval(self, start: datetime, end: datetime, rows: list[ProcessRate]) -> None:
        """Integrate sampled rates, splitting at local midnight and every minute.

        The minute split exists for `traffic_samples`: a segment that straddled
        two minutes could not be attributed to either. It reuses the arithmetic
        that already produces the day total, so a sample is a slice of the same
        interval rather than a second measurement that could disagree with it.
        """
        if self._usage_db is None:
            return
        while start < end:
            segment_end = min(end, self._segment_end(start))
            seconds = (segment_end - start).total_seconds()
            day = start.date().isoformat()
            minute = start.hour * 60 + start.minute
            unattributed_sent = 0.0
            unattributed_received = 0.0
            with self._usage_db:
                self._usage_db.execute(
                    """INSERT INTO daily_totals(day, sent_kib, received_kib) VALUES (?, ?, ?)
                       ON CONFLICT(day) DO UPDATE SET
                           sent_kib = sent_kib + excluded.sent_kib,
                           received_kib = received_kib + excluded.received_kib""",
                    (day, sum(row.sent_kib_s for row in rows) * seconds,
                     sum(row.received_kib_s for row in rows) * seconds),
                )
                for row in rows:
                    sent, received = row.sent_kib_s * seconds, row.received_kib_s * seconds
                    if sent == 0 and received == 0:
                        continue
                    if row.pid <= 0:
                        # nethogs could not map the flow to a process. Keeping
                        # its raw label ("unknown TCP", "1.2.3.4:443-…") stored
                        # rows for processes that never existed and pushed the
                        # real ones down the history list; one labelled bucket
                        # stays visible next to the day total instead.
                        unattributed_sent += sent
                        unattributed_received += received
                        continue
                    self._usage_db.execute(
                        """INSERT INTO process_usage(day, process_name, pid, sent_kib, received_kib)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(day, process_name, pid) DO UPDATE SET
                               sent_kib = sent_kib + excluded.sent_kib,
                               received_kib = received_kib + excluded.received_kib""",
                        (day, row.name, row.pid, sent, received),
                    )
                if unattributed_sent or unattributed_received:
                    self._usage_db.execute(
                        """INSERT INTO process_usage(day, process_name, pid, sent_kib, received_kib)
                           VALUES (?, ?, 0, ?, ?)
                           ON CONFLICT(day, process_name, pid) DO UPDATE SET
                               sent_kib = sent_kib + excluded.sent_kib,
                               received_kib = received_kib + excluded.received_kib""",
                        (day, UNATTRIBUTED_PROCESS, unattributed_sent, unattributed_received),
                    )
            self._accumulate_sample(day, minute, rows, seconds)
            start = segment_end
        self._flush_traffic_samples()

    @staticmethod
    def _segment_end(start: datetime) -> datetime:
        """The next boundary a segment may not cross: a whole minute or midnight."""
        midnight = datetime.combine(start.date() + timedelta(days=1), time.min)
        next_minute = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
        return min(midnight, next_minute)

    def _accumulate_sample(
        self, day: str, minute: int, rows: list[ProcessRate], seconds: float
    ) -> None:
        """Add one split's per-process KiB to the minute bucket it belongs to."""
        bucket = self._sample_buffer.setdefault((day, minute), {})
        unattributed_sent = 0.0
        unattributed_received = 0.0
        for row in rows:
            sent, received = row.sent_kib_s * seconds, row.received_kib_s * seconds
            if sent == 0 and received == 0:
                continue
            if row.pid <= 0:
                unattributed_sent += sent
                unattributed_received += received
                continue
            totals = bucket.setdefault(row.name, [0.0, 0.0])
            totals[0] += sent
            totals[1] += received
        if unattributed_sent or unattributed_received:
            totals = bucket.setdefault(UNATTRIBUTED_PROCESS, [0.0, 0.0])
            totals[0] += unattributed_sent
            totals[1] += unattributed_received

    def _flush_traffic_samples(self, force: bool = False) -> None:
        """Write closed minute buckets to `traffic_samples` in one transaction.

        Called from `_record_interval`, so the flush rides the tick that crosses
        the minute boundary: at most one write a minute, and never a write in
        the one-second path. `force` is for the moments the writer is about to
        go quiet — Stop, window close, or a read of the table — where waiting
        for the rollover would lose the tail of the current minute.
        """
        if self._usage_db is None or not self._sample_buffer:
            return
        now = datetime.now()
        current_minute = (now.date().isoformat(), now.hour * 60 + now.minute)
        # A bucket is final only once its minute has ended: publishing the
        # current one would draw a partial minute as a finished point. The
        # elapsed check is the fallback for a clock that does not advance as
        # expected, so a buffer can never sit unflushed for longer than a minute.
        if not force and max(self._sample_buffer) >= current_minute:
            if (
                self._last_sample_flush is None
                or (now - self._last_sample_flush).total_seconds() < SAMPLE_FLUSH_SECONDS
            ):
                return
        payload: list[tuple[str, int, str, float, float]] = []
        for (day, minute), bucket in sorted(self._sample_buffer.items()):
            for name, (sent, received) in sorted(bucket.items()):
                if sent == 0 and received == 0:
                    continue
                payload.append((day, minute, name, sent, received))
        if payload:
            try:
                with self._usage_db:
                    self._usage_db.executemany(
                        """INSERT INTO traffic_samples(day, minute, process_name, sent_kib, received_kib)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(day, minute, process_name) DO UPDATE SET
                               sent_kib = sent_kib + excluded.sent_kib,
                               received_kib = received_kib + excluded.received_kib""",
                        payload,
                    )
                self._sample_flush_error = None
            except sqlite3.Error as error:
                # The day totals for this interval are already committed. A
                # sample failure must not touch them, must not wedge the tick,
                # and must leave a trace someone can read back.
                self._sample_flush_error = str(error)
        self._sample_buffer.clear()
        self._last_sample_flush = now

    def _account_usage(self) -> None:
        if self._usage_db is None:
            return
        now = datetime.now()
        previous = self._last_accounted_at
        if previous is None:
            return
        elapsed = (now - previous).total_seconds()
        self._last_accounted_at = now
        if elapsed <= 0:
            return
        # Avoid inventing usage after a suspended machine or a stalled GUI.
        start = now - timedelta(seconds=min(elapsed, 2.0))
        displayed = dict(self._rows)
        displayed.update(self._pending_rows)
        self._record_interval(start, now, list(displayed.values()))

    def _store_instance(self, instance: ProcessInstance) -> None:
        """Insert or refresh one process instance, keeping its first_seen."""
        if self._usage_db is None:
            return
        with self._usage_db:
            self._usage_db.execute(
                """INSERT INTO process_instances(
                       pid, started_at, first_seen, last_seen, process_name,
                       user_name, command_line, parent_pid, parent_command)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pid, started_at) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       process_name = excluded.process_name""",
                (
                    instance.pid,
                    instance.started_at,
                    instance.first_seen,
                    instance.last_seen,
                    instance.process_name,
                    instance.user_name,
                    instance.command_line,
                    instance.parent_pid,
                    instance.parent_command,
                ),
            )

    def _log_process_instances(self, rows: list[ProcessRate]) -> None:
        """Log instances the monitor has not seen yet this session, capped per refresh."""
        if self._usage_db is None:
            return
        budget = MAX_NEW_INSTANCES_PER_REFRESH
        logged: list[ProcessInstance] = []
        for row in rows:
            if row.pid <= 0 or row.pid in self._tracked_instances:
                continue
            if budget <= 0:
                break
            budget -= 1
            instance = read_process_instance(row.pid, row.name)
            if instance is None:
                continue
            self._tracked_instances[row.pid] = instance
            self._store_instance(instance)
            logged.append(instance)
        self._store_socket_snapshots(logged)

    def _flush_last_seen(self, force: bool = False) -> None:
        """Refresh last_seen for tracked PIDs, dropping gone ones and catching PID reuse."""
        if self._usage_db is None or not self._tracked_instances:
            return
        now = datetime.now()
        if not force and self._last_seen_flush_at is not None:
            if (now - self._last_seen_flush_at).total_seconds() < LAST_SEEN_FLUSH_SECONDS:
                return
        self._last_seen_flush_at = now
        refreshed: list[ProcessInstance] = []
        for pid in list(self._tracked_instances):
            tracked = self._tracked_instances[pid]
            current = read_process_instance(pid, tracked.process_name)
            if current is None:
                del self._tracked_instances[pid]
                continue
            if current.started_at != tracked.started_at:
                # The PID was recycled: the new run is a separate instance.
                self._tracked_instances[pid] = current
                self._store_instance(current)
                refreshed.append(current)
                continue
            tracked.last_seen = current.last_seen
            self._store_instance(tracked)
            refreshed.append(tracked)
        self._store_socket_snapshots(refreshed)

    def _store_socket_snapshots(self, instances: list[ProcessInstance]) -> None:
        """Replace the stored socket set for each instance, using one ss call for all."""
        if self._usage_db is None or not instances:
            return
        table = read_socket_table()
        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        peers: list[SocketState] = []
        with self._usage_db:
            for instance in instances:
                sockets = table.get(instance.pid, [])
                peers.extend(sockets)
                self._usage_db.execute(
                    "DELETE FROM process_sockets WHERE pid = ? AND started_at = ?",
                    (instance.pid, instance.started_at),
                )
                for socket in sockets:
                    self._usage_db.execute(
                        """INSERT OR REPLACE INTO process_sockets(
                               pid, started_at, netid, state, local_address, local_port,
                               peer_address, peer_port, peer_hostname, captured_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            instance.pid,
                            instance.started_at,
                            socket.netid,
                            socket.state,
                            socket.local_address,
                            socket.local_port,
                            socket.peer_address,
                            socket.peer_port,
                            self._resolver.lookup(socket.peer_address),
                            captured_at,
                        ),
                    )
        # Names for logged peers resolve in the background: the next 60 s flush
        # then stores them, so a post-mortem report can name the connection.
        self._resolver.request(peers)

    def _read_dns_cache(self) -> list[tuple[str, str | None, str]]:
        """Return stored DNS answers so a restart does not re-query them."""
        if self._usage_db is None:
            return []
        rows = self._usage_db.execute(
            "SELECT address, hostname, resolved_at FROM dns_cache"
        ).fetchall()
        return [(address, hostname, resolved_at) for address, hostname, resolved_at in rows]

    def _store_dns_answer(self, address: str, hostname: str) -> None:
        """Persist one resolver answer on the GUI thread, where the connection lives."""
        if self._usage_db is None:
            return
        with self._usage_db:
            self._usage_db.execute(
                """INSERT INTO dns_cache(address, hostname, resolved_at) VALUES (?, ?, ?)
                   ON CONFLICT(address) DO UPDATE SET
                       hostname = excluded.hostname,
                       resolved_at = excluded.resolved_at""",
                (
                    address,
                    hostname or None,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def _lookup_logged_instance(self, pid: int) -> dict | None:
        """Return the most recently seen stored instance for a PID, if any."""
        if self._usage_db is None:
            return None
        row = self._usage_db.execute(
            """SELECT pid, started_at, first_seen, last_seen, process_name,
                      user_name, command_line, parent_pid, parent_command
               FROM process_instances WHERE pid = ?
               ORDER BY last_seen DESC LIMIT 1""",
            (pid,),
        ).fetchone()
        if row is None:
            return None
        record = dict(zip(INSTANCE_FIELDS, row))
        record["sockets"] = self._lookup_logged_sockets(record["pid"], record["started_at"])
        return record

    def _lookup_logged_sockets(self, pid: int, started_at: str) -> list[dict]:
        """Return the socket set stored for one logged instance."""
        if self._usage_db is None:
            return []
        rows = self._usage_db.execute(
            """SELECT netid, state, local_address, local_port, peer_address,
                      peer_port, peer_hostname
               FROM process_sockets WHERE pid = ? AND started_at = ?
               ORDER BY netid, local_address, local_port""",
            (pid, started_at),
        ).fetchall()
        fields = (
            "netid",
            "state",
            "local_address",
            "local_port",
            "peer_address",
            "peer_port",
            "peer_hostname",
        )
        return [dict(zip(fields, row)) for row in rows]

    def _refresh_instance_history(self) -> None:
        """Populate the Instance history tab for the day selected above."""
        if self._usage_db is None:
            return
        logged_day = self.day_combo.currentData()
        self.instance_table.setSortingEnabled(False)
        if not logged_day:
            self._instance_records = []
            self.instance_table.setRowCount(0)
            self.instance_table.setSortingEnabled(True)
            return
        rows = self._usage_db.execute(
            """SELECT pid, started_at, first_seen, last_seen, process_name,
                      user_name, command_line, parent_pid, parent_command
               FROM process_instances
               WHERE substr(started_at, 1, 10) = ? OR substr(last_seen, 1, 10) = ?
               ORDER BY last_seen DESC""",
            (logged_day, logged_day),
        ).fetchall()
        self._instance_records = [dict(zip(INSTANCE_FIELDS, row)) for row in rows]
        self.instance_table.setRowCount(len(self._instance_records))
        for index, record in enumerate(self._instance_records):
            command = record["command_line"] or ""
            if len(command) > 200:
                command = command[:200] + "…"
            parent = record["parent_command"] or ""
            if len(parent) > 80:
                parent = parent[:80] + "…"
            started = record["started_at"] or ""
            cells = (
                QTableWidgetItem(started[:10]),
                QTableWidgetItem(started),
                NumericTableWidgetItem(str(record["pid"])),
                QTableWidgetItem(record["process_name"] or ""),
                QTableWidgetItem(record["user_name"] or ""),
                QTableWidgetItem(parent),
                QTableWidgetItem(command),
            )
            for column, cell in enumerate(cells):
                self.instance_table.setItem(index, column, cell)
            cells[0].setData(Qt.ItemDataRole.UserRole, index)
            cells[2].setData(Qt.ItemDataRole.UserRole, float(record["pid"]))
        self.instance_table.setSortingEnabled(True)
        if self._instance_records:
            self.instances_hint.setText(
                self.tr(
                    "{count} process instance(s) logged for {day}. "
                    "Double-click a row for the stored command line."
                ).format(count=len(self._instance_records), day=logged_day)
            )
        else:
            self.instances_hint.setText(
                self.tr("No process instances logged for {day} yet.").format(day=logged_day)
            )

    def _on_tab_changed(self, index: int) -> None:
        if index in (self._name_totals_tab_index, self._instance_tab_index):
            self._refresh_selected_day_view()
        if index == self._usage_tab_index:
            # A hidden tab issues no query: the timer only runs while the graph
            # is on screen, and switching to it renders immediately instead of
            # waiting for the first tick.
            self.usage_timer.start()
            self._refresh_usage_tab()
        else:
            self.usage_timer.stop()
        if index == self._peers_tab_index:
            self._refresh_peers_tab()
            self.peers_status_label.clear()

    def _show_logged_instance(self, row: int, _column: int = 0) -> None:
        item = self.instance_table.item(row, 0)
        if item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        try:
            record = self._instance_records[int(index)]
        except (IndexError, TypeError, ValueError):
            return
        # Sockets are fetched on demand rather than for every row in the table:
        # the day view can hold hundreds of instances, and only the one opened
        # needs its stored connection set.
        record = dict(record)
        record["sockets"] = self._lookup_logged_sockets(
            record["pid"], record["started_at"]
        )
        ProcessDetailsDialog(
            int(record["pid"]), record["process_name"] or "", self, logged=record
        ).exec()

    def _day_totals(self, day: str) -> tuple[float, float] | None:
        """Return the recorded (sent, received) KiB for one day, or None if unlogged.

        Every reader of a day's row goes through here, so the Today line and the
        day total under History cannot disagree: they are the same query.
        """
        if self._usage_db is None:
            return None
        row = self._usage_db.execute(
            "SELECT sent_kib, received_kib FROM daily_totals WHERE day = ?", (day,)
        ).fetchone()
        return (float(row[0]), float(row[1])) if row is not None else None

    def _update_today_total(self) -> None:
        today = date.today().isoformat()
        sent, received = self._day_totals(today) or (0.0, 0.0)
        self.today_label.setText(
            self.tr("Today ({day}): TX {tx}  RX {rx}  Total {total}").format(
                day=today,
                tx=format_data(sent),
                rx=format_data(received),
                total=format_data(sent + received),
            )
        )

    def _refresh_history_selectors(self) -> bool:
        """Rebuild the month/day combos when the logged days change.

        Returns True when the combos were rebuilt, so the caller knows the
        selected day's view needs re-rendering. Rebuilding is kept separate from
        rendering: the day list changes at most once a day, while the view is
        refreshed on a timer.
        """
        if self._usage_db is None:
            return False
        months = [row[0] for row in self._usage_db.execute(
            "SELECT DISTINCT substr(day, 1, 7) FROM daily_totals ORDER BY 1 DESC"
        )]
        current_month = date.today().strftime("%Y-%m")
        if current_month not in months:
            months.insert(0, current_month)
        selected_month = self.month_combo.currentData() or current_month
        if months != self._available_months:
            self._available_months = months
            self.month_combo.blockSignals(True)
            self.month_combo.clear()
            for month in months:
                self.month_combo.addItem(month, month)
            index = self.month_combo.findData(selected_month)
            self.month_combo.setCurrentIndex(index if index >= 0 else 0)
            self.month_combo.blockSignals(False)
            self._available_days = None
        month = self.month_combo.currentData()
        if month is None:
            return False
        days = [row[0] for row in self._usage_db.execute(
            "SELECT day FROM daily_totals WHERE substr(day, 1, 7) = ? ORDER BY day DESC", (month,)
        )]
        if self._available_days == (month, days):
            return False
        previous_days = self._available_days[1] if self._available_days else []
        selected_day = self.day_combo.currentData()
        # Follow the midnight rollover: a selection that pointed at the newest
        # day follows the new newest day instead of freezing on yesterday. An
        # explicit older selection stays where the user put it.
        if days and (selected_day is None or (previous_days and selected_day == previous_days[0])):
            selected_day = days[0]
        self._available_days = (month, days)
        self.day_combo.blockSignals(True)
        self.day_combo.clear()
        for logged_day in days:
            self.day_combo.addItem(logged_day, logged_day)
        index = self.day_combo.findData(selected_day)
        self.day_combo.setCurrentIndex(index if index >= 0 else (0 if days else -1))
        self.day_combo.blockSignals(False)
        return True

    def _on_history_month_changed(self, _index: int) -> None:
        self._available_days = None
        self._refresh_history_selectors()
        self._refresh_selected_day_view()
        self._usage_filter_state = None
        self._refresh_usage_tab()

    def _show_selected_day(self, _index: int = -1) -> None:
        """Combo signal handler: the user picked a day, so render it now."""
        self._refresh_selected_day_view()
        self._refresh_usage_tab()

    def _day_view_due(self) -> bool:
        """True when the throttled history view is due for a rebuild."""
        if self._last_day_view_at is None:
            return True
        return (datetime.now() - self._last_day_view_at).total_seconds() >= DAY_VIEW_REFRESH_SECONDS

    def _update_selected_day_total(self) -> None:
        """Render the selected day's total line.

        This is one indexed row read, so it runs on every tick: the line under
        `History month:` must describe the same row as the `Today` line at the
        same moment, or the two disagree for as long as the slower cadence lags.
        Only the two tables behind it are throttled.
        """
        if self._usage_db is None:
            return
        logged_day = self.day_combo.currentData()
        if not logged_day:
            self.history_label.setText(self.tr("No monitoring logs for this month yet."))
            return
        totals = self._day_totals(logged_day)
        suffix = (
            self.tr(" (today)") if logged_day == date.today().isoformat() else ""
        )
        if totals is None:
            self.history_label.setText(
                self.tr("No totals recorded for {day}.").format(day=logged_day)
            )
        else:
            sent, received = totals
            self.history_label.setText(
                self.tr("{day}{suffix}: TX {tx}  RX {rx}  Total {total}").format(
                    day=logged_day,
                    suffix=suffix,
                    tx=format_data(sent),
                    rx=format_data(received),
                    total=format_data(sent + received),
                )
            )

    def _refresh_selected_day_view(self, update_total: bool = True) -> None:
        """Render the selected day: totals line, name totals and instance history."""
        self._last_day_view_at = datetime.now()
        if self._usage_db is None:
            return
        if update_total:
            self._update_selected_day_total()
        logged_day = self.day_combo.currentData()
        if not logged_day:
            self.name_totals_table.setRowCount(0)
            self._refresh_instance_history()
            return
        # Group by executable/process name, deliberately combining all PIDs so
        # a relaunch of the same program contributes to the same daily total.
        grouped = self._usage_db.execute(
            """SELECT process_name, SUM(sent_kib), SUM(received_kib)
               FROM process_usage WHERE day = ? GROUP BY process_name
               ORDER BY SUM(sent_kib) + SUM(received_kib) DESC, process_name COLLATE NOCASE""",
            (logged_day,),
        ).fetchall()
        self.name_totals_table.setSortingEnabled(False)
        self.name_totals_table.setRowCount(len(grouped))
        for index, (name, sent, received) in enumerate(grouped):
            sent, received = float(sent), float(received)
            cells = (QTableWidgetItem(name), NumericTableWidgetItem(format_data(sent)),
                     NumericTableWidgetItem(format_data(received)), NumericTableWidgetItem(format_data(sent + received)))
            for column, cell in enumerate(cells):
                self.name_totals_table.setItem(index, column, cell)
            for column, value in enumerate((None, sent, received, sent + received)):
                if column and value is not None:
                    self.name_totals_table.item(index, column).setData(Qt.ItemDataRole.UserRole, value)
        self.name_totals_table.setSortingEnabled(True)
        if not grouped:
            self.process_totals_hint.setText(
                self.tr("No per-process traffic recorded for {day}.").format(day=logged_day)
            )
        else:
            self.process_totals_hint.setText(
                self.tr(
                    "Daily totals for {day}, grouped by process name and combining all PIDs."
                ).format(day=logged_day)
            )
        self._refresh_instance_history()

    def _first_sample_day(self) -> str | None:
        """The earliest day that still has minute rows, or None if there are none."""
        if self._usage_db is None:
            return None
        row = self._usage_db.execute("SELECT MIN(day) FROM traffic_samples").fetchone()
        return row[0] if row and row[0] else None

    def _first_sample_minute(self, day: str) -> int | None:
        """The earliest minute of one day that has a sample, or None."""
        if self._usage_db is None:
            return None
        row = self._usage_db.execute(
            "SELECT MIN(minute) FROM traffic_samples WHERE day = ?", (day,)
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _usage_window(self, day: str) -> tuple[int, int]:
        """The visible minute window for the selected day and range preset.

        A past day has no "last hour" to show, so its presets collapse to the
        whole day. On today the preset is clamped to the samples that exist:
        starting an hour before the first sample would only pad the left edge
        with a flat line.
        """
        hours = self.usage_range_combo.currentData()
        if day != date.today().isoformat() or not hours:
            return (0, MINUTES_PER_DAY)
        now = datetime.now()
        end = now.hour * 60 + now.minute + 1  # include the minute in progress
        start = max(0, end - int(hours) * 60)
        first = self._first_sample_minute(day)
        if first is not None and first > start:
            start = first
        if end <= start:
            end = start + 1
        return (start, end)

    def _sample_series(
        self, day: str, process_name: str | None, granularity: int
    ) -> list[tuple[int, float, float]]:
        """One row per bucket: (bucket minute, sent KiB, received KiB).

        The minute is floored in SQL, so 1/5/15/60-minute views of the same
        range sum the same rows and differ only in how many buckets they carry.
        """
        if self._usage_db is None:
            return []
        bucket = max(1, int(granularity))
        query = """SELECT (minute / ?) * ? AS bucket, SUM(sent_kib), SUM(received_kib)
                   FROM traffic_samples WHERE day = ?"""
        params: list = [bucket, bucket, day]
        if process_name is not None:
            query += " AND process_name = ?"
            params.append(process_name)
        query += " GROUP BY bucket ORDER BY bucket"
        rows = self._usage_db.execute(query, params).fetchall()
        return [(int(minute), float(sent), float(received)) for minute, sent, received in rows]

    def _fill_usage_process_filter(self, day: str) -> None:
        """Rebuild the process filter when the selected day's names change."""
        if self._usage_db is None:
            return
        names = [
            row[0]
            for row in self._usage_db.execute(
                """SELECT DISTINCT process_name FROM traffic_samples WHERE day = ?
                   ORDER BY process_name COLLATE NOCASE""",
                (day,),
            )
        ]
        if self._usage_filter_state == (day, names):
            return
        previous = self.usage_process_combo.currentData()
        self._usage_filter_state = (day, names)
        self.usage_process_combo.blockSignals(True)
        self.usage_process_combo.clear()
        self.usage_process_combo.addItem(self.tr("All processes"), None)
        for name in names:
            self.usage_process_combo.addItem(name, name)
        index = self.usage_process_combo.findData(previous)
        self.usage_process_combo.setCurrentIndex(index if index >= 0 else 0)
        self.usage_process_combo.blockSignals(False)

    def _show_usage_fallback(self, day: str) -> None:
        """Explain a day with no minute rows instead of drawing an empty chart.

        Detail older than the retention window is pruned on purpose; the day
        total survives it, so the tab falls back to that number and says where
        minute detail begins rather than pretending nothing was recorded.
        """
        totals = self._day_totals(day)
        if totals is None:
            day_text = self.tr("No totals recorded for {day}.").format(day=day)
        else:
            sent, received = totals
            day_text = self.tr(
                "{day}: TX {tx}  RX {rx}  Total {total} (day totals only)"
            ).format(
                day=day,
                tx=format_data(sent),
                rx=format_data(received),
                total=format_data(sent + received),
            )
        first = self._first_sample_day()
        if first is None:
            detail = self.tr("No minute samples have been recorded yet.")
        else:
            detail = self.tr("Minute detail starts on {day}.").format(day=first)
        self.usage_fallback_label.setText(f"{day_text} {detail}")
        self.usage_fallback_label.setVisible(True)

    def _refresh_usage_tab(self, *_args) -> None:
        """Flush, query and redraw the usage tab. Runs only while it is visible."""
        if self._usage_db is None or self.tabs.currentIndex() != self._usage_tab_index:
            return
        # A read is exactly the moment the open minute must not stay unwritten:
        # the graph should include it, and there is no later tick that will.
        self._flush_traffic_samples(force=True)
        day = self.day_combo.currentData()
        if not day:
            self.usage_chart.set_series([], [], "", (0, MINUTES_PER_DAY), 1)
            self.usage_stats_label.clear()
            self.usage_fallback_label.setText(
                self.tr("No monitoring logs for this month yet.")
            )
            self.usage_fallback_label.setVisible(True)
            return
        today = date.today().isoformat()
        whole_day_index = self.usage_range_combo.findData(None)
        if day != today and self.usage_range_combo.currentIndex() != whole_day_index:
            # Collapse a stale preset before the window is computed: a past day
            # has no live hour, and leaving "Last 1 h" selected would be a lie.
            self.usage_range_combo.blockSignals(True)
            self.usage_range_combo.setCurrentIndex(whole_day_index)
            self.usage_range_combo.blockSignals(False)
        self.usage_range_combo.setEnabled(day == today)
        self._fill_usage_process_filter(day)
        span = self._usage_window(day)
        granularity = int(self.usage_granularity_combo.currentData() or 1)
        # Snap the window to the bucket grid so a bucket is either wholly in the
        # view or out of it, and a floor-rounded bucket label is never dropped at
        # the left edge after its minutes had already been summed into it.
        span = (span[0] - span[0] % granularity, span[1])
        # The window is applied here rather than in SQL so the query stays one
        # statement that the range preset does not have to be threaded through.
        rows = [
            row
            for row in self._sample_series(
                day, self.usage_process_combo.currentData(), granularity
            )
            if span[0] <= row[0] < span[1]
        ]
        if not rows:
            self.usage_chart.set_series([], [], day, span, granularity)
            self.usage_stats_label.clear()
            self._show_usage_fallback(day)
            return
        self.usage_fallback_label.setVisible(False)
        sent = [(minute, sent_kib) for minute, sent_kib, _received in rows]
        received = [(minute, received_kib) for minute, _sent, received_kib in rows]
        self.usage_chart.set_series(sent, received, day, span, granularity)
        total_sent = sum(value for _minute, value in sent)
        total_received = sum(value for _minute, value in received)
        busiest = max(rows, key=lambda row: row[1] + row[2])
        busiest_time = f"{busiest[0] // 60:02d}:{busiest[0] % 60:02d}"
        if granularity == 1:
            busiest_label = self.tr("busiest minute")
        else:
            busiest_label = self.tr("busiest {minutes} min bucket").format(minutes=granularity)
        self.usage_stats_label.setText(
            self.tr(
                "{day}: sent {sent}  received {received} — {busiest} at {time} with {total}"
            ).format(
                day=day,
                sent=format_data(total_sent),
                received=format_data(total_received),
                busiest=busiest_label,
                time=busiest_time,
                total=format_data(busiest[1] + busiest[2]),
            )
        )

    def _peers_query(self) -> list[dict]:
        """One dict per stored socket, with its owner and any cached peer name.

        ``process_sockets`` is the snapshot set: the capture replaces it per
        instance, so a row is what was true when the socket table was last read,
        not a live connection. ``dns_cache`` is keyed by the bare address while a
        stored peer may carry an IPv6 scope zone, so the join strips the zone the
        same way the resolver does. A socket whose instance row is missing keeps
        empty owner fields instead of disappearing.
        """
        if self._usage_db is None:
            return []
        rows = self._usage_db.execute(
            """SELECT s.peer_address, s.peer_port, s.netid, s.state,
                      s.local_address, s.local_port,
                      COALESCE(c.hostname, s.peer_hostname),
                      COALESCE(i.process_name, ''), s.pid, COALESCE(i.user_name, ''),
                      s.started_at, COALESCE(i.last_seen, ''), s.captured_at
               FROM process_sockets AS s
               LEFT JOIN process_instances AS i
                      ON i.pid = s.pid AND i.started_at = s.started_at
               LEFT JOIN dns_cache AS c
                      ON c.address = CASE
                           WHEN instr(s.peer_address, '%') > 0
                           THEN substr(s.peer_address, 1, instr(s.peer_address, '%') - 1)
                           ELSE s.peer_address END"""
        ).fetchall()
        return [dict(zip(PEER_FIELDS, row)) for row in rows]

    @staticmethod
    def _peer_is_listening(row: dict) -> bool:
        return row["peer_port"] in ("", "*")

    def _visible_peer_rows(self) -> tuple[list[dict], list[dict]]:
        """(all stored rows, the rows the current filter keeps)."""
        rows = self._peers_query()
        if not self.peers_running_check.isChecked():
            return rows, rows
        tracked = {
            (instance.pid, instance.started_at)
            for instance in self._tracked_instances.values()
        }
        return rows, [
            row for row in rows if (row["pid"], row["started_at"]) in tracked
        ]

    def _refresh_peers_tab(self, *_args) -> None:
        """Rebuild the peers table from the stored sockets, while it is visible."""
        if self._usage_db is None or self.tabs.currentIndex() != self._peers_tab_index:
            return
        all_rows, rows = self._visible_peer_rows()
        self.peers_table.setSortingEnabled(False)
        self.peers_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            port = QTableWidgetItem(row["peer_port"])
            if row["peer_port"].isdigit():
                port.setData(Qt.ItemDataRole.UserRole, float(row["peer_port"]))
            pid = NumericTableWidgetItem(str(row["pid"]))
            pid.setData(Qt.ItemDataRole.UserRole, float(row["pid"]))
            seen = QTableWidgetItem(row["last_seen"])
            # The table says when the process was last seen. The socket itself
            # was read at this moment, which can be a little earlier.
            seen.setToolTip(
                self.tr("Sockets captured at {time}").format(time=row["captured_at"])
            )
            peer = QTableWidgetItem(
                _format_endpoint(row["peer_address"], row["peer_port"])
            )
            # A long IPv6 endpoint is elided in the column, so the cell keeps
            # the whole thing where a reader can still get it.
            peer.setToolTip(peer.text())
            cells = (
                peer,
                QTableWidgetItem(row["hostname"] or ""),
                port,
                QTableWidgetItem(row["netid"]),
                QTableWidgetItem(row["state"]),
                QTableWidgetItem(
                    _format_endpoint(row["local_address"], row["local_port"])
                ),
                QTableWidgetItem(row["process_name"]),
                pid,
                QTableWidgetItem(row["user_name"]),
                seen,
            )
            for column, cell in enumerate(cells):
                self.peers_table.setItem(index, column, cell)
        self.peers_table.setSortingEnabled(True)
        connections = len(rows)
        peers = {
            row["peer_address"] for row in rows if not self._peer_is_listening(row)
        }
        processes = {(row["pid"], row["started_at"]) for row in rows}
        unresolved = sum(
            1
            for row in rows
            if not row["hostname"] and not self._peer_is_listening(row)
        )
        if connections:
            if self.peers_running_check.isChecked():
                template = self.tr(
                    "Showing {shown} of {stored} stored connection(s) from processes "
                    "still tracked: {peers} distinct peer(s), {processes} process(es), "
                    "{unresolved} without a name."
                )
                summary = template.format(
                    shown=connections,
                    stored=len(all_rows),
                    peers=len(peers),
                    processes=len(processes),
                    unresolved=unresolved,
                )
            else:
                template = self.tr(
                    "{connections} stored connection(s) to {peers} distinct peer(s) "
                    "from {processes} process(es); {unresolved} without a name."
                )
                summary = template.format(
                    connections=connections,
                    peers=len(peers),
                    processes=len(processes),
                    unresolved=unresolved,
                )
        elif all_rows:
            summary = self.tr(
                "No stored socket belongs to a process that is still tracked. Clear "
                "Only running processes to see all {stored} stored connection(s)."
            ).format(stored=len(all_rows))
        else:
            summary = self.tr(
                "No stored socket snapshots yet. Start monitoring to capture connection "
                "activity."
            )
        self.peers_summary_label.setText(summary)

    def _resolve_peer_names(self) -> None:
        """Queue name lookups for the unresolved peers on the visible rows."""
        all_rows, rows = self._visible_peer_rows()
        if not self._resolver.enabled:
            self.peers_status_label.setText(dns_disabled_hint())
            return
        sockets = [
            SocketState(
                row["netid"],
                row["state"],
                row["local_address"],
                row["local_port"],
                row["peer_address"],
                row["peer_port"],
            )
            for row in rows
            if not row["hostname"] and not self._peer_is_listening(row)
        ]
        queued = self._resolver.request(sockets)
        if queued:
            self.peers_status_label.setText(
                self.tr(
                    "{count} name lookup(s) queued; names appear as the answers arrive."
                ).format(count=queued)
            )
        elif rows:
            self.peers_status_label.setText(
                self.tr(
                    "No lookup was queued: every peer here is already named or cached, "
                    "or is a loopback, link-local, unspecified or listening address."
                )
            )
        else:
            self.peers_status_label.setText(
                self.tr("No stored socket to resolve on this tab.")
            )

    def _on_peer_name_resolved(self, _address: str, _hostname: str) -> None:
        """One name arrived; rebuild the table once the burst is over."""
        self.peer_name_timer.start()

    def start_monitoring(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        nethogs = shutil.which("nethogs")
        if not nethogs:
            QMessageBox.warning(
                self,
                self.tr("Missing dependency"),
                self.tr("Install nethogs before starting the monitor."),
            )
            return
        missing = missing_nethogs_capabilities(os.path.realpath(nethogs))
        if missing:
            QMessageBox.warning(
                self,
                self.tr("nethogs cannot capture packets"),
                self.tr(
                    "{} is missing {}.\n\nGrant them once with:\n\nsudo setcap {}=ep {}\n\n"
                    "A nethogs package upgrade replaces the binary and drops them again, so "
                    "re-apply this after upgrading."
                ).format(
                    nethogs,
                    ", ".join(missing),
                    ",".join(name for name, _bit in NETHOGS_CAPABILITIES),
                    nethogs,
                ),
            )
            return
        arguments = self._nethogs_arguments()
        interface = self.interface_input.text().strip()
        self.settings.set_interface(interface)
        # Remembered so a later comparison can tell whether the devices nethogs
        # bound to are still the devices that carry traffic.
        self._captured_interfaces = capture_interfaces()
        self._stdout_buffer = b""
        self._in_refresh_cycle = False
        self._pending_rows.clear()
        self._rows.clear()
        self.live_table.setRowCount(0)
        self._last_accounted_at = datetime.now()
        self.process.start(nethogs, arguments)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.interface_input.setEnabled(False)
        self._awaiting_first_output = True
        self.status_label.setText(self.tr("Starting nethogs…"))

    def _nethogs_arguments(self) -> list[str]:
        """The nethogs command line: text mode, one-second refresh, no count.

        No device argument means "every device", which is evaluated by nethogs
        when it starts. A name typed into the interface field pins one device.
        """
        arguments = ["-t", "-d", "1", "-c", "0"]
        interface = self.interface_input.text().strip()
        if interface:
            arguments.append(interface)
        return arguments

    def _check_interfaces(self) -> None:
        """Restart nethogs when the set of capture devices has changed.

        nethogs opens its capture handles once and never follows a device that
        appears later, so a tunnel that comes up after the monitor started is
        invisible: the VPN reconnects, every tunnelled process reads zero, and
        nothing explains the silence. Restarting the child re-binds the current
        devices through the same teardown path a user Stop takes.
        """
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        current = capture_interfaces()
        if not current:
            return
        pinned = self.interface_input.text().strip()
        if pinned:
            # A pinned device is the user's choice, but it can still be torn
            # down and recreated under the same name, which leaves the same
            # dead handle. Only its identity is worth comparing then.
            previous = tuple(pair for pair in self._captured_interfaces if pair[0] == pinned)
            current = tuple(pair for pair in current if pair[0] == pinned)
            if not current:
                return
        else:
            previous = self._captured_interfaces
        if current == previous:
            return
        self._captured_interfaces = capture_interfaces()
        added = sorted({name for name, _index in current} - {name for name, _index in previous})
        removed = sorted({name for name, _index in previous} - {name for name, _index in current})
        if not added and not removed:
            reason = f"{pinned} was recreated"
        else:
            reason = "; ".join(
                part for part in (
                    "added " + ", ".join(added) if added else "",
                    "removed " + ", ".join(removed) if removed else "",
                ) if part
            )
        self._restart_nethogs(reason)

    def _restart_nethogs(self, reason: str) -> None:
        """Replace the capture child without losing the interval it covered."""
        nethogs = shutil.which("nethogs")
        if not nethogs:
            return
        # Account for the interval with the rates still displayed, then drop
        # them: carrying stale rates into the new capture would re-count them.
        self._account_usage()
        self._restarting = True
        try:
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.process.terminate()
                if not self.process.waitForFinished(1500):
                    self.process.kill()
                    self.process.waitForFinished(1000)
        finally:
            self._restarting = False
        self._stdout_buffer = b""
        self._in_refresh_cycle = False
        self._pending_rows.clear()
        self._rows.clear()
        self.live_table.setRowCount(0)
        self._last_accounted_at = datetime.now()
        self.process.start(nethogs, self._nethogs_arguments())
        self.status_label.setText(
            self.tr("Monitoring. Capture restarted ({reason}).").format(reason=reason)
        )

    def stop_monitoring(self) -> None:
        self._captured_interfaces = ()

        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(1500):
                self.process.kill()
                self.process.waitForFinished(1000)
        self._account_usage()
        # The current minute is not over, but nothing will write it after this.
        self._flush_traffic_samples(force=True)
        self._last_accounted_at = None
        self._commit_pending_rows()
        self._refresh_view()
        # Stop is a user action, so the final numbers must not wait for the
        # throttle window.
        self._refresh_selected_day_view()

    def _read_output(self) -> None:
        self._stdout_buffer += bytes(self.process.readAllStandardOutput())
        lines = self._stdout_buffer.split(b"\n")
        self._stdout_buffer = lines.pop()[-65536:]
        for raw_line in lines:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.lower().startswith("refreshing:"):
                self._commit_pending_rows()
                self._in_refresh_cycle = True
                continue
            rate = parse_nethogs_line(line)
            if rate is None:
                continue
            if self._awaiting_first_output:
                self._awaiting_first_output = False
                self.status_label.setText(self.tr("Monitoring."))
            key = (rate.pid, rate.name)
            if self._in_refresh_cycle:
                self._pending_rows[key] = rate
            else:
                self._rows[key] = rate

    def _commit_pending_rows(self) -> None:
        if self._pending_rows:
            self._rows = self._pending_rows
            self._pending_rows = {}

    def _refresh_view(self) -> None:
        if self._usage_db is None:
            return
        self._account_usage()
        displayed = dict(self._rows)
        displayed.update(self._pending_rows)
        ordered = sorted(displayed.values(), key=lambda row: row.name.lower())
        total_sent = sum(row.sent_kib_s for row in ordered)
        total_received = sum(row.received_kib_s for row in ordered)
        total = total_sent + total_received
        self.total_label.setText(self.tr("Total: {}").format(format_rate(total)))
        self.tx_label.setText(self.tr("Total TX: {}").format(format_rate(total_sent)))
        self.rx_label.setText(self.tr("Total RX: {}").format(format_rate(total_received)))
        self.chart.set_sample(total)
        self._update_today_total()
        # Selectors are rebuilt only when the logged days change; the selected
        # day's total line is one row read and follows every tick, while the two
        # tables behind it are re-rendered on a slower cadence so a running
        # transfer still moves the day total without rebuilding them every second.
        rebuilt = self._refresh_history_selectors()
        self._update_selected_day_total()
        if rebuilt or self._day_view_due():
            self._refresh_selected_day_view(update_total=False)
            # The peers table is a snapshot read of the same slow-changing data,
            # so it rides this cadence and not the one-second tick. While its
            # tab is hidden the call returns before issuing a query.
            self._refresh_peers_tab()
        if rebuilt:
            # The day list moved under the selection (midnight rollover or a new
            # month), so the graph follows it. Happens once a day, not per tick.
            self._refresh_usage_tab()
        self._log_process_instances(ordered)
        self._flush_last_seen()
        selected_pid = self._selected_pid()
        process_totals = {
            (name, pid): (sent, received)
            for name, pid, sent, received in self._usage_db.execute(
                "SELECT process_name, pid, sent_kib, received_kib FROM process_usage WHERE day = ?",
                (date.today().isoformat(),),
            )
        }
        self.live_table.setSortingEnabled(False)
        self.live_table.setRowCount(len(ordered))
        for index, process_rate in enumerate(ordered):
            sent_total, received_total = process_totals.get((process_rate.name, process_rate.pid), (0.0, 0.0))
            cells = (QTableWidgetItem(process_rate.name), NumericTableWidgetItem(str(process_rate.pid)),
                     NumericTableWidgetItem(format_rate(process_rate.sent_kib_s)),
                     NumericTableWidgetItem(format_rate(process_rate.received_kib_s)),
                     NumericTableWidgetItem(format_data(sent_total)), NumericTableWidgetItem(format_data(received_total)))
            values = (None, process_rate.pid, process_rate.sent_kib_s, process_rate.received_kib_s, sent_total, received_total)
            for column, cell in enumerate(cells):
                self.live_table.setItem(index, column, cell)
                if values[column] is not None:
                    cell.setData(Qt.ItemDataRole.UserRole, values[column])
        self.live_table.setSortingEnabled(True)
        if selected_pid is not None:
            for index in range(self.live_table.rowCount()):
                if self._row_identity(index)[0] == selected_pid:
                    self.live_table.selectRow(index)
                    break
        self._update_details_button()

    def _live_table_menu(self, position) -> None:
        item = self.live_table.itemAt(position)
        if item is None:
            return
        pid, name = self._row_identity(item.row())
        if pid is None:
            return
        menu = QMenu(self)
        action = menu.addAction(self.tr("Process details…"))
        action.triggered.connect(lambda: self._open_process_details(pid, name))
        menu.exec(self.live_table.viewport().mapToGlobal(position))

    def _update_details_button(self) -> None:
        """Enable Details only while the selected row resolves to a real PID."""
        self.details_button.setEnabled(self._selected_pid() is not None)

    def _show_selected_details(self, _checked: bool = False) -> None:
        """Open the details report for the row the user selected."""
        self._show_process_details(self.live_table.currentRow())

    def _selected_pid(self) -> int | None:
        """Return the PID of the selected live row, or None if no usable row is selected."""
        row = self.live_table.currentRow()
        if row < 0:
            return None
        return self._row_identity(row)[0]

    def _show_process_details(self, row: int, _column: int = 0) -> None:
        pid, name = self._row_identity(row)
        if pid is not None:
            self._open_process_details(pid, name)

    def _row_identity(self, row: int) -> tuple[int | None, str]:
        pid_item = self.live_table.item(row, 1)
        name_item = self.live_table.item(row, 0)
        if pid_item is None:
            return None, ""
        try:
            return int(pid_item.text()), name_item.text() if name_item else ""
        except ValueError:
            return None, ""

    def _open_process_details(self, pid: int, name: str) -> None:
        ProcessDetailsDialog(
            pid, name, self, lookup=self._lookup_logged_instance, resolver=self._resolver
        ).exec()

    def _process_finished(self, _exit_code: int, _exit_status) -> None:
        if self._restarting:
            # This exit is our own capture restart; a replacement child is about
            # to start, so the window must not report a stop.
            return
        self._account_usage()
        self._flush_traffic_samples(force=True)
        self._last_accounted_at = None
        self._commit_pending_rows()
        self._refresh_view()
        self._refresh_selected_day_view()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.interface_input.setEnabled(True)
        if self._awaiting_first_output:
            self._awaiting_first_output = False
            self.status_label.setText(self.tr("Monitor stopped before producing output."))
        elif self._rows:
            self.status_label.setText(self.tr("Stopped."))
        else:
            self.status_label.setText(self.tr("Stopped — no process data received."))

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._last_accounted_at = None
            self.status_label.setText(
                self.tr("Could not start monitor: {error}").format(
                    error=self.process.errorString()
                )
            )
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.interface_input.setEnabled(True)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt event naming
        self.refresh_timer.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(500)
            self._account_usage()
        # Closing the window is the last chance to write the open minute; the
        # buffer must be drained before the connection is closed below.
        self._flush_traffic_samples(force=True)
        self._last_accounted_at = None
        self._flush_last_seen(force=True)
        self._resolver.shutdown()
        if self._usage_db is not None:
            self._usage_db.close()
            self._usage_db = None
        event.accept()


def install_translation(app: QApplication, locale: str) -> QTranslator | None:
    """Load the catalogue for a stored locale, if one has been built."""
    if not locale:
        return None
    folder = Path(__file__).resolve().parent / "translations"
    translator = QTranslator(app)
    if translator.load(f"network-process_{locale}", str(folder)):
        app.installTranslator(translator)
        return translator
    return None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("network-process")
    app.setOrganizationName("network-process")
    settings = Settings()
    translator = install_translation(app, settings.locale())
    window = NetworkProcessWindow(settings)
    window.show()
    exit_code = app.exec()
    if translator is not None:
        app.removeTranslator(translator)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
