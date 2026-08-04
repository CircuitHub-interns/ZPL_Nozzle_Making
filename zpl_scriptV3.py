#!/usr/bin/env python3
"""zpl_scriptV3.py -- generate ONE circular "donut" nozzle label as raw ZPL for a
Zebra ZT610 at 600 DPI.

Each label carries:
  * curved part-name text, e.g. "CH0204-1234", baked into a ^GFA bitmap, and
  * a square Data Matrix whose payload is ALWAYS "N####" using the SAME 4-digit
    number as the text -- so the human-readable serial and the scannable serial
    can never disagree.

Why the Data Matrix is always 10x10: the payload "N" + "00" + "00" ECC200-encodes
as exactly 3 data codewords (N=1 byte, then two digit-pairs compacted to 1 byte
each). 3 data + 5 ECC codewords is the exact capacity of the 10x10 symbol -- the
smallest valid ECC200 size for this data. We force the printer to that size (rows
= cols = 10) so the DM stays small, constant, and SQUARE. DO NOT change
DM_SYMBOL_MODULES (see the knob).

The Data Matrix is emitted as a NATIVE ^BX command (not a baked ^GFA bitmap) so
the printer renders each module on exact dot boundaries -- the sharpest possible
symbol. Forcing rows = cols = 10 also guards against the "prints as a rectangle"
behavior seen when the symbol was left to auto-size. Only the curved text stays a
^GFA bitmap (it has to -- there's no native curved-text command).

Rendering recipe (curved-text-as-^GFA, concentric placement, the mm safety-net
validator) is adapted from the sibling ZPL_Script.py. The clean 600 DPI 266x266
ZPL template is from GEM_NOZZLE.txt. This file is self-contained (no sibling
imports) per the project convention, so it can be handed off on its own.

Standalone CLI usage (run with the project venv that has treepoem + Pillow):
    ..\\..\\_\\Scripts\\python.exe zpl_scriptV3.py CH0204 --number 1234 --png preview_v3.png -o v3.zpl

See the RETOOL INTEGRATION HOOK block near the bottom -- generate_label() is the
one function the website needs to call.
"""

import argparse
import base64
import io
import math
import sys

try:
    import treepoem
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:
    sys.exit(
        f"\nERROR: missing dependency '{exc.name}'.\n"
        f"You ran this with: {sys.executable}\n"
        "That's probably the WRONG Python; it doesn't have treepoem/Pillow.\n\n"
        "Use the project venv. From this 'ZPL Goat' folder:\n"
        "    ..\\..\\_\\Scripts\\python.exe zpl_scriptV2.py CH0204 --number 1234 -o v2.zpl --png preview.png\n"
    )


# #########################################################################
# #                                                                       #
# #   ===================  TUNING KNOBS  ===========================      #
# #                                                                       #
# #   Everything you hand-tune lives here. Change a value, re-run with    #
# #   --png, look at preview.png, repeat. Nothing below this block needs  #
# #   editing to move/resize/re-weight the design.                        #
# #                                                                       #
# #########################################################################

# ---- Physical geometry (FIXED -- these describe the real donut label) ----
# At 600 DPI the 0.443" outer diameter is 266 dots; the 5 mm center hole is a
# 2.5 mm radius = ~59 dots. These are authoritative -- only change them if the
# physical media actually changes.
DPI            = 600               # printer resolution (dots per inch)
LABEL_DOTS     = round(0.443 * DPI)  # 266  -- outer diameter of the round label
CENTER         = LABEL_DOTS / 2.0    # 133  -- label center (ring is concentric here)
OUTER_RADIUS   = LABEL_DOTS / 2.0    # 133  -- outer edge (RED outer circle in preview)
INNER_RADIUS   = round((2.5 / 25.4) * DPI)  # 59 -- 2.5 mm hole radius (RED inner circle)
INK_THRESHOLD  = 128               # pixel >= this counts as ink (a printed dot)

# ---- Curved part-name text ----------------------------------------------
RING_FONT_PX          = 53    # TEXT SIZE: glyph height in dots (bigger = bigger letters) | OG IS 48

