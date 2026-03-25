"""
area_map.py -- VOACAP area coverage map (METHOD 130 / VOAAREA)

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
License: GNU Affero General Public License v3.0 (AGPLv3)
See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>

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
import threading
import uuid
import logging

import numpy as np

import datetime
import ephem

from PIL import Image as _PI, ImageDraw as _PID, ImageFilter

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

# ---------------------------------------------------------------------------
# Mode → Required SNR mapping
#
# HamClock MODE values and their corresponding VOACAP required SNR (dB).
# RSN=17 for CW is empirically calibrated against CSI reference output.
# Other modes are placeholders pending their own calibration runs —
# values are reasonable starting points based on ITU/VOACAP conventions
# but should be validated against reference data when available.
#
# some relative adjustments taken from here:
# https://www.amateurradio.com/weak-signal-performance-of-common-modulation-formats/
#
# MODE  Label   RSN(dB)  Notes
#  38   SSB     34.0
#  13   FT8     10.0
#   3   WSPR     0.0
#  17   FT4     14.0
#  22   RTTY    20.0
#  19   CW      17.0     Calibrated against CSI reference output
#  49   AM      43.0
# ---------------------------------------------------------------------------
MODE_RSN_BASE = -4.4
MODE_RSN: dict[int, float] = {
    3:  MODE_RSN_BASE+ 0.0,   # WSPR     — calibrated based on reference above
    38: MODE_RSN_BASE+34.0,   # SSB      — calibrated based on reference above
    13: MODE_RSN_BASE+10.0,   # FT8      — calibrated based on reference above
    17: MODE_RSN_BASE+14.0,   # FT4      — calibrated based on reference above
    22: MODE_RSN_BASE+20.0,   # RTTY     — calibrated based on reference above
    19: MODE_RSN_BASE+17.0,   # CW       — calibrated
    49: MODE_RSN_BASE+43.0,   # AM       — calibrated based on reference above
}
MODE_RSN_DEFAULT = (MODE_RSN_BASE+17.0)   # fallback for unknown mode codes

MODE_LABEL: dict[int, str] = {
    3:  "WSPR",
    38: "SSB",
    13: "FT8",
    17: "FT4",
    22: "RTTY",
    19: "CW",
    49: "AM",
}


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

# TOA (Take-Off Angle) colormap -- 0-30 degrees
# Colors provided by HamClock reference image
#TOA_COLORS = [
#    "#0000E8",  #  0 deg
#    "#B88880",  #  5 deg
#    "#F09450",  # 10 deg
#    "#F07038",  # 15 deg
#    "#F04C28",  # 20 deg
#    "#F02810",  # 25 deg
#    "#F00400",  # 30 deg
#]

TOA_COLORS = [
    "#0000F0",  #  0 deg  blue
    "#4838C0",  #  2 deg
    "#987490",  #  4 deg
    "#E8AC60",  #  6 deg  tan
    "#F0A058",  #  8 deg
    "#F09050",  # 10 deg  orange
    "#F08448",  # 12 deg
    "#F07440",  # 14 deg
    "#F06438",  # 16 deg
    "#F05830",  # 18 deg
    "#F04C28",  # 20 deg  (interpolated, label obscured)
    "#F03820",  # 22 deg
    "#F02C18",  # 24 deg
    "#F01C10",  # 26 deg
    "#F00C08",  # 28 deg
    "#F00000",  # 30 deg  red
]
TOA_MAX = 30.0  # degrees

# VG1 field indices -- full format (METHOD 130 with frequency)
VG1_LAT        = 2
VG1_LON        = 3
VG1_MUF        = 4   # MUF (MHz)
VG1_ANGLE      = 6   # TOA / take-off angle (degrees)
VG1_REL        = 16  # REL in full format
VG1_MIN_FULL   = 17  # minimum fields for full format

# VG1 field indices -- short format (MUF-only run, MHZ=0)
VG1S_MUF   = 4
VG1S_REL   = 8
VG1S_MIN   = 9   # minimum fields for short format

# MUF colormap -- 0-35 MHz
MUF_COLORS = [
    "#000000",  #  0 MHz  black
    "#401498",  #  5 MHz  purple
    "#1040E8",  # 10 MHz  blue
    "#78F8D0",  # 15 MHz  cyan
    "#78F840",  # 20 MHz  green
    "#D0FC50",  # 25 MHz  yellow-green
    "#E87428",  # 30 MHz  orange
    "#E84020",  # 35 MHz  red
]
MUF_MAX = 35.0  # MHz

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

def fmt_4c(flt: float) -> str:
    if flt >= 100.0: 
        return f"{flt:4.1f}"[0:4]
    elif flt >= 10.0:
        return f"{flt:4.1f}"
    elif flt >= 1.0:
        return f"{flt:4.2f}"
    else:
        return f"{flt:.3f}"[1:]           # start at slice 1 to skip leading 0
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
                    path, pow_w, mhz, ssn, rsn, toa,
                    out_subdir="ohb", out_name="pyArea"):

    utc_voa  = utc if utc > 0 else 24
    path_ch  = "L" if path else "S"
    lat_abs  = abs(txlat)
    lat_hem  = "N" if txlat >= 0 else "S"
    lon_abs  = abs(txlng)
    lon_hem  = "E" if txlng >= 0 else "W"

    # COMMENT line 1 controls VG1 output path -- pad to 80 chars
    comment1 = "COMMENT   VOACAP    {}/{}.voa".format(out_subdir, out_name).ljust(80)
    pow_kw=pow_w/1000
    return (
        comment1 + "\n"
        "COMMENT       0    4   -1   -1    1    0 receive.cty\n"
        "COMMENT     {txlat:07.3f}  {txlng:08.3f} OHB                    0.0 {path_word}\n"
        "AREA        {txlat:07.3f}  {txlng:08.3f}  -20000.00  20000.00 -20000.00  20000.00   37   37    0\n"
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
        "SYSTEM     {fp} 145. {ta}  90. {rn} 3.00 0.00\n"
        "FPROB      0.00 0.00 0.00 0.00\n"
        "ANTENNA       1    1    2   30     0.000[default/isotrope     ]  0.0  {pow_kw}\n"
        "ANTENNA       2    2    2   30     0.000[default/isotrope     ]  0.0    0.0000\n"
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
        lat_str="{:05.2f}{}".format(lat_abs, lat_hem),
        lon_str="{:06.2f}{}".format(lon_abs, lon_hem),
        path_ch=path_ch,
        pow_kw=pow_w/1000,
        fp=fmt_4c(pow_kw),
        ta=fmt_4c(toa),
        rn=fmt_4c(rsn)
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
    Parse VG1 text output. Extracts REL (field 16) and ANGLE/TOA (field 6).
    Returns dict {"raw": [(lat, lon, rel, angle), ...]} or None.
    """
    import re
    # Fortran fixed-width output merges adjacent negative numbers e.g. "-4.7-136.2"
    # Split on sign boundaries: insert space before '-' that follows a digit or '.'
    _split_neg = re.compile(r'(?<=[\d.])(-)')

    def split_line(line):
        return _split_neg.sub(lambda m: ' ' + m.group(1), line).split()

    raw = []
    try:
        with open(vg1_path, errors="replace") as f:
            for line in f:
                parts = split_line(line)
                if len(parts) < VG1S_MIN:
                    continue
                try:
                    int(parts[0])   # lon_idx -- fails on header lines
                    int(parts[1])   # lat_idx
                    lat = float(parts[VG1_LAT])
                    lon = float(parts[VG1_LON])
                    if len(parts) >= VG1_MIN_FULL:
                        # Full format: has ANGLE, REL at [16]
                        rel   = float(parts[VG1_REL])
                        angle = float(parts[VG1_ANGLE])
                        muf   = float(parts[VG1_MUF])
                    elif len(parts) >= VG1S_MIN:
                        # Short format: MUF-only run (MHZ=0), no ANGLE
                        rel   = float(parts[VG1S_REL])
                        angle = 0.0
                        muf   = float(parts[VG1S_MUF])
                    else:
                        continue
                    if lon > 180.0:
                        lon -= 360.0
                    raw.append((lat, lon, rel, angle, muf))
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        log.exception("VG1 parse error: %s", e)
        return None

    if not raw:
        log.error("VG1 parse: no data in %s", vg1_path)
        return None

    lats = [p[0] for p in raw]
    lons = [p[1] for p in raw]
    log.info("VG1 parsed: %d pts lat %.1f..%.1f lon %.1f..%.1f",
             len(raw), min(lats), max(lats), min(lons), max(lons))
    return {"raw": raw}

