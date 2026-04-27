"""
voacap_service.py - WSGI application for HamClock band conditions via VOACAP

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
License: GNU Affero General Public License v3.0 (AGPLv3)
See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>

Provides a CGI-compatible HTTP endpoint that accepts HamClock band conditions
query parameters and returns VOACAP propagation predictions in HamClock wire
protocol format.

Each request runs VOACAP in an isolated temporary directory, allowing full
concurrency with no locking or shared mutable state between requests.

Calibration: RSN=17 dB, CCIR coefficients, IONCAP absorption (version.w32=I),
isotropic antennas. Empirically matched against CSI reference output.
"""

import os
import re
import math
import uuid
import shutil
import subprocess
import tempfile
import logging
from urllib.parse import parse_qs

# Area map module (VOAAREA METHOD 130 native mode)
from area_map import handle_area_request

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
VOACAP_BIN  = os.environ.get("VOACAP_BIN",  "voacapl")
VOACAP_AREA = os.environ.get("VOACAP_AREA", "/opt/voacapl/itshfbc")
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO")

# Path to the 31-day SSN file produced by OHB (YYYY MM DD SSN format).
# Default matches OHB standard layout. Set to empty string to disable.
SSN_FILE = os.environ.get(
    "VOACAP_SSN_FILE",
    "/opt/hamclock-backend/htdocs/ham/HamClock/ssn/ssn-31.txt",
)

# SSN_MODE controls which value is extracted from the 31-day file:
#   "latest"  — use the last (most recent) entry in the file  [default]
#   "average" — use the mean of all 31 entries in the file
SSN_MODE = os.environ.get("VOACAP_SSN_MODE", "latest").strip().lower()
if SSN_MODE not in ("latest", "average"):
    SSN_MODE = "latest"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("voacap_service")

# HamClock 9 bands (MHz) — 50 MHz included; VOACAP HF model returns 0 for it
BANDS_MHZ = [3.75, 5.36, 7.15, 10.13, 14.18, 18.12, 21.23, 24.94, 28.85]

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

# ---------------------------------------------------------------------------
# SSN resolution
#
# Priority: explicit SSN= query param > ssn-31.txt file > SC25 estimate
#
# ssn-31.txt format — one entry per line:
#   YYYY MM DD SSN
# Example:
#   2026 03 07 75
#
# VOACAP_SSN_MODE=latest  → SSN = last line's value (most recent observation)
# VOACAP_SSN_MODE=average → SSN = mean of all values in the file
# ---------------------------------------------------------------------------

def _read_ssn_file(path: str) -> float | None:
    """Parse ssn-31.txt and return SSN per SSN_MODE. Returns None on any error."""
    try:
        with open(path) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        values = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    values.append(float(parts[3]))
                except ValueError:
                    continue
        if not values:
            log.warning("SSN file %s: no parseable entries", path)
            return None
        if SSN_MODE == "average":
            result = round(sum(values) / len(values), 1)
            log.debug("SSN from file (average of %d): %.1f", len(values), result)
        else:
            result = values[-1]
            log.debug("SSN from file (latest): %.1f", result)
        return result
    except FileNotFoundError:
        log.debug("SSN file not found: %s", path)
        return None
    except Exception as exc:
        log.warning("Error reading SSN file %s: %s", path, exc)
        return None

# ---------------------------------------------------------------------------
# SSN
# ---------------------------------------------------------------------------
def estimate_ssn(year: int, month: int) -> float:
    """SC25 model fallback — peak ~180 at 2025, exponential decay."""
    t = (year - 2025) + (month - 1) / 12.0
    return max(1.0, min(300.0, round(180 * math.exp(-0.3 * abs(t)), 1)))

# ---------------------------------------------------------------------------
# VOACAP deck construction
# ---------------------------------------------------------------------------
def _fmt_power(kw: float, width: int = 10) -> str:
    return ("%f" % kw)[:width].strip().rjust(width)


def _antenna_card(tx_rx: int, ant_file: str, bearing: float, power_kw: float) -> str:
    df = ant_file.ljust(21)
    return (
        "ANTENNA   "
        + str(tx_rx + 1).rjust(5)
        + str(tx_rx + 1).rjust(5)
        + "   02   30"
        + ("%.3f" % 0.0).rjust(10)
        + "[" + df + "]"
        + ("%.1f" % bearing).rjust(5)
        + _fmt_power(power_kw, 10)
    )


