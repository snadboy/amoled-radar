#!/usr/bin/env python3
"""Composite RainViewer radar over a cached Esri dark basemap for a fixed location.

Outputs panel-sized JPEG frames for a Waveshare ESP32-C6-Touch-AMOLED-2.16
(480x480, CO5300). All compositing happens server-side: the C6 has 512KB SRAM
and no PSRAM, so a single 480x480 RGB565 framebuffer (450KB) will not fit.

Layer zooms are decoupled on purpose:
  * basemap  z=9  from Esri  -- no zoom cap, fetched once, cached forever
  * radar    z=7  size=512   -- RainViewer's FREE TIER REFUSES z>=8 (returns a
                                "Zoom Level Not Supported" watermark tile), and
                                512px tiles double density to ~455 m/px, which
                                is already at NEXRAD's native resolution.
"""
import io, json, math, os, time, urllib.request
from PIL import Image, ImageDraw, ImageFont

# Centre of the radar view. The default is rounded to ~1km on purpose -- the
# display spans 161km, so finer precision is invisible here and there is no
# reason for a public repo to carry an exact home position. Override in .env.
LAT  = float(os.environ.get('RADAR_LAT', '41.90'))
LON  = float(os.environ.get('RADAR_LON', '-88.32'))
RADIUS_MI   = 50.0
PANEL       = 480
# The panel's glass has ROUNDED CORNERS (~50-56 px radius, measured from a photo of
# the real board 2026-10-03). Anything inside a corner arc is cut off: it clipped
# the "50 mi" label, the first digit of the temperature and the "%". Keep text out.
CORNER_R    = 56
SIDE_INSET  = 40          # status strip text sits this far in from each side
STATUS_H    = 56
VIEW_H      = PANEL - STATUS_H          # 424 px of radar
BASE_ZOOM   = 9
RADAR_ZOOM  = 7                          # hard ceiling on the free tier
RADAR_TILE  = 512
PALETTE     = 4                          # RainViewer: 4 = The Weather Channel
UA          = "snadboy-homelab-radar/1.0 (personal homelab display)"
CACHE       = os.environ.get("RADAR_CACHE", "/tmp/radar-cache")

def mpp(lat, z, tile=256):
    return 156543.03392804097 * math.cos(math.radians(lat)) / (2 ** z) * (256.0 / tile)

def deg2px(lat, lon, z, tile=256):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n * tile
    lr = math.radians(lat)
    y = (1.0 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2.0 * n * tile
    return x, y

def window(z, tile, lat=None, lon=None, view=None, g=None):
    """Pixel box at (z, tile) for the view's geographic window.

    Two modes, set per view:
      * radius_mi (cities, default 50): the full radius fits the SHORT axis, so
        it is visible in every direction.
      * lon_span (wide regional views): the span of longitude fills the panel WIDTH.
    """
    view = view or {}; g = g or DEFAULT_GEOM
    lat = LAT if lat is None else lat; lon = LON if lon is None else lon
    if view.get("lon_span"):
        half_w = view["lon_span"] / 2.0 / 360.0 * (2 ** z) * tile
        half_h = half_w * (g.view_h / float(g.w))
    else:
        span_m = view.get("radius_mi", RADIUS_MI) * 2 * 1609.344
        half_h = span_m / mpp(lat, z, tile) / 2.0
        half_w = half_h * (g.w / float(g.view_h))
    bleed  = (g.orbit + 2) / float(g.view_h) * (half_h * 2)   # keep the orbit in-bounds
    half_h += bleed; half_w += bleed
    cx, cy = deg2px(lat, lon, z, tile)
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h

def fetch(url, timeout=25):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout).read()

