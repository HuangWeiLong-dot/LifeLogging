"""把 TTF 转成 Arduino_GFX 能直接用的 GFXfont 头文件（1-bit，ASCII 0x20-0x7E）。

GFXfont 的约定：
  bitmap   **连续位流**：每个字形从字节边界开始，像素按 MSB 先写，
           行与行之间**不补字节**，只在字形结束时补到字节边界。
           这一点很容易写错 —— Arduino_GFX 的 drawChar 是这么读的：
             if (!(bit++ & 7)) bits = pgm_read_byte(&bitmap[bo++]);
           bit 每像素加一，跨行不重置。按行补齐会让宽度不是 8 倍数的字形整体错位。
  glyph    {bitmapOffset, width, height, xAdvance, xOffset, yOffset}
           yOffset 以基线为 0，向上为负

用法：
  python tools/gen_gfxfont.py <ttf> <px> <out.h> <StructName>
"""
import sys
from PIL import Image, ImageDraw, ImageFont

FIRST, LAST = 0x20, 0x7E


def render(font, ch):
    ascent, descent = font.getmetrics()
    adv = font.getlength(ch)
    pad = 4
    w = int(adv + 2 * pad) + 8
    h = ascent + descent + 2 * pad
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).text((pad, pad), ch, fill=255, font=font, anchor="la")
    bbox = img.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    glyph = img.crop(bbox).point(lambda p: 255 if p >= 110 else 0)
    return {
        "bits": glyph,
        "xoff": x0 - pad,
        "yoff": y0 - (pad + ascent),
        "adv": int(round(adv)),
        "w": x1 - x0,
        "h": y1 - y0,
    }


def main():
    ttf, px, out, name = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    font = ImageFont.truetype(ttf, px)

    bitmaps = bytearray()
    glyphs = []
    bitpos = 0

    for code in range(FIRST, LAST + 1):
        ch = chr(code)
        g = render(font, ch)
        adv = int(round(font.getlength(ch)))
        if g is None:  # 空白字符
            glyphs.append((bitpos // 8, 0, 0, adv, 0, 0))
            continue

        # 字形从字节边界开始
        while bitpos & 7:
            bitpos += 1
        offset = bitpos >> 3
        if len(bitmaps) < offset:
            bitmaps.extend(b"\x00" * (offset - len(bitmaps)))

        img = g["bits"]
        w, h = g["w"], g["h"]
        pix = img.load()
        for y in range(h):
            for x in range(w):
                if pix[x, y]:
                    idx = bitpos >> 3
                    while len(bitmaps) <= idx:
                        bitmaps.append(0)
                    bitmaps[idx] |= 0x80 >> (bitpos & 7)
                bitpos += 1

        glyphs.append((offset, w, h, g["adv"], g["xoff"], g["yoff"]))

    while len(bitmaps) * 8 < bitpos:
        bitmaps.append(0)

    ascent, descent = font.getmetrics()
    yadv = ascent + descent

    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("// 由 tools/gen_gfxfont.py 生成，勿手改\n")
        f.write(f"// 源字体 {ttf} @ {px}px\n")
        f.write("#pragma once\n#include <gfxfont.h>\n\n")
        f.write(f"const uint8_t {name}Bitmaps[] PROGMEM = {{\n")
        for i in range(0, len(bitmaps), 16):
            f.write("  " + ", ".join(f"0x{b:02X}" for b in bitmaps[i:i + 16]) + ",\n")
        f.write("};\n\n")
        f.write(f"const GFXglyph {name}Glyphs[] PROGMEM = {{\n")
        for g in glyphs:
            f.write("  {%d, %d, %d, %d, %d, %d},\n" % g)
        f.write("};\n\n")
        f.write(f"const GFXfont {name} PROGMEM = {{\n")
        f.write(f"  (uint8_t *){name}Bitmaps,\n  (GFXglyph *){name}Glyphs,\n")
        f.write(f"  0x{FIRST:02X}, 0x{LAST:02X}, {yadv}}};\n")

    print(f"{out}: {len(bitmaps)} bytes bitmap, {len(glyphs)} glyphs, yAdvance={yadv}")


if __name__ == "__main__":
    main()
