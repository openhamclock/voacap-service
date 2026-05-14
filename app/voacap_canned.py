"""
voacap_canned.py - WSGI application for handling generating 409 graphics for VOACAP map enpoints when server is under load

Copyright (C) 2026 David Strickland KR8X and Open HamClock Backend (OHB) Contributors
License: GNU Affero General Public License v3.0 (AGPLv3)
See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>

"""
import os
import logging

# Configure logging BEFORE any other local imports
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s voacap_canned : %(message)s",
)
log = logging.getLogger("voacap_canned")
log.info("TEST - logging initialized")

from urllib.parse import parse_qs
from hamclock_ua import parse_ua

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VOACAP_BIN  = os.environ.get("VOACAP_BIN",  "voacapl")
VOACAP_AREA = os.environ.get("VOACAP_AREA", "/root/itshfbc")
DEFAULT_WIDTH  = 660
DEFAULT_HEIGHT = 330
CACHE_DIR = "/app/cache"

# ---------------------------------------------------------------------------
# WSGI application entry point — routes requests to the correct handler
# ---------------------------------------------------------------------------
def application(environ, start_response):

    def err(code, msg):
        b = msg.encode()
        start_response(code, [("Content-Type","text/plain"),("Content-Length",str(len(b)))])
        return [b]

    path   = environ.get("PATH_INFO", "/")
    qs     = environ.get("QUERY_STRING", "")
    params = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}
    log.info("voacap_canned - dispatching path %s",path)
    
    compress = True
    ua = parse_ua(environ)
    if ua.is_hamclock:
        if ua.is_version_lt(4, 0):
            compress = False
    log.info("INFO voacap_canned user_agent: %s", ua)   
    
    width      = int(params.get("WIDTH",  DEFAULT_WIDTH))
    height     = int(params.get("HEIGHT", DEFAULT_HEIGHT))  

    #
    # use compress to determine $map_type zlib or bmp
    # header lines to return in start_response are $CACHE_DIR/409-$map_type-$width-$height.txt
    # body to return is in $CACHE_DIR/409-$map_type-$width-$height.bin

    # if either file can't be read or has zero content, return.
    # return err("429 Too Many Requests","Unable to generate 429 graphic\n")
    
    if compress:
        map_type = "zlib"
    else:
        map_type = "bmp"

    base      = f"429-{map_type}-{width}-{height}"
    hdr_file  = os.path.join(CACHE_DIR, f"{base}.txt")
    body_file = os.path.join(CACHE_DIR, f"{base}.bin")

    # Read header file
    try:
        with open(hdr_file, "r") as f:
            hdr_text = f.read()
        if not hdr_text.strip():
            raise ValueError("empty header file")
    except Exception as e:
        log.warning("voacap_canned: cannot read %s: %s", hdr_file, e)
        return err("429 Too Many Requests", "Unable to generate 429 graphic\n")

    # Read body file
    try:
        with open(body_file, "rb") as f:
            body = f.read()
        if not body:
            raise ValueError("empty body file")
    except Exception as e:
        log.warning("voacap_canned: cannot read %s: %s", body_file, e)
        return err("429 Too Many Requests", "Unable to generate 429 graphic\n")

    # Parse headers from txt file — lines like "Content-Type: image/bmp"
    headers = []
    for line in hdr_text.splitlines():
        line = line.strip()
        if ":" in line:
            name, _, value = line.partition(":")
            headers.append((name.strip(), value.strip()))

    start_response("200 OK", headers)
    return [body]