def mosaic(url_for, z, tile, cache_key=None, lat=None, lon=None, view=None, g=None):
    if cache_key:
        p = os.path.join(CACHE, cache_key)
        if os.path.exists(p):
            return Image.open(p).convert("RGBA")
    x0, y0, x1, y1 = window(z, tile, lat, lon, view, g)
    tx0, ty0, tx1, ty1 = int(x0//tile), int(y0//tile), int(x1//tile), int(y1//tile)
    canvas = Image.new("RGBA", ((tx1-tx0+1)*tile, (ty1-ty0+1)*tile), (0, 0, 0, 0))
    for tx in range(tx0, tx1+1):
        for ty in range(ty0, ty1+1):
            try:
                t = Image.open(io.BytesIO(fetch(url_for(tx, ty)))).convert("RGBA")
                if t.size != (tile, tile): t = t.resize((tile, tile), Image.LANCZOS)
                canvas.paste(t, ((tx-tx0)*tile, (ty-ty0)*tile))
            except Exception as e:
                print("      tile %d/%d failed: %s" % (tx, ty, str(e)[:50]))
    out = canvas.crop((int(x0-tx0*tile), int(y0-ty0*tile), int(x1-tx0*tile), int(y1-ty0*tile)))
    if cache_key:
        os.makedirs(CACHE, exist_ok=True); out.save(os.path.join(CACHE, cache_key))
    return out

# Burn-in mitigation. AMOLED emitters age by cumulative luminance*time, roughly
# L^1.5-2, and this panel has no pixel-shift or uniformity compensation of its
# own. The radar returns move; EVERYTHING else here is static -- rings,
# crosshair, labels, basemap -- so static chrome is the whole risk.
ORBIT_PX    = int(os.environ.get("RADAR_ORBIT_PX", "6"))      # +/- px of slow drift
ORBIT_STEPS = int(os.environ.get("RADAR_ORBIT_STEPS", "24"))  # positions per cycle
RING_ALPHA  = int(os.environ.get("RADAR_RING_ALPHA", "55"))   # was 85
CROSS_ALPHA = int(os.environ.get("RADAR_CROSS_ALPHA", "110")) # was 190

class Geom:
    """Panel layout for one device profile: the radar view on top, the status strip
    below. The default is the 2.16" AMOLED (480x480, 424 px of radar + 56 px strip);
    every other profile scales from it. Pixel orbit and dimmed chrome are AMOLED
    burn-in measures, so an LCD gets no orbit."""
    def __init__(self, w=PANEL, h=PANEL, r=CORNER_R, panel="amoled", strip=True):
        self.w, self.h, self.r, self.panel = int(w), int(h), int(r), panel
        # Round glass (r = half the panel, e.g. a 240x240 GC9A01): text is kept inside the
        # circle instead of out of four corners, and the strip stacks its lines centred.
        self.round = self.r * 2 >= min(self.w, self.h)
        self.strip = bool(strip)
        if not self.strip: self.status_h = 0            # radar only (a device setting)
        elif self.round: self.status_h = 52
        else: self.status_h = STATUS_H if (self.w, self.h) == (PANEL, PANEL) else max(48, int(self.h * STATUS_H / PANEL) & ~1)
        self.view_h = self.h - self.status_h
        self.orbit = ORBIT_PX if panel == "amoled" else 0
        self.side_inset = max(16, self.r * SIDE_INSET // CORNER_R) if self.r else 16
        self.key = "%dx%d_r%d_%s%s" % (self.w, self.h, self.r, panel, "" if self.strip else "_nostrip")

DEFAULT_GEOM = Geom()

def orbit_offset(seq, g=None):
    """Walk a slow Lissajous-ish path so no static edge sits on one pixel."""
    import math as _m
    px = (g or DEFAULT_GEOM).orbit
    a = 2.0 * _m.pi * (seq % ORBIT_STEPS) / ORBIT_STEPS
    return int(round(px * _m.sin(a))), int(round(px * _m.sin(2 * a) / 2.0))

DARK_GAMMA = float(os.environ.get("RADAR_DARK_GAMMA", "1.7"))
DARK_SCALE = float(os.environ.get("RADAR_DARK_SCALE", "0.72"))

def darken_for_amoled(img):
    """AMOLED pixels are individually lit, so true black costs no power and gives
    infinite contrast. Esri's 'Dark Gray' basemap is still mid-grey, which both
    wastes power and muddies the radar palette. Push midtones down hard while
    leaving bright map features (roads, labels, coastline) legible."""
    lut = [min(255, int(255.0 * ((v / 255.0) ** DARK_GAMMA) * DARK_SCALE)) for v in range(256)]
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r.point(lut), g.point(lut), b.point(lut), a))

def basemap(lat=None, lon=None, view=None, g=None):
    view = view or {}; g = g or DEFAULT_GEOM
    lat = LAT if lat is None else lat; lon = LON if lon is None else lon
    z = view.get("base_zoom", BASE_ZOOM)
    span = "s%g" % view["lon_span"] if view.get("lon_span") else "r%g" % view.get("radius_mi", RADIUS_MI)
    url = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
           "Canvas/World_Dark_Gray_Base/MapServer/tile/%d/%d/%d")   # NOTE: z/y/x
    # The cache key MUST include the location. It used to be just "base_z9.png",
    # which every city would have silently shared.
    # ...and the window's shape: other profiles get their own (the default keeps the old key).
    shape = "" if g.key == DEFAULT_GEOM.key else "_%dx%d" % (g.w, g.view_h)
    return mosaic(lambda x, y: url % (z, y, x), z, 256,
                  cache_key="base_z%d_%.4f_%.4f_%s_o%d%s.png" % (z, lat, lon, span, g.orbit, shape),
                  lat=lat, lon=lon, view=view, g=g)

def radar(host, path, lat=None, lon=None, view=None, g=None):
    view = view or {}
    z, tile = view.get("radar_zoom", RADAR_ZOOM), view.get("radar_tile", RADAR_TILE)
    url = "%s%s/%d/%d/%%d/%%d/%d/1_1.png" % (host, path, tile, z, PALETTE)
    return mosaic(lambda x, y: url % (x, y), z, tile, lat=lat, lon=lon, view=view, g=g)

def suppress_clear_air(img):
    """Drop RainViewer's lowest band: semi-transparent tans and greys, from about
    rgba(117,112,98,52) to rgba(222,208,151,190). Nationally that band was 43.6%
    of all returns, mostly clear-air echo (insects, birds) ringing the radar sites
    in the evening, plus a soft halo around real storms. Rain (blue, B > R) and
    heavy returns (R - B > 140, opaque) are untouched.

    Always used on wide views; on city views only above 40 F, since whether this band carries light snow is
    unverified, so the city views keep it."""
    import numpy as np
    a = np.asarray(img.convert("RGBA")).copy()
    r, g, b, al = (a[..., i].astype(np.int16) for i in range(4))
    tan = (al < 200) & (r > b) & (r - b < 90) & (np.abs(r - g) < 25)
    a[..., 3][tan] = 0
    return Image.fromarray(a, "RGBA")