# ---------------------------------------------------------------------------
# Render with matplotlib + cartopy
# Mirrors pythonprop's voaAreaPlot.py approach exactly:
#   - PlateCarree projection
#   - pcolormesh with portland colormap
#   - ax.coastlines()
#   - Agg backend (headless)
# ---------------------------------------------------------------------------

def interpolate_grid(vg_data, map_type="REL"):
    """Interpolate VG1 data onto a regular grid. Returns (grid, glon, glat, vmin, vmax, cmap_colors, cmap_name)."""
    from scipy.interpolate import griddata

    raw = vg_data["raw"]

    if map_type == "TOA":
        cmap_colors = TOA_COLORS
        cmap_name   = "hamclock_toa"
        vmin, vmax  = 0.0, TOA_MAX
#        vals_arr    = np.array([p[3] for p in vg_data["raw"]], dtype=np.float32)
        # Filter out VOACAP no-propagation sentinel (<=1.0 deg clamp)
        toa_pts = [(p[0], p[1], p[3]) for p in raw if 1.0 < p[3] <= 30.0]
        if not toa_pts:
           toa_pts = [(p[0], p[1], p[3]) for p in raw]
        lats_arr = np.array([p[0] for p in toa_pts], dtype=np.float32)
        lons_arr = np.array([p[1] for p in toa_pts], dtype=np.float32)
        vals_arr = np.array([p[2] * 0.32 for p in toa_pts], dtype=np.float32)
    elif map_type == "MUF":
        cmap_colors = MUF_COLORS
        cmap_name   = "hamclock_muf"
        vmin, vmax  = 0.0, MUF_MAX
