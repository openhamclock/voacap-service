#!/usr/bin/env python3
"""
generate_watermark_images.py — Pre-render rate limiting watermark images for voacap-service

Copyright (C) 2026 Open HamClock Backend (OHB) Contributors
License: GNU Affero General Public License v3.0 (AGPLv3)

Pre-generates dark watermarked image pairs (Day & Night) for common HamClock map sizes:
  - 660x330
  - 800x400
  - 1320x660
  - 1980x990
  - 2640x1320
  - 3960x1980
  - 5280x2640
  - 5940x2970
  - 7920x3960

Saves pre-compressed zlib data and uncompressed BMP565 binaries to output directory.
"""

import sys
import os
import io
import zlib
import struct
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from PIL import Image, ImageDraw, ImageFont

STANDARD_SIZES = [
    (660, 330),
    (800, 400),
    (1320, 660),
    (1980, 990),
    (2640, 1320),
    (3960, 1980),
    (5280, 2640),
    (5940, 2970),
    (7920, 3960),
]


def image_to_bmp565(img: Image.Image, width: int, height: int) -> bytes:
    """Convert PIL RGB Image to uncompressed RGB565 BMP with BITMAPV4HEADER (108 bytes)."""
    img = img.convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height), Image.LANCZOS)

    row_bytes = width * 2
    pad = (4 - (row_bytes % 4)) % 4
    padded_row = row_bytes + pad
    pixel_data_size = padded_row * height

    DIB_HEADER_SIZE = 108
    file_size = 14 + DIB_HEADER_SIZE + pixel_data_size
    pixel_offset = 14 + DIB_HEADER_SIZE

    file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset)

    dib_header = struct.pack("<IiiHHIIiiII",
        DIB_HEADER_SIZE,
        width,
        -height,  # top-down
        1,
        16,
        3,  # BI_BITFIELDS
        pixel_data_size,
        2835,
        2835,
        0,
        0,
    )
    masks_and_cs = struct.pack("<III", 0xF800, 0x07E0, 0x001F) + b"\x00" * 56

    if HAS_NUMPY:
        arr = np.array(img)
        r = arr[:, :, 0].astype(np.uint16)
        g = arr[:, :, 1].astype(np.uint16)
        b = arr[:, :, 2].astype(np.uint16)
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        pixel_data = rgb565.astype('<u2').tobytes()

        if pad > 0:
            rows = []
            row_size = width * 2
            pad_bytes = b"\x00" * pad
            for y in range(height):
                rows.append(pixel_data[y*row_size:(y+1)*row_size] + pad_bytes)
            pixel_data = b"".join(rows)
    else:
        pixels = list(img.getdata())
        pixel_bytes = bytearray()
        pad_bytes = b"\x00" * pad
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[y * width + x]
                val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                pixel_bytes.extend(struct.pack("<H", val))
            if pad > 0:
                pixel_bytes.extend(pad_bytes)
        pixel_data = bytes(pixel_bytes)

    return bytes(file_header) + dib_header + masks_and_cs + pixel_data


def render_watermark_image(width: int, height: int, is_night: bool = False) -> Image.Image:
    """Render a watermarked map image with a 3x3 tiled grid of text (dark background)."""
    bg_color = (6, 12, 22) if is_night else (10, 20, 36)
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Note: Outer red border omitted so HamClock longitude rotation does not wrap vertical border lines.

    # Try loading a truetype font, fall back to default
    font_large = None
    font_small = None
    target_size_large = max(10, int(height * 0.028))
    target_size_small = max(8, int(height * 0.020))

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]

    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_large = ImageFont.truetype(fp, target_size_large)
                font_small = ImageFont.truetype(fp, target_size_small)
                break
            except Exception:
                pass

    if font_large is None:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    line1 = "RATE LIMITING IN EFFECT"
    line2 = "Your HamClock will try again automatically."

    # Compute text dimensions
    bbox1 = draw.textbbox((0, 0), line1, font=font_large)
    w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]

    bbox2 = draw.textbbox((0, 0), line2, font=font_small)
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

    spacing = max(2, int(height * 0.008))
    total_text_h = h1 + spacing + h2

    text_color1 = (255, 215, 0)  # Gold/Yellow
    text_color2 = (240, 240, 240) if not is_night else (190, 200, 210)
    shadow_color = (0, 0, 0)

    # Tile in a 3x3 grid (3 columns x 3 rows) so longitude rotation & zooming (up to 3x)
    # always maintain visible watermark tiles without cutoff or wrapping artifacts.
    for row in range(3):
        cy = int(height * (2 * row + 1) / 6)
        y1 = cy - total_text_h // 2
        y2 = y1 + h1 + spacing

        for col in range(3):
            cx = int(width * (2 * col + 1) / 6)
            x1 = cx - w1 // 2
            x2 = cx - w2 // 2

            # Shadow/Outline for crisp visibility
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx != 0 or dy != 0:
                        draw.text((x1 + dx, y1 + dy), line1, font=font_large, fill=shadow_color)
                        draw.text((x2 + dx, y2 + dy), line2, font=font_small, fill=shadow_color)

            draw.text((x1, y1), line1, font=font_large, fill=text_color1)
            draw.text((x2, y2), line2, font=font_small, fill=text_color2)

    return img


def generate_all(output_dir: str):
    """Generate and write all watermark assets to output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    print(f"Generating watermark images in {output_dir}...")

    for w, h in STANDARD_SIZES:
        img_day = render_watermark_image(w, h, is_night=False)
        img_night = render_watermark_image(w, h, is_night=True)

        bmp_day = image_to_bmp565(img_day, w, h)
        bmp_night = image_to_bmp565(img_night, w, h)

        z_day = zlib.compress(bmp_day, level=6)
        z_night = zlib.compress(bmp_night, level=6)

        prefix = os.path.join(output_dir, f"ratelimit_{w}x{h}")
        with open(f"{prefix}_day.bmp", "wb") as f:
            f.write(bmp_day)
        with open(f"{prefix}_night.bmp", "wb") as f:
            f.write(bmp_night)
        with open(f"{prefix}_day.z", "wb") as f:
            f.write(z_day)
        with open(f"{prefix}_night.z", "wb") as f:
            f.write(z_night)

        print(f"  Generated {w}x{h}: BMP day={len(bmp_day)}b night={len(bmp_night)}b | Z day={len(z_day)}b night={len(z_night)}b")


if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "/app/static/rate_limit"
    generate_all(out_dir)
