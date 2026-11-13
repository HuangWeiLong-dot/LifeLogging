#!/usr/bin/env python3
"""生成 LifeBridge 的启动图标（mipmap-*/ic_launcher.png）。

和桌面端 windows/live-daemon/app.ico 是同一个符号：黑底圆角方块 + 一条脉冲线。
区别只有一个 —— 脉冲用站点的强调蓝 #60A5FA，桌面上是白色，一眼能分开。

Android 8.0+ 还会用 res/mipmap-anydpi-v26/ 里的自适应图标（矢量），
这份 PNG 是给旧系统和部分启动器的。

改完重跑：
    python android/LifeBridge/make-icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
RES = HERE / "res"

INK = (16, 16, 16, 255)
ACCENT = (96, 165, 250, 255)   # #60A5FA

# Android 各密度的启动图标尺寸
SIZES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}


def draw(size: int) -> Image.Image:
    """内部放大 8 倍再缩，边缘干净。"""
    scale = 8
    s = size * scale
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 自适应图标会被启动器裁成圆形/圆角，方块直接铺满，不留外边距
    d.rounded_rectangle(
        [0, 0, s - 1, s - 1],
        radius=int(s * 0.22),
        fill=INK,
    )

    pts = [
        (s * 0.17, s * 0.53),
        (s * 0.34, s * 0.53),
        (s * 0.41, s * 0.32),
        (s * 0.53, s * 0.73),
        (s * 0.61, s * 0.53),
        (s * 0.83, s * 0.53),
    ]
    w = max(2, int(s * 0.085))
    d.line(pts, fill=ACCENT, width=w, joint="curve")

    r = max(1, int(s * 0.0425))
    for p in (pts[0], pts[-1]):
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=ACCENT)

    return im.resize((size, size), Image.LANCZOS)


def draw_small(size: int) -> Image.Image:
    """小尺寸直接按整数像素画，避免细线被缩成灰。"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=max(2, size // 5), fill=INK)
    pts = [
        (size * 0.13, size * 0.55),
        (size * 0.33, size * 0.55),
        (size * 0.42, size * 0.32),
        (size * 0.55, size * 0.78),
        (size * 0.64, size * 0.55),
        (size * 0.87, size * 0.55),
    ]
    d.line(pts, fill=ACCENT, width=2, joint="curve")
    return im


def main() -> None:
    preview = None
    for folder, px in SIZES.items():
        out_dir = RES / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        im = draw_small(px) if px <= 48 else draw(px)
        im.save(out_dir / "ic_launcher.png")
        im.save(out_dir / "ic_launcher_round.png")
        if px == 192:
            preview = im

    if preview is not None:
        preview.save(HERE / "_preview-icon.png")
    print("OK", ", ".join(f"{k}={v}" for k, v in SIZES.items()))


if __name__ == "__main__":
    main()
