"""
area_map.py -- VOACAP area coverage map (METHOD 130 / VOAAREA)

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
AGPL-3.0

Pipeline:
  1. Build DA1 deck from request params (based on shipped voaareax.da1 sample)
  2. Write to itshfbc/run/voaareax.da1, run: voacapl <dir> area calc default
  3. Parse VG1 text output by splitting whitespace:
       field[0] = lon_idx (1-based)
       field[1] = lat_idx (1-based)
       field[2] = lat (degrees)
       field[3] = lon (degrees)
       field[9] = REL (0.0-1.0)
     Skip lines containing any letter (headers/mode strings)
     Note: fixed-column parsing fails because negative coords merge e.g. -53.8-163.0
  4. Render with matplotlib+cartopy (Agg backend, no display)
     pcolormesh on PlateCarree, portland colormap, coastlines
  5. Return PNG bytes

DA1 notes (learned from bisection testing):
  - voacapl area mode always reads run/voaareax.da1 regardless of stem arg
  - COMMENT line 1 "COMMENT   VOACAP    subdir/name.voa" sets output path
    -> writes areadata/subdir/name.vg1
  - Output subdir must exist before running
  - ANTENNA files live in antennas/default/, not areadata/default/
  - Use const17.voa (TX) and swwhip.voa (RX) -- both confirmed present
  - SYSTEM power field must be "1." not "0.100" (Fortran fixed-format read)
  - CIRCUIT TX==RX coords is fine for area mode
"""

import io
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import logging

import numpy as np

log = logging.getLogger("voacap_service.area_map")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VOACAP_BIN  = os.environ.get("VOACAP_BIN",  "voacapl")
VOACAP_AREA = os.environ.get("VOACAP_AREA", "/root/itshfbc")
SSN_FILE    = os.environ.get("VOACAP_SSN_FILE",
    "/opt/hamclock-backend/htdocs/ham/HamClock/ssn/ssn-31.txt")
SSN_MODE    = os.environ.get("VOACAP_SSN_MODE", "latest").strip().lower()

DEFAULT_WIDTH  = 800
DEFAULT_HEIGHT = 400

MODE_RSN = {3:0.0, 38:34.0, 13:10.0, 17:14.0, 22:20.0, 19:17.0, 49:43.0}
MODE_RSN_DEFAULT = 17.0
MODE_LABEL = {3:"WSPR", 38:"SSB", 13:"FT8", 17:"FT4", 22:"RTTY", 19:"CW", 49:"AM"}

# Portland colormap colours (from pythonprop/voaAreaPlot.py)
HAMCLOCK_COLORS = [
    "#686460",  # 0%   grey (dead zone)
    "#786060",
    "#886060",
    "#E06460",  # 20%  red
    "#E07450",
    "#E08850",
    "#E0A050",
    "#E8A850",  # 30%  orange
    "#E8B848",
    "#E8C840",
    "#E8D840",
    "#E8EC40",  # 40%  yellow
    "#D8EC40",
    "#C8E840",
    "#B0E040",
    "#98DC40",  # 70%  yellow-green
    "#78D440",
    "#58CC40",
    "#48CC40",  # 80%  green
    "#44CC40",
    "#40CC40",  # 100% green
]

# ---------------------------------------------------------------------------
# SSN
# ---------------------------------------------------------------------------

def _read_ssn_file(path):
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
        values = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    values.append(float(parts[3]))
                except ValueError:
                    pass
        if not values:
            return None
        return values[-1] if SSN_MODE != "average" else round(sum(values)/len(values), 1)
    except Exception:
        return None

def _estimate_ssn(year, month):
    t = (year - 2025) + (month - 1) / 12.0
    return max(1.0, min(300.0, round(180 * math.exp(-0.3 * abs(t)), 1)))

def _resolve_ssn(params, year, month):
    if "SSN" in params:
        try:
            return float(params["SSN"])
        except ValueError:
            pass
    v = _read_ssn_file(SSN_FILE) if SSN_FILE else None
    return v if v is not None else _estimate_ssn(year, month)

# ---------------------------------------------------------------------------
# DA1 deck builder
#
# Template is the shipped voaareax.da1 sample with these substitutions:
#   - COMMENT line 1: output path
#   - AREA card: TX lat/lon
#   - CIRCUIT card: TX lat/lon
#   - TIME/MONTH/SUNSPOT/FREQUENCY/SYSTEM: from request params
# ---------------------------------------------------------------------------

