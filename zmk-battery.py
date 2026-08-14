#!/usr/bin/env python3
"""Read per-part battery levels from a ZMK dongle over USB HID.

The dongle exposes one HID battery report per keyboard part (ZMK PR #3458,
applied via config/zephyr/patches.yml). Report IDs are 0x04 + the devicetree
child index of config/boards/shields/totem/totem_dongle.overlay, and the hidden
dongle child is omitted, so the wire carries:

    0x05 -> peripheral 0    0x06 -> peripheral 1    0x07 -> peripheral 2

Linux only registers *every* battery of a multi-battery HID device from kernel
7.1 onward; before that just the first one shows up in /sys/class/power_supply.
This script sidesteps the kernel battery layer and asks the device directly with
a HIDIOCGINPUT ioctl, so it works on any kernel.

Needs read/write access to /dev/hidraw*, which is root-only by default. Install
the shipped udev rule once:

    sudo cp 99-zmk-hidraw.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

Usage:
    ./zmk-battery.py              # live levels + discharge rate table (default)
    ./zmk-battery.py --levels     # left 78%  trackball 75%  right 82%
    ./zmk-battery.py --min        # 78        (worst part, for a status bar)
    ./zmk-battery.py --json       # machine readable
    ./zmk-battery.py --log        # also append to ~/.local/state/zmk-battery.csv
    ./zmk-battery.py --rate       # rates from the log only, no dongle needed
    ./zmk-battery.py --graph=x.svg # graph elsewhere (.svg writes directly, else PNG)
    ./zmk-battery.py --labels a,b,c

Logging, for discharge-rate analysis: --log appends one row per reading, with a
column per part

    timestamp,epoch,left,trackball,right
    2026-08-13T15:04:21+02:00,1786000000,63,75,82

By default a row is only written when some part's level changed, since the firmware
itself only pushes on change and sampling stops while a part is idle - repeating
identical rows every minute adds no information. Use --log-all for fixed-cadence
rows instead.

A part reading 0 is written as an empty cell, not 0: it means "has not reported
since the dongle booted", so it is missing data rather than a discharge cliff.
Column order follows --labels and is taken from the existing header on later runs,
matching by name, so renaming a part leaves its column blank rather than shifting
the others.

--rate reads that log back and reports a discharge rate per part, fitted by least
squares over the current discharge segment - the samples since the part last went
*up*, because readings from before a charge do not describe the present curve. It
needs no dongle attached, being a pure file read.
"""

import argparse
import csv
import datetime
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape

VENDOR_ID = 0x1D50
PRODUCT_ID = 0x615E

BATTERY_REPORT_ID_BASE = 0x04
REPORT_LEN = 3  # report_id, battery_level, charging

DEFAULT_LOG = "~/.local/state/zmk-battery.csv"
# Part columns are appended to these two. Charging is deliberately not logged: it
# needs a zmk,battery-charge-status node wired to a charger status pin, which the
# XIAO nRF52840 does not expose, so it would be a column of constant zeros.
CSV_FIXED_COLUMNS = ["timestamp", "epoch"]

# Shortest window --rate will extrapolate from. A single 1% step logged a minute
# apart fits to a rate of ~30%/h, which is arithmetically true and useless, so
# short windows report no rate at all rather than a confident wrong one.
MIN_RATE_SPAN_HOURS = 2.0

# How big a rise counts as "this part was charged", restarting the fit window.
# A half in use flutters +-1-2%: load sags the cell voltage and it recovers
# between samples, and the voltage-to-charge lookup turns that into a 1% step. At
# a 1% threshold that flutter reset the window every minute or two, so an active
# half could never accumulate enough history to fit. Real charging steps far more.
CHARGE_STEP_PCT = 5

