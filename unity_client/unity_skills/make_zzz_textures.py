# -*- coding: utf-8 -*-
"""
六分街场景贴图生成器：用 PIL 程序化生成一套 ZZZ 风格的平铺贴图 + 中文霓虹招牌，
输出到 unity_project/Assets/Textures_ZZZ/。Unity 会自动导入这些 PNG。
纯标准库 + PIL；中文字体用系统 msyhbd.ttc（微软雅黑粗体）。
"""
import os, random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = r"D:\AI训练\ai-girlfriend-本地版-备份-20260724\unity_project\Assets\Textures_ZZZ"
os.makedirs(OUT, exist_ok=True)

random.seed(42)

FONT_PATH = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_SIMHEI = r"C:\Windows\Fonts\simhei.ttf"
font_path = FONT_PATH if os.path.exists(FONT_PATH) else FONT_SIMHEI


def save(img, name):
    img.save(os.path.join(OUT, name))
    print(f"  {name}")


def noise(img, n, dmin=-10, dmax=10, alpha=90):
    """叠加噪点，让纯色看起来像真实材质。"""
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    for _ in range(n):
        x, y = random.randint(0, w - 1), random.randint(0, h - 1)
        d = random.randint(dmin, dmax)
        draw.ellipse((x, y, x + 2, y + 2), fill=(d, d, d, alpha))
    return img


def tex_asphalt():
    img = Image.new("RGB", (256, 256), (26, 27, 32))
    noise(img, 900, -12, 10, 70)
    save(img, "asphalt.png")


def tex_sidewalk():
    img = Image.new("RGB", (256, 256), (74, 77, 88))
    noise(img, 500, -10, 8, 60)
    d = ImageDraw.Draw(img)
    for i in (0, 128):
        d.line((0, i, 255, i), fill=(60, 62, 72), width=3)
    for i in (0, 128):
        d.line((i, 0, i, 255), fill=(60, 62, 72), width=3)
    save(img, "sidewalk.png")


def tex_bricks(name, base, dark=(42, 38, 36)):
    img = Image.new("RGB", (256, 256), dark)
    d = ImageDraw.Draw(img)
    bh, bw = 32, 64
    for row in range(8):
        off = (bw // 2) if row % 2 else 0
        for col in range(-1, 5):
            x0 = col * bw + off
            c = tuple(max(0, min(255, base[i] + random.randint(-14, 14))) for i in range(3))
            d.rectangle((x0 + 2, row * bh + 2, x0 + bw - 3, row * bh + bh - 3), fill=c)
    noise(img, 350, -10, 10, 45)
    save(img, f"{name}.png")


def tex_stone():
    img = Image.new("RGB", (256, 256), (40, 42, 50))
    d = ImageDraw.Draw(img)
    for _ in range(24):
        x, y = random.randint(0, 210), random.randint(0, 210)
        w, h = random.randint(38, 80), random.randint(24, 48)
        c = random.randint(68, 96)
        d.rounded_rectangle((x, y, min(x + w, 255), min(y + h, 255)), radius=6, fill=(c, c + 4, c + 10))
    noise(img, 300, -8, 8, 40)
    save(img, "stone.png")


def tex_planks():
    img = Image.new("RGB", (256, 256), (34, 26, 18))
    d = ImageDraw.Draw(img)
    ph = 42
    for i in range(6):
        y = i * ph
        c = (122 + random.randint(-12, 12), 82 + random.randint(-8, 8), 48 + random.randint(-6, 6))
        d.rectangle((0, y + 2, 255, y + ph - 2), fill=c)
        for gx in range(8, 248, 40):  # 木纹
            d.line((gx + i * 7, y + 4, gx + i * 7 + random.randint(0, 6), y + ph - 4),
                   fill=tuple(max(0, v - 18) for v in c), width=2)
    noise(img, 260, -10, 10, 40)
    save(img, "planks.png")


def tex_roof():
    img = Image.new("RGB", (256, 256), (38, 40, 46))
    d = ImageDraw.Draw(img)
    for row in range(8):
        y = row * 32
        off = 32 if row % 2 else 0
        for col in range(4):
            x = col * 64 + off
            c = (44 + random.randint(-6, 8), 46 + random.randint(-6, 8), 54 + random.randint(-6, 8))
            d.rectangle((x + 1, y + 1, x + 61, y + 31), fill=c)
    noise(img, 300, -8, 8, 40)
    save(img, "roof_dark.png")


def tex_door():
    img = Image.new("RGB", (128, 256), (20, 21, 26))
    d = ImageDraw.Draw(img)
    d.rectangle((6, 6, 122, 250), outline=(60, 56, 50), width=5)
    d.rectangle((22, 22, 106, 108), outline=(70, 66, 58), width=3)
    d.ellipse((106, 128, 118, 140), fill=(150, 140, 120))
    save(img, "door_dark.png")


def tex_glass_warm():
    img = Image.new("RGB", (256, 256))
    d = ImageDraw.Draw(img)
    for y in range(256):
        t = y / 255
        c = (int(38 + 210 * t), int(28 + 150 * t), int(16 + 70 * t))
        d.line((0, y, 255, y), fill=c)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 255, 255), outline=(24, 18, 12), width=8)
    d.line((128, 0, 128, 255), fill=(24, 18, 12), width=6)
    d.line((0, 128, 255, 128), fill=(24, 18, 12), width=6)
    save(img, "glass_warm.png")


