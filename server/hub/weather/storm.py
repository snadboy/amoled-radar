"""The "Active" weather view: wherever the strongest storm within SEARCH_MI of home is now.

Every radar cycle the latest RainViewer frame is fetched as tiles covering SEARCH_MI
around home, each pixel is scored by how intense its colour is, and the 100-mile
window with the highest total wins. RainViewer serves one palette whatever the colour
parameter says, so intensity is read back from the colours: the translucent tans are
clear-air echo (0), as are the light cyans (the rings round radar sites in clear-air
mode); darker blues are light-to-moderate rain (up to 0.7),
yellow/orange heavy (8-12), red severe (25), pinks/white extreme (35) -- so one strong
cell outranks a wide area of drizzle.

To keep the view from hopping between two cells, the current centre is kept while it
still scores at least KEEP of the best one. Below QUIET the sky counts as quiet and the
view sits on home, saying so.
"""
import io, math, os, threading, time

import numpy as np
from PIL import Image

from . import render as R

SEARCH_MI = float(os.environ.get("STORM_SEARCH_MI", "800"))
WINDOW_MI = 100                 # the view's diameter (RADIUS_MI 50)
ZOOM      = 5                   # ~4.8 km/px at 40 N; RainViewer's free tier allows up to 7
QUIET     = float(os.environ.get("STORM_QUIET", "300"))   # ~40 px of heavy echo
KEEP      = 0.6
SNAP      = 0.25                # degrees: steady centres reuse basemap tiles and caches

_lock = threading.Lock()
_cur = None                     # {"lat", "lon", "score", "label", "at"}

def _weights(a):
    """RGBA uint8 (h, w, 4) -> float32 intensity weights."""
    r, g, b, al = (a[..., i].astype(np.int32) for i in range(4))
    w = np.zeros(r.shape, np.float32)
    solid = al >= 250
    blue = solid & (b > r) & (b > 90)
    w[blue] = np.clip((210 - b[blue]) / 150.0, 0, 0.7)    # light cyan (b > 200) is clear-air rings round radar sites
    yellow = solid & (r >= 230) & (b < 60) & (g >= 130)
    w[yellow] = 8 + (255 - g[yellow]) / 30.0
    red = solid & (r >= 180) & (g < 130) & (b < 120)
    w[red] = 25
    pink = solid & (r >= 180) & (b >= 120) & ~blue
    w[pink] = 35
    return w

def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y

def _latlon(x, y, z):
    n = 2 ** z
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon

def bearing_label(lat0, lon0, lat, lon):
    """"120 mi SW" from (lat0, lon0)."""
    dy = (lat - lat0) * 69.05
    dx = (lon - lon0) * 69.17 * math.cos(math.radians((lat + lat0) / 2))
    mi = math.hypot(dx, dy)
    if mi < 15: return "near"
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return "%d mi %s" % (int(round(mi / 10.0)) * 10, dirs[int((math.degrees(math.atan2(dx, dy)) + 22.5) % 360 // 45)])

def scan(maps, lat0, lon0):
    """(score, lat, lon) of the strongest window within SEARCH_MI of home."""
    frame = maps["radar"]["past"][-1]
    dlat = SEARCH_MI / 69.05
    dlon = SEARCH_MI / (69.17 * math.cos(math.radians(lat0)))
    x0, y0 = _tile_xy(lat0 + dlat, lon0 - dlon, ZOOM)
    x1, y1 = _tile_xy(lat0 - dlat, lon0 + dlon, ZOOM)
    tx0, ty0, tx1, ty1 = int(x0), int(y0), int(x1), int(y1)
    W, H = (tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256
    img = Image.new("RGBA", (W, H))
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            url = "%s%s/256/%d/%d/%d/%d/0_0.png" % (maps["host"], frame["path"], ZOOM, tx, ty, R.PALETTE)
            img.paste(Image.open(io.BytesIO(R.fetch(url))).convert("RGBA"), ((tx - tx0) * 256, (ty - ty0) * 256))
    w = _weights(np.asarray(img))
    # keep to the search circle
    ys, xs = np.mgrid[0:H, 0:W]
    hx, hy = _tile_xy(lat0, lon0, ZOOM)
    km_px = R.mpp(lat0, ZOOM) / 1000.0
    w[np.hypot(xs / 256.0 + tx0 - hx, ys / 256.0 + ty0 - hy) * 256 * km_px > SEARCH_MI * 1.609] = 0
    k = max(3, int(WINDOW_MI * 1.609 / km_px))           # window side in px
    c = np.pad(w, ((1, 0), (1, 0))).cumsum(0).cumsum(1)  # summed-area table
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    iy, ix = np.unravel_index(int(np.argmax(s)), s.shape)
    lat, lon = _latlon(tx0 + (ix + k / 2.0) / 256.0, ty0 + (iy + k / 2.0) / 256.0, ZOOM)
    def score_at(la, lo):                                 # the window around another centre
        px, py = _tile_xy(la, lo, ZOOM)
        jx, jy = int((px - tx0) * 256 - k / 2.0), int((py - ty0) * 256 - k / 2.0)
        return float(s[jy, jx]) if 0 <= jy < s.shape[0] and 0 <= jx < s.shape[1] else 0.0
    return float(s[iy, ix]), lat, lon, score_at

def update(maps, home):
    """Pick the Active view's centre for this radar cycle. home: the place it is measured from."""
    global _cur
    try:
        best, lat, lon, score_at = scan(maps, home["lat"], home["lon"])
    except Exception as e:
        print("[storm] scan failed: %s" % str(e)[:120], flush=True)
        return
    with _lock: cur = _cur
    if best < QUIET:
        new = {"lat": home["lat"], "lon": home["lon"], "score": best, "label": "No active storms", "short": "No storms"}
    elif cur and cur["score"] >= QUIET and score_at(cur["lat"], cur["lon"]) >= KEEP * best:
        new = dict(cur, score=score_at(cur["lat"], cur["lon"]))     # still busy there: stay put
    else:
        lat, lon = round(lat / SNAP) * SNAP, round(lon / SNAP) * SNAP
        where = bearing_label(home["lat"], home["lon"], lat, lon)
        new = {"lat": lat, "lon": lon, "score": best, "short": "Storm %s" % where,
               "label": "Storm %s of %s" % (where, home["name"])}
    new["at"] = int(time.time())
    with _lock: _cur = new
    print("[storm] %s (score %.0f, best %.0f)" % (new["label"], new["score"], best), flush=True)

def current():
    with _lock: return dict(_cur) if _cur else None