RING_STROKE_WIDTH     = 1     # TEXT THICKNESS/BOLDNESS: outline dots around each glyph
                              #   (0 = font's normal weight, bigger = bolder/thicker)
RING_RADIUS           = 96    # TEXT CURVATURE: radius of the text baseline in dots.
                              #   This is the BLUE circle in the preview. Bigger = wider
                              #   ring + bigger empty middle. Keep font/2 inside the
                              #   INNER_RADIUS..OUTER_RADIUS annulus (59..133).
DASH_MID_DEG          = 180   # ANGLE the MIDDLE of the text (the dash) sits at:
                              #   270 = top, 90 = bottom, 0 = right, 180 = left.
                              #   180 puts the dash LEFT so the gap (and DM) sit RIGHT.
RING_CLOCKWISE        = False # False = readable-from-outside, text sweeps clockwise
RING_LETTER_SPACING_PX = 12   # TRACKING: extra dots between glyphs (bigger = more air,
                              #   negative = tighter/overlapping)
RING_SUPERSAMPLE      = 4     # TEXT CRISPNESS: internal render scale before the 1-bit
                              #   ^GFA conversion. 1 = render straight at 600 dpi; 4 = render
                              #   4x larger then downsample, so glyph curves/edges land on the
                              #   best-fit dots -> smoother text at NO printer cost (still 1-bit
                              #   600 dpi). 4-6 is plenty; higher only slows generation.

# ---- Data Matrix (native ^BX -- printer-rendered, sharpest) --------------
DM_MODULE         = 4    # DM SIZE: dots per module = the ^BX 'h' param. Side length =
                         #   DM_MODULE * DM_SYMBOL_MODULES. 4 -> 40x40 dots = ~1.70mm square,
                         #   the closest integer-dot size that still keeps the symbol
                         #   small and fits the 10x10 ECC200 payload. The validator
                         #   below refuses anything that overruns the annulus.
DM_SYMBOL_MODULES = 10   # !!! DO NOT CHANGE !!! The "N####" payload only fits the 10x10
                         #   ECC200 symbol (3 data + 5 ECC codewords). Emitted as the ^BX
                         #   rows AND cols so the symbol is forced square (no rectangle).
DM_ORIENT         = "N"  # ^BX orientation: N=normal, R=90deg, I=180deg, B=270deg.
DM_QUALITY        = 200  # ^BX ECC level: 200 = ECC200 (the modern square Data Matrix). Leave at 200.
DM_GAP_DEG        = None  # ANGLE of the gap the DM sits in. None = auto (opposite the dash,
                          #   i.e. DASH_MID_DEG + 180). Set a number to override.
DM_RADIUS         = None  # DM DISTANCE from center. None = auto (= RING_RADIUS), so the DM
                          #   center sits ON the blue text circle and visually continues the ring.
DM_NUDGE_X        = 0     # fine left/right nudge of the DM in dots (+ = right)
DM_NUDGE_Y        = 0     # fine up/down nudge of the DM in dots (+ = down)

# ---- Print registration (^PW/^LL/^LS/^LT) --------------------------------
# GEM-style clean 266x266 canvas with no shift. These are the knobs to tune on
# the REAL printer if the image lands off-center on the physical media.
PRINT_WIDTH   = 266   # ^PW  print width in dots
LABEL_LENGTH  = 266   # ^LL  label length in dots
LABEL_SHIFT_X = 0     # ^LS  horizontal shift (negative = left)
LABEL_TOP_Y   = 0     # ^LT  vertical shift (negative = up)

# ---- Physical-boundary safety net (mm, measured from the hole/label center) --
# A future knob change that pushes the DM into the hole or past the outer edge
# fails LOUD here instead of silently shipping a bad label.
DM_INNER_CLEARANCE_MM = 2.5   # DM must stay entirely OUTSIDE this radius (the 5 mm hole)
DM_OUTER_BOUND_MM     = 5.64  # DM must stay entirely INSIDE this radius (outer edge)

