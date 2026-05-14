# voacap-service Dockerfile
#
# Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
# License: GNU Affero General Public License v3.0 (AGPLv3)
# See LICENSE or <https://www.gnu.org/licenses/agpl-3.0.html>
#
# Builds a self-contained VOACAP HTTP service compatible with HamClock's
# fetchBandConditions wire protocol. Serves multiple concurrent clients via
# nginx + uWSGI + Python, with per-request isolated VOACAP run directories.
#
# Build:
#   docker build -t voacap-service .
#
# Run:
#   docker run -p 8080:8080 voacap-service
#
# Test:
#   curl "http://localhost:8080/fetchBandConditions?YEAR=2026&MONTH=1&RXLAT=0&RXLNG=0&TXLAT=28&TXLNG=-81&PATH=0&POW=100&MODE=19&TOA=3.0"

FROM debian:13.4-slim AS builder

WORKDIR /build/voacapl

# Install build prerequisites
COPY docker/package-list-support.txt /build/

RUN apt-get update && \
    apt install -y --no-install-recommends $(grep -v '^#' /build/package-list-support.txt) && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Full build sequence per upstream README:
#   automake/autoreconf regenerates Makefiles from the .am/.ac sources
#   configure + make + make install compiles and stages the binary
#   makeitshfbc populates the itshfbc data directory (ionospheric coefficients,
#   antenna patterns, etc.) — without this voacapl has no data and fails at runtime
RUN git clone --depth=1 https://github.com/jawatson/voacapl.git /build/voacapl && \
    autoreconf && \
    automake --add-missing && \
    ./configure && \
    make && \
    make install DESTDIR=/build/install

# ---------------------------------------------------------------------------
FROM debian:13.4-slim

WORKDIR /app

LABEL org.opencontainers.image.title="voacap-service"
LABEL org.opencontainers.image.description="VOACAP HF propagation service for HamClock (OHB)"
LABEL org.opencontainers.image.licenses="AGPL-3.0"
LABEL org.opencontainers.image.source="https://github.com/your-org/open-hamclock-backend"

COPY docker/package-list.txt /app/
RUN apt update && \
    apt install -y --no-install-recommends $(grep -v '^#' /app/package-list.txt) && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ARG GIT_VERSION

COPY --from=builder /build/install/usr/local/bin/ /usr/local/bin/
COPY --from=builder /build/install/usr/local/share/ /usr/local/share/
COPY app/voacap_service.py  /app/voacap_service.py
COPY app/area_map.py        /app/area_map.py
COPY app/hamclock_ua.py     /app/hamclock_ua.py
COPY app/voacap_canned.py   /app/voacap_canned.py
COPY app/antenna_data.py    /app/antenna_data.py
COPY app/antenna_lookup.py  /app/antenna_lookup.py
COPY app/cancellable_run.py /app/cancellable_run.py
COPY app/uwsgi.ini          /app/uwsgi.ini
COPY app/nginx.conf         /app/nginx.conf
COPY app/entrypoint.sh      /app/entrypoint.sh


