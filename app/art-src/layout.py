"""Writes the piston crop and the layer geometry from cut.py's output into index.html."""
import json, re, sys
from PIL import Image
geo = json.loads(open("cut.out").readline()); s = 320 / 640
(lx, ly, lrx, _), (rx, _, rrx, _) = geo["engine"]["mouths"]; P = geo["piston"]
rod_w = min(lrx, rrx) * 2 * s * .56; scale = rod_w / P["rod"]; pw = P["width"] * scale; head = P["head"] * scale
TRAVEL, E_TOP = 46, 70
mouth_y = E_TOP + ly * s; top = mouth_y - head - 3 - TRAVEL; rod_len = TRAVEL + head + 150
Image.open("piston_full.png").crop((0, 0, P["width"], int(rod_len / scale))).resize((round(pw * 2), round(rod_len * 2)), Image.LANCZOS).save("piston.webp", quality=90, method=6, exact=True)
h = open(sys.argv[1]).read()
def sub(pat, rep, flags=0):
    global h
    h, n = re.subn(pat, rep, h, flags=flags); assert n, pat
sub(r"#engine,#engineFront\{[^}]*\}", f"#engine,#engineFront{{left:0;top:{E_TOP}px;width:320px}}")
sub(r"\.rod\{width:[\d.]+px;top:-?[\d.]+px;", f".rod{{width:{pw:.1f}px;top:{top:.1f}px;")
sub(r"#rodL\{[^}]*\}", f"#rodL{{left:{lx*s-pw/2:.1f}px}}"); sub(r"#rodR\{left:[^;]*;", f"#rodR{{left:{rx*s-pw/2:.1f}px;")
sub(r"to\{transform:translateY\(\d+px\)\}", f"to{{transform:translateY({TRAVEL}px)}}")
for k in ("eyes", "mouth"):
    g = geo[k]; sub(rf"#{k}\{{left:[^;]*;top:[^;]*;width:[^;]*;height:[^;]*;", f"#{k}{{left:{g['left']:.3f}%;top:{g['top']:.3f}%;width:{g['width']:.3f}%;height:{g['height']:.3f}%;")
y = mouth_y - 2
sub(r"\[\[\d+, \d+, 0\].*?\]\]\.forEach", f"[[{lx*s:.0f}, {y:.0f}, 0], [{lx*s:.0f}, {y:.0f}, .7], [{lx*s:.0f}, {y:.0f}, 1.4], [{rx*s:.0f}, {y:.0f}, .35], [{rx*s:.0f}, {y:.0f}, 1.05], [{rx*s:.0f}, {y:.0f}, 1.75]].forEach", re.S)
open(sys.argv[1], "w").write(h)
