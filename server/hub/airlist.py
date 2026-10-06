"""Aircraft listing app: the flights nearest a location, two lines each, as a picture.

Same data as the Aircraft radar app -- its OpenSky poller for the view (so a device
watching either spends the same 1 credit per poll) and its cached type / route lookups
-- drawn by the hub as a JPEG the device just puts on screen:

  UAL1658  → LAX  Los Angeles                     12 mi NE
  B738 · 35,000 ft ↑ · 452 kt                     ORD → LAX

Endpoints
  /airlist/<view>/list.jpg?w=&h=&r=&panel=&mi=50&page=0   the list (page wraps)
"""
import io, math, time

from PIL import Image, ImageDraw

from . import aircraft, settings
from .aircraft import basemap, lookup, opensky

ID = "airlist"
MAX_MI = 100
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
DIM, INK, SUB, AMBER = (120, 130, 142), (236, 240, 244), (150, 160, 172), (255, 183, 3)
LINE2 = (196, 204, 214)                  # second line: brighter than SUB (was too dim to read)

def view_summary(view, radius_mi=50):
    return {"id": view["id"], "name": view["name"], "lat": view["lat"], "lon": view["lon"], "radius_mi": radius_mi}

def start(): pass

def health():
    return True, {}

def _bearing(lat0, lon0, lat, lon):
    x = math.radians(lon - lon0) * math.cos(math.radians((lat + lat0) / 2))
    return (math.degrees(math.atan2(x, math.radians(lat - lat0))) + 360) % 360

