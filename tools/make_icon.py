from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)


def make_icon(size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = int(size * 0.035)
    radius = int(size * 0.19)
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius,
        fill="#17324d",
        outline="#2f80ed",
        width=max(2, size // 64),
    )

    # 三个模组方块，暗示“整合包”。
    block = int(size * 0.23)
    gap = int(size * 0.055)
    start_x = int(size * 0.17)
    start_y = int(size * 0.16)
    colors = ["#6fcf97", "#f2c94c", "#56ccf2"]
    for index, color in enumerate(colors):
        x = start_x + (index % 2) * (block + gap)
        y = start_y + (index // 2) * (block + gap)
        draw.rounded_rectangle(
            (x, y, x + block, y + block),
            radius=max(4, block // 8),
            fill=color,
        )
        draw.rectangle(
            (x + block * 0.18, y + block * 0.18, x + block * 0.82, y + block * 0.50),
            fill="#17324d",
        )

    # 向下箭头
    cx = int(size * 0.69)
    top = int(size * 0.46)
    bottom = int(size * 0.83)
    width = int(size * 0.13)
    draw.rounded_rectangle(
        (cx - width // 2, top, cx + width // 2, bottom - width),
        radius=width // 3,
        fill="#ffffff",
    )
    draw.polygon(
        [
            (cx - width, bottom - width * 1.2),
            (cx + width, bottom - width * 1.2),
            (cx, bottom),
        ],
        fill="#ffffff",
    )
    return image


def main() -> None:
    image = make_icon()
    image.save(ASSETS / "app.png")
    image.save(
        ASSETS / "app.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"已生成：{ASSETS / 'app.ico'}")


if __name__ == "__main__":
    main()
