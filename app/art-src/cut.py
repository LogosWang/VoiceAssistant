"""Cuts the generated images into the layers app/index.html uses, and prints their geometry."""
import json
import numpy as np
from PIL import Image, ImageChops, ImageFilter

def cut(name, core_at=150):
    a = np.asarray(Image.open(name + ".png").convert("RGB")).astype(float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    key = np.minimum(r, b) - g
    core = key > core_at
    if name in ("base", "eyes_closed", "mouth_open"):   # gaps between strands of hair hold diluted magenta
        core[430:] = key[430:] > 45
    band = np.asarray(Image.fromarray((core * 255).astype("uint8")).filter(ImageFilter.MaxFilter(9))) > 0
    alpha = np.ones(key.shape)
    alpha[band] = 1 - np.clip(key[band] / 235, 0, 1)
    k = np.maximum(alpha, 0.12)
    un = lambda c, m: np.where(band, (c - (1 - k) * m) / k, c)
    fr, fg, fb = un(r, 255), un(g, 0), un(b, 255)
    fr = np.where(band, np.minimum(fr, np.maximum(fg, fb * .75) + 6), fr)   # what's left of the magenta tint on fine hair
    if name in ("base", "eyes_closed", "mouth_open"):   # the blue-black hair below the hat, where a purple tint shows
        y = np.arange(key.shape[0])[:, None] >= 560
        wide = np.asarray(Image.fromarray(((key > core_at) * 255).astype("uint8")).filter(ImageFilter.MaxFilter(31))) > 0
        hair = y & wide & (fb > fg) & (fr > fg)
        fr = np.where(hair, np.minimum(fr, fg + 4), fr)
        fb = np.where(hair, np.minimum(fb, fg * 1.35 + 10), fb)
    return Image.fromarray(np.dstack([fr, fg, fb, alpha * 255]).clip(0, 255).astype("uint8"), "RGBA")

W, H = 1024, 1536
geo = {}
cut("base").resize((683, 1024), Image.LANCZOS).save("char.webp", quality=92, method=6, exact=True)

def patch(name, out, region):
    """The patch is exactly what changed: its box is found from the difference to the base."""
    x0, y0, x1, y1 = region
    d = np.asarray(ImageChops.difference(Image.open("base.png").convert("RGB"), Image.open(name + ".png").convert("RGB"))).sum(2)
    ys, xs = np.where(d[y0:y1, x0:x1] > 70)
    m = 26
    box = (x0 + int(np.percentile(xs, .5)) - m, y0 + int(np.percentile(ys, .5)) - m, x0 + int(np.percentile(xs, 99.5)) + m, y0 + int(np.percentile(ys, 99.5)) + m)
    v = cut(name)
    mask = Image.new("L", v.size, 0); mask.paste(255, (box[0] + 14, box[1] + 14, box[2] - 14, box[3] - 14))
    mask = mask.filter(ImageFilter.GaussianBlur(7))
    v.putalpha(Image.fromarray((np.asarray(mask) / 255 * np.asarray(v.split()[3])).astype("uint8")))
    s = 683 / W
    v.crop(box).resize((round((box[2] - box[0]) * s), round((box[3] - box[1]) * s)), Image.LANCZOS).save(out + ".webp", quality=92, method=6, exact=True)
    geo[out] = {"left": box[0] / W * 100, "top": box[1] / H * 100, "width": (box[2] - box[0]) / W * 100, "height": (box[3] - box[1]) / H * 100}

patch("eyes_closed", "eyes", (250, 420, 780, 680))
patch("mouth_open", "mouth", (380, 680, 650, 860))

# ---------- the engine, and the same engine without what lies behind each cylinder's front lip ----------
e = cut("engine", 45); e = e.crop(e.split()[3].point(lambda v: 255 if v > 128 else 0).getbbox())
e = e.resize((640, round(e.height * 640 / e.width)), Image.LANCZOS)
rgba = np.asarray(e).astype(float)
lum = rgba[..., :3].mean(2); solid = rgba[..., 3] > 128
mouths = []
for lo, hi in ((0, 320), (320, 640)):
    top = solid[:80, lo:hi]; cols = np.where(top.any(0))[0]; cx = lo + (cols[0] + cols[-1]) // 2
    dark_lim = np.percentile(lum[:120, cx - 40:cx + 40][solid[:120, cx - 40:cx + 40]], 30) + 8
    col = lum[:160, cx] < dark_lim
    ys = np.where(col & solid[:160, cx])[0]; runs = np.split(ys, np.where(np.diff(ys) > 2)[0] + 1); run = max(runs, key=len)
    cy, ry = (run[0] + run[-1]) / 2, (run[-1] - run[0]) / 2
    row = (lum[int(cy), lo:hi] < dark_lim) & solid[int(cy), lo:hi]
    xs = np.where(row)[0] + lo; rs = np.split(xs, np.where(np.diff(xs) > 2)[0] + 1); r = max(rs, key=len)
    mx, rx = (r[0] + r[-1]) / 2, (r[-1] - r[0]) / 2
    mouths.append((mx, cy, rx, ry))
front = np.asarray(e).copy()
yy, xx = np.mgrid[:front.shape[0], :front.shape[1]]
for mx, cy, rx, ry in mouths:
    inside = np.abs(xx - mx) < rx
    lip = cy + ry * np.sqrt(np.clip(1 - ((xx - mx) / rx) ** 2, 0, 1))
    front[..., 3][inside & (yy < lip)] = 0
e.save("engine.webp", quality=90, method=6, exact=True)
Image.fromarray(front, "RGBA").save("engine_front.webp", quality=90, method=6, exact=True)
geo["engine"] = {"width": 640, "height": e.height, "mouths": mouths}

# ---------- the piston: crosshead block plus a rod just long enough to stay inside the cylinder ----------
p = cut("piston", 45); p = p.crop(p.split()[3].point(lambda v: 255 if v > 128 else 0).getbbox())
pa = np.asarray(p.split()[3]) > 128
widths = pa.sum(1); rod = int(np.median(widths)); head = int(np.where(widths > rod * 1.25)[0].max()) + 1
geo["piston"] = {"width": p.width, "height": p.height, "rod": rod, "head": head}
p.save("piston_full.png")
print(json.dumps(geo))
for n in ("char", "engine", "engine_front"):
    a = np.asarray(Image.open(n + ".webp").convert("RGBA")).astype(int); k = np.minimum(a[..., 0], a[..., 2]) - a[..., 1]
    print(n, "visible magenta", int(((k > 60) & (a[..., 3] > 128)).sum()))
