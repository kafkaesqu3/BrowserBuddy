import io

from PIL import Image

from browserbuddy.phash import difference_hash, hamming_distance, stitch_vertical


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _gradient_jpeg(ascending: bool, width: int = 64, height: int = 64) -> bytes:
    """A smooth left-to-right gradient. Ascending (dark->light) and
    descending (light->dark) versions are near-maximally different under a
    difference-hash: every adjacent-pixel comparison flips sign."""
    img = Image.new("L", (width, height))
    for x in range(width):
        value = int(255 * x / (width - 1))
        for y in range(height):
            img.putpixel((x, y), value if ascending else 255 - value)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_difference_hash_is_64_bit_hex():
    h = difference_hash(_solid((100, 100, 100)))
    assert len(h) == 16
    int(h, 16)  # raises if not valid hex


def test_identical_image_has_zero_distance():
    a = _gradient_jpeg(ascending=True)
    assert hamming_distance(difference_hash(a), difference_hash(a)) == 0


def test_near_identical_gradients_are_near_duplicates():
    a = _gradient_jpeg(ascending=True, width=64)
    b = _gradient_jpeg(ascending=True, width=65)  # tiny resize difference only
    assert hamming_distance(difference_hash(a), difference_hash(b)) <= 4


def test_inverted_gradient_is_far_from_original():
    a = _gradient_jpeg(ascending=True)
    b = _gradient_jpeg(ascending=False)
    assert hamming_distance(difference_hash(a), difference_hash(b)) >= 40


def test_hamming_distance_counts_differing_bits():
    assert hamming_distance("0000000000000000", "0000000000000001") == 1
    assert hamming_distance("ffffffffffffffff", "0000000000000000") == 64


def test_stitch_vertical_stacks_tiles_top_to_bottom():
    tile1 = _solid((10, 10, 10), size=(20, 10))
    tile2 = _solid((20, 20, 20), size=(20, 15))
    stitched = stitch_vertical([tile1, tile2])
    img = Image.open(io.BytesIO(stitched))
    assert img.size == (20, 25)  # width unchanged, heights summed


def test_stitch_vertical_single_tile_preserves_dimensions():
    tile = _solid((1, 2, 3), size=(30, 40))
    stitched = stitch_vertical([tile])
    img = Image.open(io.BytesIO(stitched))
    assert img.size == (30, 40)
