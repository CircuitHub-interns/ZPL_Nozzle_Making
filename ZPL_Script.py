#!/usr/bin/env python3
"""ZPL_Script.py -- generate one circular nozzle label (curved part-name text
+ Data Matrix) as raw ZPL for a Zebra ZT610, with the tool name checked
against allowed_parts.txt and the serial number auto-assigned using the same
lowest-free-id logic PiCo_LabelsV24.py uses.

Why self-contained: this project's convention (see PiCo_LabelsV24.py's own
docstring) is that each generator script has no sibling-file dependency, so
it can be handed off/deployed on its own. The label-rendering code here is
adapted from ../../ZPL/zpl_render.py and the numbering/whitelist code from
PiCo_LabelsV24.py, both inlined rather than imported.

Standalone CLI usage (for local testing before Retool is wired up):
    python ZPL_Script.py CH0204                  # auto-assigns lowest free N-id
    python ZPL_Script.py CH0204 --n-number 411    # explicit serial (Retool's future mode)
    python ZPL_Script.py CH0204 --png preview.png -o label.zpl

See the RETOOL INTEGRATION HOOK block near the bottom for where the website
plugs in once the transport (in-process Workflow call vs. HTTP endpoint) is
decided -- generate_label() below is the one function it needs to call.
"""

import argparse
import base64
import io
import math
import json
import sys
from pathlib import Path

try:
    import treepoem
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:
    sys.exit(
        f"\nERROR: missing dependency '{exc.name}'.\n"
        f"You ran this with: {sys.executable}\n"
        "That's the WRONG Python (probably the ESP-IDF one on your PATH); it\n"
        "doesn't have treepoem/Pillow, so nothing gets generated.\n\n"
        "Use the project venv instead. From this 'ZPL Goat' folder:\n"
        "    ..\\..\\_\\Scripts\\python.exe ZPL_Script.py CH0204 --n-number 412 -o label.zpl --png preview.png\n\n"
        "or activate the venv once, then plain 'python' works for the session:\n"
        "    ..\\..\\_\\Scripts\\Activate.ps1\n"
    )

# ---- Paths -------------------------------------------------------------
# This file lives in Goat/ZPL Goat/; allowed_parts.txt and SWAP Outputs/ are
# one level up, in Goat/ -- the same files PiCo_LabelsV24.py reads/writes.
BASE_DIR = Path(__file__).resolve().parent
GOAT_DIR = BASE_DIR.parent
ALLOWED_PARTS_FILE = GOAT_DIR / "allowed_parts.txt"
SWAP_DIR = GOAT_DIR / "SWAP Outputs"
DO_NOT_PRINT_FILE = SWAP_DIR / "do_not_print.txt"
PENDING_MANIFEST_FILE = SWAP_DIR / "pending_tools_manifest.json"
GLOBAL_MANIFEST_FILE = SWAP_DIR / "global_manifest.json"

# Fallback whitelist if allowed_parts.txt doesn't exist yet -- kept in sync
# with PiCo_LabelsV24.DEFAULT_ALLOWED_PARTS. Never written to disk from here;
# only PiCo_LabelsV24.py owns creating/seeding the file.
DEFAULT_ALLOWED_PARTS = [
    "P056", "P057", "P054", "P055", "P017", "P018", "P019", "P063",
    "CH0406", "CH0204", "CH01503",
]

# ---- Physical geometry (600 dpi) ---------------------------------------
DPI = 600
LABEL_DOTS = round(0.443 * DPI)     # 266  round-label diameter; the square we render the ring on
CENTER = LABEL_DOTS / 2.0           # 133  center of that render canvas
PRINT_WIDTH = 400                   # ^PW  liner width in dots (your validated media)
LABEL_LENGTH = 362                  # ^LL  label length in dots
INK_THRESHOLD = 128                 # >= this = ink

# ---- Physical geometry validation (Data Matrix vs. hole/outer edge) ----
# Kept as their own explicit constants rather than derived from LABEL_DOTS
# (a rendering-canvas size that's ~0.01mm off from these authoritative specs)
# so an unrelated canvas-size change can never silently shift the safety bound.
DM_INNER_CLEARANCE_MM = 2.5   # hole radius (5mm dia) -- DM must be entirely OUTSIDE this
DM_OUTER_BOUND_MM     = 5.64  # label outer radius (11.28mm dia) -- DM must be entirely INSIDE this