def tex_awning(name, c1, c2=(232, 220, 200)):
    img = Image.new("RGB", (256, 128), c1)
    d = ImageDraw.Draw(img)
    for x in range(0, 256, 32):
        d.rectangle((x + 16, 0, x + 31, 127), fill=c2)
    d.rectangle((0, 0, 255, 127), outline=tuple(max(0, v - 40) for v in c1), width=4)
    save(img, f"awning_{name}.png")


def tex_vend():
    img = Image.new("RGB", (128, 256), (16, 20, 32))
    d = ImageDraw.Draw(img)
    for r in range(3):
        for c in range(2):
            x, y = 12 + c * 56, 16 + r * 74
            col = [(200, 60, 60), (60, 170, 220), (240, 190, 60), (90, 200, 120),
                   (220, 90, 190), (180, 120, 240)][r * 2 + c]
            d.rounded_rectangle((x, y, x + 46, y + 60), radius=6, fill=col)
    d.rectangle((10, 240, 118, 248), fill=(30, 36, 52))
    save(img, "vend_front.png")


def tex_concrete():
    img = Image.new("RGB", (256, 256), (138, 141, 150))
    noise(img, 700, -12, 10, 55)
    save(img, "concrete.png")


def tex_sign(name, text, accent):
    """512x128 霓虹招牌：深底 + 边框 + 发光中文店名。"""
    img = Image.new("RGB", (512, 128), (10, 11, 16))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 511, 127), outline=accent, width=8)
    d.rectangle((12, 12, 499, 115), outline=tuple(max(0, v - 60) for v in accent), width=2)

    font = ImageFont.truetype(font_path, 64)
    glow = Image.new("RGB", (512, 128), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    bb = gd.textbbox((0, 0), text, font=font)
    tx = (512 - (bb[2] - bb[0])) // 2 - bb[0]
    ty = (128 - (bb[3] - bb[1])) // 2 - bb[1]
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2), (-2, 2), (2, -2)):
        gd.text((tx + dx, ty + dy), text, font=font, fill=accent)
    glow = glow.filter(ImageFilter.GaussianBlur(3))
    img = Image.blend(img, glow, 0.55)
    d = ImageDraw.Draw(img)
    d.text((tx, ty), text, font=font, fill=(255, 255, 255))
    save(img, f"sign_{name}.png")


def main():
    print("[纹理] 生成六分街贴图集 -> Assets/Textures_ZZZ/")
    tex_asphalt()
    tex_sidewalk()
    tex_bricks("brick_red", (140, 58, 46))
    tex_bricks("brick_teal", (40, 84, 88))
    tex_bricks("brick_brown", (107, 74, 47))
    tex_bricks("brick_dark", (56, 60, 74))
    tex_bricks("brick_green", (72, 100, 62))
    tex_stone()
    tex_planks()
    tex_roof()
    tex_door()
    tex_glass_warm()
    tex_concrete()
    tex_vend()
    tex_awning("kitchen", (194, 42, 30))
    tex_awning("market", (26, 134, 140))
    tex_awning("forge", (80, 26, 16))
    tex_awning("farm", (61, 122, 52))
    tex_awning("lumber", (150, 96, 40))
    tex_awning("mine", (66, 70, 78))
    tex_sign("kitchen", "拉面馆", (255, 80, 48))
    tex_sign("market", "便利店", (51, 224, 255))
    tex_sign("forge", "铁匠铺", (255, 122, 26))
    tex_sign("farm", "农田温室", (77, 255, 102))
    tex_sign("lumber", "木材店", (255, 179, 64))
    tex_sign("mine", "矿洞", (255, 232, 77))
    tex_sign("well", "水井", (77, 166, 255))
    print("[纹理] 完成")


if __name__ == "__main__":
    main()
