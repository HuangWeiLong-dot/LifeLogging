#!/usr/bin/env python3
"""生成 LifeLoggingLive 的简单应用图标（app.ico）。

设计：黑底圆角方块 + 白色描边 + 一条折线脉冲。和站点一样只有黑白，
在浅色/深色任务栏上都靠白色描边定义轮廓；16px 也还能看出是一条脉冲。

不依赖字体、不依赖外部素材；改了颜色/形状后重跑：
    python windows/live-daemon/make-icon.py
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / "app.ico"

INK = (16, 16, 16, 255)
PAPER = (250, 250, 250, 255)


def draw(size: int) -> Image.Image:
    """按尺寸画一张，内部放大 8 倍再缩，边缘干净。"""
    scale = 8
    s = size * scale
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 圆角方块：留一点外边距，避免某些任务栏把图标裁掉边
    margin = s * 0.055
    radius = s * 0.17
    d.rounded_rectangle(
        [margin, margin, s - margin, s - margin],
        radius=radius,
        fill=INK,
        outline=PAPER,
        width=max(2, int(s * 0.06)),
    )

    # 脉冲：从左侧平线 → 两个尖峰 → 右侧平线
    pts = [
        (s * 0.17, s * 0.53),
        (s * 0.34, s * 0.53),
        (s * 0.41, s * 0.32),
        (s * 0.53, s * 0.73),
        (s * 0.61, s * 0.53),
        (s * 0.83, s * 0.53),
    ]
    d.line(pts, fill=PAPER, width=max(2, int(s * 0.085)), joint="curve")

    # 线端补圆点，小尺寸下不会出现“断头”
    r = max(1, int(s * 0.0425))
    for p in (pts[0], pts[-1]):
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=PAPER)

    return im.resize((size, size), Image.LANCZOS)


def draw_small(size: int) -> Image.Image:
    """16/24px 直接按整数像素画，不靠缩图 —— 否则细线会被 LANCZOS 磨成灰。"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(2, size // 5),
        fill=INK,
        outline=PAPER,
        width=1,
    )
    pts = [
        (size * 0.13, size * 0.55),
        (size * 0.33, size * 0.55),
        (size * 0.42, size * 0.32),
        (size * 0.55, size * 0.78),
        (size * 0.64, size * 0.55),
        (size * 0.87, size * 0.55),
    ]
    d.line(pts, fill=PAPER, width=2, joint="curve")
    return im


def write_ico(images: list[Image.Image], path: Path) -> None:
    """手写 ICO 容器：每个尺寸放自己的 PNG，避免 16px 被 256 缩糊。"""
    pngs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        pngs.append((im.width, im.height, buf.getvalue()))

    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    data = b""
    for w, h, blob in pngs:
        entries += struct.pack(
            "<BBBBHHII",
            0 if w >= 256 else w,
            0 if h >= 256 else h,
            0, 0, 1, 32, len(blob), offset,
        )
        offset += len(blob)
        data += blob
    path.write_bytes(header + entries + data)


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw_small(n) if n <= 24 else draw(n) for n in sizes]
    write_ico(images, OUT)
    # 预览图（不进 git）：256 原图和 16 放大，方便肉眼检查
    images[-1].save(HERE / "_preview-256.png")
    images[0].resize((128, 128), Image.NEAREST).save(HERE / "_preview-16.png")
    print(f"OK {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
