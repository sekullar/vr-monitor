"""VR Monitor ikonu (PNG + ICO) uretir: koyu yuvarlak kare, gradyan cerceve, VR gozluk silueti."""
from PIL import Image, ImageDraw, ImageFilter
import os
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# arka plan: koyu yuvarlak kare + mavi->mor gradyan zemin
grad = Image.new("RGBA", (S, S))
gp = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        gp[x, y] = (int(20 + 60 * t), int(24 + 30 * (1 - t)), int(56 + 110 * t), 255)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=230, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
# gozluk govdesi
d.rounded_rectangle((150, 330, 874, 700), radius=150, fill=(14, 18, 36, 255), outline=(150, 190, 255, 255), width=18)
# burun girintisi
d.pieslice((420, 560, 604, 760), 0, 180, fill=(40, 60, 130, 255))
d.ellipse((428, 580, 596, 748), fill=None)
# lensler (parlak)
for cx in (340, 684):
    d.ellipse((cx - 118, 400, cx + 118, 636), fill=(92, 157, 255, 255), outline=(230, 240, 255, 255), width=10)
    d.ellipse((cx - 70, 440, cx + 10, 520), fill=(200, 225, 255, 200))
# kafa bandi
d.rounded_rectangle((60, 470, 160, 560), radius=40, fill=(150, 190, 255, 255))
d.rounded_rectangle((864, 470, 964, 560), radius=40, fill=(150, 190, 255, 255))
img = img.filter(ImageFilter.SMOOTH)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
os.makedirs(out, exist_ok=True)
img.resize((512, 512), Image.LANCZOS).save(os.path.join(out, "icon.png"))
img.resize((256, 256), Image.LANCZOS).save(os.path.join(out, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ikon hazir:", out)
