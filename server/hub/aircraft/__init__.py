"""Aircraft app: live planes around a centre point, from OpenSky.

The hub does everything except drawing the live overlay: OpenSky credentials and the
shared credit budget, filtering, aircraft/route lookups with a shared cache, and the
backgrounds (map, towns, rings) sized to each device. The device draws planes, trails,
labels and the info panel itself, dead-reckoning between polls, so motion stays
smooth and taps feel instant.

Endpoints
  /aircraft/views.json                   [{id, name, lat, lon, radius_mi, levels}]
  /aircraft/<view>/manifest.json?w=&h=&r=  bundle id/size, centre, per-level scale + rings
  /aircraft/<view>/bundle.bin?w=&h=&r=     backgrounds for every zoom level (see basemap.py)
  /aircraft/<view>/states.bin?since=<seq>  binary states (below); 304 if seq unchanged
  /aircraft/<view>/states.json             the same, readable
  /aircraft/<view>/preview.png?w=&h=&r=&level=   what a device should show now
  /aircraft/info/<icao>?cs=&lat=&lon=      type, registration, owner, vetted route

states.bin (little-endian): 20-byte header then 48-byte records.
  header  4s "AST1", u32 seq, u32 server_time, i32 credits_left (-1 unknown),
          u16 count, u8 status (0 ok, 1 auth failed, 2 rate limited, 3 error, 4 idle), u8 reserved
  record  u32 icao, f32 lat, f32 lon, f32 alt_m, f32 speed_ms, f32 track_deg, f32 vrate_ms,
          u32 time_position, 8s callsign (NUL-padded, not terminated), 4s squawk,
          u8 category, u8 flags (1 on ground, 2 info cached, 4 route known), 2x
          Unknown floats are NaN.
"""
import io, json, math, os, struct, threading, time

from PIL import Image, ImageDraw

from . import basemap, lookup, opensky
from .. import core, settings

ID = "aircraft"

# Views live in the hub's settings (admin page): centre, radius, zoom levels. The exact
# home position is set there, never in this public repo.

HDR = struct.Struct("<4sIIiHBB")
REC = struct.Struct("<IffffffI8s4sBB2x")
F_GROUND, F_INFO, F_ROUTE = 1, 2, 4

_pollers = {}                   # view id -> Poller, created when a device first asks
_plock = threading.Lock()
_bundles = basemap.Bundles(core.CACHE)

def _poller(view):
    """The view's poller; replaced if the view was edited (new centre or radius)."""
    with _plock:
        p = _pollers.get(view["id"])
        if p is None or p.view != view:
            p = _pollers[view["id"]] = opensky.Poller(view, lookup.prefetch)
            threading.Thread(target=p.run, daemon=True).start()
        return p

def snapshots():
    """{view id: poller snapshot + status name} for the views being polled (for MQTT)."""
    with _plock: pollers = dict(_pollers)
    return {vid: dict(p.snapshot(), status_name=opensky.ST_NAMES[p.snapshot()["status"]]) for vid, p in pollers.items()}

def miles_from(view, ac):
    return opensky.miles_between(view["lat"], view["lon"], ac["lat"], ac["lon"])

def view_summary(view, start_level=0):
    """How a device sees its aircraft view (in /device/hello)."""
    return dict({k: view[k] for k in ("id", "name", "lat", "lon", "radius_mi", "levels")}, start_level=start_level)

def start():
    lookup.start()
    def warm():                 # the default profile's bundles, so a first device doesn't wait
        for v in settings.views_in_use():
            try: _bundles.get(v, *DEFAULT_PROFILE)
            except Exception as e: print("[aircraft] %s bundle FAILED: %s" % (v["id"], e), flush=True)
    threading.Thread(target=warm, daemon=True).start()
    print("[aircraft] views in use=%s  (poll every %ds while a device is watching)"
          % (",".join(v["id"] for v in settings.views_in_use()), opensky.POLL_S), flush=True)

def health():
    per, ok = {}, True
    with _plock: pollers = dict(_pollers)
    for vid, p in pollers.items():
        s = p.snapshot()
        age = int(time.time() - s["last_ok"]) if s["last_ok"] else None
        fresh = s["status"] == opensky.ST_IDLE or (age is not None and age < 5 * opensky.POLL_S)
        ok = ok and s["status"] != opensky.ST_AUTH and fresh
        per[vid] = {"status": opensky.ST_NAMES[s["status"]], "age_s": age, "aircraft": len(s["aircraft"]),
                    "credits": s["credits"], "err": s["err"]}
    return ok, per

