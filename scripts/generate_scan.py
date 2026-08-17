#!/usr/bin/env python3
"""
Generate an animated "hologram scan" SVG from a GitHub contribution calendar.

The output SVG loops through phases:
  1. blank grid
  2. a beam sweeps left to right, lighting the days you actually committed
  3. hold
  4. a second sweep morphs the grid into a mask (your avatar, or text)
  5. hold, then fade back to blank

No JavaScript - pure SMIL <animate>, which GitHub renders inside a README
<img> tag. CSS and <script> are both stripped by GitHub's markdown
sanitiser, so SMIL is the only option that survives.

Usage:
    python scripts/generate_scan.py --user Nico0429 --out dist/scan.svg
    python scripts/generate_scan.py --user Nico0429 --mask text --text NICO
    python scripts/generate_scan.py --user Nico0429 --mask none   # commits only
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import requests

ROWS = 7
CELL = 11
GAP = 3
STEP = CELL + GAP
PAD = 18

# ---------------------------------------------------------------------------
# Levels 1-4 are GitHub's own dark-mode contribution greens, so phase 1 reads
# as the real graph. Phase 2 uses a brighter phosphor ramp to signal that the
# mask is a projection rather than data.
# ---------------------------------------------------------------------------
EMPTY = "#161b22"
COMMIT_COLORS = {1: "#0e4429", 2: "#006d32", 3: "#26a641", 4: "#39d353"}
MASK_COLORS = {1: "#2ea043", 2: "#56d364", 3: "#7ee787", 4: "#d7ffdf"}
BG = "#010409"
BEAM = "#39d353"

# ---------------------------------------------------------------------------
# Timeline, as fractions of the total loop duration.
# ---------------------------------------------------------------------------
SWEEP_1 = (0.02, 0.22)   # beam reveals commits
HOLD_1 = (0.22, 0.44)
SWEEP_2 = (0.46, 0.66)   # beam morphs to mask
HOLD_2 = (0.66, 0.88)
FADE = (0.90, 0.98)


# ---------------------------------------------------------------------------
# 5x7 bitmap font, for --mask text. Each glyph is 7 rows of 5 chars.
# ---------------------------------------------------------------------------
FONT_5X7 = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10011", "01111"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    " ": ["00000"] * 7,
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
}


@dataclass
class Day:
    row: int          # 0 = Sunday
    col: int          # week index
    level: int        # 0-4, GitHub's own bucket
    count: int
    date: str


# ---------------------------------------------------------------------------
# Step 1 - pull the calendar.
# ---------------------------------------------------------------------------
def fetch_calendar(user: str) -> list[Day]:
    """Scrape the public contributions fragment. No auth token needed.

    GitHub serves the calendar as an HTML <table> where each <td> carries
    data-date, data-level (0-4) and an id of the form
    contribution-day-component-{weekday}-{week}. That id is what gives us
    grid coordinates for free.
    """
    url = f"https://github.com/users/{user}/contributions"
    resp = requests.get(
        url,
        headers={
            "User-Agent": "contribution-scan-generator",
            "Accept": "text/html",
        },
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    cell_re = re.compile(
        r'<td[^>]*data-date="(\d{4}-\d{2}-\d{2})"'
        r'[^>]*id="contribution-day-component-(\d+)-(\d+)"'
        r'[^>]*data-level="(\d)"'
    )
    tip_re = re.compile(
        r'<tool-tip[^>]*for="contribution-day-component-(\d+)-(\d+)"[^>]*>(.*?)</tool-tip>',
        re.S,
    )

    counts: dict[tuple[int, int], int] = {}
    for m in tip_re.finditer(html):
        text = re.sub(r"\s+", " ", m.group(3)).strip()
        head = re.match(r"(\d+|No)", text)
        n = 0 if (head and head.group(1) == "No") else (int(head.group(1)) if head else 0)
        counts[(int(m.group(1)), int(m.group(2)))] = n

    days: list[Day] = []
    for m in cell_re.finditer(html):
        row, col = int(m.group(2)), int(m.group(3))
        days.append(
            Day(
                row=row,
                col=col,
                level=int(m.group(4)),
                count=counts.get((row, col), 0),
                date=m.group(1),
            )
        )

    if not days:
        raise RuntimeError(
            f"Parsed zero cells from {url} - GitHub likely changed its markup. "
            "Check the data-date / data-level attributes on the <td> elements."
        )
    return days


# ---------------------------------------------------------------------------
# Step 2 - build the mask grid.
# ---------------------------------------------------------------------------
def mask_from_image(path_or_url: str, cols: int) -> dict[tuple[int, int], int]:
    """Downsample an image to cols x 7 and bucket luminance into levels 0-4.

    Seven rows is coarse. High-contrast subjects survive; detailed
    photographs mostly do not. The autocontrast stretch below buys back
    some legibility.
    """
    from PIL import Image, ImageOps

    if path_or_url.startswith(("http://", "https://")):
        raw = requests.get(
            path_or_url,
            headers={"User-Agent": "contribution-scan-generator"},
            timeout=30,
        )
        raw.raise_for_status()
        img = Image.open(io.BytesIO(raw.content))
    else:
        img = Image.open(path_or_url)

    img = img.convert("L")
    # Centre-crop to the grid's aspect ratio so the subject isn't squashed.
    target_ratio = cols / ROWS
    w, h = img.size
    if w / h > target_ratio:
        new_w = int(h * target_ratio)
        img = img.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
    else:
        new_h = int(w / target_ratio)
        img = img.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))

    img = ImageOps.autocontrast(img, cutoff=6)
    img = img.resize((cols, ROWS), Image.LANCZOS)

    mask: dict[tuple[int, int], int] = {}
    for c in range(cols):
        for r in range(ROWS):
            v = img.getpixel((c, r))
            if v < 56:
                level = 0
            elif v < 112:
                level = 1
            elif v < 168:
                level = 2
            elif v < 216:
                level = 3
            else:
                level = 4
            if level:
                mask[(r, c)] = level
    return mask


def mask_from_text(text: str, cols: int) -> dict[tuple[int, int], int]:
    """Render text as 5x7 glyphs, centred in the grid."""
    text = text.upper()
    glyphs = [FONT_5X7.get(ch, FONT_5X7[" "]) for ch in text]
    width = sum(len(g[0]) + 1 for g in glyphs) - 1
    if width > cols:
        raise SystemExit(
            f"'{text}' needs {width} columns but the grid is only {cols} wide. "
            "Use fewer characters."
        )

    offset = (cols - width) // 2
    mask: dict[tuple[int, int], int] = {}
    x = offset
    for glyph in glyphs:
        for r, line in enumerate(glyph):
            for i, ch in enumerate(line):
                if ch == "1":
                    mask[(r, x + i)] = 4
        x += len(glyph[0]) + 1
    return mask


def default_avatar_url(user: str) -> str:
    return f"https://github.com/{user}.png?size=200"


# ---------------------------------------------------------------------------
# Step 3 - emit the SVG.
# ---------------------------------------------------------------------------
def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fmt(x: float) -> str:
    """Trim float noise so the file stays small."""
    return f"{x:.4f}".rstrip("0").rstrip(".") or "0"


def column_window(col: int, cols: int, window: tuple[float, float]) -> float:
    """The moment the beam reaches this column, as a fraction of the loop."""
    start, end = window
    return start + (col / max(cols - 1, 1)) * (end - start)


def build_svg(
    days: Iterable[Day],
    mask: dict[tuple[int, int], int],
    cols: int,
    duration: float,
    title: str,
) -> str:
    days = list(days)
    commit_levels = {(d.row, d.col): d.level for d in days if d.level > 0}
    tooltips = {(d.row, d.col): (d.date, d.count) for d in days}

    grid_w = cols * STEP - GAP
    grid_h = ROWS * STEP - GAP
    width = grid_w + PAD * 2
    height = grid_h + PAD * 2

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">'
    )
    out.append(f"<title>{esc(title)}</title>")

    # Glow. A single blur filter shared by the animated layer keeps the
    # renderer from compositing one filter per rect.
    out.append(
        "<defs>"
        '<filter id="glow" x="-40%" y="-40%" width="180%" height="180%">'
        '<feGaussianBlur stdDeviation="1.9" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
        "</filter>"
        '<linearGradient id="beamGrad" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0%" stop-color="{BEAM}" stop-opacity="0"/>'
        f'<stop offset="50%" stop-color="{BEAM}" stop-opacity="0.9"/>'
        f'<stop offset="100%" stop-color="{BEAM}" stop-opacity="0"/>'
        "</linearGradient>"
        "</defs>"
    )

    out.append(f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>')

    # Cells that never light in either phase get no <animate> at all. On a
    # typical calendar that is most of the grid, and it roughly halves the
    # file size.
    animated: list[str] = []
    static: list[str] = []

    for col in range(cols):
        t1 = column_window(col, cols, SWEEP_1)
        t2 = column_window(col, cols, SWEEP_2)
        for row in range(ROWS):
            x = PAD + col * STEP
            y = PAD + row * STEP
            key = (row, col)
            c_level = commit_levels.get(key, 0)
            m_level = mask.get(key, 0)

            tip = ""
            if key in tooltips:
                date, count = tooltips[key]
                noun = "contribution" if count == 1 else "contributions"
                tip = f"<title>{count} {noun} on {date}</title>"

            base = (
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" '
                f'rx="2" fill="{EMPTY}"'
            )

            if not c_level and not m_level:
                static.append(base + (f">{tip}</rect>" if tip else "/>"))
                continue

            c_col = COMMIT_COLORS.get(c_level, EMPTY)
            m_col = MASK_COLORS.get(m_level, EMPTY)

            # keyTimes must be strictly increasing and start at 0 / end at 1.
            times = [0.0, t1, min(t1 + 0.012, t2 - 0.001), t2,
                     min(t2 + 0.012, FADE[0] - 0.001), FADE[0], FADE[1], 1.0]
            values = [EMPTY, EMPTY, c_col, c_col, m_col, m_col, EMPTY, EMPTY]

            kt = ";".join(fmt(t) for t in times)
            vals = ";".join(values)
            anim = (
                f'<animate attributeName="fill" values="{vals}" keyTimes="{kt}" '
                f'dur="{fmt(duration)}s" repeatCount="indefinite" '
                f'calcMode="discrete"/>'
            )
            animated.append(base + ">" + tip + anim + "</rect>")

    out.append("<g>" + "".join(static) + "</g>")
    out.append('<g filter="url(#glow)">' + "".join(animated) + "</g>")

    # The beam. Two passes, parked off-canvas the rest of the time.
    beam_w = 16
    x_start = PAD - beam_w
    x_end = PAD + grid_w
    beam_times = [
        0.0, SWEEP_1[0], SWEEP_1[1], SWEEP_1[1] + 0.001,
        SWEEP_2[0], SWEEP_2[1], SWEEP_2[1] + 0.001, 1.0,
    ]
    beam_x = [
        x_start, x_start, x_end, x_start,
        x_start, x_end, x_start, x_start,
    ]
    beam_op = [0, 0.95, 0.95, 0, 0.95, 0.95, 0, 0]

    out.append(
        f'<rect y="{PAD - 3}" width="{beam_w}" height="{grid_h + 6}" '
        f'fill="url(#beamGrad)" x="{x_start}">'
        f'<animate attributeName="x" values="{";".join(fmt(v) for v in beam_x)}" '
        f'keyTimes="{";".join(fmt(t) for t in beam_times)}" '
        f'dur="{fmt(duration)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values="{";".join(fmt(v) for v in beam_op)}" '
        f'keyTimes="{";".join(fmt(t) for t in beam_times)}" '
        f'dur="{fmt(duration)}s" repeatCount="indefinite"/>'
        f"</rect>"
    )

    # Scanline texture, faint.
    out.append('<g opacity="0.05" fill="#ffffff">')
    yy = PAD
    while yy < PAD + grid_h:
        out.append(f'<rect x="{PAD}" y="{fmt(yy)}" width="{grid_w}" height="1"/>')
        yy += 3
    out.append("</g>")

    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", required=True, help="GitHub handle")
    p.add_argument("--out", default="dist/scan.svg", help="output SVG path")
    p.add_argument(
        "--mask",
        default="avatar",
        choices=["avatar", "image", "text", "none"],
        help="what the second sweep draws (default: avatar)",
    )
    p.add_argument("--image", help="local path or URL, for --mask image")
    p.add_argument("--text", default=None, help="for --mask text; defaults to the handle")
    p.add_argument("--duration", type=float, default=14.0, help="loop length in seconds")
    args = p.parse_args()

    print(f"Fetching calendar for {args.user}...", file=sys.stderr)
    days = fetch_calendar(args.user)
    cols = max(d.col for d in days) + 1
    total = sum(d.count for d in days)
    active = sum(1 for d in days if d.count > 0)
    print(
        f"  {len(days)} days over {cols} weeks - "
        f"{total} contributions on {active} active days",
        file=sys.stderr,
    )

    if args.mask == "none":
        mask: dict[tuple[int, int], int] = {}
    elif args.mask == "text":
        mask = mask_from_text(args.text or args.user, cols)
        print(f"  mask: text '{(args.text or args.user).upper()}'", file=sys.stderr)
    elif args.mask == "image":
        if not args.image:
            raise SystemExit("--mask image requires --image")
        mask = mask_from_image(args.image, cols)
        print(f"  mask: image {args.image} -> {len(mask)} lit cells", file=sys.stderr)
    else:
        url = default_avatar_url(args.user)
        mask = mask_from_image(url, cols)
        print(f"  mask: avatar -> {len(mask)} lit cells", file=sys.stderr)

    svg = build_svg(
        days,
        mask,
        cols,
        args.duration,
        title=f"{args.user}: {total} contributions in the last year",
    )

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(svg)

    print(f"Wrote {args.out} ({len(svg) / 1024:.1f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