# =========================================================================
# LAYOUT KNOBS for build_zpl_JOA -- these are the ONLY numbers you should
# need to hand-tune to move/resize things. Every one is documented in the
# "HOW TO ADJUST THE LAYOUT" block just above build_zpl_JOA (search for it).
# Quick reference:
#   RING_FONT_PX      text size (bigger number = bigger letters)
#   RING_RADIUS       how far the text sits from the RING CENTER (bigger =
#                     wider ring, bigger empty middle)
#   DASH_MID_DEG      angle the MIDDLE of "CH0204-0000" (i.e. the dash) sits at.
#                     270 = top, 90 = bottom, 0 = right, 180 = left.
#                     Set to 180 so the dash sits LEFT, opposite the DM.
#   DESIGN_CENTER_X   left/right position of the RING CENTER on the liner
#   DESIGN_CENTER_Y   up/down position of the RING CENTER (bigger = DOWN)
#   DM_OFFSET_X/Y     where the Data Matrix sits RELATIVE to the ring center.
#                     +X pushes it RIGHT into the ring's opening (middle-right,
#                     matching Single_Label.py); -Y lifts it to vertical middle.
# ---------------------------------------------------------------------------
# Layout matches Single_Label.py (the V23 recipe): ring centered on the label,
# dash at 9 o'clock, text sweeping clockwise C(lower-right)->bottom->left->top
# ->last-0(upper-right), and the Data Matrix sitting in the gap at middle-right.
# V23 values scaled from 96 dpi to 600 dpi (x6.25): ring radius 13px->81 dots,
# DM center offset (+13.9px,-0.7px)->(+87,-4) dots, DM 8.6px->~54 dots.
# =========================================================================
RING_FONT_PX      = 48       # curved glyph height in dots (sized so the text wraps
                             # ~257 deg and hugs the DM, like Single_Label.py)
RING_RADIUS       = 92       # radius of the curved text baseline (V23: 13px x6.25)
DASH_MID_DEG      = 180      # 180 = dash at LEFT, gap (and DM) at RIGHT
RING_CLOCKWISE    = False    # False = readable-from-outside, sweeps C->...->0 CW
DESIGN_CENTER_X   = 252      # horizontal center of the RING on the 400-dot liner
DESIGN_CENTER_Y   = 150      # vertical center of the RING (bigger = lower)
DM_MODULE         = 6        # Data Matrix module (dot) size, in dots/module. Drives
                             # BOTH axes equally (it's the resize target side length
                             # for _render_dm_image(), not a printer barcode param),
                             # so DM_MODULE*DM_SYMBOL_MODULES is always an equal-sided
                             # square -- no separate width/height knob to desync. At
                             # 600 dpi, 6*10=60 dots = 2.54mm x 2.54mm -- confirmed by
                             # brute-force search over every offset/angle to be the
                             # LARGEST integer DM_MODULE that still fits the annulus
                             # between the 5mm hole (DM_INNER_CLEARANCE_MM) and the
                             # label's outer edge (DM_OUTER_BOUND_MM): 7 (2.96mm) does
                             # not fit anywhere in that annulus, so this is the ceiling,
                             # not just a hand-picked value.
DM_SYMBOL_MODULES = 10       # DM symbol grid size (columns=rows=this). "N####" ECC200-
                             # encodes as 3 data codewords (N=1 + "00","00" digit-pair-
                             # compacted=2), which is the 10x10 size's exact capacity
                             # (3 data + 5 ECC) -- the smallest valid ECC200 symbol for
                             # this data, so this shrinks the DM without touching N0000.
                             # Forced explicitly via treepoem's "version" option in
                             # _render_dm_image() -- without that it auto-picked 12x12.
DM_OFFSET_X       = 96       # push DM RIGHT into the ring opening (V23: +13.9px x6.25)
DM_OFFSET_Y       = -6       # lift DM to the vertical middle (V23: -0.7px x6.25)
RING_LETTER_SPACING_PX = 12 # extra gap between glyphs, in dots (bigger = more spread out;
                             # negative = tighter/overlapping)
RING_STROKE_WIDTH = 1        # glyph outline thickness in dots (0 = font's normal weight,
                             # bigger = bolder/thicker strokes)


# This is the best V18 version, where the data matrix should be outside the inner circle and hopefully out of the outside label sight