def _alt(a):
    if a["on_ground"]: return "on ground"
    if a["alt_m"] is None: return "--"
    ft = int(round(a["alt_m"] * 3.28084 / 100.0)) * 100
    return "FL%03d" % (ft // 100) if ft >= 18000 else "{:,} ft".format(ft)

def rows(view, radius_mi):
    """[(aircraft, miles, bearing, info)] within radius_mi of the view, nearest first."""
    snap = aircraft._poller(view).snapshot()
    out = []
    for a in snap["aircraft"]:
        mi = opensky.miles_between(view["lat"], view["lon"], a["lat"], a["lon"])
        if mi <= radius_mi:
            info = lookup.lookup(a["icao"], a["callsign"], a["lat"], a["lon"], fetch=False) or {}
            out.append((a, mi, _bearing(view["lat"], view["lon"], a["lat"], a["lon"]), info))
    out.sort(key=lambda r: r[1])
    return out, snap

def _fit(d, text, font, room):
    """text cut with an ellipsis to fit room px."""
    if d.textlength(text, font=font) <= room: return text
    while text and d.textlength(text + "…", font=font) > room: text = text[:-1]
    return text + "…"

def render(view, w, h, r, radius_mi=50, page=0):
    lst, snap = rows(view, radius_mi)
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    s = h / 480.0 if h >= w else w / 480.0                 # scale from the 480 px design
    f_cs, f_dest, f_sub, f_hd = (basemap.font(max(12, int(sz * min(1.0, s))), b)
                                 for sz, b in ((21, True), (17, False), (17, False), (14, True)))
    # AMOLED: drift the whole page by a few px over time (static text is the burn-in risk)
    t = int(time.time() // 120) % 4
    ox, oy = (0, 2, 2, 0)[t], (0, 0, 2, 2)[t]
    side = max(14, int(r * 0.45))                          # clear of the rounded corners
    top, row_h = max(14, int(r * 0.5)) + 26, 58
    per = max(1, (h - top - 12) // row_h)
    pages = max(1, (len(lst) + per - 1) // per)
    page %= pages
    status = opensky.ST_NAMES.get(snap["status"], "?")
    head = "%d within %d mi of %s" % (len(lst), radius_mi, view["name"])
    if pages > 1: head += "  ·  %d/%d" % (page + 1, pages)
    if snap["status"] not in (opensky.ST_OK, opensky.ST_IDLE): head += "  ·  " + status
    d.text((w // 2 + ox, top - 22 + oy), _fit(d, head, f_hd, w - 2 * side), font=f_hd, fill=DIM, anchor="mm")
    if not lst:
        msg = "Waiting for aircraft…" if snap["status"] == opensky.ST_IDLE else "No aircraft in range"
        d.text((w // 2, h // 2), msg, font=f_dest, fill=SUB, anchor="mm")
    y = top
    for a, mi, brg, info in lst[page * per:(page + 1) * per]:
        x0, x1 = side + ox, w - side + ox
        route = info.get("route") or {}
        dest = route.get("dest") or {}
        orig = route.get("origin") or {}
        # line 1: flight, destination ............ distance + direction
        cs = a["callsign"] or "%06x" % a["icao"]
        far = "%.0f mi %s" % (mi, DIRS[int((brg + 22.5) % 360 // 45)]) if mi >= 9.5 else "%.1f mi %s" % (mi, DIRS[int((brg + 22.5) % 360 // 45)])
        d.text((x0, y + oy), cs, font=f_cs, fill=INK, anchor="la")
        rw = d.textlength(far, font=f_dest)
        d.text((x1, y + 2 + oy), far, font=f_dest, fill=AMBER, anchor="ra")
        cx = x0 + d.textlength(cs, font=f_cs) + 12
        if dest.get("iata") or dest.get("city"):
            dt = "→ %s  %s" % (dest.get("iata", ""), dest.get("city", ""))
            d.text((cx, y + 3 + oy), _fit(d, dt.strip(), f_dest, x1 - rw - 12 - cx), font=f_dest, fill=SUB, anchor="la")
        # line 2: type · altitude (climbing/descending) · speed ........ origin → destination
        vr = a.get("vrate_ms")
        trend = " ↑" if vr and vr > 1.5 else " ↓" if vr and vr < -1.5 else ""
        sep = "  \u00b7  "
        rt = "%s \u2192 %s" % (orig.get("iata", "?"), dest.get("iata", "?")) if route else ""
        rtw = d.textlength(rt, font=f_sub) if rt else 0
        room = x1 - x0 - rtw - (12 if rt else 0)
        # altitude and speed always whole; the aircraft type takes what's left (or goes)
        tail = sep.join(p for p in (_alt(a) + trend,
                                    "%d kt" % round(a["speed_ms"] * 1.94384) if a.get("speed_ms") is not None else "") if p)
        typ_room = room - d.textlength(sep + tail, font=f_sub)
        typ = info.get("type", "")
        line2 = (_fit(d, typ, f_sub, typ_room) + sep + tail) if typ and typ_room >= 60 else tail
        if rt: d.text((x1, y + 27 + oy), rt, font=f_sub, fill=LINE2, anchor="ra")
        d.text((x0, y + 27 + oy), _fit(d, line2, f_sub, room), font=f_sub, fill=LINE2, anchor="la")
        d.line((x0, y + row_h - 8 + oy, x1, y + row_h - 8 + oy), fill=(32, 37, 43))
        y += row_h
    return img

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=85); return b.getvalue()

def preview_png(view, w, h, r, radius_mi=50):
    b = io.BytesIO(); render(view, w, h, r, radius_mi).save(b, "PNG"); return b.getvalue()

def handle(h, p, q):
    parts = p.split("/")                        # ['', 'airlist', <view>, 'list.jpg']
    if len(parts) != 4 or parts[1] != ID or parts[3] != "list.jpg":
        return False
    view = settings.air_view(parts[2])
    if view is None:
        return h.json({"error": "no such view"}, 404)
    try:
        w, hh, r = int(q.get("w", 480)), int(q.get("h", 480)), int(q.get("r", 56))
        mi = max(5, min(MAX_MI, int(q.get("mi", 50))))
        page = int(q.get("page", 0))
    except ValueError:
        return h.json({"error": "bad parameters"}, 400)
    if not (100 <= w <= 2048 and 100 <= hh <= 2048):
        return h.json({"error": "bad size"}, 400)
    aircraft._poller(view).touch()             # a device is watching: keep OpenSky polling
    return h.send(_jpeg(render(view, w, hh, r, mi, page)), "image/jpeg")