def build_area_deck(year, month, utc, txlat, txlng,
                    path, pow_w, mhz, ssn, rsn,
                    out_subdir="ohb", out_name="pyArea"):

    utc_voa  = utc if utc > 0 else 24
    path_ch  = "L" if path else "S"
    lat_abs  = abs(txlat)
    lat_hem  = "N" if txlat >= 0 else "S"
    lon_abs  = abs(txlng)
    lon_hem  = "E" if txlng >= 0 else "W"

    # COMMENT line 1 controls VG1 output path -- pad to 80 chars
    comment1 = "COMMENT   VOACAP    {}/{}.voa".format(out_subdir, out_name).ljust(80)

    return (
        comment1 + "\n"
        "COMMENT       0    4   -1   -1    1    0 receive.cty\n"
        "COMMENT      {txlat:.3f}  {txlng:.3f} OHB                    0.0 {path_word}\n"
        "AREA         {txlat:.3f}  {txlng:.3f}  -20000.00  20000.00 -20000.00  20000.00   73   73    0\n"
        "COMMENT   Parameters:    4\n"
        "COMMENT   MUF      0\n"
        "COMMENT   DBU      0\n"
        "COMMENT   SNR      0\n"
        "COMMENT   REL      0\n"
        "COMMENT    Any VOACAP default cards may be placed in the file: VOACAP.DEF\n"
        "LINEMAX      55       number of lines-per-page\n"
        "COEFFS    CCIR\n"
        "TIME         {utc}   {utc}    1    1\n"
        "MONTH      {year} {month:.2f}\n"
        "SUNSPOT    {ssn:.0f}.\n"
        "FREQUENCY ${mhz:.3f}\n"
        "LABEL     OHB   OHB\n"
        "CIRCUIT   {lat_str:<6s}  {lon_str:>8s}    {lat_str:<6s}  {lon_str:>8s}  {path_ch}     0\n"
        "SYSTEM       1. 145. 0.10  90. {rsn:.1f} 3.00 0.10\n"
        "FPROB      1.00 1.00 1.00 0.00\n"
        "ANTENNA       1    1    2   30     0.000[default/const17.voa  ] 57.0  500.0000\n"
        "ANTENNA       2    2    2   30     0.000[default/swwhip.voa   ]  0.0    0.0000\n"
        "METHOD      130    0\n"
        "EXECUTE\n"
        "QUIT\n"
    ).format(
        txlat=txlat, txlng=txlng,
        path_word="Long" if path else "Short",
        utc=utc_voa, year=year, month=float(month),
        ssn=ssn, mhz=mhz, rsn=rsn,
        lat_abs=lat_abs, lat_hem=lat_hem,
        lon_abs=lon_abs, lon_hem=lon_hem,
        lat_str="{:.2f}{}".format(lat_abs, lat_hem),
        lon_str="{:.2f}{}".format(lon_abs, lon_hem),
        path_ch=path_ch,
    )

# ---------------------------------------------------------------------------
# Run voacapl area mode
# ---------------------------------------------------------------------------