# ---- Preview overlay colors / weights (preview PNG only, NEVER printed) ---
PREVIEW_UPSCALE     = 3           # render the preview this many times bigger for crisp guide lines
PREVIEW_OUTER_COLOR = (220, 0, 0)   # RED  -- outer label edge
PREVIEW_INNER_COLOR = (220, 0, 0)   # RED  -- inner die-cut hole
PREVIEW_CURVE_COLOR = (0, 0, 220)   # BLUE -- text curvature (baseline)
PREVIEW_GUIDE_WIDTH = 1           # guide-line thickness in *label* dots (scaled up with the canvas)

FONT_CANDIDATES = [
    "RobotoMono-Regular.ttf", "Roboto Mono", "consola.ttf",
    "DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

# #########################################################################
# #   ===================  END TUNING KNOBS  =======================      #
# #########################################################################


# =========================================================================
# Rendering -- curved text + Data Matrix -> 1-bit image -> ^GFA
# (adapted from ZPL_Script.py; see that file for the original commentary)
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
                  letter_spacing_px=0, stroke_width=0, center=None):
    """Paste text glyph-by-glyph around an arc centered on mid_deg. `canvas` is
    mode 'L' with ink drawn as 255 on a 0 background.

    letter_spacing_px is the "tracking" knob (extra dots per glyph before the
    angular step). stroke_width is the "boldness" knob (outline dots per glyph).
    center is the arc center on `canvas` (defaults to the module CENTER); pass a
    scaled value when rendering supersampled.
    """
    c = CENTER if center is None else center
    ang = [(_char_w(font, ch, stroke_width) + letter_spacing_px) / radius for ch in text]
    total = sum(ang)
    direction = -1 if clockwise else 1
    a = math.radians(mid_deg) - direction * total / 2.0
    pad = font.size * 3 + stroke_width * 2
    for ch, aw in zip(text, ang):
        ca = a + direction * aw / 2.0                 # this glyph's center angle
        x = c + radius * math.cos(ca)
        y = c + radius * math.sin(ca)
        rot = -(math.degrees(ca) + (90 if not clockwise else -90))
        g = Image.new("L", (pad, pad), 0)
        ImageDraw.Draw(g).text((g.width / 2, g.height / 2), ch, font=font, fill=255,
                               anchor="mm", stroke_width=stroke_width, stroke_fill=255)
        g = g.rotate(rot, resample=Image.BICUBIC, expand=True)
        canvas.paste(g, (int(x - g.width / 2), int(y - g.height / 2)), g)
        a += direction * aw


def image_to_gfa(canvas):
    """'L' image (ink=255) -> ZPL ^GFA field. Ink pixel => printed dot (bit 1).
    Emits ONE unbroken hex string (no embedded whitespace -- unlike the ^GFA in
    GEM_NOZZLE.txt, whose spaces could corrupt the graphic on some interpreters)."""
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


def render_ring(text):
    """Render the curved `text` (no Data Matrix) on the full round-label canvas,
    then crop tight to the ink. Returns (cropped_image, (cx_in_crop, cy_in_crop)),
    where the second value is where the RING'S CENTER lands inside the crop (the
    tight crop trims unequal margins, so it isn't the middle of the rectangle).

    Rendered supersampled (RING_SUPERSAMPLE) then downsampled to the 600-dpi grid,
    so glyph edges land on the best-fit dots -> the crispest possible 1-bit text.
    image_to_gfa applies the final threshold."""
    ss = max(1, int(RING_SUPERSAMPLE))
    canvas = Image.new("L", (LABEL_DOTS * ss, LABEL_DOTS * ss), 0)
    draw_arc_text(canvas, text, _load_font(RING_FONT_PX * ss), RING_RADIUS * ss,
                  DASH_MID_DEG, RING_CLOCKWISE,
                  letter_spacing_px=RING_LETTER_SPACING_PX * ss,
                  stroke_width=RING_STROKE_WIDTH * ss, center=CENTER * ss)
    if ss != 1:
        canvas = canvas.resize((LABEL_DOTS, LABEL_DOTS), Image.LANCZOS)
    bbox = canvas.getbbox()
    if bbox is None:
        raise ValueError(f"Curved text for '{text}' produced no ink -- check the RING_* knobs.")
    cropped = canvas.crop(bbox)
    center_in_crop = (CENTER - bbox[0], CENTER - bbox[1])
    return cropped, center_in_crop


