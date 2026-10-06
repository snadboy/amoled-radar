"""Aircraft listing app: the flights nearest a location, two lines each, as a picture.

Same data as the Aircraft radar app -- its OpenSky poller for the view (so a device
watching either spends the same 1 credit per poll) and its cached type / route lookups
-- drawn by the hub as a JPEG the device just puts on screen:

  UAL1658  → LAX  Los Angeles                     12 mi NE
  B738 · 35,000 ft ↑ · 452 kt                     ORD → LAX

Endpoints
  /airlist/<view>/list.jpg?w=&h=&r=&mi=50&types=airline,private   one page, nearest first
  /airlist/<view>/detail.jpg?...same...&y=<tap y>   the list dimmed under a card about the
      flight drawn on that row of the last list.jpg (404: no flight there)

The device appends the "query" its hello gave it (radius, types...) as-is, so new
listing options need no firmware change.
"""
import io, math, threading, time

from PIL import Image, ImageDraw

from . import aircraft, settings
from .aircraft import basemap, lookup, opensky

ID = "airlist"
MAX_MI = 100
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
DIM, INK, SUB, AMBER = (120, 130, 142), (236, 240, 244), (150, 160, 172), (255, 183, 3)
LINE2 = (196, 204, 214)                  # second line: brighter than SUB (was too dim to read)

# The flights drawn on each row of the last page, per (view, size, radius, kinds): a tap on
# the device names a row by its y; the flight under it is the one that was drawn there.
_shown = {}
_shown_lock = threading.Lock()

def _key(view, w, h, radius_mi, kinds):
    return (view["id"], w, h, radius_mi, frozenset(kinds or ()))