# Next to this script rather than in ~/.local/state, so the graph sits with the
# config it describes. Written on every run unless --no-graph. A .svg path is
# written directly; any other extension goes through an SVG -> PNG converter.
DEFAULT_GRAPH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "zmk-battery.png"
)
GRAPH_PNG_WIDTH = 1920  # 2x the SVG's logical width, so text stays crisp
# Tokyo Night, to match the polybar theme this normally feeds.
GRAPH_BG = "#1a1b26"
GRAPH_FG = "#c0caf5"
GRAPH_GRID = "#565f89"
GRAPH_COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#f7768e"]

# Peripheral slots are assigned in split connection order, so these names are a
# convention, not something the firmware guarantees. Override with --labels.
# Verified on this keyboard with the &bapp behavior (Media layer, T and Y), which
# types the level of the half the key is physically on: the right half came back
# as slot 2, not slot 1.
DEFAULT_LABELS = ["left", "trackball", "right"]

# Battery System usage page (0x85) followed immediately by HID_REPORT_ID(n),
# which is how the generated descriptor introduces each battery collection.
DESC_PREFIX = bytes([0x05, 0x85, 0x85])


def _ioc(direction, type_char, nr, size):
    return (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr


def hidiocginput(size):
    # _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x0A, size) from linux/hidraw.h
    return _ioc(3, "H", 0x0A, size)


def hidraw_nodes():
    """Yield (device_path, sysfs_path) for every hidraw node of the dongle."""
    for sysfs in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent = os.path.join(sysfs, "device", "uevent")
        try:
            with open(uevent) as handle:
                text = handle.read()
        except OSError:
            continue
        # HID_ID=0003:00001D50:0000615E
        for line in text.splitlines():
            if not line.startswith("HID_ID="):
                continue
            parts = line.split("=", 1)[1].split(":")
            if len(parts) != 3:
                continue
            try:
                vid, pid = int(parts[1], 16), int(parts[2], 16)
            except ValueError:
                continue
            if (vid, pid) == (VENDOR_ID, PRODUCT_ID):
                yield os.path.join("/dev", os.path.basename(sysfs)), sysfs


def battery_report_ids(sysfs):
    """Parse the report descriptor for the battery report IDs it declares."""
    try:
        with open(os.path.join(sysfs, "device", "report_descriptor"), "rb") as handle:
            desc = handle.read()
    except OSError:
        return []

    ids, offset = [], 0
    while True:
        offset = desc.find(DESC_PREFIX, offset)
        if offset == -1 or offset + len(DESC_PREFIX) >= len(desc):
            return ids
        ids.append(desc[offset + len(DESC_PREFIX)])
        offset += len(DESC_PREFIX)


def read_battery(fd, report_id):
    """Fetch one battery report; returns (level, charging)."""
    buf = bytearray(REPORT_LEN)
    buf[0] = report_id
    fcntl.ioctl(fd, hidiocginput(REPORT_LEN), buf)
    return buf[1], bool(buf[2])


def collect(labels):
    for device, sysfs in hidraw_nodes():
        report_ids = battery_report_ids(sysfs)
        if not report_ids:
            continue

        try:
            fd = os.open(device, os.O_RDWR)
        except PermissionError:
            sys.exit(
                f"{device}: permission denied. Install the udev rule:\n"
                "  sudo cp 99-zmk-hidraw.rules /etc/udev/rules.d/\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger"
            )
        except OSError as err:
            continue

        try:
            parts = []
            for report_id in report_ids:
                try:
                    level, charging = read_battery(fd, report_id)
                except OSError as err:
                    print(f"report 0x{report_id:02x}: {err}", file=sys.stderr)
                    continue
                index = report_id - BATTERY_REPORT_ID_BASE - 1
                if index < 0:
                    # Report 0x04 is the central's own slot, which firmware built
                    # with SPLIT_REPORT_LOWEST_CHARGE=y uses for the aggregate.
                    name = "lowest"
                elif index < len(labels):
                    name = labels[index]
                else:
                    name = f"part{index}"
                parts.append(
                    {
                        "name": name,
                        "level": level,
                        "charging": charging,
                        "report_id": report_id,
                    }
                )
        finally:
            os.close(fd)

        if parts:
            return parts

    sys.exit(
        f"no ZMK dongle battery reports found ({VENDOR_ID:04x}:{PRODUCT_ID:04x}).\n"
        "Is the dongle plugged in, and does its firmware have "
        "CONFIG_ZMK_BATTERY_REPORTING_USB=y?"
    )


def append_csv(path, parts, log_all=False):
    """Append one row holding every part, unless nothing changed since the last."""
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    epoch = int(time.time())
    levels = {part["name"]: part["level"] for part in parts}

    # a+ so the file is created if absent; the lock keeps a manual run from
    # interleaving with the polybar one.
    with open(path, "a+", newline="") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)

        handle.seek(0)
        rows = list(csv.reader(handle))
        writer = csv.writer(handle)

        if rows:
            # Keep the established column order, so a run with different --labels
            # leaves unknown parts blank instead of shifting existing columns.
            columns = rows[0][len(CSV_FIXED_COLUMNS) :]
        else:
            columns = list(levels)
            writer.writerow(CSV_FIXED_COLUMNS + columns)

        # 0 means "not reported yet" - empty cell, not a drop to zero.
        row = [stamp, epoch] + [
            str(levels[name]) if levels.get(name) else "" for name in columns
        ]

        previous = rows[-1] if len(rows) > 1 else None
        if not log_all and previous and previous[len(CSV_FIXED_COLUMNS) :] == row[2:]:
            return 0

        writer.writerow(row)
        return 1


