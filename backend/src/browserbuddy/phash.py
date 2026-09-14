"""Perceptual hashing and tile stitching for screenshot dedup (Slice C).

Backend-side by design (parent spec's portability policy: no DOM or compute
in the extension's background — "screenshot hashing/dedup happens in the
backend... the extension ships raw JPEGs over localhost").
"""

import io

from PIL import Image

HASH_HEX_CHARS = 16  # 64-bit difference hash, as hex


def difference_hash(image_bytes: bytes) -> str:
    """8x8 grayscale difference hash (dHash): resize to 9x8, compare each
    row's adjacent pixels left-to-right. Robust to JPEG re-encoding and tiny
    resize differences; a shift in overall brightness or color doesn't
    change it, only the pattern of local light/dark transitions does."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((9, 8))
    pixels = list(img.tobytes())
    value = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bit = pixels[offset + col] > pixels[offset + col + 1]
            value = (value << 1) | int(bit)
    return f"{value:0{HASH_HEX_CHARS}x}"


def hamming_distance(a: str, b: str) -> int:
    """Count of differing bits between two hex-encoded hashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def stitch_vertical(tiles: list[bytes]) -> bytes:
    """Stack ordered viewport tiles top-to-bottom into one JPEG. Used for
    on_demand_fullpage captures; a single-tile list is a dimension-preserving
    passthrough (re-encoded, not byte-identical)."""
    images = [Image.open(io.BytesIO(t)).convert("RGB") for t in tiles]
    width = max(img.width for img in images)
    total_height = sum(img.height for img in images)
    canvas = Image.new("RGB", (width, total_height))
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
