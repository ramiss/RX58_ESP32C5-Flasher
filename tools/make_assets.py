"""Generate the flasher's GUI image and Windows icon from the board photo.

Source: tools/source/xiao_esp32c5.png (Seeed XIAO ESP32-C5 on a white
background).

Outputs (committed to flasher/assets/):
  - product.png   the board alone: cropped, white background made transparent
  - icon.png      the board centred on a square transparent canvas
  - icon.ico      multi-resolution Windows icon built from icon.png

Re-run after updating the source photo:
  python tools/make_assets.py [path/to/photo.png]
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "flasher" / "assets"
DEFAULT_SRC = REPO / "tools" / "source" / "xiao_esp32c5.png"

# Flood-fill tolerance (sum of |dR|+|dG|+|dB| from pure white).  The fill
# starts at the image border, so only the white connected to the edge goes
# transparent; the white parts of the board (its RF shield label) are
# enclosed by the black PCB and stay.
FILL_THRESH = 60
MARGIN = 6


def cut_out(src: Image.Image) -> Image.Image:
    """White background -> transparent, by flood fill from every border pixel."""
    im = src.convert("RGB")
    w, h = im.size
    # Fill a copy with a key colour the photo doesn't contain, then turn that
    # key into alpha.  Seeding along all four edges catches background regions
    # the board might cut off from a single corner.
    key = (255, 0, 255)
    work = im.copy()
    seeds = [(x, 0) for x in range(0, w, 16)] + [(x, h - 1) for x in range(0, w, 16)] \
        + [(0, y) for y in range(0, h, 16)] + [(w - 1, y) for y in range(0, h, 16)]
    for xy in seeds:
        if work.getpixel(xy) != key and sum(255 - c for c in work.getpixel(xy)) <= FILL_THRESH:
            ImageDraw.floodfill(work, xy, key, thresh=FILL_THRESH)
    mask = Image.new("L", (w, h), 255)
    mpx, wpx = mask.load(), work.load()
    for y in range(h):
        for x in range(w):
            if wpx[x, y] == key:
                mpx[x, y] = 0
    # Soften the 1-px fringe left by anti-aliasing against the white.
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def crop_to_board(im: Image.Image) -> Image.Image:
    box = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not box:
        raise SystemExit("Nothing left after removing the background - check the photo.")
    x0, y0, x1, y1 = box
    return im.crop((max(0, x0 - MARGIN), max(0, y0 - MARGIN),
                    min(im.width, x1 + MARGIN), min(im.height, y1 + MARGIN)))


def make_product(board: Image.Image):
    im = board
    max_h = 420     # the GUI shows it at 400 px high
    if im.height > max_h:
        im = im.resize((round(im.width * max_h / im.height), max_h), Image.LANCZOS)
    im.save(ASSETS / "product.png")
    print(f"product.png  {im.size}")


def make_icon(board: Image.Image):
    side = max(board.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(board, ((side - board.width) // 2, (side - board.height) // 2), board)
    canvas = canvas.resize((512, 512), Image.LANCZOS)
    canvas.save(ASSETS / "icon.png")
    canvas.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"icon.png/.ico  from {board.size} -> 512 square")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.is_file():
        raise SystemExit(f"Source image not found: {src}")
    ASSETS.mkdir(parents=True, exist_ok=True)
    board = crop_to_board(cut_out(Image.open(src)))
    make_product(board)
    make_icon(board)
    print(f"Wrote assets to {ASSETS}")


if __name__ == "__main__":
    main()