def read_log(path, strict=True):
    """Return {part: [(epoch, level), ...]} from a --log CSV, oldest first.

    With strict=False an absent or header-only file yields {} instead of exiting,
    so a status-bar call is never killed by a log that has not filled in yet.
    """
    path = os.path.expanduser(path)
    try:
        with open(path, newline="") as handle:
            rows = list(csv.reader(handle))
    except FileNotFoundError:
        if not strict:
            return {}
        sys.exit(f"no log at {path} yet - run with --log first (see --help)")

    if len(rows) < 2:
        if not strict:
            return {}
        sys.exit(f"{path} has no readings yet")

    columns = rows[0][len(CSV_FIXED_COLUMNS) :]
    series = {name: [] for name in columns}
    for row in rows[1:]:
        if len(row) != len(CSV_FIXED_COLUMNS) + len(columns):
            continue
        try:
            epoch = int(row[1])
        except ValueError:
            continue
        for name, cell in zip(columns, row[len(CSV_FIXED_COLUMNS) :]):
            if cell:  # blank = that part had not reported at this timestamp
                series[name].append((epoch, int(cell)))
    return series


def discharge_segment(samples):
    """Samples since the part was last charged, i.e. the present discharge run.

    A rise only counts as charging past CHARGE_STEP_PCT, so measurement flutter on
    a half in use does not keep truncating the window.
    """
    start = 0
    for index in range(1, len(samples)):
        if samples[index][1] - samples[index - 1][1] >= CHARGE_STEP_PCT:
            start = index
    return samples[start:]


def fit_line(samples):
    """Least-squares fit; returns (slope_per_second, level_at_first_sample)."""
    if len(samples) < 2:
        return None

    # Times are rebased to the first sample: raw epochs squared exceed float64's
    # exact integer range, which would wreck the slope.
    t0 = samples[0][0]
    times = [epoch - t0 for epoch, _ in samples]
    levels = [level for _, level in samples]
    count = len(samples)

    mean_t = sum(times) / count
    mean_l = sum(levels) / count
    denominator = sum((t - mean_t) ** 2 for t in times)
    if denominator == 0:
        return None

    slope = sum((t - mean_t) * (l - mean_l) for t, l in zip(times, levels)) / denominator
    return slope, mean_l - slope * mean_t


def fit_rate(samples):
    """Least-squares %/hour of discharge (positive = draining), or None."""
    fit = fit_line(samples)
    if fit is None:
        return None
    return -fit[0] * 3600  # sign flipped so draining reads positive


