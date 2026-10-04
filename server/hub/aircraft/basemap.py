"""Aircraft backgrounds: dark basemaps with towns, range rings and the home mark
baked in, one per zoom level, sized to a device profile (w, h, corner radius).

Projection is Web Mercator centred on the view, scaled so that range_mi ground
miles reach from the centre to the edge of the SHORT axis. The device projects
aircraft with the same numbers (bundle header), so planes line up with the map.

Background: Esri World Dark Gray Canvas (Esri, HERE, Garmin, (c) OpenStreetMap
contributors). Towns come from OpenStreetMap place nodes (Overpass), placed here so
each zoom gets nearby towns and no label is cut off by rounded corners or hidden
under the device's UI pills.

Bundle format ABN1 (little-endian), served as /aircraft/<view>/bundle.bin:
  0   4s  "ABN1"            4  u16 version (1)      6  u16 nlevels
  8   u16 w                 10 u16 h                12 u32 reserved
  16  f64 centre merc x     24 f64 centre merc y    (Web Mercator metres)
  32  f32 centre lat        36 f32 centre lon
  40  per level, 16 bytes:  u16 range_mi, u16 reserved, f32 merc metres per px,
                            u32 offset, u32 length
  ... pixels: RGB565 little-endian (LVGL native), w*h*2 bytes per level
"""
import hashlib, json, math, os, struct, threading, time, urllib.parse, urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

R_EARTH  = 6378137.0
M_PER_MI = 1609.344
TILE_PX  = 256
ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA = "snadboy-display-hub/1.0 (personal homelab display)"
GAMMA = 2.0                 # land near black (power, burn-in); roads and water edges stay visible
EDGE_PAD = 6
RING_RGB = (0x3f, 0xb7, 0xa0)
RING_ALPHA = 0.5
HOME_GREY = 180             # was white on the Arduino build; dimmed like weather's crosshair

FONT_DIR = "/usr/share/fonts/truetype/dejavu"

def font(size, bold=False):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)
    except OSError:
        return ImageFont.load_default()

def merc(lat, lon):
    return (math.radians(lon) * R_EARTH,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R_EARTH)

def rings_for(range_mi):
    return {50: (10, 25, 50), 25: (5, 10, 25), 10: (2, 5, 10)}.get(range_mi, (range_mi / 5, range_mi / 2, range_mi))

def min_population(range_mi):
    return 60000 if range_mi >= 40 else 15000 if range_mi >= 20 else 1500

def ui_boxes(w, h):
    """Screen areas the firmware covers with its UI (range pill, clock, status,
    attribution) plus the home mark. Labels stay out of them. This is a contract
    with the firmware's layout."""
    cx, cy = w // 2, h // 2
    return [(20, 8, 120, 48), (w - 140, 8, w - 20, 48), (20, h - 48, 250, h - 8),
            (w - 150, h - 40, w - 20, h - 8), (cx - 7, cy - 7, cx + 7, cy + 7)]

def mpp_for(view, range_mi, w, h):
    """Mercator metres per screen pixel (ground m/px divided by cos(lat))."""
    return range_mi * M_PER_MI / (min(w, h) / 2.0) / math.cos(math.radians(view["lat"]))

def _cache_dir(cache, *sub):
    p = os.path.join(cache, "aircraft", *sub); os.makedirs(p, exist_ok=True); return p

