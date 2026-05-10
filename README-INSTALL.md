# OHB voacap-service Installation

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors  
License: **AGPLv3** — see [LICENSE](LICENSE)

## Prerequisites
- Docker

## 1. Build the image

```bash
cd voacap-service
docker build -t ohb-voacap-service .
```

## 2. Run the container

```bash
docker run -d \
  --name voacap-service \
  --restart unless-stopped \
  -p 8083:8080 \
  ohb-voacap-service
```

Adjust the SSN volume path to match your OHB installation.

## 3. Enable lighttpd proxy

```bash
sudo lighttpd-enable-mod proxy
sudo cp lighttpd-53-voacap-proxy.conf /etc/lighttpd/conf-enabled/53-voacap-proxy.conf
sudo service lighttpd restart
```

Verify:
```bash
sudo lighttpd -tt -f /etc/lighttpd/lighttpd.conf 2>&1 | grep -i proxy
```

Should show only the mod_auth load-order warning, no errors.

## 4. Test

```bash
curl -v "http://localhost/ham/HamClock/fetchVOACAPArea.pl?YEAR=2026&MONTH=3&UTC=15&TXLAT=28.154&TXLNG=-80.644&PATH=0&WATTS=100&WIDTH=800&HEIGHT=400&MHZ=14.10&TOA=3.0&MODE=19" \
  --output /tmp/test.bin 2>&1 | grep -i "x-2z\|content-type\|generator"
```

Expected output (desktop HamClock):
```
< Content-Type: application/octet-stream
< Content-Length: NNNNNN
< X-2Z-lengths: NNNNNN NNNNNN
< X-Generator: OHB-voacap-area-cached
```

## Notes
- First request returns a blank map and triggers background generation (~15-20s)
- Second request (after generation completes) serves the real map from cache
- Cache lives at /tmp/ohb_area_cache inside the container (3 hour TTL)
- Mount a volume at /tmp/ohb_area_cache to persist cache across restarts
- ESP8266/ESP32 HamClock clients are detected by User-Agent and receive raw BMPs without zlib
- Desktop HamClock clients receive zlib-compressed BMPs with X-2Z-lengths header