COPY app/cache/429-bmp-1320-660.bin     /app/cache/429-bmp-1320-660.bin
COPY app/cache/429-bmp-1320-660.txt     /app/cache/429-bmp-1320-660.txt
COPY app/cache/429-bmp-1980-990.bin     /app/cache/429-bmp-1980-990.bin
COPY app/cache/429-bmp-1980-990.txt     /app/cache/429-bmp-1980-990.txt
COPY app/cache/429-bmp-2640-1320.bin    /app/cache/429-bmp-2640-1320.bin
COPY app/cache/429-bmp-2640-1320.txt    /app/cache/429-bmp-2640-1320.txt
COPY app/cache/429-bmp-3960-1980.bin    /app/cache/429-bmp-3960-1980.bin
COPY app/cache/429-bmp-3960-1980.txt    /app/cache/429-bmp-3960-1980.txt
COPY app/cache/429-bmp-5280-2640.bin    /app/cache/429-bmp-5280-2640.bin
COPY app/cache/429-bmp-5280-2640.txt    /app/cache/429-bmp-5280-2640.txt
COPY app/cache/429-bmp-5940-2970.bin    /app/cache/429-bmp-5940-2970.bin
COPY app/cache/429-bmp-5940-2970.txt    /app/cache/429-bmp-5940-2970.txt
COPY app/cache/429-bmp-660-330.bin      /app/cache/429-bmp-660-330.bin
COPY app/cache/429-bmp-660-330.txt      /app/cache/429-bmp-660-330.txt
COPY app/cache/429-bmp-7920-3960.bin    /app/cache/429-bmp-7920-3960.bin
COPY app/cache/429-bmp-7920-3960.txt    /app/cache/429-bmp-7920-3960.txt
COPY app/cache/429-zlib-1320-660.bin    /app/cache/429-zlib-1320-660.bin
COPY app/cache/429-zlib-1320-660.txt    /app/cache/429-zlib-1320-660.txt
COPY app/cache/429-zlib-1980-990.bin    /app/cache/429-zlib-1980-990.bin
COPY app/cache/429-zlib-1980-990.txt    /app/cache/429-zlib-1980-990.txt
COPY app/cache/429-zlib-2640-1320.bin   /app/cache/429-zlib-2640-1320.bin
COPY app/cache/429-zlib-2640-1320.txt   /app/cache/429-zlib-2640-1320.txt
COPY app/cache/429-zlib-3960-1980.bin   /app/cache/429-zlib-3960-1980.bin
COPY app/cache/429-zlib-3960-1980.txt   /app/cache/429-zlib-3960-1980.txt
COPY app/cache/429-zlib-5280-2640.bin   /app/cache/429-zlib-5280-2640.bin
COPY app/cache/429-zlib-5280-2640.txt   /app/cache/429-zlib-5280-2640.txt
COPY app/cache/429-zlib-5940-2970.bin   /app/cache/429-zlib-5940-2970.bin
COPY app/cache/429-zlib-5940-2970.txt   /app/cache/429-zlib-5940-2970.txt
COPY app/cache/429-zlib-660-330.bin     /app/cache/429-zlib-660-330.bin
COPY app/cache/429-zlib-660-330.txt     /app/cache/429-zlib-660-330.txt
COPY app/cache/429-zlib-7920-3960.bin   /app/cache/429-zlib-7920-3960.bin
COPY app/cache/429-zlib-7920-3960.txt   /app/cache/429-zlib-7920-3960.txt

# Patch version file to use IONCAP absorption model at build time.
# entrypoint.sh re-applies this in case the data dir is mounted externally.
RUN chmod 755 /usr/local/bin/voacapl && \
    chmod +x /usr/local/bin/* && \
    makeitshfbc && \
    \
    VERSION_FILE=/usr/local/share/voacapl/itshfbc/database/version.w32 && \
    if [ -f "$VERSION_FILE" ]; then \
        sed -i 's/Version \([0-9.]*\)W/Version \1I/' "$VERSION_FILE"; \
        echo "Patched: $(cat $VERSION_FILE)"; \
    fi && \
    \
    chmod +x /app/entrypoint.sh && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf && \
    echo $GIT_VERSION > /app/git.version

ENV VOACAP_BIN=/usr/local/bin/voacapl
ENV VOACAP_AREA=/usr/local/share/voacapl/itshfbc
ENV VOACAP_SSN_FILE="/opt/hamclock-backend/htdocs/ham/HamClock/ssn/ssn-31.txt"
ENV VOACAP_SSN_MODE=latest
ENV LOG_LEVEL=INFO

# Use RAM-backed tmpfs for VOACAP temp files — fastest possible I/O
ENV TMPDIR=/dev/shm

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -s http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