def run_voaarea(deck):
    """
    Write deck to a tmp clone of itshfbc, run voacapl, return (vg1_path, tmp_dir).
    Caller must shutil.rmtree(tmp_dir) when done.
    """
    run_id   = uuid.uuid4().hex[:8]
    out_sub  = "ohb_{}".format(run_id)
    out_name = "pyArea"

    # Patch output path in deck
    deck = deck.replace("ohb/pyArea", "{}/{}".format(out_sub, out_name), 1)

    tmp_dir  = tempfile.mkdtemp(prefix="voaarea_", dir="/tmp")
    area_dir = os.path.join(tmp_dir, "itshfbc")
    run_dir  = os.path.join(area_dir, "run")
    data_dir = os.path.join(area_dir, "areadata")
    out_dir  = os.path.join(data_dir, out_sub)

    os.makedirs(run_dir)
    os.makedirs(out_dir)   # must exist before voacapl runs

    # Symlink everything from real itshfbc except run/ and areadata/
    real = VOACAP_AREA
    for entry in os.listdir(real):
        if entry in ("run", "areadata"):
            continue
        os.symlink(os.path.join(real, entry), os.path.join(area_dir, entry))

    # Symlink existing areadata subdirs (antenna refs etc)
    real_data = os.path.join(real, "areadata")
    if os.path.isdir(real_data):
        for entry in os.listdir(real_data):
            src = os.path.join(real_data, entry)
            dst = os.path.join(data_dir, entry)
            if not os.path.exists(dst):
                os.symlink(src, dst)

    # Write DA1
    da1 = os.path.join(run_dir, "voaareax.da1")
    with open(da1, "w") as f:
        f.write(deck)

    log.debug("VOAAREA run_id=%s deck:\n%s", run_id, deck)

    try:
        r = subprocess.run(
            [VOACAP_BIN, area_dir, "area", "calc", "default"],
            cwd=run_dir,
            capture_output=True, text=True, timeout=120,
        )
        log.debug("VOAAREA rc=%d stdout=%s", r.returncode, r.stdout[:400])
        if r.stderr:
            log.warning("VOAAREA stderr: %s", r.stderr[:200])
    except subprocess.TimeoutExpired:
        log.error("VOAAREA timed out")
        return None, tmp_dir

    vg1 = os.path.join(out_dir, "{}.vg1".format(out_name))
    if not os.path.exists(vg1):
        log.error("VOAAREA no VG1 at %s (rc=%d)", vg1, r.returncode)
        return None, tmp_dir

    log.info("VOAAREA OK: %s (%d bytes)", vg1, os.path.getsize(vg1))
    return vg1, tmp_dir

# ---------------------------------------------------------------------------
# VG1 parser
#
# Split on whitespace -- fixed columns fail due to merged negative numbers.
# Field indices (confirmed by bisection on live container output):
#   [0]  lon_idx  (1-based integer)
#   [1]  lat_idx  (1-based integer)
#   [2]  lat      (degrees float)
#   [3]  lon      (degrees float)
#   [9]  REL      (0.0-1.0 float)
# Field indices confirmed from VG1 header line:
# [0]=lon_idx [1]=lat_idx [2]=lat [3]=lon [4]=MUF [5]=MODE(letters)
# [6]=ANGLE [7]=DELAY [8]=VHITE [9]=MUFda [10]=LOSS [11]=DBU
# [12]=SDBW [13]=NDBW [14]=SNR [15]=RPWRG [16]=REL
# Skip lines where field[0] is not an integer (header/label lines).
# Do NOT filter on letter presence -- MODE field e.g. "F1F2" is on every data line.
# ---------------------------------------------------------------------------