#        vals_arr    = np.array([p[4] for p in vg_data["raw"]], dtype=np.float32)
        lats_arr = np.array([p[0] for p in raw], dtype=np.float32)
        lons_arr = np.array([p[1] for p in raw], dtype=np.float32)
        vals_arr = np.array([p[4] for p in raw], dtype=np.float32)
    else:
        cmap_colors = HAMCLOCK_COLORS
        cmap_name   = "hamclock_rel"
        vmin, vmax  = 0.0, 1.0
        #vals_arr    = np.array([p[2] for p in vg_data["raw"]], dtype=np.float32)

#    raw      = vg_data["raw"]
#    lats_arr = np.array([p[0] for p in raw], dtype=np.float32)
#    lons_arr = np.array([p[1] for p in raw], dtype=np.float32)
        lats_arr = np.array([p[0] for p in raw], dtype=np.float32)
        lons_arr = np.array([p[1] for p in raw], dtype=np.float32)
        vals_arr = np.array([p[2] for p in raw], dtype=np.float32)
    grid_lons = np.linspace(-180, 180, 360)
    grid_lats = np.linspace(-90,   90, 180)
    glon, glat = np.meshgrid(grid_lons, grid_lats)

    # Wrap data points at ±180° to eliminate the date line seam.
    # Duplicate points near the edges shifted by ±360° so the interpolator
    # has data on both sides of the boundary.
    wrap_mask_pos = lons_arr > 90
    wrap_mask_neg = lons_arr < -90

    # Assuming your lats_arr is sorted, we find the highest unique latitude 
    # that isn't the pole itself.
    unique_lats = np.unique(lats_arr)
    near_pole_lat = unique_lats[-1] # The highest latitude available in your data

    # Get all data points sitting on that latitude line
    near_pole_mask = lats_arr == near_pole_lat

    # We create a new set of points at 90.0N using the values from that last row
    pole_lons = np.linspace(-180, 180, 100) # Dense enough to "seal" the grid
    pole_lats = np.full_like(pole_lons, 90.0)

    # This repeats the values from your near_pole_lat across the entire 90N line
    # Note: If your last row has multiple longitudes, you might want to interpolate 
    # those onto these new pole_lons, but usually, just repeating the values works.
    pole_vals = np.interp(pole_lons, lons_arr[near_pole_mask], vals_arr[near_pole_mask])

    lons_wrapped = np.concatenate([lons_arr,
                                   lons_arr[wrap_mask_pos] - 360.0,
                                   lons_arr[wrap_mask_neg] + 360.0,
                                   pole_lons
    ])
    lats_wrapped = np.concatenate([lats_arr,
                                   lats_arr[wrap_mask_pos],
                                   lats_arr[wrap_mask_neg],
                                   pole_lats
    ])
    vals_wrapped = np.concatenate([vals_arr,
                                   vals_arr[wrap_mask_pos],
                                   vals_arr[wrap_mask_neg],
                                   pole_vals
    ])

    # Linear interpolation inside convex hull
    grid_rel = griddata(
        (lons_wrapped, lats_wrapped), vals_wrapped,
        (glon, glat),
        method="linear",
        fill_value=np.nan,
    )

    # Fill NaN regions (outside convex hull) using nearest-neighbor extrapolation
    nan_regions = np.isnan(grid_rel)
    if nan_regions.any():
        grid_nearest = griddata(
            (lons_wrapped, lats_wrapped), vals_wrapped,
            (glon, glat),
            method="nearest",
        )
        grid_rel[nan_regions] = np.clip(grid_nearest[nan_regions], 0.0, 20.0 if map_type == "TOA" else 1e9)

    return grid_rel, glon, glat, vmin, vmax, cmap_colors, cmap_name