def _dm_symbol_dots():
    """Side length in dots of the native Data Matrix square."""
    return DM_SYMBOL_MODULES * DM_MODULE


def _render_dm_image(slot, s):
    """PREVIEW ONLY. Render the Data Matrix for `slot` as an s x s ink mask (ink=255)
    via treepoem's ECC200 generator, forced to a DM_SYMBOL_MODULES x DM_SYMBOL_MODULES
    grid. This is a layout stand-in so preview.png shows the DM's size/position vs the
    blue/red guide circles -- the PRINTED symbol is drawn natively by the printer's own
    ^BX renderer (see build_zpl), which is sharper than this rasterized preview."""
    dm = treepoem.generate_barcode(
        barcode_type="datamatrix", data=slot,
        options={"version": f"{DM_SYMBOL_MODULES}x{DM_SYMBOL_MODULES}"},
    ).convert("L")
    dm = dm.resize((s, s), Image.NEAREST)
    return dm.point(lambda p: 255 if p < INK_THRESHOLD else 0)


def _validate_dm_geometry(dmx, dmy, s, cx, cy):
    """Raise ValueError if the axis-aligned DM square [dmx,dmx+s]x[dmy,dmy+s]
    doesn't clear the inner hole or overruns the outer edge, measured from the
    hole/label center (cx, cy). Safety net, not a placement strategy."""
    nx = min(max(cx, dmx), dmx + s)
    ny = min(max(cy, dmy), dmy + s)
    min_dist_mm = math.hypot(cx - nx, cy - ny) / DPI * 25.4

    fx = dmx if abs(cx - dmx) > abs(cx - (dmx + s)) else dmx + s
    fy = dmy if abs(cy - dmy) > abs(cy - (dmy + s)) else dmy + s
    max_dist_mm = math.hypot(cx - fx, cy - fy) / DPI * 25.4

    if min_dist_mm < DM_INNER_CLEARANCE_MM:
        raise ValueError(
            f"Data Matrix overlaps the inner hole: closest edge is {min_dist_mm:.3f} mm "
            f"from center, but must be >= {DM_INNER_CLEARANCE_MM} mm. Move it out via "
            f"DM_RADIUS/DM_NUDGE_*, or shrink it with DM_MODULE."
        )
    if max_dist_mm > DM_OUTER_BOUND_MM:
        raise ValueError(
            f"Data Matrix extends past the outer edge: farthest corner is {max_dist_mm:.3f} mm "
            f"from center, but must be <= {DM_OUTER_BOUND_MM} mm. Pull it in via "
            f"DM_RADIUS/DM_NUDGE_*, or shrink it with DM_MODULE."
        )


def _placement(part, number):
    """Compute the (^LH0,0-relative) top-left corners for the ring graphic and the
    Data Matrix so the ring is concentric with the label and the DM sits on the
    text circle inside the ring's gap.

    `dmx, dmy` is the top-left corner where the native ^BX symbol is placed (its
    ^FO). `dm_ink` is a preview-only stand-in bitmap (see _render_dm_image).

    Returns (cropped_ring, ring_gfa, gx, gy, dmx, dmy, s, dm_ink)."""
    text = f"{part}-{number:04d}"
    cropped, (ccx, ccy) = render_ring(text)
    ring_gfa = image_to_gfa(cropped)

    # Ring center -> label center (concentric with the donut).
    gx = round(CENTER - ccx)
    gy = round(CENTER - ccy)

    # Data Matrix: centered on the text circle (DM_RADIUS, default = RING_RADIUS)
    # in the gap opposite the dash (DM_GAP_DEG, default = DASH_MID_DEG + 180).
    gap_deg = DM_GAP_DEG if DM_GAP_DEG is not None else DASH_MID_DEG + 180
    dm_radius = DM_RADIUS if DM_RADIUS is not None else RING_RADIUS
    gap_rad = math.radians(gap_deg)
    dm_cx = CENTER + dm_radius * math.cos(gap_rad) + DM_NUDGE_X
    dm_cy = CENTER + dm_radius * math.sin(gap_rad) + DM_NUDGE_Y

    s = _dm_symbol_dots()
    dmx = round(dm_cx - s / 2)
    dmy = round(dm_cy - s / 2)
    _validate_dm_geometry(dmx, dmy, s, CENTER, CENTER)

    slot = f"N{number:04d}"
    dm_ink = _render_dm_image(slot, s)   # preview stand-in only
    return cropped, ring_gfa, gx, gy, dmx, dmy, s, dm_ink