def _layout(w, h, r):
    """(side, top, row_h, rows per page): shared by the list and the tap lookup."""
    side = max(14, int(r * 0.45))                          # clear of the rounded corners
    top, row_h = max(14, int(r * 0.5)) + 26, 58
    return side, top, row_h, max(1, (h - top - 12) // row_h)

def view_summary(view, radius_mi=50, types=lookup.KINDS):
    q = "mi=%d" % radius_mi
    if set(types) != set(lookup.KINDS): q += "&types=" + ",".join(types)
    return {"id": view["id"], "name": view["name"], "lat": view["lat"], "lon": view["lon"],
            "radius_mi": radius_mi, "query": q}

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

def rows(view, radius_mi, kinds=None):
    """[(aircraft, miles, bearing, info)] within radius_mi of the view, nearest first;
    kinds: lookup.kind() classes to keep (None = all)."""
    snap = aircraft._poller(view).snapshot()
    out = []
    for a in snap["aircraft"]:
        if kinds and lookup.kind(a["callsign"]) not in kinds: continue
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

def render(view, w, h, r, radius_mi=50, kinds=None):
    lst, snap = rows(view, radius_mi, kinds)
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    s = h / 480.0 if h >= w else w / 480.0                 # scale from the 480 px design
    f_cs, f_dest, f_sub, f_hd = (basemap.font(max(12, int(sz * min(1.0, s))), b)
                                 for sz, b in ((21, True), (17, False), (17, False), (14, True)))
    # AMOLED: drift the whole page by a few px over time (static text is the burn-in risk)
    t = int(time.time() // 120) % 4
    ox, oy = (0, 2, 2, 0)[t], (0, 0, 2, 2)[t]
    side, top, row_h, per = _layout(w, h, r)
    with _shown_lock: _shown[_key(view, w, h, radius_mi, kinds)] = [a["icao"] for a, _, _, _ in lst[:per]]
    status = opensky.ST_NAMES.get(snap["status"], "?")
    head = ("Nearest %d of %d within %d mi of %s" % (per, len(lst), radius_mi, view["name"]) if len(lst) > per
            else "%d within %d mi of %s" % (len(lst), radius_mi, view["name"]))
    if snap["status"] not in (opensky.ST_OK, opensky.ST_IDLE): head += "  ·  " + status
    d.text((w // 2 + ox, top - 22 + oy), _fit(d, head, f_hd, w - 2 * side), font=f_hd, fill=DIM, anchor="mm")
    if not lst:
        msg = "Waiting for aircraft…" if snap["status"] == opensky.ST_IDLE else "No aircraft in range"
        d.text((w // 2, h // 2), msg, font=f_dest, fill=SUB, anchor="mm")
    y = top
    for a, mi, brg, info in lst[:per]:                       # one page: the nearest that fit
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

def detail(view, w, h, r, radius_mi, kinds, tap_y):
    """The list dimmed, under a card about the flight on the tapped row; None if no flight there."""
    side, top, row_h, per = _layout(w, h, r)
    idx = (tap_y - top) // row_h if tap_y >= top else -1
    with _shown_lock: shown = list(_shown.get(_key(view, w, h, radius_mi, kinds), []))
    if not (0 <= idx < len(shown)):
        return None
    icao = shown[idx]
    snap = aircraft._poller(view).snapshot()
    a = next((x for x in snap["aircraft"] if x["icao"] == icao), None)
    img = render(view, w, h, r, radius_mi, kinds).point(lambda v: v // 4)      # the list, dimmed
    d = ImageDraw.Draw(img)
    s = min(1.0, (h if h >= w else w) / 480.0)
    F = lambda sz, b=False: basemap.font(max(11, int(sz * s)), b)
    cx0, cx1 = side - 4, w - side + 4
    cy0, cy1 = max(top - 30, 10), h - max(14, int(r * 0.5))
    d.rounded_rectangle((cx0, cy0, cx1, cy1), radius=14, fill=(16, 20, 24), outline=AMBER, width=2)
    x, y = cx0 + 16, cy0 + 14
    if a is None:
        d.text(((cx0 + cx1) // 2, (cy0 + cy1) // 2), "That flight has left the area", font=F(18), fill=SUB, anchor="mm")
        return img
    info = lookup.lookup(a["icao"], a["callsign"], a["lat"], a["lon"]) or {}   # fetch now if not cached
    route = info.get("route") or {}
    orig, dest = route.get("origin") or {}, route.get("dest") or {}
    mi = opensky.miles_between(view["lat"], view["lon"], a["lat"], a["lon"])
    brg = _bearing(view["lat"], view["lon"], a["lat"], a["lon"])
    cs = a["callsign"] or "%06x" % a["icao"]
    # title: flight + registration ............ distance
    d.text((x, y), cs, font=F(30, True), fill=INK, anchor="la")
    if info.get("registration") and info["registration"].upper() != cs.upper():
        d.text((x + d.textlength(cs, font=F(30, True)) + 12, y + 10), info["registration"], font=F(17), fill=SUB, anchor="la")
    d.text((cx1 - 16, y + 6), "%.1f mi %s" % (mi, DIRS[int((brg + 22.5) % 360 // 45)]), font=F(19, True), fill=AMBER, anchor="ra")
    y += 42
    who = info.get("owner") or ("Private" if lookup.kind(a["callsign"]) == "private" else "")
    room = cx1 - 16 - x
    if who:
        d.text((x, y), _fit(d, who, F(17), room), font=F(17), fill=SUB, anchor="la"); y += 26
    if route:
        o = "%s %s" % (orig.get("iata", "?"), orig.get("city", ""))
        t = "%s %s" % (dest.get("iata", "?"), dest.get("city", ""))
        d.text((x, y), _fit(d, o.strip(), F(19, True), room), font=F(19, True), fill=INK, anchor="la"); y += 26
        d.text((x, y), _fit(d, "\u2192 " + t.strip(), F(19, True), room), font=F(19, True), fill=INK, anchor="la"); y += 32
    else:
        d.text((x, y), "Route not known", font=F(17), fill=DIM, anchor="la"); y += 30
    if info.get("type"):
        d.text((x, y), _fit(d, info["type"], F(18), room), font=F(18), fill=INK, anchor="la"); y += 30
    vr = a.get("vrate_ms")
    climb = ("  \u2191 %d fpm" % round(vr * 196.85, -1) if vr and vr > 0.5 else
             "  \u2193 %d fpm" % round(-vr * 196.85, -1) if vr and vr < -0.5 else "  level")
    lines = [
        ("Altitude", _alt(a) + ("" if a["on_ground"] else climb)),
        ("Speed", "%d kt" % round(a["speed_ms"] * 1.94384) if a.get("speed_ms") is not None else "--"),
        ("Heading", "%03d\u00b0" % round(a["track_deg"]) if a.get("track_deg") is not None else "--"),
        ("Squawk", a.get("squawk") or "--"),
        ("Country", a.get("country") or "--"),
        ("ICAO", "%06X" % a["icao"]),
    ]
    for label, val in lines:
        if y > cy1 - 44: break
        d.text((x, y), label, font=F(15), fill=DIM, anchor="la")
        d.text((x + int(96 * s), y), val, font=F(17), fill=INK, anchor="la")
        y += 25
    d.text(((cx0 + cx1) // 2, cy1 - 14), "tap or KEY to close", font=F(13), fill=DIM, anchor="mm")
    return img

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=85); return b.getvalue()

def preview_png(view, w, h, r, radius_mi=50, kinds=None):
    b = io.BytesIO(); render(view, w, h, r, radius_mi, kinds).save(b, "PNG"); return b.getvalue()

def handle(h, p, q):
    parts = p.split("/")                        # ['', 'airlist', <view>, 'list.jpg']
    if len(parts) != 4 or parts[1] != ID or parts[3] not in ("list.jpg", "detail.jpg"):
        return False
    view = settings.air_view(parts[2])
    if view is None:
        return h.json({"error": "no such view"}, 404)
    try:
        w, hh, r = int(q.get("w", 480)), int(q.get("h", 480)), int(q.get("r", 56))
        mi = max(5, min(MAX_MI, int(q.get("mi", 50))))
    except ValueError:
        return h.json({"error": "bad parameters"}, 400)
    if not (100 <= w <= 2048 and 100 <= hh <= 2048):
        return h.json({"error": "bad size"}, 400)
    aircraft._poller(view).touch()             # a device is watching: keep OpenSky polling
    kinds = set(q.get("types", "").split(",")) & set(lookup.KINDS)
    if parts[3] == "detail.jpg":
        try: tap_y = int(q.get("y", -1))
        except ValueError: tap_y = -1
        img = detail(view, w, hh, r, mi, kinds or None, tap_y)
        return h.send(_jpeg(img), "image/jpeg") if img else h.json({"error": "no flight on that row"}, 404)
    return h.send(_jpeg(render(view, w, hh, r, mi, kinds or None)), "image/jpeg")