# Coastline polygons loaded once at module level
_COASTLINES = None
_BORDERS    = None
_COAST_CACHE = {}

def _load_coastlines():
    """Load Natural Earth coastline/border polygons from cartopy cache."""
    global _COASTLINES, _BORDERS
    if _COASTLINES is not None:
        return
    try:
        import cartopy.io.shapereader as shpreader
        coast_shp  = shpreader.natural_earth(resolution="110m", category="physical", name="coastline")
        border_shp = shpreader.natural_earth(resolution="110m", category="cultural", name="admin_0_boundary_lines_land")
        _COASTLINES = list(shpreader.Reader(coast_shp).geometries())
        _BORDERS    = list(shpreader.Reader(border_shp).geometries())
        log.info("Coastlines loaded: %d coast, %d border geoms", len(_COASTLINES), len(_BORDERS))
        # Pre-warm coast cache for common HamClock screen sizes in a background thread
        from PIL import Image as _PI, ImageDraw as _PID
        for _w, _h in [(660,330),(800,400),(1320,660),(1980,990),(2640,1320),(3960,1980),(5280,2640),(5940,2970),(7920,3960)]:
            if (_w, _h) not in _COAST_CACHE:
                _ci = _PI.new("RGBA", (_w, _h), (0,0,0,0))
                _cd = _PID.Draw(_ci)
                for _g in _COASTLINES:
                    _draw_geom_lines(_cd, _g, _w, _h, (0,0,0,255), line_width=2)
                for _g in _BORDERS:
                    _draw_geom_lines(_cd, _g, _w, _h, (0,0,0,255), line_width=3)
                _COAST_CACHE[(_w, _h)] = _ci
                log.info("Coast cache warmed: %dx%d", _w, _h)
    except Exception as e:
        log.warning("Could not load coastlines: %s", e)
        _COASTLINES = []
        _BORDERS    = []


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _make_colormap_lut(colors_hex, n=256):
    """Build an (n,3) uint8 LUT from a list of hex color stops."""
    stops = [_hex_to_rgb(c) for c in colors_hex]
    lut = np.zeros((n, 3), dtype=np.uint8)
    segs = len(stops) - 1
    for i in range(n):
        t    = i / (n - 1) * segs
        lo   = int(t)
        hi   = min(lo + 1, segs)
        frac = t - lo
        for ch in range(3):
            lut[i, ch] = int(stops[lo][ch] * (1 - frac) + stops[hi][ch] * frac)
    return lut


