"""Generate AFK app icons from the project logo source.

Produces assets/icon.png, assets/tray.png, assets/trayTemplate.png,
assets/trayTemplate@2x.png, assets/icon.ico, and assets/icon.icns used by
Electron and electron-builder.

Run: python scripts/make_icons.py
"""

import shutil
import subprocess
from pathlib import Path

from PIL import Image
from PIL import ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SOURCE = ASSETS / "logo-source.png"


def _fit_square(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA")
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fitted = img.resize((size, size), Image.Resampling.LANCZOS)
    canvas.alpha_composite(fitted, (0, 0))
    return canvas


def _draw_template_tray_icon(size: int) -> Image.Image:
    scale = size / 18
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (0, 0, 0, 255)

    def px(value: float) -> int:
        return round(value * scale)

    bars = [
        (4.5, 7.0, 11.0),
        (7.5, 4.0, 14.0),
        (10.5, 2.5, 15.5),
        (13.5, 6.0, 12.0),
    ]
    radius = max(1, px(0.9))
    width = max(2, px(1.8))
    for x, top, bottom in bars:
        left = px(x) - width // 2
        right = left + width
        draw.rounded_rectangle((left, px(top), right, px(bottom)), radius=radius, fill=color)

    draw.rounded_rectangle((px(3.5), px(15.3), px(14.5), px(16.8)), radius=px(0.7), fill=color)
    return img


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(f"Logo source not found: {SOURCE}")

    ASSETS.mkdir(exist_ok=True)
    source = Image.open(SOURCE)
    app = _fit_square(source, 512)
    tray = _fit_square(source, 64)

    app.save(ASSETS / "icon.png")
    tray.save(ASSETS / "tray.png")
    _draw_template_tray_icon(18).save(ASSETS / "trayTemplate.png")
    _draw_template_tray_icon(36).save(ASSETS / "trayTemplate@2x.png")
    app.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    iconutil = shutil.which("iconutil")
    if iconutil:
        iconset = ASSETS / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            _fit_square(source, size).save(iconset / f"icon_{size}x{size}.png")
            _fit_square(source, size * 2).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)
        for child in iconset.iterdir():
            child.unlink()
        iconset.rmdir()

    print("Wrote icon.png, tray.png, trayTemplate*.png, icon.ico, icon.icns to", ASSETS)


if __name__ == "__main__":
    main()