def render_preview_image(part, number):
    """Full-label preview (RGB) laid out EXACTLY like build_zpl: ring + the SAME
    Data Matrix bitmap that ships to the printer, composited black-on-white, with
    guide geometry drawn on top:
        RED  outer circle  -> physical outer edge of the label
        RED  inner circle  -> 5 mm die-cut hole
        BLUE circle        -> text curvature (baseline)

    The guide circles are a VISUAL AID ONLY -- they are NOT in the ZPL sent to the
    printer (build_zpl emits just the two ^GFA fields). Rendered at PREVIEW_UPSCALE
    for crisp lines."""
    cropped, _g, gx, gy, dmx, dmy, _s, dm_ink = _placement(part, number)

    # Compose the design (ink=255) at native label size.
    label = Image.new("L", (LABEL_DOTS, LABEL_DOTS), 0)
    label.paste(cropped, (gx, gy), cropped)   # ink-only mask: 0s stay transparent
    label.paste(dm_ink, (dmx, dmy), dm_ink)

    # Black-on-white, upscaled, as RGB so we can draw colored guides.
    k = max(1, int(PREVIEW_UPSCALE))
    bw = label.point(lambda p: 0 if p >= INK_THRESHOLD else 255)  # black design on white
    big = bw.resize((LABEL_DOTS * k, LABEL_DOTS * k), Image.NEAREST).convert("RGB")

    draw = ImageDraw.Draw(big)
    cx = cy = CENTER * k
    lw = max(1, PREVIEW_GUIDE_WIDTH * k)

    def circle(r, color):
        draw.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k],
                     outline=color, width=lw)

    circle(OUTER_RADIUS, PREVIEW_OUTER_COLOR)   # RED outer
    circle(INNER_RADIUS, PREVIEW_INNER_COLOR)   # RED inner hole
    circle(RING_RADIUS,  PREVIEW_CURVE_COLOR)   # BLUE text curvature
    return big


# =========================================================================
# ZPL builder -- GEM-style clean 266x266 template
# =========================================================================
#
# IMPORTANT ZPL-comment rule (learned in ZPL_Script.py): every ';' comment must be
# on its OWN line. ZPL keeps reading a command's value until the next ^ or ~, so a
# comment appended after a command on the SAME line gets swallowed into that
# command's parameter (e.g. "^LL266 ; length" parses the length as "266 ; length").
# =========================================================================