def _draw_geom_lines(draw, geom, width, height, color, line_width=1):
    """Draw shapely geometry edges onto a PIL ImageDraw."""
    from shapely.geometry import MultiLineString, LineString, MultiPolygon, Polygon, GeometryCollection
    def lonlat_to_xy(lon, lat):
        x = int((lon + 180.0) / 360.0 * width)
        y = int((90.0  - lat)  / 180.0 * height)
        return x, y
    def draw_line(coords):
        pts = [lonlat_to_xy(lo, la) for lo, la in coords]
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=line_width)
    def draw_geom(g):
        if isinstance(g, (LineString,)):
            draw_line(g.coords)
        elif isinstance(g, MultiLineString):
            for part in g.geoms: draw_geom(part)
        elif isinstance(g, Polygon):
            draw_line(g.exterior.coords)
        elif isinstance(g, MultiPolygon):
            for part in g.geoms: draw_geom(part)
        elif isinstance(g, GeometryCollection):
            for part in g.geoms: draw_geom(part)
    draw_geom(geom)


def render_map(vg_data, txlat, txlng, mhz, utc, ssn, month, year,
               mode_label, width, height, map_type="REL",
               _precomputed=None):
    """
    Render coverage map using PIL (fast). Returns PNG bytes.
    Pass _precomputed tuple from interpolate_grid() to skip recomputing.
    """
    try:
        from PIL import Image, ImageDraw

        _load_coastlines()

        if _precomputed is not None:
            grid_rel, glon, glat, vmin, vmax, cmap_colors, cmap_name = _precomputed
        else:
            grid_rel, glon, glat, vmin, vmax, cmap_colors, cmap_name = interpolate_grid(vg_data, map_type)

        # Build colormap LUT and map grid values -> RGB
        lut = _make_colormap_lut(cmap_colors)
        nan_mask = np.flipud(np.isnan(grid_rel))
        safe_grid = np.flipud(np.where(np.isnan(grid_rel), 0.0, grid_rel))
        clipped = np.clip((safe_grid - vmin) / (vmax - vmin), 0.0, 1.0)

        indices = (clipped * 255).astype(np.uint8)
        rgb = lut[indices]  # shape (H, W, 3)

        # NaN = outside VOACAP convex hull
        # Uses colormap index 0 (dead zone for REL, black for MUF, blue for TOA)

        img = Image.fromarray(rgb.astype(np.uint8), mode="RGB")
        img = img.resize((width, height), Image.BILINEAR)

        draw = ImageDraw.Draw(img)

        # Draw coastlines and borders (cached per size)
        coast_key = (width, height)
        if coast_key not in _COAST_CACHE:
            from PIL import Image as _CI
            _coast_img = _CI.new("RGBA", (width, height), (0, 0, 0, 0))
            _coast_draw = ImageDraw.Draw(_coast_img)
            for geom in _COASTLINES:
                _draw_geom_lines(_coast_draw, geom, width, height, (0, 0, 0, 255), line_width=2)
            for geom in _BORDERS:
                _draw_geom_lines(_coast_draw, geom, width, height, (0, 0, 0, 255), line_width=3)
            _COAST_CACHE[coast_key] = _coast_img
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, _COAST_CACHE[coast_key])
        img = img.convert("RGB")

        # TX marker (open circle)
        tx_x = int((txlng + 180.0) / 360.0 * width)
        tx_y = int((90.0 - txlat)  / 180.0 * height)
        r = 5
        draw.ellipse([tx_x-r, tx_y-r, tx_x+r, tx_y+r], outline=(255, 0, 0), width=2)

        # Day map: add subtle white haze to distinguish from night
        #if not night:
        #    from PIL import Image as _Img
        #    haze = _Img.new("RGBA", img.size, (255, 255, 255, 40))
        #    img = img.convert("RGBA")
        #    img = _Img.alpha_composite(img, haze).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()

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
        -height,           # biHeight (negative = top-down, matches CSI)
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

    # Pixel data -- vectorized numpy RGB565 conversion (fast for large images)
    arr = np.array(img)  # shape (H, W, 3) uint8
    r = arr[:, :, 0].astype(np.uint16)
    g = arr[:, :, 1].astype(np.uint16)
    b = arr[:, :, 2].astype(np.uint16)
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    # Convert to little-endian bytes
    pixel_data = rgb565.astype('<u2').tobytes()
    # Add row padding if needed
    if pad > 0:
        rows = []
        row_size = width * 2
        pad_bytes = b"\x00" * pad
        for y in range(height):
            rows.append(pixel_data[y*row_size:(y+1)*row_size] + pad_bytes)
        pixel_data = b"".join(rows)

    return bytes(file_header) + dib_header + masks_and_cs + pixel_data