def format_hours(hours):
    if hours >= 48:
        return f"{int(hours // 24)}d {int(hours % 24)}h"
    if hours >= 1:
        return f"{hours:.1f}h"
    return f"{int(hours * 60)}m"


def compute_rates(series, live_levels=None):
    """Build a rate row per part; live_levels overrides the logged 'now' value."""
    results = []
    names = list(series)
    for name in (live_levels or {}):
        if name not in series:
            names.append(name)  # a part with readings but no history yet

    for name in names:
        samples = series.get(name, [])
        segment = discharge_segment(samples)
        span = (segment[-1][0] - segment[0][0]) / 3600 if len(segment) > 1 else 0.0
        # Refuse to extrapolate from a window too short to mean anything.
        rate = fit_rate(segment) if span >= MIN_RATE_SPAN_HOURS else None

        level = None
        if live_levels and live_levels.get(name):
            level = live_levels[name]
        elif samples:
            level = samples[-1][1]

        results.append(
            {
                "part": name,
                "level": level,
                "rate_pct_per_hour": round(rate, 3) if rate is not None else None,
                "hours_to_empty": (
                    round(level / rate, 1) if rate and rate > 0 and level else None
                ),
                "samples": len(segment),
                "span_hours": round(span, 2),
            }
        )
    return results


def print_rate_table(results):
    print(f"{'part':<12}{'now':>5}{'rate':>12}{'empty in':>12}{'window':>10}  samples")
    for row in results:
        rate = row["rate_pct_per_hour"]
        empty = row["hours_to_empty"]
        print(
            f"{row['part']:<12}"
            f"{(str(row['level']) + '%') if row['level'] is not None else '--':>5}"
            f"{(f'{rate:.2f}%/h' if rate is not None else '--'):>12}"
            f"{(format_hours(empty) if empty else '--'):>12}"
            f"{format_hours(row['span_hours']) if row['span_hours'] else '--':>10}"
            f"  {row['samples']}"
        )

    if any(row["rate_pct_per_hour"] is None for row in results):
        sys.stdout.flush()  # keep the note below the table when both are piped
        print(
            f"\nParts without a rate have under {MIN_RATE_SPAN_HOURS:g}h of history. "
            "Change-only logging records a\nrow per 1% step, so a useful fit needs a "
            "day or two of accumulated readings.",
            file=sys.stderr,
        )


def report_rates(path, as_json=False):
    results = compute_rates(read_log(path))
    if as_json:
        print(json.dumps(results))
    else:
        print_rate_table(results)


def find_tool(name):
    """Locate a converter without depending on PATH; polybar starts with a bare
    environment, and mise-installed binaries are only reachable via shims."""
    return shutil.which(name) or next(
        (
            candidate
            for candidate in (f"/usr/bin/{name}", f"/usr/local/bin/{name}")
            if os.path.exists(candidate)
        ),
        None,
    )


def svg_to_png(svg, path):
    """Convert SVG text to a PNG using whichever renderer is installed."""
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as handle:
        handle.write(svg)
        temp_svg = handle.name

    commands = []
    if exe := find_tool("rsvg-convert"):
        commands.append([exe, "-w", str(GRAPH_PNG_WIDTH), temp_svg, "-o", path])
    if exe := find_tool("inkscape"):
        commands.append(
            [
                exe,
                temp_svg,
                "--export-type=png",
                f"--export-width={GRAPH_PNG_WIDTH}",
                f"--export-filename={path}",
            ]
        )
    for name in ("magick", "convert"):
        if exe := find_tool(name):
            commands.append([exe, "-density", "192", temp_svg, path])
            break

    try:
        for command in commands:
            try:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except (OSError, subprocess.CalledProcessError):
                continue
    finally:
        os.unlink(temp_svg)

    raise RuntimeError(
        "no SVG to PNG converter found - install librsvg2-bin, or pass a .svg path"
    )


