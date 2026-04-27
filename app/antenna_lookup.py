"""
antenna_lookup.py  --  Builds and exposes the runtime lookup dict.
Import this anywhere you need antenna resolution.
"""
# LICENSE_BEGIN
# Copyright (c) 2026 David Strickland KR8X
# SPDX-License-Identifier: AGPL-3.0-or-later
# LICENSE_END 
from antenna_data import ANTENNA_DATA

# key = msb*256 + lsb  →  {'path': ..., 'description': ...}
antenna_lookup: dict[int, dict[str, str]] = {
    msb * 256 + lsb: {'path': path, 'description': desc}
    for msb, lsb, path, desc in ANTENNA_DATA
}


def lookup_antenna(index: int) -> dict[str, str] | None:
    """Return {'path', 'description'} for a given msb*256+lsb index, or None."""
    return antenna_lookup.get(index)