# ---------------------------------------------------------------------------
# WSGI handler
# ---------------------------------------------------------------------------

def get_subsolar_point():
    """Get the lat/lon of the point on Earth directly under the sun."""
    now = datetime.datetime.utcnow()
    
    obs = ephem.Observer()
    obs.date = now
    obs.lat = '0'
    obs.lon = '0'
    obs.elevation = 0
    
    sun = ephem.Sun(obs)
    
    # Sun's declination = subsolar latitude
    lat = math.degrees(sun.dec)
    
    # Convert RA to longitude using Greenwich Sidereal Time
    gst = obs.sidereal_time()  # returns GST in radians as ephem angle
    lon = math.degrees(sun.ra - gst)
    lon = (lon + 180) % 360 - 180  # normalize to -180..180
    
    return lat, lon


def add_night_overlay(image, darkness):
    """
    Overlay a night-side darkening on a world map image.
    
    Args:
        image: PIL Image of the world map (equirectangular projection)
        darkness: 0.0 = no darkening, 1.0 = fully black
    
    Returns:
        New PIL Image with night side darkened
    """
    from PIL import Image as _PI, ImageDraw as _PID, ImageFilter
    
    width, height = image.size

    # Get subsolar point
    sun_lat, sun_lon = get_subsolar_point()
    print(f"Subsolar point: lat={sun_lat:.2f}, lon={sun_lon:.2f}")
    sun_lat_r = math.radians(sun_lat)
    sun_lon_r = math.radians(sun_lon)

    # --- Vectorized day/night mask ---
    lons = np.linspace(-180, 180, width, endpoint=False)
    lats = np.linspace(90, -90, height, endpoint=False)
    lon_grid, lat_grid = np.meshgrid(np.radians(lons), np.radians(lats))

    cos_angle = (
        math.sin(sun_lat_r) * np.sin(lat_grid) +
        math.cos(sun_lat_r) * np.cos(lat_grid) * np.cos(lon_grid - sun_lon_r)
    )

    # 0=night, 255=day — use cos_angle clipped and scaled for smooth twilight band
    mask_array = np.clip(cos_angle * 255 / 0.1, 0, 255).astype(np.uint8)
    # The 0.1 factor controls the width of the twilight transition band

    # Blur terminator edge for a soft transition
    mask = _PI.fromarray(mask_array)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=width // 100))
    mask_array = np.array(mask, dtype=np.float32) / 255.0  # range 0.0 (night) to 1.0 (day)

    # Save debug mask
    # _PI.fromarray((mask_array * 255).astype(np.uint8)).save("debug_mask.png")

    # --- Compositing: darken night side ---
    result_array = np.array(image.convert("RGB"), dtype=np.float32)

    # alpha: 0.0 = full day (no darkening), darkness = full night
    alpha = (1.0 - mask_array) * darkness  # shape (H, W)

    # Apply darkening with a slight blue night tint
    result_array[:, :, 0] = np.clip(result_array[:, :, 0] * (1.0 - alpha),        0, 255)  # R
    result_array[:, :, 1] = np.clip(result_array[:, :, 1] * (1.0 - alpha),        0, 255)  # G
    result_array[:, :, 2] = np.clip(result_array[:, :, 2] * (1.0 - alpha * 0.7),  0, 255)  # B (darken less for blue tint)

    return _PI.fromarray(result_array.astype(np.uint8))

    
