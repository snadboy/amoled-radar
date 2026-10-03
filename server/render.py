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

LAT  = float(os.environ.get('RADAR_LAT', '41.90'))
LON  = float(os.environ.get('RADAR_LON', '-88.32'))
RADIUS_MI   = 50.0
PANEL       = 480
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

def window(z, tile):
    """Pixel box at (z, tile) for the same geographic window: the full 50-mile
    radius fits the SHORT axis, so the radius is visible in every direction."""
    span_m = RADIUS_MI * 2 * 1609.344
    m      = mpp(LAT, z, tile)
    half_h = span_m / m / 2.0
    half_w = half_h * (PANEL / VIEW_H)
    bleed  = (ORBIT_PX + 2) / float(VIEW_H) * (half_h * 2)   # keep the orbit in-bounds
    half_h += bleed; half_w += bleed
    cx, cy = deg2px(LAT, LON, z, tile)
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h

def fetch(url, timeout=25):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout).read()

def mosaic(url_for, z, tile, cache_key=None):
    if cache_key:
        p = os.path.join(CACHE, cache_key)
        if os.path.exists(p):
            return Image.open(p).convert("RGBA")
    x0, y0, x1, y1 = window(z, tile)
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

def orbit_offset(seq):
    """Walk a slow Lissajous-ish path so no static edge sits on one pixel."""
    import math as _m
    a = 2.0 * _m.pi * (seq % ORBIT_STEPS) / ORBIT_STEPS
    return int(round(ORBIT_PX * _m.sin(a))), int(round(ORBIT_PX * _m.sin(2 * a) / 2.0))

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

def basemap():
    url = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
           "Canvas/World_Dark_Gray_Base/MapServer/tile/%d/%d/%d")   # NOTE: z/y/x
    return mosaic(lambda x, y: url % (BASE_ZOOM, y, x), BASE_ZOOM, 256,
                  cache_key="base_z%d.png" % BASE_ZOOM)

def radar(host, path):
    url = "%s%s/%d/%d/%%d/%%d/%d/1_1.png" % (host, path, RADAR_TILE, RADAR_ZOOM, PALETTE)
    return mosaic(lambda x, y: url % (x, y), RADAR_ZOOM, RADAR_TILE)

def is_watermark(img):
    c = img.getcolors(maxcolors=1 << 20) or []
    d = {col: n for n, col in c}
    return d.get((0, 0, 0, 140), 0) > 2000 and d.get((255, 255, 255, 200), 0) > 200

def decorate(img):
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = img.width/2, img.height/2
    px_per_mi = (img.height/2) / RADIUS_MI
    for mi in (25, 50):
        r = mi*px_per_mi
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(125, 145, 165, RING_ALPHA), width=2)
    d.line([cx-8, cy, cx+8, cy], fill=(235, 240, 248, CROSS_ALPHA), width=2)
    d.line([cx, cy-8, cx, cy+8], fill=(235, 240, 248, CROSS_ALPHA), width=2)
    d.text((img.width-6, 4), "50 mi", font=fnt(13), fill=(120, 134, 148, 130), anchor="ra")
    return img

_fc = {}
def fnt(sz, bold=False):
    k = (sz, bold)
    if k not in _fc:
        p = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
        try: _fc[k] = ImageFont.truetype(p, sz)
        except Exception: _fc[k] = ImageFont.load_default()
    return _fc[k]

def status_strip(temp, hum, stamp):
    img = Image.new("RGB", (PANEL, STATUS_H), (0, 0, 0)); d = ImageDraw.Draw(img)
    d.line([0, 0, PANEL, 0], fill=(40, 46, 54), width=1)
    y = STATUS_H//2 + 1
    d.text((16, y), temp, font=fnt(36, True), fill=(255, 255, 255), anchor="lm")
    w = d.textlength(temp, font=fnt(36, True))
    d.text((16+w+5, y+3), "°F", font=fnt(20), fill=(145, 156, 168), anchor="lm")
    d.text((PANEL-16, y+3), "%", font=fnt(20), fill=(145, 156, 168), anchor="rm")
    pw = d.textlength("%", font=fnt(20))
    d.text((PANEL-16-pw-5, y), hum, font=fnt(36, True), fill=(255, 255, 255), anchor="rm")
    d.text((PANEL//2, y), stamp, font=fnt(17), fill=(115, 128, 142), anchor="mm")
    return img

def ha_reading():
    """Live outdoor temp/humidity from Home Assistant. The TSR and garage FP300s
    read ~10F warm because they sit in sheltered spaces, so use the dedicated
    outdoor sensors."""
    base = os.environ.get("HASS_SERVER", "").rstrip("/")
    tok  = os.environ.get("HASS_TOKEN", "")
    if not (base and tok):
        return os.environ.get("RADAR_TEMP", "--"), os.environ.get("RADAR_HUM", "--")
    def one(eid, default):
        try:
            r = urllib.request.Request(base + "/api/states/" + eid,
                                       headers={"Authorization": "Bearer " + tok})
            st = json.loads(urllib.request.urlopen(r, timeout=10).read())["state"]
            return str(int(round(float(st))))
        except Exception as e:
            print("    HA %s unavailable (%s)" % (eid, str(e)[:40])); return default
    return (one(os.environ.get("RADAR_TEMP_ENTITY", "sensor.outdoor_temperature"), "--"),
            one(os.environ.get("RADAR_HUM_ENTITY",  "sensor.outdoor_humidity"),    "--"))

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