def build_zpl(part, number):
    """Raw ZPL for one 266x266 round label: a config ^XA..^XZ block, then the print
    block with the curved-text ^GFA and a NATIVE ^BX Data Matrix (rows = cols =
    DM_SYMBOL_MODULES, so it's forced square and printer-sharp). The DM payload is
    always N<number> matching the text's number."""
    _c, ring_gfa, gx, gy, dmx, dmy, _s, _dm = _placement(part, number)
    slot = f"N{number:04d}"

    # Native Data Matrix: ^BX o,h,s,c,r -- orientation, module dots, ECC level,
    # columns, rows. Forcing c = r = DM_SYMBOL_MODULES pins it to a square 10x10.
    dm_field = f"^BX{DM_ORIENT},{DM_MODULE},{DM_QUALITY},{DM_SYMBOL_MODULES},{DM_SYMBOL_MODULES}"

    return (
        "^CT~~CD,~CC^~CT~\n"
        "^XA\n"
        "~TA000\n"
        "~JSN\n"
        "^MMP\n"
        "^MPE\n"
        "~SD14\n"
        "^JZY\n"
        "^MFN,N\n"
        "^XZ\n"
        "\n"
        "^XA\n"
        "^PR2\n"
        "^PW400\n"
        f"^LL{LABEL_LENGTH}\n"
        "^LH0,0\n"
        "; LH118, 2\n"
        "^MNM,0\n"
        "^MTT\n"
        "^PMN\n"
        f"^LS{LABEL_SHIFT_X}\n"
        f"^LT{LABEL_TOP_Y}\n"
        "\n"
        f"^FO{gx},{gy}{ring_gfa}^FS\n"
        f"^FO{dmx},{dmy}{dm_field}^FD{slot}^FS\n"
        "^XZ\n"
    )


# =========================================================================
# Public API
# =========================================================================

def generate_label(part="CH0204", number=1234, include_preview_png=True):
    """Build one nozzle label as raw ZPL.

    part:   tool/part name (default CH0204 for testing).
    number: 4-digit serial baked into BOTH the text (<part>-<number>) and the
            Data Matrix payload (N<number>) so they always match.
    include_preview_png: also return a base64 PNG preview (with the red/blue guides).

    Returns a dict: {"part","number","slot","text","zpl"[, "preview_png_base64"]}.
    """
    text = f"{part}-{number:04d}"
    slot = f"N{number:04d}"
    result = {
        "part": part,
        "number": number,
        "slot": slot,
        "text": text,
        "zpl": build_zpl(part, number),
    }
    if include_preview_png:
        preview = render_preview_image(part, number)
        buf = io.BytesIO()
        preview.save(buf, format="PNG")
        result["preview_png_base64"] = base64.b64encode(buf.getvalue()).decode("ascii")
    return result


# NOTE: V2 intentionally does NOT copy the SWAP-manifest auto-numbering or the
# allowed_parts.txt whitelist machinery from ZPL_Script.py -- it takes `number`
# as a parameter (Retool will supply it). That logic can be ported later if
# standalone auto-assignment is ever needed here.


# =========================================================================
# RETOOL INTEGRATION HOOK -- not wired up yet, transport TBD.
# generate_label() above is the one call site the website needs.
#
# --- Option A: Retool Workflow "Python code" block calls this in-process ---
# result = generate_label(args["part"], number=int(args["number"]))
# return result
#
# --- Option B: small HTTP endpoint Retool calls as a REST resource -------
# import json
# from http.server import BaseHTTPRequestHandler, HTTPServer
#
# class Handler(BaseHTTPRequestHandler):
#     def do_POST(self):
#         length = int(self.headers["Content-Length"])
#         body = json.loads(self.rfile.read(length))
#         try:
#             payload = generate_label(body.get("part", "CH0204"), int(body["number"]))
#             status = 200
#         except (ValueError, KeyError) as exc:
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
    ap.add_argument("part", nargs="?", default="CH0204",
                    help="Tool/part name (default: CH0204)")
    ap.add_argument("--number", type=int, default=1234,
                    help="4-digit serial baked into both the text and the DM (default: 1234)")
    ap.add_argument("--png", help="Also save a preview PNG here (with red/blue guides)")
    ap.add_argument("-o", "--out", help="Output .zpl path")
    args = ap.parse_args()

    try:
        result = generate_label(args.part, number=args.number,
                                include_preview_png=bool(args.png))
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    if args.png:
        with open(args.png, "wb") as f:
            f.write(base64.b64decode(result["preview_png_base64"]))
        print(f"Wrote preview {args.png}")

    out = args.out or f"label_{result['slot']}.zpl"
    with open(out, "w") as f:
        f.write(result["zpl"])

    print(f"Part: {result['part']}  Text: {result['text']}  DM: {result['slot']}")
    print(f"Wrote {out} ({len(result['zpl'])} bytes).")


if __name__ == "__main__":
    main()