def process_map_with_night(bytebuf, darkness: float = 0.5):
    from PIL import Image as _PI
    # Load image from input buffer
    buf = io.BytesIO(bytebuf)
    img = _PI.open(buf)
    
    # Apply night overlay
    result = add_night_overlay(img, darkness=darkness)
    
    # Save result to a new BytesIO buffer   
    out_buf = io.BytesIO()
    result.save(out_buf, format="PNG")
    out_buf.seek(0)
    return out_buf.read()
# ---------------------------------------------------------------------------
# Server-side BMP pair cache
# ---------------------------------------------------------------------------

def _blank_bmp(width, height):
    return png_to_bmp565(_blank_png(width, height), width, height)

def _build_response(bmp_day, bmp_night, environ, start_response, generator="OHB-voacap-area"):
    import zlib
    z_day   = zlib.compress(bmp_day,   level=6)
    z_night = zlib.compress(bmp_night, level=6)
    body    = z_day + z_night
    start_response("200 OK", [
        ("Content-Type",   "application/octet-stream"),
        ("Content-Length", str(len(body))),
        ("X-2Z-lengths",   "{} {}".format(len(z_day), len(z_night))),
        ("Cache-Control",  "no-store"),
        ("X-Generator",    generator),
    ])
    return [body]

def handle_area_request(params, start_response, environ={}):

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
    path_info  = environ.get("PATH_INFO", "")
    if "TOA" in path_info:
        map_type = "TOA"
    elif "MUF" in path_info:
        map_type = "MUF"
    else:
        map_type = "REL"

    log.info("AreaMap(%s): %d/%02d UTC=%02d TX=(%.4f,%.4f) %.3fMHz %s SSN=%.0f %dx%d",
             map_type, year, month, utc, txlat, txlng, mhz, mode_label, ssn, width, height)

    import time as _time
    t0 = _time.time()
    deck = build_area_deck(year, month, utc, txlat, txlng, path, pow_w, mhz, ssn, rsn, toa)
    vg1_path, tmp_dir = run_voaarea(deck)
    log.info("TIMING voacapl: %.2fs", _time.time()-t0); t1=_time.time()
    try:
        vg_data = parse_vg1(vg1_path) if vg1_path else None
        log.info("TIMING parse: %.2fs", _time.time()-t1); t2=_time.time()
        if vg_data:
            precomputed = interpolate_grid(vg_data, map_type)
            log.info("TIMING interp: %.2fs", _time.time()-t2); t3=_time.time()
            png_day   = render_map(vg_data, txlat, txlng, mhz, utc, ssn,
                                   month, year, mode_label, width, height,
                                   map_type=map_type, _precomputed=precomputed)
            log.info("TIMING render_day: %.2fs", _time.time()-t3); t4=_time.time()
            png_night = process_map_with_night(png_day, darkness=0.5)
                                                                          
                                                                                                                          
            log.info("TIMING convert night: %.2fs", _time.time()-t4); t4=_time.time()
        else:
            png_day = png_night = _blank_png(width, height)
            t4 = _time.time()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    bmp_day   = png_to_bmp565(png_day,   width, height)
    bmp_night = png_to_bmp565(png_night, width, height)
    log.info("TIMING bmp565: %.2fs  TOTAL: %.2fs", _time.time()-t4, _time.time()-t0)
    return _build_response(bmp_day, bmp_night, environ, start_response, "OHB-voacap-area")

# Trigger coastline load at startup
_load_coastlines()
