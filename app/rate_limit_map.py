"""
rate_limit_map.py — Fast rate-limited map response handler for voacap-service

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
License: GNU Affero General Public License v3.0 (AGPLv3)
See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>

Serves pre-rendered watermarked map images (or resizes on the fly if non-standard)
with HTTP 200 OK and valid HamClock map headers (including X-2Z-lengths for compressed maps).
"""

import os
import zlib
import logging
from hamclock_ua import parse_ua
from generate_watermark_images import (
    render_watermark_image,
    image_to_bmp565,
    STANDARD_SIZES
)

log = logging.getLogger("voacap_service.rate_limit_map")

STATIC_DIR = os.environ.get("RATE_LIMIT_STATIC_DIR", "/app/static/rate_limit")
_CACHE: dict[tuple[int, int], tuple[bytes, bytes, bytes, bytes]] = {}


def load_pregenerated_cache():
    """Pre-load generated rate limit assets into memory if available."""
    global _CACHE
    if not os.path.exists(STATIC_DIR):
        log.warning("Rate limit static directory not found at %s. Will generate on demand.", STATIC_DIR)
        return

    loaded = 0
    for w, h in STANDARD_SIZES:
        prefix = os.path.join(STATIC_DIR, f"ratelimit_{w}x{h}")
        bmp_day_p = f"{prefix}_day.bmp"
        bmp_night_p = f"{prefix}_night.bmp"
        z_day_p = f"{prefix}_day.z"
        z_night_p = f"{prefix}_night.z"

        if (os.path.exists(bmp_day_p) and os.path.exists(bmp_night_p) and
                os.path.exists(z_day_p) and os.path.exists(z_night_p)):
            try:
                with open(bmp_day_p, "rb") as f:
                    bmp_day = f.read()
                with open(bmp_night_p, "rb") as f:
                    bmp_night = f.read()
                with open(z_day_p, "rb") as f:
                    z_day = f.read()
                with open(z_night_p, "rb") as f:
                    z_night = f.read()
                _CACHE[(w, h)] = (bmp_day, bmp_night, z_day, z_night)
                loaded += 1
            except Exception as e:
                log.error("Failed to load pre-generated assets for %dx%d: %s", w, h, e)

    log.info("Loaded %d pre-rendered rate limit map resolutions into memory.", loaded)


def _get_rate_limit_assets(width: int, height: int) -> tuple[bytes, bytes, bytes, bytes]:
    """Return (bmp_day, bmp_night, z_day, z_night) for the given width and height."""
    key = (width, height)
    if key in _CACHE:
        return _CACHE[key]

    log.info("Generating watermarked fallback image on the fly for non-standard size %dx%d", width, height)
    img_day = render_watermark_image(width, height, is_night=False)
    img_night = render_watermark_image(width, height, is_night=True)

    bmp_day = image_to_bmp565(img_day, width, height)
    bmp_night = image_to_bmp565(img_night, width, height)

    z_day = zlib.compress(bmp_day, level=6)
    z_night = zlib.compress(bmp_night, level=6)

    _CACHE[key] = (bmp_day, bmp_night, z_day, z_night)
    return _CACHE[key]


def handle_rate_limit_request(params: dict, start_response, environ: dict = None):
    """
    Handle a rate-limited map request by returning pre-rendered watermarked map images.
    Returns HTTP 200 OK so HamClock parses the map and does not retry rapidly.
    """
    if environ is None:
        environ = {}

    try:
        width = int(params.get("WIDTH", 800))
    except (ValueError, TypeError):
        width = 800

    try:
        height = int(params.get("HEIGHT", 400))
    except (ValueError, TypeError):
        height = 400

    compress = True
    ua = parse_ua(environ)
    if ua.is_hamclock and ua.is_version_lt(4, 13):
        compress = False

    log.info("Serving rate limit watermark map for UA %s (%dx%d, compress=%s)", ua, width, height, compress)

    bmp_day, bmp_night, z_day, z_night = _get_rate_limit_assets(width, height)

    if compress:
        body = z_day + z_night
        start_response("200 OK", [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", str(len(body))),
            ("X-2Z-lengths", f"{len(z_day)} {len(z_night)}"),
            ("Cache-Control", "no-store"),
            ("X-Generator", "OHB-voacap-ratelimit"),
        ])
    else:
        body = bmp_day + bmp_night
        start_response("200 OK", [
            ("Content-Type", "image/bmp; charset=ISO-8859-1"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Generator", "OHB-voacap-ratelimit"),
        ])

    return [body]


# Load pre-generated cache when module is loaded
load_pregenerated_cache()
