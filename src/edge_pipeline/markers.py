"""Test-fixture markers: a 4-cell colour code drawn in the top-left corner of synthetic images so
that the mock model can tell which synthetic case it is looking at (it cannot read text). Real
images have no marker; the mock then falls back to pixel analysis. The cells are large solid
blocks, so the code survives the pipeline's JPEG re-encoding.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

CELL = 14
PALETTE = [(220, 30, 30), (30, 170, 60), (30, 60, 220), (20, 20, 20)]
CELLS = 4
MAX_CODE = len(PALETTE) ** CELLS - 1
FRAME = (255, 0, 255)  # magenta frame: a marker is only read if the frame is present


def draw(img: Image.Image, code: int) -> None:
    if not 0 <= code <= MAX_CODE:
        raise ValueError("marker code out of range")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, CELL * CELLS + 3, CELL + 3], fill=FRAME)
    for i in range(CELLS):
        digit = (code // (len(PALETTE) ** i)) % len(PALETTE)
        x0 = 2 + i * CELL
        d.rectangle([x0, 2, x0 + CELL - 1, 2 + CELL - 1], fill=PALETTE[digit])


def _nearest(rgb: tuple[int, int, int], colours: list[tuple[int, int, int]]) -> tuple[int, int]:
    dists = [sum((a - b) ** 2 for a, b in zip(rgb, c, strict=True)) for c in colours]
    i = min(range(len(colours)), key=dists.__getitem__)
    return i, dists[i]


def read(img: Image.Image) -> int | None:
    rgb = img.convert("RGB")
    if rgb.width < CELL * CELLS + 4 or rgb.height < CELL + 4:
        return None
    _, frame_dist = _nearest(rgb.getpixel((CELL * CELLS + 2, CELL + 2)), [FRAME])
    if frame_dist > 3 * 60**2:
        return None
    code = 0
    for i in range(CELLS):
        px = rgb.getpixel((2 + i * CELL + CELL // 2, 2 + CELL // 2))
        digit, dist = _nearest(px, PALETTE)
        if dist > 3 * 70**2:
            return None
        code += digit * (len(PALETTE) ** i)
    return code