def build_deck(
    year: int, month: int,
    txlat: float, txlng: float,
    rxlat: float, rxlng: float,
    path: int, pow_kw: float, ssn: float,
    rsn: float, toa: float,
) -> str:
    def lat(deg):
        return ("%.2f" % abs(deg)).rjust(5) + ("N" if deg >= 0 else "S")

    def lon(deg):
        return ("%.2f" % abs(deg)).rjust(9) + ("E" if deg >= 0 else "W")

    circuit = (
        "CIRCUIT   "
        + lat(txlat) + lon(txlng)
        + ("%.2f" % abs(rxlat)).rjust(9) + ("N" if rxlat >= 0 else "S")
        + lon(rxlng)
        + ("  S " if not path else "  L ")
        + str(path).rjust(5)
    )

    # RSN is passed in from the MODE→RSN map; calibrated at 17 dB for CW.
    # SYSTEM fields: pow(kW) noise(dBW) amind xlufp rsn pmp dmpx
    system = (
        "SYSTEM    "
        + ("%.3f" % pow_kw).rjust(5)
        + ("%.0f"  % 145   ).rjust(5)
        + ("%.2f"  % 3.00  ).rjust(5)
        + ("%.0f"  % 90    ).rjust(5)
        + ("%.2f"  % rsn   ).rjust(5)
        + ("%.2f"  % toa   ).rjust(5)
        + ("%.2f"  % 0.00  ).rjust(5)
    )

    freq_card = "FREQUENCY $" + "".join(("%.3f" % f).rjust(6) for f in BANDS_MHZ)

    return "\n".join([
        "COMMENT   OHB voacap-service",
        "LINEMAX      55",
        "COEFFS    CCIR",
        "TIME          1   24    1    1",
        f"MONTH      {year} {month:2d}.00",
        f"SUNSPOT   {ssn:.1f}",
        "LABEL",
        circuit,
        system,
        "FPROB      1.00 1.00 1.00 0.00",
        _antenna_card(0, "default/isotrope", 0.0, pow_kw),
        _antenna_card(1, "default/isotrope", 0.0, 0.0),
        freq_card,
        "METHOD       30    0",
        "EXECUTE",
        "QUIT",
        "",
    ])


# ---------------------------------------------------------------------------
# Run VOACAP in an isolated temp directory, return raw output lines
# ---------------------------------------------------------------------------
def run_voacap(deck: str) -> list[str]:
    # Each request gets its own run directory cloned from the VOACAP area.
    # This avoids any shared mutable state between concurrent requests.
    run_id  = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"voacap_{run_id}_")
    try:
        # Clone the itshfbc directory structure with symlinks for read-only data,
        # but create a writable run/ subdirectory unique to this request.
        area_dir = os.path.join(tmp_dir, "itshfbc")
        run_dir  = os.path.join(area_dir, "run")
        os.makedirs(run_dir)

        # Symlink all read-only subdirectories from the real VOACAP area
        real_area = VOACAP_AREA
        for entry in os.listdir(real_area):
            if entry == "run":
                continue  # skip — we created our own writable run/
            src = os.path.join(real_area, entry)
            dst = os.path.join(area_dir, entry)
            os.symlink(src, dst)

        # Write the input deck
        dat_file = os.path.join(run_dir, "voacapx.dat")
        out_file = os.path.join(run_dir, "voacapx.out")
        with open(dat_file, "w") as f:
            f.write(deck)

        # Run VOACAP
        result = subprocess.run(
            [VOACAP_BIN, area_dir, "voacapx.dat", "voacapx.out"],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            log.error("VOACAP error: %s", result.stderr.strip())
            return []

        if not os.path.exists(out_file):
            log.error("VOACAP produced no output file")
            return []

        with open(out_file) as f:
            return f.readlines()

    except subprocess.TimeoutExpired:
        log.error("VOACAP timed out for run %s", run_id)
        return []
    except Exception as e:
        log.exception("Unexpected error in run_voacap: %s", e)
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Parse REL lines from METHOD 30 output
# REL line format: MUFday_rel  band1..band9  -  -  REL
# First value is MUF reliability — skip it; take next 9 band values.
# Hours run 1-24; hour 24 (index 23 after skip) = UTC 00:00 → rotate to front.
# ---------------------------------------------------------------------------
def parse_rel(output_lines: list[str]) -> list[list[float]]:
    rel = []
    for line in output_lines:
        if re.search(r"\bREL\s*$", line):
            vals = [float(v) for v in re.findall(r"\d+\.\d+", line)]
            band_vals = (vals[1:] + [0.0] * 9)[:9]
            rel.append(band_vals)
            if len(rel) >= 24:
                break

    if len(rel) < 24:
        log.warning("Only got %d REL lines (expected 24)", len(rel))
        while len(rel) < 24:
            rel.append([0.0] * 9)

    # Hour 24 = UTC 00:00 → rotate to index 0
    rel.insert(0, rel.pop())
    return rel


# ---------------------------------------------------------------------------
# Format HamClock wire protocol output
# Line 1:    UTC=0 band reliabilities (comma-separated)
# Line 2:    parameter echo
# Lines 3-25: hours 1-23 prefixed with hour number
# Line 26:   UTC=0 repeat
# ---------------------------------------------------------------------------
def format_output(
    rel: list[list[float]],
    pow_w: float, mode_label: str, toa: float, path: int, ssn: float,
) -> str:
    path_label = "LP" if path else "SP"

    def fmt_row(row):
        return ",".join(f"{v:.2f}" for v in row)

    lines = [
        fmt_row(rel[0]),
        f"{int(pow_w)}W,{mode_label},TOA>{toa:g},{path_label},S={int(ssn)}",
    ]
    for h in range(1, 24):
        lines.append(f"{h} {fmt_row(rel[h])}")
    lines.append(f"0 {fmt_row(rel[0])}")
    return "\n".join(lines) + "\n"

# Internal alias used by application() — same function
_format_output_inner = format_output


def zero_output(pow_w: float, mode_label: str, toa: float, path: int, ssn: float) -> str:
    rel = [[0.0] * 9] * 24
    return format_output(rel, pow_w, mode_label, toa, path, ssn)


# ---------------------------------------------------------------------------
# WSGI application entry point — routes requests to the correct handler
# ---------------------------------------------------------------------------
def application(environ, start_response):
    path   = environ.get("PATH_INFO", "/")
    qs     = environ.get("QUERY_STRING", "")
    params = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}

    # VOAAREA single-frequency coverage map (CSI-compatible endpoint)
    if path in ("/fetchVOACAPArea.pl",
                "/ham/HamClock/fetchVOACAPArea.pl",
                "/fetchVOACAP-TOA.pl",
                "/ham/HamClock/fetchVOACAP-TOA.pl",
                "/fetchVOACAP-MUF.pl",
                "/ham/HamClock/fetchVOACAP-MUF.pl"):
        return handle_area_request(params, start_response, environ)

    # Fall through to band conditions handler
    return _handle_band_conditions(params, start_response)