def parse_vg1(vg1_path):
    """
    Parse VG1 into a regular 2D grid using integer grid indices.
    VG1 fields: [0]=lon_idx [1]=lat_idx [2]=lat [3]=lon ... [16]=REL
    Returns dict {grid, lats, lons} or None.
    """
    raw = []
    try:
        with open(vg1_path, errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 17:
                    continue
                try:
                    li  = int(parts[0])   # lon_idx 1-based
                    lj  = int(parts[1])   # lat_idx 1-based
                    lat = float(parts[2])
                    lon = float(parts[3])
                    rel = float(parts[16])
                    raw.append((li, lj, lat, lon, rel))
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        log.exception("VG1 parse error: %s", e)
        return None

    if not raw:
        log.error("VG1 parse: no data in %s", vg1_path)
        return None

    # Wrap longitudes 0..360 -> -180..180 for cartopy
    points = []
    for li, lj, lat, lon, rel in raw:
        if lon > 180.0:
            lon -= 360.0
        points.append((lat, lon, rel))

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    log.info("VG1 parsed: %d pts lat %.1f..%.1f lon %.1f..%.1f",
             len(points), min(lats), max(lats), min(lons), max(lons))
    return {"raw": points}

# ---------------------------------------------------------------------------
# Render with matplotlib + cartopy
# Mirrors pythonprop's voaAreaPlot.py approach exactly:
#   - PlateCarree projection
#   - pcolormesh with portland colormap
#   - ax.coastlines()
#   - Agg backend (headless)
# ---------------------------------------------------------------------------

def render_map(vg_data, txlat, txlng, mhz, utc, ssn, month, year,
               mode_label, width, height):
    """
    Render coverage map from parse_vg1 dict. Returns PNG bytes.
    Points are on great-circle paths (irregular lat/lon) so we use
    scatter plot rather than pcolormesh.
    Falls back to a blank navy PNG if matplotlib/cartopy unavailable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
        import cartopy.crs as ccrs

        hamclock_cmap = ListedColormap(HAMCLOCK_COLORS, name="hamclock_rel")
        try:
            matplotlib.colormaps.register(cmap=hamclock_cmap)
        except ValueError:
            pass  # already registered

        # vg_data["raw"] is list of (lat, lon, rel) with lon in -180..180
        raw = vg_data["raw"]
        lats_arr = np.array([p[0] for p in raw], dtype=np.float32)
        lons_arr = np.array([p[1] for p in raw], dtype=np.float32)
        rels_arr = np.array([p[2] for p in raw], dtype=np.float32)

        dpi = 100
        fig_w = width  / dpi
        fig_h = height / dpi

        projection = ccrs.PlateCarree()
        fig, ax = plt.subplots(
            figsize=(fig_w, fig_h),
            dpi=dpi,
            subplot_kw={"projection": projection},
        )

        import cartopy.feature as cfeature
        ax.set_global()
        ax.set_facecolor("#1a1a1a")
        fig.patch.set_facecolor("#1a1a1a")

        # Interpolate scattered great-circle points onto a regular geographic grid
        from scipy.interpolate import griddata
        grid_lons = np.linspace(-180, 180, 720)
        grid_lats = np.linspace(-90,   90, 360)
        glon, glat = np.meshgrid(grid_lons, grid_lats)
        grid_rel = griddata(
            (lons_arr, lats_arr), rels_arr,
            (glon, glat),
            method="cubic",
            fill_value=0.0,
        )
        im = ax.pcolormesh(
            glon, glat, grid_rel,
            vmin=0.0, vmax=1.0,
            cmap="hamclock_rel",
            transform=projection,
            shading="auto",
            alpha=0.85,
            zorder=3,
        )

        # Country borders and coastlines drawn AFTER data so they appear on top
        ax.coastlines(linewidth=0.6, color="black", zorder=4)
        ax.add_feature(cfeature.BORDERS, linewidth=0.4, edgecolor="black", zorder=4)
        # No colorbar (matches HamClock style)

        # TX marker
        ax.plot(txlng, txlat, marker="o", color="red", markerfacecolor="none",
                markersize=6, markeredgewidth=1.5, transform=projection, zorder=5)

        # Title
        ax.set_title("")
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        # Hide the geo spine (cartopy border) -- works in both old and new cartopy
        try:
            ax.spines['geo'].set_visible(False)
        except KeyError:
            pass
        try:
            ax.outline_patch.set_visible(False)
        except AttributeError:
            pass

        plt.tight_layout(pad=0.3)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=dpi,
                    facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except ImportError as e:
        log.error("matplotlib/cartopy not available: %s -- returning blank", e)
        return _blank_png(width, height)
    except Exception as e:
        log.exception("render_map error: %s", e)
        return _blank_png(width, height)


def _blank_png(width, height):
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (width, height), (10, 22, 40)).save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # 1x1 navy PNG as absolute last resort
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
            b"\x00\x0cIDATx\x9cc\x08\x16 \x00\x00\x00\x04\x00\x01\xa3"
            b"\x14\x81\x00\x00\x00\x00IEND\xaeB`\x82"
        )


# ---------------------------------------------------------------------------
# BMP565 encoder
# Matches hc_raw_to_bmp565.py used by other OHB map scripts.
# Converts a PNG (bytes) to an uncompressed RGB565 BMP (bytes).
# ---------------------------------------------------------------------------

def png_to_bmp565(png_bytes, width, height):
    """
    Convert PNG bytes -> RGB565 BMP bytes.
    BMP565 = BITMAPFILEHEADER + BITMAPV4HEADER (108 bytes) + pixel data.
    Rows are stored bottom-up, each pixel is 2 bytes little-endian RGB565.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    img = img.resize((width, height), Image.LANCZOS)
    pixels = list(img.getdata())

    row_bytes = width * 2
    # BMP rows must be padded to 4-byte boundary (already aligned for even widths)
    pad = (4 - (row_bytes % 4)) % 4
    padded_row = row_bytes + pad
    pixel_data_size = padded_row * height

    # BITMAPV4HEADER (108 bytes) with BI_BITFIELDS compression and RGB565 masks
    DIB_HEADER_SIZE = 108
    file_size = 14 + DIB_HEADER_SIZE + pixel_data_size
    pixel_offset = 14 + DIB_HEADER_SIZE

    # File header (14 bytes)
    file_header = struct.pack("<2sIHHI",
        b"BM",
        file_size,
        0, 0,
        pixel_offset,
    )

    # BITMAPV4HEADER (108 bytes)
    dib_header = struct.pack("<IiiHHIIiiII",
        DIB_HEADER_SIZE,   # biSize
        width,             # biWidth
        height,            # biHeight (positive = bottom-up, standard BMP)
        1,                 # biPlanes
        16,                # biBitCount
        3,                 # biCompression = BI_BITFIELDS
        pixel_data_size,   # biSizeImage
        2835,              # biXPelsPerMeter (~72dpi)
        2835,              # biYPelsPerMeter
        0,                 # biClrUsed
        0,                 # biClrImportant
    )
    # RGB565 bitmasks + BITMAPV4 color space fields (remaining 52 bytes)
    masks_and_cs = struct.pack("<III",
        0xF800,  # red mask
        0x07E0,  # green mask
        0x001F,  # blue mask
    ) + b"\x00" * 56  # color space padding to reach 108-byte BITMAPV4HEADER

    # Pixel data -- bottom-up rows
    row_pad = b"\x00" * pad
    pixel_buf = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b = pixels[y * width + x]
            pixel_buf += struct.pack("<H",
                ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
        pixel_buf += row_pad

    return bytes(file_header) + dib_header + masks_and_cs + bytes(pixel_buf)

# ---------------------------------------------------------------------------
# WSGI handler
# ---------------------------------------------------------------------------

def handle_area_request(params, start_response):

    def p_int(k):
        v = params.get(k)
        if v is None: raise KeyError(k)
        return int(v)

    def p_float(k):
        v = params.get(k)
        if v is None: raise KeyError(k)
        return float(v)

    def err(code, msg):
        b = msg.encode()
        start_response(code, [("Content-Type","text/plain"),("Content-Length",str(len(b)))])
        return [b]

    REQUIRED = ("YEAR","MONTH","UTC","TXLAT","TXLNG","PATH","WATTS","MHZ","TOA","MODE")
    try:
        year  = p_int  ("YEAR")
        month = p_int  ("MONTH")
        utc   = p_int  ("UTC")
        txlat = p_float("TXLAT")
        txlng = p_float("TXLNG")
        path  = p_int  ("PATH")
        pow_w = p_float("WATTS")
        mhz   = p_float("MHZ")
        toa   = p_float("TOA")
        mode  = p_int  ("MODE")
    except KeyError as e:
        return err("400 Bad Request",
            "Missing required parameter: {}\nRequired: {}\n".format(e, ", ".join(REQUIRED)))
    except ValueError as e:
        return err("400 Bad Request", "Bad parameter value: {}\n".format(e))

    width      = int(params.get("WIDTH",  DEFAULT_WIDTH))
    height     = int(params.get("HEIGHT", DEFAULT_HEIGHT))
    mode_label = MODE_LABEL.get(mode, "MODE{}".format(mode))
    rsn        = MODE_RSN.get(mode, MODE_RSN_DEFAULT)
    ssn        = _resolve_ssn(params, year, month)

    log.info("AreaMap: %d/%02d UTC=%02d TX=(%.4f,%.4f) %.3fMHz %s SSN=%.0f %dx%d",
             year, month, utc, txlat, txlng, mhz, mode_label, ssn, width, height)

    deck = build_area_deck(year, month, utc, txlat, txlng,
                           path, pow_w, mhz, ssn, rsn)
    vg1_path, tmp_dir = run_voaarea(deck)

    try:
        if vg1_path is None:
            png = _blank_png(width, height)
        else:
            vg_data = parse_vg1(vg1_path)
            if not vg_data:
                png = _blank_png(width, height)
            else:
                png = render_map(vg_data, txlat, txlng, mhz, utc, ssn,
                                 month, year, mode_label, width, height)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    bmp = png_to_bmp565(png, width, height)
    start_response("200 OK", [
        ("Content-Type",   "image/bmp"),
        ("Content-Length", str(len(bmp))),
        ("Cache-Control",  "no-store"),
        ("X-Generator",    "OHB-voacap-area"),
    ])
    return [bmp]
