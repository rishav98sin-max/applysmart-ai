"""Generate the Open Graph share image (1200x630) for ApplySmart AI.

Renders a branded card matching the landing (deep-slate canvas + green glow,
brand mark, the hero hook). Output: landing/assets/og.png. Re-run after copy
or brand changes:  python scripts/generate_og.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 630
CANVAS = (2, 6, 23)        # #020617
ACCENT = (34, 197, 94)     # #22C55E
ACCENT2 = (134, 239, 172)  # #86EFAC
STRONG = (248, 250, 252)   # #F8FAFC
MUTED = (148, 163, 184)    # #94A3B8
FAINT = (100, 116, 139)    # #64748B

FONTS = "C:/Windows/Fonts/"
def font(name, size):
    return ImageFont.truetype(FONTS + name, size)

f_word = font("segoeuib.ttf", 38)
f_eyebrow = font("segoeui.ttf", 24)
f_head = font("segoeuib.ttf", 72)
f_sub = font("segoeui.ttf", 30)
f_mono = font("consola.ttf", 24)

# ── base canvas + soft green aurora ───────────────────────────────────────
base = Image.new("RGBA", (W, H), CANVAS + (255,))
glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([-260, -300, 560, 480], fill=ACCENT + (95,))
gd.ellipse([820, -240, 1480, 380], fill=(22, 163, 74, 70))
gd.ellipse([300, 480, 980, 980], fill=ACCENT + (45,))
glow = glow.filter(ImageFilter.GaussianBlur(130))
base = Image.alpha_composite(base, glow).convert("RGB")
draw = ImageDraw.Draw(base)

# faint grid
for x in range(0, W, 64):
    draw.line([(x, 0), (x, H)], fill=(15, 23, 42))
for y in range(0, H, 64):
    draw.line([(0, y), (W, y)], fill=(15, 23, 42))

PAD = 84

# ── brand mark + wordmark ─────────────────────────────────────────────────
mx, my, ms = PAD, 70, 64
draw.rounded_rectangle([mx, my, mx + ms, my + ms], radius=16, fill=ACCENT)
cx, cy, s = mx + ms / 2, my + ms / 2, 19
draw.polygon([(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)], fill=STRONG)
wx = mx + ms + 22
draw.text((wx, my + 12), "ApplySmart", font=f_word, fill=STRONG)
asw = draw.textlength("ApplySmart", font=f_word)
draw.text((wx + asw, my + 12), " AI", font=f_word, fill=ACCENT)

# ── eyebrow ───────────────────────────────────────────────────────────────
draw.text((PAD, 232), "H O N E S T   ·   E N D - T O - E N D", font=f_eyebrow, fill=ACCENT)

# ── headline ──────────────────────────────────────────────────────────────
y = 280
for line, col in [("Found. Tailored. Written.", STRONG),
                  ("True to your layout.", STRONG),
                  ("True to your facts.", ACCENT2)]:
    draw.text((PAD, y), line, font=f_head, fill=col)
    y += 86

# ── footer strip ──────────────────────────────────────────────────────────
fy = 552
draw.ellipse([PAD, fy + 9, PAD + 11, fy + 20], fill=ACCENT)
draw.text((PAD + 24, fy), "Open source  ·  Free during beta", font=f_sub, fill=MUTED)
draw.text((W - PAD - draw.textlength("applysmart-ai.streamlit.app", font=f_mono), fy + 3),
          "applysmart-ai.streamlit.app", font=f_mono, fill=FAINT)

out = Path(__file__).resolve().parent.parent / "landing" / "assets" / "og.png"
out.parent.mkdir(parents=True, exist_ok=True)
base.save(out, "PNG")
print("wrote", out, base.size)
