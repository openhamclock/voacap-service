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

from cancellable_run import run_cancellable, ClientDisconnected

# Configure logging BEFORE any other local imports
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("voacap_service")
log.info("TEST - logging initialized")


from urllib.parse import parse_qs

from antenna_lookup import lookup_antenna



# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
VOACAP_BIN  = os.environ.get("VOACAP_BIN",  "voacapl")
VOACAP_AREA = os.environ.get("VOACAP_AREA", "/opt/voacapl/itshfbc")

# import area_map after basicConfig but before any use of logging
# Area map module (VOAAREA METHOD 130 native mode)
from area_map import handle_area_request
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
    ant_dedx_control: int, ant_de_index: int,
    ant_dx_index: int,
    ant_de_az: float, ant_dx_az: float,
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

    # Validate antenna selection
    ant_de_index_str = "default/isotrope"
    ant_dx_index_str = "default/isotrope"
    if ant_dedx_control & 1:
        log.info("VOACAP info ant_dedx_control 1 for ant_de_index %d", ant_de_index)
        ant = lookup_antenna(ant_de_index)
        if ant:
            log.info("VOACAP info tx path is %s", ant['path'])
            ant_de_index_str = ant['path']
    if ant_dedx_control & 2:
        log.info("VOACAP info ant_dedx_control 2 for ant_dx_index %d", ant_dx_index) 
        ant = lookup_antenna(ant_dx_index)
        if ant:
            log.info("VOACAP info rx path is %s", ant['path'])
            ant_dx_index_str = ant['path']

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
        _antenna_card(0, ant_de_index_str, ant_de_az, pow_kw),
        _antenna_card(1, ant_dx_index_str, ant_dx_az, 0.0),
        freq_card,
        "METHOD       30    0",
        "EXECUTE",
        "QUIT",
        "",
    ])