def write_graph(series, results, path):
    """Render the discharge curves. SVG is built in-process, no plotting deps."""
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    stamps = [epoch for samples in series.values() for epoch, _ in samples]
    if not stamps:
        raise ValueError("no samples to plot")
    t_min, t_max = min(stamps), max(stamps)
    if t_max == t_min:
        t_max = t_min + 3600  # a single reading still gets a sane axis

    width, height = 960, 460
    left, right, top, bottom = 56, 168, 44, 46
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_of(epoch):
        return left + (epoch - t_min) / (t_max - t_min) * plot_w

    def y_of(level):
        return top + (100 - level) / 100 * plot_h

    rate_by_part = {row["part"]: row["rate_pct_per_hour"] for row in results}
    span_hours = (t_max - t_min) / 3600
    tick_format = "%H:%M" if span_hours <= 36 else "%d %b"

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{GRAPH_BG}"/>',
        f'<text x="{left}" y="26" fill="{GRAPH_FG}" font-size="15">'
        f"ZMK Totem discharge - {escape(datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M'))}"
        "</text>",
    ]

    for level in range(0, 101, 20):
        y = y_of(level)
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="{GRAPH_GRID}" stroke-width="1" stroke-opacity="0.35"/>'
        )
        out.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" fill="{GRAPH_GRID}" '
            f'font-size="12" text-anchor="end">{level}%</text>'
        )

    for index in range(5):
        epoch = t_min + (t_max - t_min) * index / 4
        x = x_of(epoch)
        label = datetime.datetime.fromtimestamp(epoch).strftime(tick_format)
        out.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 20}" fill="{GRAPH_GRID}" '
            f'font-size="12" text-anchor="middle">{escape(label)}</text>'
        )

    for index, (name, samples) in enumerate(series.items()):
        if not samples:
            continue
        color = GRAPH_COLORS[index % len(GRAPH_COLORS)]

        points = " ".join(f"{x_of(e):.1f},{y_of(l):.1f}" for e, l in samples)
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            'stroke-width="2" stroke-linejoin="round"/>'
        )
        for epoch, level in samples:
            out.append(
                f'<circle cx="{x_of(epoch):.1f}" cy="{y_of(level):.1f}" r="2.6" '
                f'fill="{color}"/>'
            )

        # Dashed least-squares trend over the current discharge segment, extended
        # to the right edge so the slope reads as a projection.
        segment = discharge_segment(samples)
        fit = fit_line(segment) if rate_by_part.get(name) is not None else None
        if fit:
            slope, intercept = fit
            t0 = segment[0][0]
            for start, end in ((segment[0][0], t_max),):
                y_start = intercept + slope * (start - t0)
                y_end = intercept + slope * (end - t0)
                y_start = max(0.0, min(100.0, y_start))
                y_end = max(0.0, min(100.0, y_end))
                out.append(
                    f'<line x1="{x_of(start):.1f}" y1="{y_of(y_start):.1f}" '
                    f'x2="{x_of(end):.1f}" y2="{y_of(y_end):.1f}" stroke="{color}" '
                    'stroke-width="1.5" stroke-dasharray="6 5" stroke-opacity="0.7"/>'
                )

        legend_y = top + 6 + index * 22
        rate = rate_by_part.get(name)
        caption = f"{name}  {rate:.2f}%/h" if rate is not None else f"{name}  --"
        out.append(
            f'<rect x="{left + plot_w + 22}" y="{legend_y - 9}" width="11" height="11" '
            f'fill="{color}"/>'
        )
        out.append(
            f'<text x="{left + plot_w + 40}" y="{legend_y}" fill="{GRAPH_FG}" '
            f'font-size="13">{escape(caption)}</text>'
        )

    out.append("</svg>")
    svg = "\n".join(out) + "\n"

    if path.lower().endswith(".svg"):
        with open(path, "w") as handle:
            handle.write(svg)
    else:
        svg_to_png(svg, path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit JSON")
    output.add_argument(
        "--levels",
        action="store_true",
        help="one plain line of current levels, without the rate table",
    )
    output.add_argument(
        "--min",
        action="store_true",
        help="print only the lowest level, replacing the aggregate that "
        "CONFIG_ZMK_BATTERY_REPORTING_SPLIT_REPORT_LOWEST_CHARGE used to provide",
    )
    parser.add_argument(
        "--labels",
        default=",".join(DEFAULT_LABELS),
        help="comma-separated names for peripherals 0,1,2 "
        f"(default: {','.join(DEFAULT_LABELS)})",
    )
    parser.add_argument(
        "--log",
        nargs="?",
        const=DEFAULT_LOG,
        metavar="CSV",
        help=f"append readings to a CSV file (default: {DEFAULT_LOG}). "
        "Combines with any output mode",
    )
    parser.add_argument(
        "--log-all",
        action="store_true",
        help="log every reading, not only changed values",
    )
    parser.add_argument(
        "--rate",
        nargs="?",
        const=DEFAULT_LOG,
        metavar="CSV",
        help="report the discharge rate per part from a log file instead of "
        f"reading the dongle (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--graph",
        nargs="?",
        const=DEFAULT_GRAPH,
        metavar="SVG",
        help=f"where to save the discharge graph (default: {DEFAULT_GRAPH}, "
        "written on every run)",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        help="skip writing the discharge graph",
    )
    args = parser.parse_args()

    def save_graph(series, results, log_path):
        if args.no_graph:
            return

        target = os.path.expanduser(args.graph or DEFAULT_GRAPH)
        # The status bar calls this every minute, but the log only gains a row when
        # a level actually changes, so re-rendering an identical image is waste.
        # An explicit --graph always re-renders.
        if not args.graph and os.path.exists(target):
            try:
                if os.path.getmtime(target) >= os.path.getmtime(log_path):
                    return
            except OSError:
                pass

        try:
            written = write_graph(series, results, target)
        except (OSError, ValueError, RuntimeError) as err:
            print(f"graph not written: {err}", file=sys.stderr)
        else:
            # Only announce it when explicitly asked, so the default path stays
            # quiet for the once-a-minute status bar call.
            if args.graph:
                print(f"graph: {written}", file=sys.stderr)

    if args.rate:
        series = read_log(args.rate)
        results = compute_rates(series)
        if args.json:
            print(json.dumps(results))
        else:
            print_rate_table(results)
        save_graph(series, results, os.path.expanduser(args.rate))
        return

    parts = collect([label.strip() for label in args.labels.split(",")])
    # A part that is asleep or disconnected reports 0; it is not a flat cell.
    live = [part for part in parts if part["level"] > 0]

    if args.log:
        try:
            append_csv(args.log, parts, log_all=args.log_all)
        except OSError as err:
            # Never let logging break the reading itself, e.g. a status bar call.
            print(f"logging to {args.log} failed: {err}", file=sys.stderr)

    levels_line = "  ".join(
        f"{part['name']} {part['level']}%{'+' if part['charging'] else ''}"
        for part in parts
    )

    # History feeds both the default table and the graph, so read it once for
    # every output mode - the graph must refresh on the status bar's calls too.
    log_path = os.path.expanduser(args.log or DEFAULT_LOG)
    series = read_log(log_path, strict=False)
    live_levels = {part["name"]: part["level"] for part in parts}
    results = compute_rates(series, live_levels) if series else None

    if args.json:
        print(json.dumps(parts))
    elif args.min:
        print(min((part["level"] for part in live), default=0))
    elif args.levels:
        print(levels_line)
    elif results:
        print_rate_table(results)
    else:
        print(levels_line)
        print(
            f"\nNo history at {log_path} yet - run with --log to start recording, "
            "then\nthis view gains discharge rates.",
            file=sys.stderr,
        )

    if results:
        save_graph(series, results, log_path)


if __name__ == "__main__":
    main()
