"""由项目内的波形图案生成标准 macOS .icns，不依赖外部设计文件。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


def render(size: int) -> Image.Image:
    scale = size / 1024
    image = Image.new("RGBA", (size, size), (11, 29, 52, 255))
    draw = ImageDraw.Draw(image)
    margin = round(70 * scale)
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=round(220 * scale),
        fill=(37, 99, 235, 255),
    )
    points = [
        (180, 560),
        (290, 560),
        (370, 300),
        (485, 730),
        (590, 420),
        (680, 560),
        (845, 560),
    ]
    draw.line(
        [(round(x * scale), round(y * scale)) for x, y in points],
        fill=(255, 255, 255, 255),
        width=max(1, round(65 * scale)),
        joint="curve",
    )
    return image


def main() -> None:
    output = Path(__file__).with_name("imu-data-collector.icns")
    with tempfile.TemporaryDirectory() as temporary:
        iconset = Path(temporary) / "imu-data-collector.iconset"
        iconset.mkdir()
        for logical in (16, 32, 128, 256, 512):
            render(logical).save(iconset / f"icon_{logical}x{logical}.png")
            render(logical * 2).save(iconset / f"icon_{logical}x{logical}@2x.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(output)],
            check=True,
        )


if __name__ == "__main__":
    main()