FONT_CANDIDATES = [
    "RobotoMono-Regular.ttf", "Roboto Mono", "consola.ttf",
    "DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


# =========================================================================
# Part whitelist -- validate the tool name against allowed_parts.txt
# =========================================================================

def load_allowed_parts():
    """Read Goat/allowed_parts.txt (one part per line, '#' comments/blank
    lines ignored). Falls back to DEFAULT_ALLOWED_PARTS if the file doesn't
    exist yet. Read-only: never creates or modifies the file (that's
    PiCo_LabelsV24.PartCatalog's job)."""
    if not ALLOWED_PARTS_FILE.exists():
        return list(DEFAULT_ALLOWED_PARTS)

    parts = []
    seen = set()
    with open(ALLOWED_PARTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            cleaned = line.strip().upper()
            if not cleaned or cleaned.startswith("#"):
                continue
            if cleaned not in seen:
                parts.append(cleaned)
                seen.add(cleaned)
    return parts or list(DEFAULT_ALLOWED_PARTS)


def validate_part(part):
    """Normalize + check part against the whitelist. Returns the normalized
    (uppercased, stripped) part name. Raises ValueError if not allowed."""
    normalized = (part or "").strip().upper()
    if not normalized:
        raise ValueError("Part name is required.")

    allowed = load_allowed_parts()
    if normalized not in allowed:
        raise ValueError(
            f"'{normalized}' is not in the allowed parts list "
            f"({ALLOWED_PARTS_FILE}). Allowed: {', '.join(allowed)}"
        )
    return normalized


# =========================================================================
# Numbering -- lowest free N-id, mirroring PiCo_LabelsV24's
# _reserved_ids()/_next_available_id(). Read-only here: this does NOT
# reserve the id anywhere. It's a dev-time convenience for testing this
# script standalone -- once Retool manages numbering itself (see the
# clarified plan), callers should pass n_number explicitly instead of
# relying on this auto-assign path.
# =========================================================================

def _load_skip_ids():
    """Parse do_not_print.txt -> set of blocked serial ids. Accepts 'N0005',
    bare '813', inclusive ranges '800-820', '#' comments, and an inline
    reason after the id."""
    ids = set()
    if not DO_NOT_PRINT_FILE.exists():
        return ids
    for line in DO_NOT_PRINT_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0].lstrip("Nn")
        try:
            if "-" in token:
                lo, hi = token.split("-", 1)
                ids.update(range(int(lo), int(hi) + 1))
            else:
                ids.add(int(token))
        except ValueError:
            continue
    return ids


def _serial_of(tool):
    sid = tool.get("serial_id")
    if isinstance(sid, int):
        return sid
    digits = "".join(c for c in str(tool.get("tool_number", "")) if c.isdigit())
    return int(digits) if digits else None


def _reserved_ids():
    """Every serial id NOT available for a new print: mounted on a gantry,
    sitting in pending, or listed in do_not_print.txt."""
    reserved = set()

    if PENDING_MANIFEST_FILE.exists():
        with open(PENDING_MANIFEST_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
        for tool in pending.get("tools", []):
            sid = _serial_of(tool)
            if sid is not None:
                reserved.add(sid)

    if GLOBAL_MANIFEST_FILE.exists():
        with open(GLOBAL_MANIFEST_FILE, "r", encoding="utf-8") as f:
            global_manifest = json.load(f)
        for _gantry, tools in global_manifest.get("gantries", {}).items():
            for tool in tools:
                sid = _serial_of(tool)
                if sid is not None:
                    reserved.add(sid)

    reserved |= _load_skip_ids()
    return reserved


def next_available_n_number():
    """Lowest positive N-id that isn't reserved."""
    reserved = _reserved_ids()
    n = 1
    while n in reserved:
        n += 1
    return n


# =========================================================================
# Rendering -- curved part-name text + Data Matrix -> 1-bit image -> ^GFA
# =========================================================================

def _load_font(px):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _char_w(font, ch, stroke_width=0):
    box = font.getbbox(ch, stroke_width=stroke_width)
    return max(1, box[2] - box[0])


def draw_arc_text(canvas, text, font, radius, mid_deg, clockwise,
                   letter_spacing_px=0, stroke_width=0):
    """Paste text glyph-by-glyph around an arc centered on mid_deg. canvas is
    mode 'L' with ink drawn as 255 on a 0 background.

    letter_spacing_px: extra dots added to each glyph's width before it's
    converted to an angular step -- this is the "tracking" knob (bigger =
    more air between letters, negative = tighter/overlapping).
    stroke_width: dots added as an outline around each glyph, i.e. the
    "boldness"/thickness knob (0 = font's own weight)."""
    ang = [(_char_w(font, c, stroke_width) + letter_spacing_px) / radius for c in text]
    total = sum(ang)
    direction = -1 if clockwise else 1
    a = math.radians(mid_deg) - direction * total / 2.0
    pad = font.size * 3 + stroke_width * 2
    for ch, aw in zip(text, ang):
        ca = a + direction * aw / 2.0                 # this glyph's center angle
        x = CENTER + radius * math.cos(ca)
        y = CENTER + radius * math.sin(ca)
        rot = -(math.degrees(ca) + (90 if not clockwise else -90))
        g = Image.new("L", (pad, pad), 0)
        ImageDraw.Draw(g).text((g.width / 2, g.height / 2), ch, font=font, fill=255,
                                anchor="mm", stroke_width=stroke_width, stroke_fill=255)
        g = g.rotate(rot, resample=Image.BICUBIC, expand=True)
        canvas.paste(g, (int(x - g.width / 2), int(y - g.height / 2)), g)
        a += direction * aw


def image_to_gfa(canvas):
    """'L' image (ink=255) -> ZPL ^GFA. Ink pixel => printed dot (bit 1)."""
    w, h = canvas.size
    px = canvas.load()
    row_bytes = (w + 7) // 8
    total = row_bytes * h
    out = []
    for y in range(h):
        row = bytearray(row_bytes)
        for x in range(w):
            if px[x, y] >= INK_THRESHOLD:
                row[x >> 3] |= 0x80 >> (x & 7)
        out.append(row.hex().upper())
    return f"^GFA,{total},{total},{row_bytes},{''.join(out)}"


def render_ring(part, n_number):
    """Render the curved '<part>-<n_number>' text (NO Data Matrix) on the full
    round-label canvas, then crop tight to the ink. Returns
    (cropped_image, (center_x_in_crop, center_y_in_crop)).

    The second value is where the RING'S CENTER lands inside the cropped
    image. We need it because the crop trims unequal top/bottom/left/right
    margins, so the ring center is NOT the middle of the cropped rectangle.
    build_zpl_JOA uses it to line the native Data Matrix up on the exact same
    center as the ring (that concentric alignment is the whole fix)."""
    text = f"{part}-{n_number:04d}"
    canvas = Image.new("L", (LABEL_DOTS, LABEL_DOTS), 0)
    draw_arc_text(canvas, text, _load_font(RING_FONT_PX), RING_RADIUS, DASH_MID_DEG, RING_CLOCKWISE,
                  letter_spacing_px=RING_LETTER_SPACING_PX, stroke_width=RING_STROKE_WIDTH)
    bbox = canvas.getbbox()
    if bbox is None:
        raise ValueError(f"Rendered curved text for '{text}' produced no ink -- check the RING_* knobs.")
    cropped = canvas.crop(bbox)
    center_in_crop = (CENTER - bbox[0], CENTER - bbox[1])
    return cropped, center_in_crop


def _dm_symbol_dots():
    """Side length in dots of the native Data Matrix, so we can center it.
    DM_SYMBOL_MODULES modules * DM_MODULE dots/module for the 'N####' payload."""
    return DM_SYMBOL_MODULES * DM_MODULE


def _render_dm_image(slot, s):
    """Render the Data Matrix for `slot` as a pixel-exact s x s square ink
    mask (ink=255, same convention as the ring bitmap) via treepoem's ECC200
    Data Matrix generator, forced to a DM_SYMBOL_MODULES x DM_SYMBOL_MODULES
    grid. Baking this into a ^GFA bitmap (see image_to_gfa) instead of using
    the native ^BXN barcode guarantees a perfect square on paper: it removes
    any dependency on the printer's own barcode-symbology renderer, whose
    aspect-ratio/columns/rows handling isn't guaranteed to behave the same
    across firmware/printer models (observed printing as a rectangle here
    despite ^BXN's explicit square-aspect and equal columns/rows params)."""
    dm = treepoem.generate_barcode(
        barcode_type="datamatrix", data=slot,
        options={"version": f"{DM_SYMBOL_MODULES}x{DM_SYMBOL_MODULES}"},
    ).convert("L")
    dm = dm.resize((s, s), Image.NEAREST)
    return dm.point(lambda p: 255 if p < INK_THRESHOLD else 0)


def _validate_dm_geometry(dmx, dmy, s, cx, cy):
    """Raise ValueError if the axis-aligned DM square [dmx,dmx+s]x[dmy,dmy+s]
    (all in liner dots) doesn't clear the inner hole or overruns the outer
    edge, measured from (cx, cy) = the hole/label center.

    This is a safety net, not a placement strategy: DM_OFFSET_X/Y etc. are
    still hand-tuned, but a future knob change that breaks the physical
    boundaries will fail loud here instead of silently shipping a bad label."""
    nx = min(max(cx, dmx), dmx + s)
    ny = min(max(cy, dmy), dmy + s)
    min_dist_mm = math.hypot(cx - nx, cy - ny) / DPI * 25.4

    fx = dmx if abs(cx - dmx) > abs(cx - (dmx + s)) else dmx + s
    fy = dmy if abs(cy - dmy) > abs(cy - (dmy + s)) else dmy + s
    max_dist_mm = math.hypot(cx - fx, cy - fy) / DPI * 25.4

    if min_dist_mm < DM_INNER_CLEARANCE_MM:
        raise ValueError(
            f"Data Matrix overlaps the inner hole: closest edge is {min_dist_mm:.3f}mm "
            f"from center, but must be >= {DM_INNER_CLEARANCE_MM}mm. Move it away from "
            f"center via DM_OFFSET_X/DM_OFFSET_Y, or check DESIGN_CENTER_X/DESIGN_CENTER_Y "
            f"and RING_RADIUS/DASH_MID_DEG haven't shifted the design center."
        )
    if max_dist_mm > DM_OUTER_BOUND_MM:
        raise ValueError(
            f"Data Matrix extends past the label's outer edge: farthest corner is "
            f"{max_dist_mm:.3f}mm from center, but must be <= {DM_OUTER_BOUND_MM}mm. "
            f"Pull it toward center via DM_OFFSET_X/DM_OFFSET_Y, or reduce DM_MODULE/"
            f"DM_SYMBOL_MODULES."
        )


def _placement(part, n_number):
    """Work out the absolute (^LH0,0-relative) top-left corners for the ring
    graphic and the Data Matrix so both share DESIGN_CENTER_X/Y.

    Returns (cropped_ring, gfa_field, gx, gy, dmx, dmy, s, dm_ink, dm_gfa_field)."""
    cropped, (ccx, ccy) = render_ring(part, n_number)
    gfa_field = image_to_gfa(cropped)

    # DO NOT hand-edit gx/gy/dmx/dmy -- they are COMPUTED from the knobs.
    # To move things, change DESIGN_CENTER_X/Y, DM_OFFSET_X/Y, DASH_MID_DEG,
    # RING_RADIUS, RING_FONT_PX at the top of the file instead.
    #
    # Put the ring's center on the design center by offsetting the graphic's
    # top-left corner back by where the center sits inside the crop.
    gx = round(DESIGN_CENTER_X - ccx)
    gy = round(DESIGN_CENTER_Y - ccy)

    # Data Matrix: DM_OFFSET_X/Y place it relative to the ring center (default
    # pushes it right, into the ring's opening -> middle-right, like V23).
    s = _dm_symbol_dots()
    dmx = round(DESIGN_CENTER_X - s / 2 + DM_OFFSET_X)
    dmy = round(DESIGN_CENTER_Y - s / 2 + DM_OFFSET_Y)
    _validate_dm_geometry(dmx, dmy, s, DESIGN_CENTER_X, DESIGN_CENTER_Y)

    slot = f"N{n_number:04d}"
    dm_ink = _render_dm_image(slot, s)
    dm_gfa_field = image_to_gfa(dm_ink)
    return cropped, gfa_field, gx, gy, dmx, dmy, s, dm_ink, dm_gfa_field


def render_label_image(part, n_number):
    """Full-label preview (black-on-white PIL image, PRINT_WIDTH x LABEL_LENGTH)
    laid out EXACTLY like build_zpl_JOA: ring graphic + the SAME Data Matrix
    bitmap that gets shipped to the printer (see _placement). Use this to
    eyeball position/size before printing -- anything running off the top/edge
    here will run off on the printer too."""
    cropped, _gfa, gx, gy, dmx, dmy, _s, dm_ink, _dm_gfa = _placement(part, n_number)

    label = Image.new("L", (PRINT_WIDTH, LABEL_LENGTH), 0)
    label.paste(cropped, (gx, gy), cropped)   # ink-only mask: 0s stay transparent
    label.paste(dm_ink, (dmx, dmy), dm_ink)

    return label.point(lambda p: 0 if p >= INK_THRESHOLD else 255)  # black-on-white


# =========================================================================
# HOW TO ADJUST THE LAYOUT (build_zpl_JOA)
# -------------------------------------------------------------------------
# Everything is driven by the LAYOUT KNOBS near the top of the file. Change a
# knob, re-run with --png, look at the preview, repeat. Nothing inside
# build_zpl_JOA itself needs editing to move/resize the design.
#
#   TEXT TOO SMALL / TOO BIG ....... RING_FONT_PX   (52 now; bigger = bigger)
#   LETTERS TOO CRAMPED / SPREAD ... RING_LETTER_SPACING_PX (0 now; bigger = more
#                                    air between glyphs, negative = tighter)
#   LETTERS TOO THIN / TOO BOLD .... RING_STROKE_WIDTH (0 now; bigger = thicker
#                                    strokes, like faux-bold)
#   RING TOO TIGHT / TOO WIDE ...... RING_RADIUS    (92 now; bigger = wider
#                                    ring + bigger empty hole for the DM)
#   DASH NOT AT THE TOP ............ DASH_MID_DEG   (270 = top. 70..90 = bottom,
#                                    0 = right side, 180 = left side)
#   WHOLE DESIGN TOO FAR L/R ....... DESIGN_CENTER_X (252 now; bigger = right)
#   WHOLE DESIGN TOO HIGH/LOW ...... DESIGN_CENTER_Y (150 now; bigger = down).
#                                    If the TOP of the ring clips, either lower
#                                    this OR recalibrate media (see NOTES at the
#                                    bottom of the generated ZPL).
#   DATA MATRIX NOT CENTERED ....... DM_OFFSET_X / DM_OFFSET_Y nudge it; the DM
#                                    grid size is FORCED to DM_SYMBOL_MODULES via
#                                    _render_dm_image()'s treepoem "version" option,
#                                    so keep that in sync if you ever change it.
#   DATA MATRIX TOO SMALL/BIG ...... DM_MODULE (6 now; this is the resize side length
#                                    per module, applied to both axes so it always
#                                    stays a perfect square -- see _render_dm_image()).
#
# The two gaps you asked about -- part-name-end-to-DM and number-end-to-DM --
# are equal by construction (the text is centered on DASH_MID_DEG, so the two
# ends are symmetric) and their size = RING_RADIUS minus the DM's reach. Widen
# the gap with a bigger RING_RADIUS or a smaller DM_MODULE.
# =========================================================================

def build_zpl_JOA(part, n_number, copies=1):
    """Raw ZPL for the hand-tuned CH0204-style round label (see
    Nozzle_ZPL.txt), parameterized on part + n_number. The only per-label
    changes vs. the hand-tuned original:
      1. the curved part-name ring graphic (the ^FO.. ^GFA line)
      2. the Data Matrix, ALSO rendered as a ^GFA bitmap (not the native ^BXN
         barcode) so its printed shape is pixel-exact and guaranteed square --
         see _render_dm_image()'s docstring for why
      3. both graphics' ^FO placement -- computed from the LAYOUT KNOBS so
         the ring and the DM always share one center (concentric)
    copies is accepted for interface parity with generate_label() but unused --
    this template has no ^PQ (the original didn't either).

    IMPORTANT ZPL-comment rule: every ; comment below is on its OWN line.
    ZPL keeps reading a parameterized command's value until the next ^ or ~,
    so a comment appended after a command ON THE SAME LINE gets silently
    swallowed into that command's parameter (e.g. ^LL362 ; Label Length was
    parsed as ^LL's value being "362 ; Label Length", corrupting the label
    length). Never put a comment on the same line as a command.
    """
    part = validate_part(part)
    slot = f"N{n_number:04d}"
    text = f"{part}-{n_number:04d}"
    _cropped, gfa_field, gx, gy, dmx, dmy, _s, _dm_ink, dm_gfa_field = _placement(part, n_number)

    return (
        "CT~~CD,~CC^~CT~\n"
        "^XA\n"
        "~TA000\n"
        "~JSN\n"
        "^MMT\n"
        "^MPE\n"
        "~SD20\n"
        "^JZY\n"
        "^MFN,N\n"
        "^XZ\n"
        "\n"
        "^XA\n"
        "^MNM,0\n"
        "^MTT\n"
        "\n"
        "^PMN\n"
        "^LL362\n"
        "^PW400\n"
        "\n"
        "^LH0,0\n"
        "^LS-83\n" #Label Shift (X): -42 (original) plus -41 to re-anchor the
                   # ring+DM combo back onto the registration validated by the
                   # FO100,38/FO285,125 print in the commented block below --
                   # see the pixel-centroid comparison in this file's history.
        "\n"
        "^LT-4\n" # Label shift (Y) -- unchanged; the vertical registration
                  # already matches (within 1 dot) that validated print.
        "^JUS\n"
        "^XZ\n"
        "^XA\n"
        "^PW400\n"
        "^LL362\n"
        "^LH0,0\n"
        f"^FO{gx},{gy}{gfa_field}^FS\n"
        f"^FO{dmx},{dmy}{dm_gfa_field}^FS\n"
        "^XZ\n"
    )
    
'''
^CT~~CD,~CC^~CT~
^XA
~TA000
~JSN
^MMT
^MPE
~SD20
^JZY
^MFN,N
^XZ

^XA
! --- Label Dimensions & Global Offsets --- !
^PW400
^LL362
^LH0,0
^MNM,0
^MTT
^PMN
^LS-42 
^LT-4
; I changed the values of F0 from both the data matrix part and the Text part, meaning that I have to later change the LS and LT code, but again that is a later issue once I am properly testing

! --- 1. Curved Text Graphic (Background Layer) --- !
^FO100,38^GFA,5152,5152,23,000000000000000000000000000000000000000000000000000000000000000000000000000003C00000000000000000000000000000000000000000003FF80000000000000000000000000000000000000000007FFE00000000000000000000000000000000FF80000000FFFF00000000000000000000000000000007FFC0000000FFFF8000000000000000000000000000001FFFE00000007C3FC000000000000000000000000000007FFFF0000000701FC000000000000000000000000000007FFFF0000000000FC000000000000000000000000000003F83F8000000000FC000000000000000000000000000003C01F80000000007E000000000000000000000000000003001F80000000007E000000000000000000000000000000001F80000000007C000000000000000000000000000000001F8000000000FC000000000000000000000000000000001F8000000000FC000000000000000000000000000000001F0000000001FC000000000000000000000000000000003F0000000003F800000000000000000000000000000000FF0000000003F800000000000000000000000000000007FFC00000000FF00000000000000000000000000000001FFFF00000001FE00000000000000000000000000000000FFFF00000003FC00000000000000000000000000000000FFFF80000007F800000000010000000000000000000000F03FC000001FF0000000000FC000000000000000000000000FC000003FE0000000003FE000000000000000000000000FC00000FF8000000000FFF8000000000038000000000007C00001FF0000000007FFF800000000007C000000000007C00003FE000000001FFFF80000000000FC000000000007C0000FF8000000007FFFF00000000000FE000000000007C0000FF000000003FFFFE00000000001FF00000000000FC0001FFF0000000FFF8FE00000000001FF80000000001FC0001FFFFC00007FFE1FC00000000003FFC0000000001F80001FFFFF8000FFF03F800000000003FFE0000000007F00001FFFFF8000FFC03F000000000003FFE000000007FF000003FFFF8001FE007F000000000007CFF0000000FFFE0000000FFF0001FE00FE000000000007C7F8000000FFF8000000003F0000FF00FC000000000007C3FC000000FFF0000000000000007FC1FC00000000000FC1FE000000FF80000000000000001FE3F800000000000F80FF0000007C00000000000000000FFFF0000000000003807F00000000000000000000000007FFF0000000000000007F80000000000000000000000001FFE0000000000000003FC0600000000000000000000000FFC0000000000000001FE0F000000000000000000000003FE0000000000000000FF3F800000000000000000000003FF00000000000000007FFF800000000000000000000007FFC0000000000000003FFF800000000000000000000007FFC0000000000000003FFE00000000000000000000000FFF80000000000000001FFC00000000000000000000001FC700000000000000000FF800000000000000000000001F8300000000000000001FF000000000000000000000000F8000000000000000003FE00000000000000000000000070000000000000000007FC0000000000000000000000000000000000000000000FF00000000000000000000000000000000000000000001FE00000000000000000000000000000000000000000001FC00000000000000000000000000000000000000000000F80000000000000000000000000000000000000000000070000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000007000000000000000000000000000000000000000000000F800000000000000000000000000000000000000000000FC00000000000000000000000000000000000000000000F800000000000000000000000000000000000000000001F800000000000000000000000000000000000000000001F000000000000000000000000000000000000000000003F000000000000000000000000000000000000000000003E000000000000000000000000000000000000000000007E000000000000000000000000000000000000000000007C00000000000000000000000000000000000000000000FC00000000000000000000000000000000000000000000F800000000000000000000000000000000000000000001F800000000000000000000000000000000000000000001F000000000000000000000000000000000000000000000700000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000007C000000000000000000000000000000000000000000007C000000000000000000000000000000000000000000007C000000000000000000000000000000000000000000007C000000000000000000000000000000000000000000007C0000000000000000000000000000000000000007FFFFFFF00000000000000000000000000000000000000FFFFFFFF80000000000000000000000000000000000000FFFFFFFF80000000000000000000000000000000000000FFFFFFFF80000000000000000000000000000000000000FFFFFFFF80000000000000000000000000000000000000FFFFFFFF80000000000000000000000000000000000000FF8007C0000000000000000000000000000000000000007FC007C0000000000000000000000000000000000000003FF007C0000000000000000000000000000000000000001FF807C00000000000000000000000000000000000000007FE07C00000000000000000000000000000000000000001FF07C00000000000000000000000000000000000000000FFC7C000000000000000000000000000000000000000003FE7C000000000000000000000000000000000000000001FFFC0000000000000000000000000000000000000000007FFC0000000000000000000000000000000000000000003FFC0000000000000000000000000000000000000000000FFC00000000000000000000000000000000000000000003FC00000000000000000000000000000000000000000001FC0000000000000000000000000000000000000000000078000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000003FE0000000000000000000000000000000000000000001FFF8000000000000000000000000000000000000000007FFFE00000000000000000000000000000000000000001FFFFF00000000000000000000000000000000000000007FFFFF8000000000000000000000000000000000000001FFFC7F8000000000000000000000000000000000000003FFC00FC000000000000000000000000000000000000007FF000FC00000000000000000000000000000000000000FFC0007C00000000000000000000000000000000000001FFE0007C00000000000000000000000000000000000003FFF8007E00000000000000000000000000000000000003FFFE007E00000000000000000000000000000000000003F3FF80FC00000000000000000000000000000000000007E0FFE0FC00000000000000000000000000000000000007E07FF9FC00000000000000000000000000000000000007C01FFFF800000000000000000000000000000000000007C007FFF800000000000000000000000000000000000007C001FFF000000000000000000000000000000000000007E0007FE000000000000000000000000000000000000003E001FFC000000000000000000000000000000000000003F007FF8000000000000000000000000000000000000001FFFFFE0000000000000000000000000000000000000001FFFFFC0000000000000000000000000000000000000000FFFFF000000000000000000000000000000000000000007FFFC000000000000000000000000000000000000000001FFE00000000000000000000000000000000000000000003E000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000001C000000000000000000000000000000000000000000003F000000000000000000000000000000000000000000003F800000000000000000000000000000000000000000003FC00000000000000000000000000000000000000000001FE00000000000000000000000000000000000000000000FF000000000000000000000000000000000000000000007FC00000000000000000000000000000000000000000003FE00000000000000000000000000000000000000000001FF000000000000000000000000F0000000000000000000FF800000000000000000000007FE0000000000000000003FC0000000000000000000001FFF8000000000000000001FF0000000000000000000003FFFC00000000000000003FFF800000000000000000000FFFFE000000000000001FFFFF000000000000000000001FFFFF00000000000001FFFFFF000000000000000000001FF1FF80000000000003FFFFFE000000000000000000003FC07FC000000000000FFFFFFC000000000000000000007F001FE000000000001FFFF000000000000000000000007E000FF000000000003FF80000000000000000000000007C0007F000000000007FC0000000000000000000000000380003F800000000007F00000000007C00000000000000380003F800000000007E0000000001FF80000000000000100001FC0000000000FE0000000007FFC000000001F000000000FC0000000000FC000000000FFFF000000003F000000000FE0000000000FC000000001FFFF000000001F0000000007E0000000000FC000000003FE7F80003E001F0000000007E0000000000FC000000003F81FC0003E001F8000000007E0000000000FE000000007F00FC0003E001F8000000003E00000000007E000000007E00FC0003F001F8000000003E00000000003F80000000FC00FC0003F001F8000000003E00000000003FFC000000FC007C0003F000F8000000007E00000000001FFE000001F8007E0003F000F8000000007E00000000000FFE000001F800FE0001F000FC000000007E000000000003FE000001F803FC0001F800FC00000000FE000000000000FC000003F01FFC0001F800FC00000001FC00000000000000000003F07FFC0001F800FC00000003FC00000000000000000003F3FFFC0001F801FC0000000FF800000000000000000003FFFFFC0001F87FFC000000FFF000000000000000000003FFFEFC0000FFFFFE000001FFE000000000000000000007FFF0FC0000FFFFFE000001FFC000000000000000000007FFC0F80000FFFFFE000000FF0000000000000000000007FF01F80000FFFF7E0000007C0000000000000000000007F801F80000FFC03E000000000000000000000000000007E003F00000FC003F000000000000000000000000000007E003F000007C003F000000000000000000000000000003E007E000007E003F000000000000000000000000000003F007E000007E003F000000000000000000000000000003F00FC000007E003F000000000000000000000000000003F81FC000007E001F000000000000000000000000000001FFFF8000003E001F800000000000000000000000000000FFFF0000003E001F8000000000000000000000000000007FFE0000003F001F8000000000000000000000000000003FFC0000003F001F8000000000000000000000000000000FF00000003F000F80000000000000000000000000000000000000003F000000000000000000000000000000000000000000001F000000000000000000000000000000000000000000001F0000000000000000000000000000000000000000000000000000000000000^FS

! --- 2. Data Matrix Barcode (Layered On Top) --- !
! FO is now absolute (X=190, Y=125). You can scale size up freely !
^FO285,125^BXN,4,200,,,1^FDN0001^FS

; THIS IS THE BEST, no proper label shift measurements but, THIS IS THE BEST

^XZ
'''


# =========================================================================
# Public API
# =========================================================================

def generate_label(part, n_number=None, copies=1, include_preview_png=True):
    """Build one nozzle label as raw ZPL.

    part: tool/part name; must be in allowed_parts.txt (raises ValueError
        otherwise).
    n_number: explicit serial (int) to bake into the label. If None, the
        lowest free N-id is auto-assigned (dev/testing convenience -- see
        module docstring; this does NOT reserve the id anywhere).
    copies: value for ^PQ (print quantity).
    include_preview_png: also render a base64 PNG preview (black-on-white)
        for a UI to show before printing.

    Returns a dict: {"part", "n_number", "slot", "zpl", ["preview_png_base64"]}.
    """
    part = validate_part(part)
    if n_number is None:
        n_number = next_available_n_number()

    result = {
        "part": part,
        "n_number": n_number,
        "slot": f"N{n_number:04d}",
        "zpl": build_zpl_JOA(part, n_number, copies=copies),
    }

    if include_preview_png:
        # render_label_image composites the ring + the exact same Data Matrix
        # bitmap that build_zpl_JOA (above) bakes into the ^GFA field -- this
        # preview is pixel-accurate, not an approximation.
        preview = render_label_image(part, n_number).point(lambda p: 0 if p >= INK_THRESHOLD else 255)
        buf = io.BytesIO()
        preview.save(buf, format="PNG")
        result["preview_png_base64"] = base64.b64encode(buf.getvalue()).decode("ascii")

    return result


# =========================================================================
# RETOOL INTEGRATION HOOK -- not wired up yet, transport TBD.
# generate_label() above is the one call site the website needs. Once the
# transport is decided, uncomment + adapt whichever option applies:
#
# --- Option A: Retool Workflow "Python code" block calls this in-process ---
# result = generate_label(args["part"], n_number=args.get("n_number"))
# return result
#
# --- Option B: small HTTP endpoint Retool calls as a REST resource -------
# from http.server import BaseHTTPRequestHandler, HTTPServer
#
# class Handler(BaseHTTPRequestHandler):
#     def do_POST(self):
#         length = int(self.headers["Content-Length"])
#         body = json.loads(self.rfile.read(length))
#         try:
#             payload = generate_label(body["part"], body.get("n_number"))
#             status = 200
#         except ValueError as exc:
#             payload, status = {"error": str(exc)}, 400
#         self.send_response(status)
#         self.send_header("Content-Type", "application/json")
#         self.end_headers()
#         self.wfile.write(json.dumps(payload).encode("utf-8"))
#
# if __name__ == "__main__" and "--serve" in sys.argv:
#     HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
# =========================================================================


# =========================================================================
# CLI -- for local testing before Retool is wired up.
# =========================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Render one circular nozzle label to raw ZPL (^GFA) for a Zebra ZT610."
    )
    ap.add_argument("part", help="Tool/part name, e.g. CH0204 (checked against allowed_parts.txt)")
    ap.add_argument("--n-number", type=int, default=None,
                     help="Explicit serial number, e.g. 411 -> N0411. Omit to auto-assign the lowest free id.")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--png", help="Also save a black-on-white preview PNG here")
    ap.add_argument("-o", "--out", help="Output .zpl path")
    args = ap.parse_args()

    try:
        result = generate_label(
            args.part, n_number=args.n_number, copies=args.copies,
            include_preview_png=bool(args.png),
        )
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    if args.png:
        png_bytes = base64.b64decode(result["preview_png_base64"])
        with open(args.png, "wb") as f:
            f.write(png_bytes)
        print(f"Wrote preview {args.png}")

    out = args.out or f"label_{result['slot']}.zpl"
    with open(out, "w") as f:
        f.write(result["zpl"])

    assigned_note = " (auto-assigned)" if args.n_number is None else ""
    print(f"Part: {result['part']}  Slot: {result['slot']}{assigned_note}")
    print(f"Wrote {out} ({len(result['zpl'])} bytes).")


if __name__ == "__main__":
    main()