# --- device profile (from the query string; defaults = the 2.16" C6 board) ---

DEFAULT_PROFILE = (480, 480, 56)

def _profile(q):
    try:
        w, h, r = (int(q.get(k, d)) for k, d in zip("whr", DEFAULT_PROFILE))
    except ValueError:
        return None
    if not (100 <= w <= 2048 and 100 <= h <= 2048 and 0 <= r <= min(w, h) // 2):
        return None
    return w, h, r

# --- states ---

def _nan(v):
    return float("nan") if v is None else v

def _flags(ac):
    f = F_GROUND if ac["on_ground"] else 0
    info = lookup.lookup(ac["icao"], ac["callsign"], ac["lat"], ac["lon"], fetch=False)
    if info is not None:
        f |= F_INFO
        if info["route"]: f |= F_ROUTE
    return f

def encode_states(s):
    acs = s["aircraft"]
    out = [HDR.pack(b"AST1", s["seq"], s["server_time"], s["credits"], len(acs), s["status"], 0)]
    for a in acs:
        out.append(REC.pack(a["icao"], a["lat"], a["lon"], _nan(a["alt_m"]), _nan(a["speed_ms"]),
                            _nan(a["track_deg"]), _nan(a["vrate_ms"]), a["time_position"],
                            a["callsign"].encode("ascii", "replace")[:8], a["squawk"].encode("ascii", "replace")[:4],
                            a["category"] & 0xFF, _flags(a)))
    return b"".join(out)

def _states_json(s):
    return {"seq": s["seq"], "server_time": s["server_time"], "credits": s["credits"],
            "status": opensky.ST_NAMES[s["status"]], "err": s["err"],
            "aircraft": [dict(a, icao="%06x" % a["icao"], flags=_flags(a)) for a in s["aircraft"]]}

# --- preview: mirrors the firmware's drawing closely enough to check alignment ---

ALT_STOPS = [(0, 0xff6a2b), (5000, 0xffb52b), (10000, 0xf2f23c), (20000, 0x45f07a), (30000, 0x2ad4ff), (40000, 0xb07cff)]

def altitude_rgb(alt_m, ground=False):
    """FlightRadar-ish ramp: orange low -> yellow -> green -> cyan -> violet high."""
    if ground or alt_m is None: return (154, 154, 154)
    rgb = lambda v: ((v >> 16) & 255, (v >> 8) & 255, v & 255)
    ft = alt_m * 3.28084
    if ft <= 0: return rgb(ALT_STOPS[0][1])
    for (f0, c0), (f1, c1) in zip(ALT_STOPS, ALT_STOPS[1:]):
        if ft <= f1:
            t = (ft - f0) / (f1 - f0)
            return tuple(round(x0 + (x1 - x0) * t) for x0, x1 in zip(rgb(c0), rgb(c1)))
    return rgb(ALT_STOPS[-1][1])

def format_altitude(alt_m):
    if alt_m is None: return "--"
    ft = round(alt_m * 3.28084)
    return "FL%03d" % (ft // 100) if ft >= 18000 else str((ft + 50) // 100 * 100)

def render_preview(view, entry, level, s, w, h):
    img = entry["images"][level].copy()
    lv = entry["levels"][level]
    mx0, my0 = basemap.merc(view["lat"], view["lon"])
    d = ImageDraw.Draw(img, "RGBA")
    f12, f15 = basemap.font(12), basemap.font(15, True)
    now = time.time()
    for a in s["aircraft"]:
        lat, lon = a["lat"], a["lon"]
        if a["speed_ms"] is not None and a["track_deg"] is not None:      # dead-reckon like the device
            dt = max(0, min(60, now - a["time_position"]))
            dist = a["speed_ms"] * dt
            lat += dist * math.cos(math.radians(a["track_deg"])) / 111320.0
            lon += dist * math.sin(math.radians(a["track_deg"])) / (111320.0 * math.cos(math.radians(a["lat"])))
        mx, my = basemap.merc(lat, lon)
        x, y = w / 2 + (mx - mx0) / lv["mpp"], h / 2 - (my - my0) / lv["mpp"]
        if not (-20 < x < w + 20 and -20 < y < h + 20): continue
        trk = math.radians(a["track_deg"] or 0)
        sc = {2: 0.75, 5: 1.25, 6: 1.25}.get(a["category"], 1.0)
        pts = [(x + px * sc * math.cos(trk) - py * sc * math.sin(trk), y + px * sc * math.sin(trk) + py * sc * math.cos(trk))
               for px, py in ((0, -10), (-7, 8), (0, 4), (7, 8))]
        c = altitude_rgb(a["alt_m"], a["on_ground"])
        d.polygon([pts[0], pts[1], pts[2]], fill=c); d.polygon([pts[0], pts[2], pts[3]], fill=c)
        d.multiline_text((x + 11, y - 6), "%s\n%s" % (a["callsign"] or "?", format_altitude(a["alt_m"])),
                         fill=(216, 216, 216), font=f12, spacing=1)
    d.rounded_rectangle((20, 8, 120, 48), radius=14, fill=(0, 0, 0, 153))
    d.text((70, 28), "%d mi" % lv["range_mi"], fill="white", font=f15, anchor="mm")
    d.rounded_rectangle((20, h - 48, 250, h - 8), radius=14, fill=(0, 0, 0, 153))
    d.text((34, h - 28), "%d aircraft  -  %s" % (len(s["aircraft"]), opensky.ST_NAMES[s["status"]]),
           fill=(216, 216, 216), font=f12, anchor="lm")
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()

def preview_png(view, w, h, r, level=0):
    """For the admin page: the view as a device of this profile would show it now
    (without waking the OpenSky poller)."""
    entry = _bundles.get(view, w, h, r)
    if entry is None: return None
    return render_preview(view, entry, level % len(entry["levels"]), _poller(view).snapshot(), w, h)

# --- routes ---

def _bundle(h, view, q):
    prof = _profile(q)
    if prof is None:
        h.json({"error": "bad profile (w, h, r)"}, 400); return None, None
    try:
        entry = _bundles.get(view, *prof)
    except Exception as e:
        entry = None
        print("[aircraft] %s bundle FAILED: %s" % (view["id"], e), flush=True)
    if entry is None:
        h.json({"error": "bundle not available"}, 503); return None, None
    return entry, prof

def _info(h, icao_s, q):
    try: icao = int(icao_s, 16)
    except ValueError: return h.json({"error": "bad icao"}, 400)
    with _plock: pollers = list(_pollers.values())
    latest = next((a for p in pollers for a in p.snapshot()["aircraft"] if a["icao"] == icao), None)
    def num(k, fallback):
        try: return float(q[k])
        except (KeyError, ValueError): return fallback
    cs = q.get("cs", latest["callsign"] if latest else "")
    lat = num("lat", latest["lat"] if latest else None)
    lon = num("lon", latest["lon"] if latest else None)
    try:
        info = lookup.lookup(icao, cs, lat, lon)
    except Exception as e:
        return h.json({"error": "lookup failed: %s" % str(e)[:80]}, 502)
    info["country"] = latest["country"] if latest else ""
    return h.json(info)

def handle(h, p, q):
    parts = p.split("/")                    # ['', 'aircraft', <view|info>, ...]
    if len(parts) < 3 or parts[1] != "aircraft":
        return False
    if p == "/aircraft/views.json":
        return h.json([view_summary(v) for v in settings.air_views()])
    if len(parts) == 4 and parts[2] == "info":
        return _info(h, parts[3], q)
    view = settings.air_view(parts[2]) if len(parts) == 4 else None
    if view is None:
        return False
    what, poller = parts[3], _poller(view)
    if what in ("states.bin", "states.json"):
        poller.touch()
        s = poller.snapshot()
        if what == "states.json":
            return h.json(_states_json(s))
        if q.get("since") == str(s["seq"]):
            return h.send(b"", "application/octet-stream", 304)
        return h.send(encode_states(s), "application/octet-stream")
    if what in ("manifest.json", "bundle.bin", "preview.png"):
        entry, prof = _bundle(h, view, q)
        if entry is None:
            return True
        if what == "bundle.bin":
            return h.send(entry["blob"], "application/octet-stream")
        if what == "manifest.json":
            return h.json({"view": view["id"], "bundle_id": entry["id"], "bundle_size": len(entry["blob"]),
                           "bundle_crc32": entry["crc32"],    # devices verify what they wrote to flash
                           "w": prof[0], "h": prof[1], "r": prof[2], "lat": view["lat"], "lon": view["lon"],
                           "radius_mi": view["radius_mi"], "levels": entry["levels"]})
        try: level = int(q.get("level", "0")) % len(entry["levels"])
        except ValueError: level = 0
        poller.touch()
        return h.send(render_preview(view, entry, level, poller.snapshot(), prof[0], prof[1]), "image/png")
    return False
