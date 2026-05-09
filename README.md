# voacap-service

**VOACAP HF propagation HTTP service for HamClock** — part of the
[Open HamClock Backend (OHB)](https://github.com/your-org/open-hamclock-backend) project.

License: **AGPLv3** — see [LICENSE](LICENSE)

---

## What it does

Provides a drop-in replacement for Clear Sky Institute's
`fetchBandConditions.pl` CGI endpoint, returning VOACAP-based HF band
reliability predictions in HamClock's wire protocol format.

Serves multiple HamClock clients concurrently. Each request runs VOACAP
in an isolated RAM-backed temp directory — no locking, no shared mutable
state, full parallelism.

## Quick start

```bash
docker build -t voacap-service .
docker run -p 8080:8080 voacap-service
```

Or with Compose:

```bash
docker compose up -d
```

Test it:

```bash
curl "http://localhost:8080/fetchBandConditions?YEAR=2026&MONTH=1&RXLAT=0.000&RXLNG=0.000&TXLAT=28.000&TXLNG=-81.000&UTC=0&PATH=0&POW=100&MODE=19&TOA=3.0"
```

Expected output (HamClock wire protocol):

```
0.05,0.88,0.89,0.81,0.70,0.47,0.12,0.01,0.00
100W,CW,TOA>3,SP,S=71
1 0.40,0.89,0.87,0.71,0.48,0.09,0.00,0.00,0.00
...
0 0.05,0.88,0.89,0.81,0.70,0.47,0.12,0.01,0.00
```

## Endpoints

| Path | Description |
|------|-------------|
| `GET /fetchBandConditions?...` | Band conditions (short form) |
| `GET /ham/HamClock/fetchBandConditions.pl?...` | Band conditions (CSI-compatible path) |
| `GET /health` | Health check — returns `200 OK` |

## Query parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `YEAR` | Year | `2026` |
| `MONTH` | Month (1–12) | `1` |
| `TXLAT` | Transmitter latitude (decimal degrees, N positive) | `28.000` |
| `TXLNG` | Transmitter longitude (decimal degrees, E positive) | `-81.000` |
| `RXLAT` | Receiver latitude | `0.000` |
| `RXLNG` | Receiver longitude | `0.000` |
| `PATH` | 0=short path, 1=long path | `0` |
| `POW` | TX power in watts | `100` |
| `MODE` | Mode: 19=CW, 0/1=SSB | `19` |
| `TOA` | Minimum takeoff angle (degrees) | `3.0` |
| `SSN` | Sunspot number (optional — auto-estimated if omitted) | `71` |

## Configure HamClock to use OHB

In HamClock settings, set the band conditions server to:

```
http://your-server:8080/ham/HamClock/fetchBandConditions.pl
```

## Concurrency model

- **nginx** handles HTTP, proxies to uWSGI via Unix socket
- **uWSGI** runs N worker processes (default: 4, one per CPU core)
- Each worker handles one VOACAP run at a time
- Each run creates a unique temp directory under `/dev/shm` (RAM), runs
  VOACAP, reads output, then deletes the directory — no disk I/O
- Set `processes = N` in `app/uwsgi.ini` to match your CPU count

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VOACAP_BIN` | `voacapl` | Path to voacapl binary |
| `VOACAP_AREA` | `/opt/voacapl/itshfbc` | VOACAP data area |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `TMPDIR` | `/dev/shm` | Where per-request temp dirs are created |

## Calibration notes

| Setting | Value | Reason |
|---------|-------|--------|
| RSN | 17 dB | Empirically matched against CSI reference output |
| Coefficients | CCIR | Matches CSI |
| Absorption | IONCAP (version.w32 = `I`) | Required for realistic reliability values |
| Antenna | `default/isotrope` (0 dBi) | True isotropic, matches CSI |
| FPROB | `1.00 1.00 1.00 0.00` | Standard layer probabilities |
| 6m (50 MHz) | Always 0.00 | Outside VOACAP HF model range |

## Building without Docker

```bash
# Install dependencies
pip3 install uwsgi

# Ensure voacapl is in PATH and VOACAP_AREA is set
export VOACAP_AREA=/usr/local/share/voacapl/itshfbc

# Patch version file
sed -i 's/Version \([0-9.]*\)W/Version \1I/' \
    $VOACAP_AREA/database/version.w32

# Run directly
cd app
uwsgi --ini uwsgi.ini --http :8080 --wsgi-disable-file-wrapper
```