def _fetch(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()

def _tile(cache, z, x, y):
    p = os.path.join(_cache_dir(cache, "tiles"), "dkgray_%d_%d_%d.img" % (z, x, y))
    if not os.path.exists(p):
        data = _fetch(ESRI.format(z=z, x=x, y=y))
        with open(p + ".tmp", "wb") as f: f.write(data)
        os.replace(p + ".tmp", p)
        time.sleep(0.1)
    return Image.open(p).convert("RGB")

def places(cache, view):
    """[(population, name, lat, lon)] biggest first, covering the widest view's corners."""
    radius = int(max(view["levels"]) * 1.45)
    p = os.path.join(_cache_dir(cache), "places_%.4f_%.4f_%d.json" % (view["lat"], view["lon"], radius))
    if not os.path.exists(p):
        q = ('[out:json][timeout:60];node(around:%.0f,%s,%s)[place~"^(city|town|village)$"][name];out body;'
             % (radius * M_PER_MI, view["lat"], view["lon"]))
        for url in OVERPASS * 2:               # the public servers are often busy
            try:
                data = _fetch(url, urllib.parse.urlencode({"data": q}).encode(), timeout=90); break
            except Exception as e:
                print("[aircraft] overpass %s: %s; retrying" % (url, str(e)[:60]), flush=True); time.sleep(5)
        else:
            raise RuntimeError("Overpass API unavailable")
        with open(p, "wb") as f: f.write(data)
    out = []
    for e in json.load(open(p))["elements"]:
        try: pop = int(e["tags"].get("population", "0").replace(",", ""))
        except ValueError: pop = 0
        out.append((pop, e["tags"]["name"], e["lat"], e["lon"]))
    return sorted(out, reverse=True)

def _visible(box, w, h, r):
    """True if the whole box lies inside the panel's rounded visible area."""
    x0, y0, x1, y1 = box
    if x0 < EDGE_PAD or y0 < EDGE_PAD or x1 > w - EDGE_PAD or y1 > h - EDGE_PAD:
        return False
    for cx in (r, w - r):
        for cy in (r, h - r):
            for px in (x0, x1):
                for py in (y0, y1):
                    in_corner = (px < cx) == (cx == r) and (py < cy) == (cy == r)
                    if in_corner and math.hypot(px - cx, py - cy) > r - EDGE_PAD:
                        return False
    return True

def _overlaps(a, b, pad=3):
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])

def _draw_places(img, towns, view, mpp, range_mi, w, h, r):
    """Greedy, biggest-first label placement with collision avoidance."""
    d = ImageDraw.Draw(img)
    cx0, cy0 = merc(view["lat"], view["lon"])
    taken, placed = list(ui_boxes(w, h)), 0
    for pop, name, lat, lon in towns:
        if pop < min_population(range_mi): continue
        mx, my = merc(lat, lon)
        x, y = w / 2 + (mx - cx0) / mpp, h / 2 - (my - cy0) / mpp
        if not (0 <= x < w and 0 <= y < h): continue
        big = pop >= 100000 or (range_mi < 20 and pop >= 30000)
        f = font(16 if big else 13, bold=big)
        grey = 200 if big else 150
        dot = (x - 2, y - 2, x + 2, y + 2)
        if any(_overlaps(dot, t, 1) for t in taken) or not _visible(dot, w, h, r): continue
        l, t, rr, b = d.textbbox((0, 0), name, font=f)
        tw, th = rr - l, b - t
        for ox, oy in ((6, -th / 2), (-6 - tw, -th / 2), (-tw / 2, -th - 6), (-tw / 2, 6)):   # right, left, above, below
            box = (x + ox, y + oy, x + ox + tw, y + oy + th)
            if _visible(box, w, h, r) and not any(_overlaps(box, tb) for tb in taken):
                d.ellipse(dot, fill=grey)
                d.text((box[0] - l, box[1] - t), name, font=f, fill=grey)
                taken += [box, dot]; placed += 1
                break
    return placed