def _handle_band_conditions(params, start_response):

    def p_int(k):
        v = params.get(k)
        if v is None:
            raise KeyError(k)
        try:
            return int(v)
        except ValueError:
            raise ValueError(f"Invalid integer for {k}: {v!r}")

    def p_float(k):
        v = params.get(k)
        if v is None:
            raise KeyError(k)
        try:
            return float(v)
        except ValueError:
            raise ValueError(f"Invalid float for {k}: {v!r}")

    # All parameters are required — return 400 if any are missing or malformed
    REQUIRED = ("YEAR", "MONTH", "RXLAT", "RXLNG", "TXLAT", "TXLNG",
                "PATH", "POW", "MODE", "TOA")
    try:
        year  = p_int  ("YEAR")
        month = p_int  ("MONTH")
        rxlat = p_float("RXLAT")
        rxlng = p_float("RXLNG")
        txlat = p_float("TXLAT")
        txlng = p_float("TXLNG")
        path  = p_int  ("PATH")
        pow_w = p_float("POW")
        mode  = p_int  ("MODE")
        toa   = p_float("TOA")
    except KeyError as e:
        msg = f"Missing required parameter: {e}\nRequired: {', '.join(REQUIRED)}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]
    except ValueError as e:
        msg = f"Bad parameter value: {e}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]

    # Resolve mode → label and required SNR
    mode_label = MODE_LABEL.get(mode, f"MODE{mode}")
    rsn        = MODE_RSN.get(mode, MODE_RSN_DEFAULT)

    # SSN priority: explicit query param > ssn-31.txt file > SC25 model estimate
    if "SSN" in params:
        try:
            ssn = float(params["SSN"])
        except ValueError:
            ssn = estimate_ssn(year, month)
        log.debug("SSN from query param: %.1f", ssn)
    else:
        file_ssn = _read_ssn_file(SSN_FILE) if SSN_FILE else None
        if file_ssn is not None:
            ssn = file_ssn
        else:
            ssn = estimate_ssn(year, month)
            log.debug("SSN from SC25 estimate: %.1f", ssn)

    pow_kw = pow_w / 1000.0

    log.info(
        "Request: %dY %dM TX=%.2f,%.2f RX=%.2f,%.2f PATH=%d POW=%.0fW "
        "MODE=%s(RSN=%.1f) TOA=%.1f SSN=%.1f",
        year, month, txlat, txlng, rxlat, rxlng, path, pow_w,
        mode_label, rsn, toa, ssn,
    )

    deck  = build_deck(year, month, txlat, txlng, rxlat, rxlng, path, pow_kw, ssn, rsn, toa)
    lines = run_voacap(deck)
    rel   = parse_rel(lines) if lines else [[0.0] * 9] * 24

    # format_output uses mode_label for the param echo line
    body  = _format_output_inner(rel, pow_w, mode_label, toa, path, ssn)

    start_response("200 OK", [
        ("Content-Type", "text/plain; charset=ISO-8859-1"),
        ("Content-Length", str(len(body.encode()))),
        ("Cache-Control", "no-store"),
        ("X-Generator", "OHB-voacap-service"),
    ])
    return [body.encode("ISO-8859-1")]

# ---------------------------------------------------------------------------
