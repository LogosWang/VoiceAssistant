"""Cuts the engine's moving parts out of their generated images."""
import json
import numpy as np
from PIL import Image, ImageFilter

def cut(name, core_at=45):
    a = np.asarray(Image.open(name + ".png").convert("RGB")).astype(float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    key = np.minimum(r, b) - g
    band = np.asarray(Image.fromarray(((key > core_at) * 255).astype("uint8")).filter(ImageFilter.MaxFilter(9))) > 0
    alpha = np.ones(key.shape); alpha[band] = 1 - np.clip(key[band] / 235, 0, 1)
    k = np.maximum(alpha, 0.12)
    un = lambda c, m: np.where(band, (c - (1 - k) * m) / k, c)
    im = Image.fromarray(np.dstack([un(r, 255), un(g, 0), un(b, 255), alpha * 255]).clip(0, 255).astype("uint8"), "RGBA")
    return im.crop(im.split()[3].point(lambda v: 255 if v > 128 else 0).getbbox())

geo = {}
beam = cut("beam"); a = np.asarray(beam.split()[3]) > 128
end = np.where(a[:, -6:].any(1))[0]; geo["beam"] = {"w": beam.width, "h": beam.height, "eye": (end[-1] - end[0] + 1) / 2}
beam.resize((900, round(beam.height * 900 / beam.width)), Image.LANCZOS).save("beam.webp", quality=90, method=6, exact=True)
wheel = cut("flywheel"); wheel.resize((240, 240), Image.LANCZOS).save("flywheel.webp", quality=90, method=6, exact=True)
ped = cut("pedestal"); ped.resize((200, round(ped.height * 200 / ped.width)), Image.LANCZOS).save("pedestal.webp", quality=90, method=6, exact=True)
geo["pedestal"] = {"aspect": ped.height / ped.width}
rod = cut("conrod"); s = 60 / rod.width; rod = rod.resize((60, round(rod.height * s)), Image.LANCZOS); rod.save("conrod.webp", quality=90, method=6, exact=True)
geo["conrod"] = {"w": rod.width, "h": rod.height}
print(json.dumps(geo))