def render_level(cache, view, towns, range_mi, w, h, r):
    """RGB image for one zoom level, plus its Mercator m/px."""
    mpp = mpp_for(view, range_mi, w, h)
    world = 2 * math.pi * R_EARTH
    z = 0
    while world / (TILE_PX * 2 ** z) > mpp and z < 18:      # smallest zoom at least as fine as mpp
        z += 1
    tile_mpp = world / (TILE_PX * 2 ** z)
    cx, cy = merc(view["lat"], view["lon"])
    gx, gy = (cx + world / 2) / tile_mpp, (world / 2 - cy) / tile_mpp
    hw, hh = w / 2 * mpp / tile_mpp, h / 2 * mpp / tile_mpp
    x0, y0, x1, y1 = gx - hw, gy - hh, gx + hw, gy + hh
    tx0, ty0, tx1, ty1 = int(x0 // TILE_PX), int(y0 // TILE_PX), int(x1 // TILE_PX), int(y1 // TILE_PX)
    mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_PX, (ty1 - ty0 + 1) * TILE_PX))
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            mosaic.paste(_tile(cache, z, tx, ty), ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))
    box = (x0 - tx0 * TILE_PX, y0 - ty0 * TILE_PX, x1 - tx0 * TILE_PX, y1 - ty0 * TILE_PX)
    img = mosaic.resize((w, h), Image.LANCZOS, box=box).convert("L")
    img = img.point([round(255 * (v / 255) ** GAMMA) for v in range(256)])
    n = _draw_places(img, towns, view, mpp, range_mi, w, h, r)
    img = img.convert("RGB")

    # Range rings (50% teal over the map) and the home mark.
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    ppm = min(w, h) / 2.0 / range_mi
    c = (w // 2, h // 2)
    f = font(11)
    for miles in rings_for(range_mi):
        rr = miles * ppm
        d.ellipse((c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr), outline=RING_RGB + (int(255 * RING_ALPHA),))
        d.text((c[0] + 3, c[1] - rr - 14), "%g" % miles, font=f, fill=RING_RGB + (255,))
    d.line((c[0] - 6, c[1], c[0] + 6, c[1]), fill=(HOME_GREY,) * 3 + (255,), width=2)
    d.line((c[0], c[1] - 6, c[0], c[1] + 6), fill=(HOME_GREY,) * 3 + (255,), width=2)
    img.paste(ov, (0, 0), ov)
    print("[aircraft] %s %d mi %dx%d: zoom %d, %d towns" % (view["id"], range_mi, w, h, z, n), flush=True)
    return img, mpp

def rgb565_le(img):
    """Neutral greys map to EXACT neutral RGB565 (g6 = 2*r5): quantizing the channels
    independently tints dark greys blue or purple on the panel, because green has
    one more bit."""
    a = np.asarray(img.convert("RGB")).astype(np.int32)
    r5 = (a[..., 0] * 31 + 127) // 255
    g6 = (a[..., 1] * 63 + 127) // 255
    b5 = (a[..., 2] * 31 + 127) // 255
    grey = (a[..., 0] == a[..., 1]) & (a[..., 1] == a[..., 2])
    g6 = np.where(grey, r5 * 2, g6)
    b5 = np.where(grey, r5, b5)
    return ((r5 << 11) | (g6 << 5) | b5).astype("<u2").tobytes()

def encode_bundle(view, levels, w, h):
    """levels: [(range_mi, mpp, image)] -> ABN1 bytes."""
    mx, my = merc(view["lat"], view["lon"])
    head = 40 + 16 * len(levels)
    table, blobs, off = b"", [], head
    for rng, mpp, img in levels:
        px = rgb565_le(img)
        table += struct.pack("<HHfII", int(rng), 0, mpp, off, len(px))
        blobs.append(px); off += len(px)
    hdr = struct.pack("<4sHHHHIddff", b"ABN1", 1, len(levels), w, h, 0, mx, my, view["lat"], view["lon"])
    return hdr + table + b"".join(blobs)

class Bundles:
    """Bundles per (view, profile), built on first request and kept in memory (tiles
    and towns are cached on disk, so a rebuild after a restart is quick). The images
    are kept for previews. A failed build returns None and is retried next time."""
    def __init__(self, cache):
        self.cache, self.lock, self.mem, self.building = cache, threading.Lock(), {}, {}

    def get(self, view, w, h, r):
        key = (view["id"], w, h, r)
        with self.lock:
            if key in self.mem: return self.mem[key]
            ev = self.building.get(key)
            if ev is None:
                ev = self.building[key] = threading.Event(); mine = True
            else:
                mine = False
        if not mine:
            ev.wait(); return self.mem.get(key)
        try:
            towns = places(self.cache, view)
            levels = []
            for rng in view["levels"]:
                img, mpp = render_level(self.cache, view, towns, rng, w, h, r)
                levels.append((rng, mpp, img))
            blob = encode_bundle(view, levels, w, h)
            entry = {"blob": blob, "id": hashlib.sha256(blob).hexdigest()[:16],
                     "levels": [{"range_mi": rng, "mpp": mpp, "rings": list(rings_for(rng))} for rng, mpp, _ in levels],
                     "images": [img for _, _, img in levels]}
            with self.lock: self.mem[key] = entry
            return entry
        finally:
            with self.lock: self.building.pop(key, None)
            ev.set()