# ---------------------------------------------------------------------------
# Quality control against NOAA's QC'd mosaic
#
# RainViewer appears to serve near-raw reflectivity, so it shows birds, insects
# and clutter that weather apps strip out. NOAA/NCEP publishes a quality-
# controlled CONUS base-reflectivity mosaic (dual-pol filtered), every 1-2 min,
# ~2 h of history, as a WMS -- any bounding box, no tiles, no key. On
# 2026-10-03 a light-blue patch east of St. Louis at 27% humidity had ZERO
# returns there: migrating birds, not rain.
#
# It is blockier than RainViewer and uses another palette, so it is used as a
# MASK: keep RainViewer's rendering, erase echo where NOAA shows none.
# ---------------------------------------------------------------------------
QC_WMS = "https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows"
QC_UA  = {"User-Agent": "snadboy-homelab-radar/1.0 (dschless@gmail.com)"}
QC_DILATE_KM = float(os.environ.get("RADAR_QC_DILATE_KM", "4"))
QC_MAX_SKEW_S = 600          # don't use a NOAA frame more than 10 min from the RainViewer one

def qc_times():
    """Epoch seconds of every NOAA QC'd frame currently available."""
    import re
    from datetime import datetime, timezone
    cap = urllib.request.urlopen(urllib.request.Request(
        QC_WMS + "?service=WMS&version=1.3.0&request=GetCapabilities", headers=QC_UA), timeout=30).read().decode("utf-8", "ignore")
    raw = re.search(r'<Dimension name="time"[^>]*>([^<]+)<', cap).group(1).split(",")
    return [(datetime.strptime(t.strip()[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp(), t.strip())
            for t in raw if t.strip()]

def qc_mask(lat, lon, view, when, available, ow, oh, g=None):
    """Boolean mask (oh x ow) of where NOAA's QC'd mosaic has echo nearest to
    `when`, dilated by QC_DILATE_KM, over exactly the frame's oversized window.
    Returns None if no NOAA frame is close enough in time."""
    import math, numpy as np, cv2
    if not available:
        return None
    t_epoch, t_iso = min(available, key=lambda a: abs(a[0] - when))
    if abs(t_epoch - when) > QC_MAX_SKEW_S:
        return None
    z = (view or {}).get("base_zoom", BASE_ZOOM)
    x0, y0, x1, y1 = window(z, 256, lat, lon, view, g)
    world = (2 ** z) * 256.0; C = 2 * math.pi * 6378137.0
    mx0, mx1 = (x0 / world - 0.5) * C, (x1 / world - 0.5) * C
    my0, my1 = (0.5 - y1 / world) * C, (0.5 - y0 / world) * C       # EPSG:3857 metres
    q = {"service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": "conus_bref_qcd",
         "styles": "", "crs": "EPSG:3857", "bbox": "%f,%f,%f,%f" % (mx0, my0, mx1, my1),
         "width": ow, "height": oh, "format": "image/png", "transparent": "true", "time": t_iso}
    import urllib.parse
    png = urllib.request.urlopen(urllib.request.Request(QC_WMS + "?" + urllib.parse.urlencode(q), headers=QC_UA), timeout=40).read()
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3] > 0
    # metres per displayed pixel, in ground terms at this latitude
    m_per_px = (mx1 - mx0) / ow * math.cos(math.radians(lat))
    r = max(1, int(round(QC_DILATE_KM * 1000.0 / m_per_px)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    return cv2.dilate(a.astype(np.uint8), k) > 0

def apply_mask(layer, mask):
    import numpy as np
    a = np.asarray(layer.convert("RGBA")).copy()
    a[..., 3][~mask] = 0
    return Image.fromarray(a, "RGBA")

def is_watermark(img):
    c = img.getcolors(maxcolors=1 << 20) or []
    d = {col: n for n, col in c}
    return d.get((0, 0, 0, 140), 0) > 2000 and d.get((255, 255, 255, 200), 0) > 200

def decorate(img, view=None, g=None):
    view = view or {}; g = g or DEFAULT_GEOM
    if view.get("wide"):
        return img          # rings and a crosshair mean nothing on a regional view
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = img.width/2, img.height/2
    # The oversized canvas also covers the orbit bleed, so its half-height is MORE
    # than the radius. Scaling rings from img.height alone drew the "50 mi" ring
    # ~4% too big (about 52 mi).
    radius = view.get("radius_mi", RADIUS_MI)
    bleed_frac = 2.0 * (g.orbit + 2) / float(g.view_h)
    px_per_mi = (img.height / 2.0) / (radius * (1 + bleed_frac))
    for mi in (25, 50):
        r = mi*px_per_mi
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(125, 145, 165, RING_ALPHA), width=2)
    d.line([cx-8, cy, cx+8, cy], fill=(235, 240, 248, CROSS_ALPHA), width=2)
    d.line([cx, cy-8, cx, cy+8], fill=(235, 240, 248, CROSS_ALPHA), width=2)
    # decorate() runs on the OVERSIZED canvas, which the orbit then crops by up to
    # ORBIT_PX on every side -- so keep the label 2*ORBIT_PX in from the edges or
    # some orbit positions cut it off ("50 m").
    # ...and clear of the rounded top-right corner of the glass.
    if g.round:                                   # round glass: top centre is the only safe spot
        d.text((cx, 2*g.orbit + 6), "50 mi", font=fnt(11), fill=(120, 134, 148, 150), anchor="ma")
        return img
    inset = 22 if g.r else 8
    d.text((img.width - 2*g.orbit - inset, 2*g.orbit + (24 if g.r else 8)), "50 mi", font=fnt(13), fill=(120, 134, 148, 130), anchor="ra")
    return img

_fc = {}
def fnt(sz, bold=False):
    k = (sz, bold)
    if k not in _fc:
        p = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
        try: _fc[k] = ImageFont.truetype(p, sz)
        except Exception: _fc[k] = ImageFont.load_default()
    return _fc[k]

def status_strip(temp, hum, stamp, city=None, g=None):
    g = g or DEFAULT_GEOM
    W, SH, I = g.w, g.status_h, g.side_inset
    k = SH / float(STATUS_H)                       # font scale (1.0 on the default panel)
    f = lambda sz, bold=False: fnt(max(10, int(round(sz * k))), bold)
    img = Image.new("RGB", (W, SH), (0, 0, 0)); d = ImageDraw.Draw(img)
    if g.round:
        return _status_round(img, d, temp, hum, stamp, city, g)
    d.line([0, 0, W, 0], fill=(40, 46, 54), width=1)
    y = SH//2 + 1
    d.text((I, y), temp, font=f(36, True), fill=(255, 255, 255), anchor="lm")
    w = d.textlength(temp, font=f(36, True))
    d.text((I+w+5, y+3), "°F", font=f(20), fill=(145, 156, 168), anchor="lm")
    d.text((W-I, y+3), "%", font=f(20), fill=(145, 156, 168), anchor="rm")
    pw = d.textlength("%", font=f(20))
    d.text((W-I-pw-5, y), hum, font=f(36, True), fill=(255, 255, 255), anchor="rm")
    if city:
        d.text((W//2, y - int(9*k)), city, font=f(17, True), fill=(196, 204, 214), anchor="mm")
        d.text((W//2, y + int(11*k)), stamp, font=f(13), fill=(115, 128, 142), anchor="mm")
    else:
        d.text((W//2, y), stamp, font=f(17), fill=(115, 128, 142), anchor="mm")
    return img

def chord(g, y):
    """Half-width of the round glass at panel row y."""
    dy = abs(y - g.h / 2.0)
    return math.sqrt(max(0.0, g.r * g.r - dy * dy))

def _status_round(img, d, temp, hum, stamp, city, g):
    """The strip at the bottom of a round panel: reading on one line, city and time
    under it, both centred and shrunk until they fit the circle's chord."""
    W, SH, top = g.w, g.status_h, g.h - g.status_h
    def fit(text, y, sz, bold, room_y):
        room = 2 * chord(g, top + room_y) - 8
        while sz > 9 and d.textlength(text, font=fnt(sz, bold)) > room: sz -= 1
        return fnt(sz, bold)
    line1 = "%s\u00b0  %s%%" % (temp, hum)
    d.text((W // 2, 15), line1, font=fit(line1, 15, 20, True, 26), fill=(255, 255, 255), anchor="mm")
    line2 = "%s \u00b7 %s" % (city, stamp) if city else stamp
    d.text((W // 2, 36), line2, font=fit(line2, 36, 12, False, 44), fill=(150, 160, 172), anchor="mm")
    return img

def place_pixels(places, lat, lon, ow, oh, view=None, g=None):
    """Pixel position of each town on the OVERSIZED frame (ow x oh), which covers
    exactly window() at any zoom. Towns the orbit could push off-screen are dropped."""
    g = g or DEFAULT_GEOM
    z = (view or {}).get("base_zoom", BASE_ZOOM)
    x0, y0, x1, y1 = window(z, 256, lat, lon, view, g)
    out = []
    for place in places:
        name, pla, plo = place[0], place[1], place[2]
        px, py = deg2px(pla, plo, z, 256)
        x = (px - x0) / (x1 - x0) * ow; y = (py - y0) / (y1 - y0) * oh
        m = 2 * g.orbit + 4
        if m < x < ow - m and m < y < oh - m:
            out.append((name, x, y, bool(place[3]) if len(place) > 3 else False))
    return out

def draw_places(frame, pts, crosshair=True, g=None):
    """Town markers on TOP of the radar, so they stay readable in the storms where
    the context matters. Drawn before the orbit crop, so they drift with it.

    Each label goes right of its dot unless that would hit the centre crosshair,
    another label, or the edge -- then it flips left. (Canton's "Ann Arbor" ran
    straight into the crosshair before this.)"""
    g = g or DEFAULT_GEOM
    d = ImageDraw.Draw(frame, "RGBA")
    f = fnt(14)
    cx, cy = frame.width / 2.0, frame.height / 2.0
    keep_out = [(cx - 14, cy - 14, cx + 14, cy + 14)] if crosshair else []
    keep_out += [(x - 5, y - 5, x + 5, y + 5) for _, x, y, _ in pts]   # every dot
    edge = 2 * g.orbit + 4
    CR = g.r
    # The radar view is the panel's TOP 424 px, so only its two top corners are
    # rounded (the strip below takes the bottom ones). Frame coords include the
    # orbit margin, so the visible panel starts at ORBIT_PX.
    def in_corner(px, py):
        vx, vy = px - g.orbit, py - g.orbit
        if g.round:
            return (vx - g.w / 2.0) ** 2 + (vy - g.h / 2.0) ** 2 > (g.r - 4) ** 2
        if not CR or vy >= CR: return False
        if vx < CR:          cx = CR
        elif vx > g.w - CR: cx = g.w - CR
        else: return False
        return (vx - cx) ** 2 + (vy - CR) ** 2 > (CR - 4) ** 2
    def hard(box):      # off the panel or inside a rounded corner: never acceptable
        if box[0] < edge or box[2] > frame.width - edge: return True
        return any(in_corner(x, y) for x in (box[0], box[2]) for y in (box[1], box[3]))
    def soft(box):      # overlaps the crosshair, a dot or another label
        return any(not (box[2] < k[0] or box[0] > k[2] or box[3] < k[1] or box[1] > k[3]) for k in keep_out)
    for name, x, y, mine in pts:
        w = d.textlength(name, font=f) + 4                    # + the 2 px outline each side
        right = ((x + 8, y - 9, x + 8 + w, y + 9), "lm", x + 8)
        left  = ((x - 8 - w, y - 9, x - 8, y + 9), "rm", x - 8)
        choice = next((o for o in (right, left) if not hard(o[0]) and not soft(o[0])), None)
        if choice is None and mine:                           # your cities always get a label
            choice = next((o for o in (right, left) if not hard(o[0])), None)
        if choice is None:
            continue                                          # a reference town that won't fit: drop it
        box, anchor, tx = choice
        keep_out.append(box)
        # your own cities (wide view) get an amber marker so they stand out
        dot = (240, 172, 30, 240) if mine else (225, 230, 238, 230)
        r = 4.5 if mine else 3.5
        d.ellipse([x - r, y - r, x + r, y + r], fill=dot, outline=(0, 0, 0, 255), width=1)
        d.text((tx, y), name, font=f, fill=(214, 220, 228, 225), anchor=anchor,
               stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return frame

def progress_bar(frame, frac, left, right, ox=0, oy=0, g=None):
    """Loop progress along the bottom of the radar view: start time on the left,
    latest time on the right, a fill and playhead for where this frame sits.

    Burned into each frame server-side -- every frame knows its own position, so
    the device needs no code for it. Kept dim (amber at reduced strength, grey
    labels) and nudged by the orbit offset, because the track is static chrome
    and static chrome is the burn-in risk on this panel."""
    W, H = frame.size
    d = ImageDraw.Draw(frame, "RGBA")
    # soft dark band so the labels stay legible over heavy returns
    base = H - 13
    if g is not None and g.round:                  # no lower than where the circle is ~190 px wide
        base = min(base, int(g.h / 2 + math.sqrt(max(0, g.r * g.r - 96 * 96))))
    band = 34
    for k in range(band):
        a = int(170 * (k / float(band)) ** 1.6)
        yy = base + 13 - band + k
        d.line([0, yy, W, yy], fill=(0, 0, 0, a if yy < base + 13 else 170))
    if base + 13 < H:                              # round, no strip: keep it dark below the bar too
        d.rectangle([0, base + 13, W, H], fill=(0, 0, 0, 170))
    jx, jy = ox // 2, oy // 2
    y = base + jy
    f = fnt(13)
    m = 12
    if g is not None and g.round:                  # labels inside the circle at this row
        f = fnt(11)
        left, right = (t.replace(" AM", "").replace(" PM", "") for t in (left, right))
        m = int(W / 2 - chord(g, y) + 8)
    d.text((m + jx, y), left, font=f, fill=(150, 160, 172, 235), anchor="lm",
           stroke_width=2, stroke_fill=(0, 0, 0, 255))
    d.text((W - m + jx, y), right, font=f, fill=(150, 160, 172, 235), anchor="rm",
           stroke_width=2, stroke_fill=(0, 0, 0, 255))
    x0 = m + jx + d.textlength(left, font=f) + 14     # clear of the playhead dot
    x1 = W - m + jx - d.textlength(right, font=f) - 14
    d.rounded_rectangle([x0, y - 1.5, x1, y + 1.5], radius=1.5, fill=(58, 65, 75, 255))
    xf = x0 + (x1 - x0) * max(0.0, min(1.0, frac))
    d.rounded_rectangle([x0, y - 1.5, xf, y + 1.5], radius=1.5, fill=(214, 150, 20, 255))
    d.ellipse([xf - 4, y - 4, xf + 4, y + 4], fill=(240, 172, 30, 255))
    return frame

# Panned views (the device's swipe): a pill in the top-left of every frame says how far
# the view is off its city, and doubles as the device's "tap to recentre" target
# (firmware: a tap in the top PAN_TAP_H px while panned).
PAN_TAP_H = 80

def offset_pill(frame, text, g=None, close=True):
    """Burn "50 mi W  x" into a frame (the radar view, after the orbit crop). Without
    close: just the label (the Active view's "Storm 390 mi SW")."""
    g = g or DEFAULT_GEOM
    d = ImageDraw.Draw(frame, "RGBA")
    f = fnt(13 if g.round else 16, True)
    label = "%s   \u2715" % text if close else text
    w = d.textlength(label, font=f)
    ph = 24 if g.round else 30
    if g.round: x0, y0 = int(g.w / 2 - (w + 24) / 2), 24      # top centre, inside the circle
    else: x0, y0 = max(12, int(g.r * 0.6)), max(10, int(g.r * 0.35))
    d.rounded_rectangle([x0, y0, x0 + w + 24, y0 + ph], radius=ph // 2, fill=(0, 0, 0, 175), outline=(255, 183, 3, 200), width=2)
    d.text((x0 + 12, y0 + ph // 2), label, font=f, fill=(255, 205, 90, 255), anchor="lm")
    return frame

def message_pill(text, g=None):
    """A standalone pill ("Rendering 50 mi W...") the device draws over a dimmed frame.
    Width a multiple of 4 and even height (the CO5300 wants even windows)."""
    g = g or DEFAULT_GEOM
    f = fnt(18, True)
    w = int(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(text, font=f)) + 44
    w = min(g.w - 16, w + (-w) % 4)
    img = Image.new("RGB", (w, 44), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, 43], radius=21, fill=(16, 20, 24), outline=(255, 183, 3), width=2)
    d.text((w // 2, 22), text, font=f, fill=(231, 235, 240), anchor="mm")
    return img

# ---------------------------------------------------------------------------
# On-device UI, rendered here so the firmware needs no fonts. The device draws
# these JPEGs over a dimmed radar frame and animates only the bars itself.
# Geometry is shared with the firmware (see ui.c): keep it in sync.
# ---------------------------------------------------------------------------
PICKER_W     = 392                     # drawn at x = (480 - 392) / 2 = 44
PICKER_ROW_H = 52
HOLD_W, HOLD_H = 240, 40               # drawn at x = 120, y = 380 (below the picker, above the strip)

def picker_panel(entries, hl, cur, g=None):
    """entries: [(id, name, temp)]. Height is a multiple of 4 so the device can
    centre it on even coordinates (the CO5300 wants even windows). The bottom 22 px
    are left for the countdown bar the device animates."""
    g = g or DEFAULT_GEOM
    n = len(entries)
    PW = min(PICKER_W, g.w - 32) & ~3
    RH, GAP = PICKER_ROW_H, 8
    TOP, FOOT, small = 54, 30, g.w < 320
    if small:                                            # 240x240: compact, inside the circle
        PW, RH, GAP, TOP, FOOT = int(g.w * 0.74) & ~3, 26, 3, 30, 16
    elif TOP + n * (RH + GAP) + FOOT > g.view_h - 8:     # short view (480x320): tighter rows
        RH, GAP = 38, 4
    H = TOP + n * (RH + GAP) + FOOT
    H += (-H) % 4
    img = Image.new("RGB", (PW, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, PW - 1, H - 1], radius=11, outline=(58, 65, 75), width=2)
    d.text((PW // 2, 17) if small else (20, 30), "SHOW RADAR FOR", font=fnt(11 if small else 15, True),
           fill=(138, 149, 163), anchor="mm" if small else "lm")
    y = 26 if small else 50
    nf, tf, inx = (fnt(15, True), fnt(13), 34) if small else (None, None, 52)
    for i, (cid, name, temp) in enumerate(entries):
        on = i == hl
        d.rounded_rectangle([16, y, PW - 17, y + RH], radius=8,
                            fill=(255, 183, 3) if on else (17, 19, 23))
        ink = (26, 18, 0) if on else (231, 235, 240)
        if cid == cur:
            ex = 20 if small else 30
            d.ellipse([ex, y + RH / 2 - 4, ex + 8, y + RH / 2 + 4], fill=(26, 18, 0) if on else (154, 164, 177))
        d.text((inx, y + RH / 2), name, font=nf or fnt(25 if RH >= 48 else 21, True), fill=ink, anchor="lm")
        if temp not in (None, "", "--"):
            d.text((PW - (24 if small else 34), y + RH / 2), "%s\u00b0" % temp, font=tf or fnt(21 if RH >= 48 else 18), fill=(61, 44, 0) if on else (154, 164, 177), anchor="rm")
        y += RH + GAP
    if not small:
        d.text((20, H - 26), "KEY or tap: next   \u00b7   waits 3 s, then shows it", font=fnt(13), fill=(125, 135, 148), anchor="lm")
    return img

def hold_pill():
    img = Image.new("RGB", (HOLD_W, HOLD_H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, HOLD_W - 1, HOLD_H - 1], radius=10, outline=(58, 65, 75), width=2)
    d.text((HOLD_W // 2, 15), "Hold to turn off", font=fnt(15, True), fill=(231, 235, 240), anchor="mm")
    return img                              # device draws the fill bar at y+28..y+32

_station_cache = {}

def obs_reading(lat, lon):
    """Latest REAL observation from the nearest NWS station (free, no key; NWS asks
    for a contact in the User-Agent). Falls back to Open-Meteo's model value if the
    station is down or reports null, which NWS stations occasionally do."""
    hdr = {"User-Agent": "snadboy-homelab-radar/1.0 (dschless@gmail.com)",
           "Accept": "application/geo+json"}
    def get(u, h=hdr):
        return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=15).read())
    try:
        key = (round(lat, 3), round(lon, 3))
        if key not in _station_cache:
            pt = get("https://api.weather.gov/points/%.4f,%.4f" % (lat, lon))
            _station_cache[key] = get(pt["properties"]["observationStations"])["features"][0]["properties"]["stationIdentifier"]
        ob = get("https://api.weather.gov/stations/%s/observations/latest" % _station_cache[key])["properties"]
        tc, rh = ob["temperature"]["value"], ob["relativeHumidity"]["value"]
        if tc is not None and rh is not None:
            return str(int(round(tc * 9 / 5 + 32))), str(int(round(rh)))
    except Exception as e:
        print("    NWS %.3f,%.3f unavailable (%s)" % (lat, lon, str(e)[:40]))
    try:
        cur = get("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                  "&current=temperature_2m,relative_humidity_2m&temperature_unit=fahrenheit"
                  % (lat, lon), {"User-Agent": UA})["current"]
        return str(int(round(cur["temperature_2m"]))), str(int(round(cur["relative_humidity_2m"])))
    except Exception as e:
        print("    Open-Meteo %.3f,%.3f unavailable (%s)" % (lat, lon, str(e)[:40]))
        return "--", "--"

def ha_reading(temp_eid="sensor.outdoor_temperature", hum_eid="sensor.outdoor_humidity"):
    """Live temp/humidity from Home Assistant entities (a place's "temp" setting).
    At home that's the dedicated outdoor sensors: the TSR and garage FP300s read ~10F
    warm because they sit in sheltered spaces."""
    from ..ha import ha_entity
    def one(eid):
        try: return str(int(round(float(ha_entity(eid)[0]))))
        except (TypeError, ValueError): return "--"
    return one(temp_eid), one(hum_eid)

def main():
    out = os.environ.get("RADAR_OUT", "./out"); os.makedirs(out, exist_ok=True)
    print("  radar  z=%d tile=%d -> %.0f m/px native" % (RADAR_ZOOM, RADAR_TILE, mpp(LAT, RADAR_ZOOM, RADAR_TILE)))
    print("  base   z=%d tile=256 -> %.0f m/px native" % (BASE_ZOOM, mpp(LAT, BASE_ZOOM, 256)))
    print("  panel  %dx%d radar + %dpx status = %.0f m/px displayed"
          % (PANEL, VIEW_H, STATUS_H, RADIUS_MI*2*1609.344/VIEW_H))
    bm = darken_for_amoled(basemap())
    print("  basemap mosaic %s%s" % (bm.size, "  (CACHED)" if os.path.exists(os.path.join(CACHE, "base_z%d.png" % BASE_ZOOM)) else ""))
    OW, OH = PANEL + 2*ORBIT_PX, VIEW_H + 2*ORBIT_PX
    base = decorate(bm.resize((OW, OH), Image.LANCZOS)).convert("RGB")

    maps = json.loads(fetch("https://api.rainviewer.com/public/weather-maps.json"))
    host, frames = maps["host"], maps["radar"]["past"] + maps["radar"].get("nowcast", [])
    n = int(os.environ.get("RADAR_FRAMES", "4"))
    temp, hum = ha_reading()
    # one orbit position per refresh cycle -- constant across the loop so the
    # animation does not judder, advancing every 10 min so no static edge
    # (rings, crosshair, coastlines) occupies one pixel for more than that.
    ox, oy = orbit_offset(int(time.time() // 600))
    print("  HA outdoor: %s F / %s %%" % (temp, hum))
    for i, f in enumerate(frames[-n:]):
        rl = radar(host, f["path"])
        if is_watermark(rl):
            print("    frame%02d  WATERMARK -- zoom %d rejected by RainViewer" % (i, RADAR_ZOOM)); continue
        rl = rl.resize((OW, OH), Image.LANCZOS)
        live = sum(1 for p in rl.getdata() if p[3] > 0)
        frame = base.copy(); frame.paste(rl, (0, 0), rl)
        frame = frame.crop((ORBIT_PX + ox, ORBIT_PX + oy,
                            ORBIT_PX + ox + PANEL, ORBIT_PX + oy + VIEW_H))
        full = Image.new("RGB", (PANEL, PANEL), (0, 0, 0)); full.paste(frame, (0, 0))
        strip = status_strip(temp, hum, time.strftime("%-I:%M %p", time.localtime(f["time"])))
        full.paste(strip, (ox // 2, VIEW_H))
        p = os.path.join(out, "frame%02d.jpg" % i)
        full.save(p, "JPEG", quality=86, optimize=True)
        print("    frame%02d  %s  %6d radar px  %d bytes"
              % (i, time.strftime("%-I:%M %p", time.localtime(f["time"])), live, os.path.getsize(p)))

if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Smoothing: in-between frames
#
# RainViewer frames are 10 minutes apart, so a storm at 30 mph jumps ~5 miles
# (~20 px here) per frame -- a cut, not motion. The device cannot blend frames
# itself (it can never hold even one full frame), so tweens are rendered here
# and the firmware just plays more frames.
#
# Interpolate the RADAR LAYER ONLY, then composite over the static basemap.
# Interpolating finished frames would make the optical flow warp coastlines.
#
# Work in PREMULTIPLIED alpha: transparent radar pixels carry junk RGB (e.g.
# rgba(71,112,76,0)), and blending straight RGBA drags that into dark fringes.
# ---------------------------------------------------------------------------

def _premul(img):
    import numpy as np
    a = np.asarray(img.convert("RGBA"), dtype=np.float32).copy()
    a[..., :3] *= a[..., 3:4] / 255.0
    return a

def _unpremul(arr):
    import numpy as np
    a = arr[..., 3:4] / 255.0
    rgb = np.where(a > 1e-3, arr[..., :3] / np.maximum(a, 1e-3), 0.0)
    out = np.concatenate([rgb, arr[..., 3:4]], axis=2)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGBA")

def _flow(a_pm, b_pm):
    """Dense motion from a to b, computed on premultiplied luminance so the
    texture inside heavy cells (not just the echo outline) drives the flow."""
    import cv2, numpy as np
    def key(pm):
        l = 0.30 * pm[..., 0] + 0.59 * pm[..., 1] + 0.11 * pm[..., 2]
        l = 0.5 * l + 0.5 * pm[..., 3]          # outline matters too
        return cv2.GaussianBlur(np.clip(l, 0, 255).astype(np.uint8), (0, 0), 1.5)
    f = cv2.calcOpticalFlowFarneback(key(a_pm), key(b_pm), None,
                                     0.5, 5, 25, 5, 7, 1.5, 0)
    # radar fields move coherently; smooth the field so cells translate as a
    # whole instead of shearing at their edges
    return cv2.GaussianBlur(f, (0, 0), 6)

def tweens(a, b, n, mode="motion"):
    """n in-between RGBA layers from a to b. mode: "motion" (optical flow) or
    "blend" (plain crossfade, kept for comparison)."""
    import cv2, numpy as np
    if n <= 0:
        return []
    A, B = _premul(a), _premul(b)
    ts = [(k + 1) / (n + 1.0) for k in range(n)]
    if mode == "blend":
        return [_unpremul((1 - t) * A + t * B) for t in ts]
    F01, F10 = _flow(A, B), _flow(B, A)
    h, w = A.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    out = []
    for t in ts:
        Wa = cv2.remap(A, gx - t * F01[..., 0], gy - t * F01[..., 1],
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        Wb = cv2.remap(B, gx - (1 - t) * F10[..., 0], gy - (1 - t) * F10[..., 1],
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        out.append(_unpremul((1 - t) * Wa + t * Wb))
    return out

def tween_count(gap_s, per_10min):
    """In-betweens for one gap, scaled by real time so a skipped RainViewer
    frame (a 20-minute gap) plays at the same speed as a normal one."""
    steps = max(1, int(round(gap_s / 600.0))) * (per_10min + 1)
    return steps - 1


# ---------------------------------------------------------------------------
# Loop format for the device ("RDL1")
#
# JPEG decode on the C6 takes ~240 ms per frame and the panel shows each frame
# being painted as a visible downward wipe. This format lets the device put a
# whole frame up in a few tens of ms: the bare map is sent ONCE as raw pixels,
# and each frame is just "which pixels differ from the map", as a zlib-compressed
# 8-bit layer (0 = keep the map pixel, 1..255 = palette colour). Unchanged pixels
# are copied straight from flash; nothing is decoded except the small layer.
#
#   "RDL1" | u16 version=1 | u16 nframes | u16 w | u16 h | u16 ncolours | u16 0
#   nframes x { u32 offset, u32 length, u8 is_real_frame, 3 x pad }   (offsets from blob start)
#   256 x u16 palette (RGB565 big-endian; entry 0 unused)
#   w*h x u16 map pixels (RGB565 big-endian)
#   nframes x zlib(w*h index bytes)
# ---------------------------------------------------------------------------
import struct as _struct, zlib as _zlib

def _rgb565_be(a):
    import numpy as np
    v = ((a[..., 0].astype(np.uint16) >> 3) << 11) | ((a[..., 1].astype(np.uint16) >> 2) << 5) | (a[..., 2].astype(np.uint16) >> 3)
    return v.astype(">u2")

def encode_loop(base_img, frames, keys):
    import numpy as np
    base = np.asarray(base_img.convert("RGB"))
    h, w = base.shape[:2]
    b565 = _rgb565_be(base)
    arrs = [np.asarray(f.convert("RGB")) for f in frames]
    masks = [_rgb565_be(a) != b565 for a in arrs]
    changed = [a[m] for a, m in zip(arrs, masks) if m.any()]
    pal_img = Image.new("P", (1, 1))
    ncol = 0
    if changed:
        allpx = np.concatenate(changed).reshape(-1, 1, 3)
        q = Image.fromarray(allpx.astype(np.uint8), "RGB").quantize(255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        pal = q.getpalette()[:255 * 3]
        ncol = len(pal) // 3
        pal_img.putpalette(pal + [0] * (768 - len(pal)))
        pal_rgb = np.array(pal, dtype=np.uint8).reshape(-1, 3)
    else:
        pal_rgb = np.zeros((0, 3), np.uint8)
    palette = np.zeros(256, dtype=">u2")
    if ncol:
        palette[1:ncol + 1] = _rgb565_be(pal_rgb.reshape(1, -1, 3))[0]
    payloads = []
    for a, m in zip(arrs, masks):
        idx = np.zeros((h, w), np.uint8)
        if m.any():
            qi = np.asarray(Image.fromarray(a, "RGB").quantize(palette=pal_img, dither=Image.Dither.NONE))
            idx[m] = np.minimum(qi[m], ncol - 1).astype(np.uint8) + 1
        payloads.append(_zlib.compress(idx.tobytes(), 9))
    n = len(frames)
    head = 16 + n * 12
    off = head + 512 + w * h * 2
    table = b""
    keyset = set(keys)
    for i, pl in enumerate(payloads):
        table += _struct.pack("<IIB3x", off, len(pl), 1 if i in keyset else 0)
        off += len(pl)
    hdr = _struct.pack("<4sHHHHHH", b"RDL1", 1, n, w, h, ncol, 0)
    return hdr + table + palette.tobytes() + b565.tobytes() + b"".join(payloads)
