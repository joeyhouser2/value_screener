"""Generate assets/icon.ico (+ icon.png): a magnifying glass over rising value bars."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # draw large, downsample for crisp edges
OUT = Path(__file__).resolve().parent.parent / "assets"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        c = lerp(top, bottom, y / (size[1] - 1))
        for x in range(size[0]):
            px[x, y] = c
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return m


def build():
    # background tile: deep navy -> teal
    bg = vertical_gradient((S, S), (11, 27, 52), (13, 78, 92))
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), rounded_mask((S, S), 220))

    # soft glow behind the bars
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((180, 300, 840, 960), fill=(40, 210, 150, 90))
    icon.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))

    # rising bars (green gradient, tallest on the right)
    bars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base, w, gap, x0 = 810, 130, 48, 170
    heights = [230, 360, 520, 680]
    for i, h in enumerate(heights):
        x = x0 + i * (w + gap) - 20
        grad = vertical_gradient((w, h), (70, 235, 160), (20, 150, 110)).convert("RGBA")
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), 34, fill=255)
        bars.paste(grad, (x, base - h), m)
    icon.alpha_composite(bars)

    # magnifying glass (gold) circling the second-tallest bar = "screening for value"
    lens = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(lens)
    cx, cy, r = 470, 470, 215
    # handle
    d.line((cx + r * 0.72, cy + r * 0.72, 880, 880), fill=(255, 196, 61), width=88)
    d.ellipse((880 - 44, 880 - 44, 880 + 44, 880 + 44), fill=(255, 196, 61))
    # glass: faint white fill + gold ring
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 38))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 205, 80), width=60)
    # highlight arc
    d.arc((cx - r + 55, cy - r + 55, cx + r - 55, cy + r - 55), 200, 260, fill=(255, 255, 255, 190), width=18)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), (0, 0), lens.split()[3].filter(ImageFilter.GaussianBlur(14)))
    icon.alpha_composite(shadow, (0, 14))
    icon.alpha_composite(lens)

    # dollar sign inside the lens
    d2 = ImageDraw.Draw(icon)
    d2.text((cx, cy), "$", fill=(255, 255, 255, 235), anchor="mm", font=_font(260))
    return icon


def _font(size):
    from PIL import ImageFont

    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    img = build()
    img.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "icon.ico")