# ---------------------------------------------------------------------------
# Run VOACAP in an isolated temp directory, return raw output lines
# ---------------------------------------------------------------------------
def run_voacap(deck: str, environ: dict | None = None) -> list[str]:
    # Each request gets its own run directory cloned from the VOACAP area.
    # This avoids any shared mutable state between concurrent requests.
    #
    # `environ` is the WSGI environ dict; pass it in so we can SIGKILL
    # voacapl the moment the upstream client disconnects (see cancellable_run).
    # If environ is None we fall back to plain blocking subprocess.run, which
    # preserves prior behaviour for any direct caller / test harness.
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

        # Run VOACAP — cancellable on client disconnect.
        result = run_cancellable(
            [VOACAP_BIN, area_dir, "voacapx.dat", "voacapx.out"],
            cwd=run_dir,
            timeout=30,
            environ=environ,
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
    except ClientDisconnected:
        log.info("VOACAP aborted for run %s — client gone", run_id)
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
    log.info("APP - dispatching path %s",path)
    # VOAAREA single-frequency coverage map (CSI-compatible endpoint)
    if path in ("/fetchVOACAPArea.pl",
                "/ham/HamClock/fetchVOACAPArea.pl",
                "/fetchVOACAP-TOA.pl",
                "/ham/HamClock/fetchVOACAP-TOA.pl",
                "/fetchVOACAP-MUF.pl",
                "/ham/HamClock/fetchVOACAP-MUF.pl"):
        return handle_area_request(params, start_response, environ)

    if path in ("/fetchVOACAPcapability.pl"):
        return _handle_compatibility_request(params, start_response)
    # Fall through to band conditions handler
    return _handle_band_conditions(params, start_response, environ)

def _handle_compatibility_request(params, start_response):

    # process integer parameter, return tuple
    # key,value in,value received,value as int
    def ps_int(k):
        vin = params.get(k)
        vouti = 0
        if vin is None:
            vouti = 0
            vin = ""
        try:
            vouti = int(vin)
        except ValueError:
            vouti = 0
        vout = str(vouti)
        return k,vin,vout,vouti
        
    # process float parameter, return tuple
    # key,value in,value received,value as float 
    def ps_float(k):
        vin = params.get(k)
        voutf = 0.0
        if vin is None:
            voutf = 0.0
            vin = ""
        try:
            voutf = float(vin)
        except ValueError:
            voutf = 0.0
        vout = f"{voutf:.1f}"
        return k,vin,vout,voutf 

    lines = []

    lines.append("parameter,received,used")
    
    deazk,deazin,deazout,ant_de_az = ps_float("ANTDEAZ")
    if (ant_de_az < 0.0 or ant_de_az > 360.0):
        deazout = "0.0"    
    if deazin:
        lines.append(f"{deazk},{deazin},{deazout}")

    dxazk,dxazin,dxazout,ant_dx_az = ps_float("ANTDXAZ")
    if (ant_dx_az < 0.0 or ant_dx_az > 360.0):
        dxazout = "0.0"  
    if dxazin:
        lines.append(f"{dxazk},{dxazin},{dxazout}")

    ctlk,ctlin,ctlout,ant_dedx_control = ps_int("ANTDEDXCONTROL")
    ant_dedx_control = ant_dedx_control & 3
    ctlout = str(ant_dedx_control)
    if ctlin:
        lines.append(f"{ctlk},{ctlin},{ctlout}")

    deinxk,deinxin,deinxout,ant_de_index = ps_int("ANTDEINDEX")
    ant = lookup_antenna(ant_de_index)
    if ant is None:
        deinxout = "0"
    if deinxin:
        lines.append(f"{deinxk},{deinxin},{deinxout}")

    dxinxk,dxinxin,dxinxout,ant_dx_index = ps_int("ANTDXINDEX")
    ant = lookup_antenna(ant_dx_index)
    if ant is None:
        dxinxout = "0"
    if dxinxin:
        lines.append(f"{dxinxk},{dxinxin},{dxinxout}")

    body = "\n".join(lines) + "\n"
    start_response("200 OK", [
        ("Content-Type", "text/plain; charset=ISO-8859-1"),
        ("Content-Length", str(len(body.encode()))),
        ("Cache-Control", "no-store"),
        ("X-Generator", "OHB-voacap-service"),
    ])
    return [body.encode("ISO-8859-1")]

def _handle_band_conditions(params, start_response, environ=None):

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


    info_request = False
    info_type = 0

    if "INFOREQUEST" in params:
        try:
            info_type = int(params["INFOREQUEST"])
        except (ValueError, KeyError):
            info_type = 0
        log.info("INFOREQUEST from query param: %d", info_type)
    else:
        log.info("INFOTYPE not present in query")
    
    if (info_type == 1):
        return _handle_compatibility_request(params, start_response)

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
    log.info("BC Mandatory Parameters Fetched")

    if "ANTDEINDEX" in params:
        try:
            ant_de_index = int(params["ANTDEINDEX"])
        except (ValueError, KeyError):
            ant_de_index = 0
        log.info("ANTDEINDEX from query param: %d", ant_de_index)
    else:
        log.info("ANTDEINDEX not present in query")
        ant_de_index = 0

    ant_de_az = 0.0
    try:
        ant_de_az = p_float("ANTDEAZ")
    except KeyError as e:
        ant_de_az = 0.0
    except ValueError as e:
        msg = f"Bad parameter value: {e}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]
    log.info("BC ANTDEAZ Fetched %.1f", ant_de_az)

    if (ant_de_az < 0.0 or ant_de_az > 360.0):
        msg = f"Bad parameter value: antdeax {ant_de_az}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]

    log.info("ANTDEAZ from query param: %.1f", ant_de_az)

    ant_dx_az = 0.0
    try:
        ant_dx_az = p_float("ANTDXAZ")
    except KeyError as e:
        ant_dx_az = 0.0
    except ValueError as e:
        msg = f"Bad parameter value: {e}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]

    log.info("ANTDXAZ from query param: %.1f", ant_dx_az)

    if (ant_dx_az < 0.0 or ant_dx_az > 360.0):
        msg = f"Bad parameter value: ANTDXAZ {ant_dx_az}\n"
        start_response("400 Bad Request", [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(msg))),
        ])
        return [msg.encode()]
        
    # Resolve mode → label and required SNR
    mode_label = MODE_LABEL.get(mode, f"MODE{mode}")
    rsn        = MODE_RSN.get(mode, MODE_RSN_DEFAULT)

    # SSN priority: explicit query param (SSN or ssn) > SC25 model estimate
    ssn_raw = params.get("SSN") or params.get("ssn")
    if ssn_raw is not None:
        try:
            ssn = float(ssn_raw)
        except ValueError:
            ssn = estimate_ssn(year, month)
        log.debug("SSN from query param: %.1f", ssn)
    else:
        ssn = estimate_ssn(year, month)
        log.debug("SSN from SC25 estimate: %.1f", ssn)

    if "ANTDEDXCONTROL" in params:
        try:
            ant_dedx_control = int(params["ANTDEDXCONTROL"])
        except (ValueError, KeyError):
            ant_dedx_control = 0
        log.info("ANTDEDXCONTROL from query param: %d", ant_dedx_control)
    else:
        log.info("ANTDEDXCONTROL not present in query")
        ant_dedx_control = 0

    if "ANTDEINDEX" in params:
        try:
            ant_de_index = int(params["ANTDEINDEX"])
        except (ValueError, KeyError):
            ant_de_index = 0
        log.info("ANTDEINDEX from query param: %d", ant_de_index)
    else:
        log.info("ANTDEINDEX not present in query")
        ant_de_index = 0

    if "ANTDXINDEX" in params:
        try:
            ant_dx_index = p_int(params["ANTDXINDEX"])
        except (ValueError, KeyError):
            ant_dx_index = 0
        log.info("ANTDXINDEX from query param: %d", ant_dx_index)
    else:
        log.info("ANTDXINDEX not present in query")
        ant_dx_index = 0

    pow_kw = pow_w / 1000.0

    log.info(
        "Request: %dY %dM TX=%.2f,%.2f RX=%.2f,%.2f PATH=%d POW=%.0fW "
        "MODE=%s(RSN=%.1f) TOA=%.1f SSN=%.1f ANTDEDXCONTROL=%d ANTDEINDEX=%d ANTDXINDEX=%d ANTDEAZ=%.1f ANTDXAZ=%.1f",
        year, month, txlat, txlng, rxlat, rxlng, path, pow_w,
        mode_label, rsn, toa, ssn, ant_dedx_control, ant_de_index, ant_dx_index, ant_de_az, ant_dx_az
    )

    deck  = build_deck(year, month, txlat, txlng, rxlat, rxlng, path, pow_kw, ssn, rsn, toa, ant_dedx_control, ant_de_index, ant_dx_index, ant_de_az, ant_dx_az)
    lines = run_voacap(deck, environ=environ)
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
