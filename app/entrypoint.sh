#!/bin/sh
# entrypoint.sh - Start nginx + uWSGI for voacap-service
#
# Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
# License: GNU Affero General Public License v3.0 (AGPLv3)
# See LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
#

set -e

# Create runtime directories
mkdir -p /run/uwsgi /run/nginx

# Patch VOACAP version file to use IONCAP absorption model (I not W).
# This is required for calibration against CSI reference output.
VERSION_FILE="${VOACAP_AREA:-/opt/voacapl/itshfbc}/database/version.w32"
if [ -f "$VERSION_FILE" ]; then
    sed -i 's/Version \([0-9.]*\)W/Version \1I/' "$VERSION_FILE" 2>/dev/null || true
    echo "voacap-service: VOACAP version: $(cat $VERSION_FILE)"
else
    echo "voacap-service: WARNING: version file not found at $VERSION_FILE"
fi

# Validate VOACAP binary is present
VOACAP_BIN="${VOACAP_BIN:-voacapl}"
if ! command -v "$VOACAP_BIN" >/dev/null 2>&1; then
    echo "voacap-service: ERROR: VOACAP binary '$VOACAP_BIN' not found in PATH"
    exit 1
fi
echo "voacap-service: Using VOACAP binary: $(command -v $VOACAP_BIN)"

# Start uWSGI in background
echo "voacap-service: Starting uWSGI..."
uwsgi --ini /app/uwsgi.ini &
UWSGI_PID=$!

# Wait for socket to appear
for i in $(seq 1 20); do
    [ -S /run/uwsgi/voacap.sock ] && break
    sleep 0.2
done

if [ ! -S /run/uwsgi/voacap.sock ]; then
    echo "voacap-service: ERROR: uWSGI socket did not appear"
    exit 1
fi

echo "voacap-service: Starting nginx..."
exec nginx -c /app/nginx.conf -g "daemon off;"
