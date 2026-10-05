"""Weather app: animated radar loops for several cities, plus a status strip.

Two independent refresh loops, because they have very different natural rates:

  * radar  -- every RADAR_REFRESH_S (600s, RainViewer's cadence). One RainViewer
              index fetch per cycle, then one loop per city.
  * status -- every STATUS_REFRESH_S (60s), so the temperature is never up to ten
              minutes stale. This is why the strip is served apart from the frames.

The device lists the views, and for the chosen one polls its manifest. When loop_id
changes it pulls loop.bin into flash and animates locally -- no per-frame WiFi. It
can cache every view's loop, so switching cities is instant.

Endpoints (the old amoled-radar paths stay as aliases until both boards migrate:
/cities.json, /c/<id>/..., /ui/picker.jpg, /ui/hold.jpg)
  /weather/views.json              [{id, name, lat, lon, default}]
  /weather/<id>/manifest.json      loop_id, frame count, sizes, times, keys (real frames)
  /weather/<id>/loop.bin           RDL1 device loop (see render.encode_loop)
  /weather/<id>/frame/<n>.jpg      480x424 radar frame
  /weather/<id>/status.jpg         480x56 temperature / humidity / city strip
  /weather/ui/picker.jpg?hl=&cur=  city picker
  /weather/ui/hold.jpg             "Hold to turn off" pill
"""
import io, json, math, os, re, threading, time, zlib
from datetime import datetime
from zoneinfo import ZoneInfo

from . import render as R
from .. import core, settings

ID = "weather"

RADAR_S    = int(os.environ.get("RADAR_REFRESH_S", "600"))
STATUS_S   = int(os.environ.get("STATUS_REFRESH_S", "60"))
N_FRAMES   = int(os.environ.get("RADAR_FRAMES", "12"))
QUALITY    = int(os.environ.get("RADAR_JPEG_QUALITY", "86"))
# In-between frames per 10 minutes of radar time (0 disables smoothing). The
# manifest lists which frames are real ("keys"), so firmware that measures its
# own decode rate as too slow can play real frames only.
TWEENS     = int(os.environ.get("RADAR_TWEENS", "3"))
TWEEN_MODE = os.environ.get("RADAR_TWEEN_MODE", "motion")
# Largest device-format loop a board's flash slot holds. Hub firmware: 6 slots, 2,068,480
# bytes of data each (reported as "slot" in /device/hello); the radar firmware's were bigger.
LOOP_BUDGET = int(os.environ.get("RADAR_LOOP_BUDGET", str(2060000)))

def _budget(g):
    # 4 MB boards (classic ESP32, 240x240) have 3 slots of 475,136 bytes (hello's "slot")
    return LOOP_BUDGET if g.w * g.h > 240 * 240 else 470000
# Clear-air echo (RainViewer's faint tan/grey band) is filtered on city views only
# when the city is at least this warm. Snow can't reach the ground at 40 F, so the
# band can't be snow then; below it, the band is kept in case it is light snow.
CLEAR_AIR_MIN_F = float(os.environ.get("RADAR_CLEAR_AIR_MIN_F", "40"))

# Cities live in the hub's settings (admin page): settings.places(). Only places some
# device shows are rendered (settings.places_in_use()).
def _cities():
    return settings.places_in_use()

# ---------------------------------------------------------------- panned views
# A device can move its view off a city in 50-mile steps (swipe, or HA): the view id is
# then "<city>@<dx>,<dy>" (east, north; at most PAN_MAX steps each way). Panned loops
# are rendered on demand, only for the profiles that ask, and dropped PAN_KEEP_S after
# the last request.
PAN_MI, PAN_MAX, PAN_KEEP_S = 50, 3, 15 * 60
_PAN_RE = re.compile(r"^([a-z0-9_-]+)@(-?\d+),(-?\d+)$")
_pans = {}                             # (view id, profile key) -> last request time
_pan_wake = threading.Event()
_maps = None                           # the latest RainViewer index (radar_loop fetches it)

def pan_label(dx, dy):
    """"50 mi NE", "100 mi N, 50 mi E" """
    ns = "N" if dy > 0 else "S"; ew = "E" if dx > 0 else "W"
    if dx and dy and abs(dx) == abs(dy): return "%d mi %s%s" % (abs(dy) * PAN_MI, ns, ew)
    parts = (["%d mi %s" % (abs(dy) * PAN_MI, ns)] if dy else []) + (["%d mi %s" % (abs(dx) * PAN_MI, ew)] if dx else [])
    return ", ".join(parts)

def parse_pan(vid):
    """(city id, dx, dy); dx = dy = 0 for a plain city id; None if malformed or too far."""
    m = _PAN_RE.match(vid or "")
    if not m: return (vid, 0, 0)
    cid, dx, dy = m.group(1), int(m.group(2)), int(m.group(3))
    if abs(dx) > PAN_MAX or abs(dy) > PAN_MAX: return None
    return (cid, dx, dy)

def _place(vid):
    """A city, or a panned view of one: the city moved by dx/dy steps, its own position
    kept as a highlighted town so you can see where home is."""
    p = parse_pan(vid)
    if p is None: return None
    cid, dx, dy = p
    base = settings.place(cid)
    if not base or (dx, dy) == (0, 0): return base
    lat = base["lat"] + dy * PAN_MI / 69.05
    lon = base["lon"] + dx * PAN_MI / (69.17 * math.cos(math.radians(base["lat"])))
    towns = [t for t in base.get("places", []) if t[0] != base["name"]] + [[base["name"], base["lat"], base["lon"], True]]
    return dict(base, id=vid, lat=lat, lon=lon, places=towns, home=base, pan=(dx, dy), pan_label=pan_label(dx, dy))

def _touch_pan(vid, g):
    """A device asked for a panned view: keep it rendered (start now if it is new)."""
    with _lock:
        new = (vid, g.key) not in _pans
        _pans[(vid, g.key)] = time.time()
    if new:
        print("[weather] pan %s %s requested -- rendering it" % (vid, g.key), flush=True)
        _pan_wake.set()

def _active_pans():
    """[(place, geom)] still wanted; forgets the rest (and their loops)."""
    now, out = time.time(), []
    with _lock:
        for (vid, gk), t in list(_pans.items()):
            if now - t > PAN_KEEP_S or gk not in _geoms:
                del _pans[(vid, gk)]; _state.pop((vid, gk), None)
                continue
            p = _place(vid)
            if p: out.append((p, _geoms[gk]))
    return out

# State per (city, profile). Loops are rendered for every profile in use: the default
# panel always, plus any profile a device registers or asks for (MAX_GEOMS at most).
MAX_GEOMS = 4
_lock  = threading.Lock()
_wake  = threading.Event()             # a new profile appeared: render it now
_geoms = {R.DEFAULT_GEOM.key: R.DEFAULT_GEOM}
_state = {}

def _st(cid, g):
    """The state dict for a city at a profile (caller holds _lock)."""
    return _state.setdefault((cid, g.key), {"loop_id": 0, "frames": [], "times": [], "keys": [], "status": b"",
                                            "built": 0, "status_built": 0, "radar_err": None, "status_err": None})

def add_geom(w, h, r, panel, strip=True):
    """Render loops for this profile from now on. Returns its Geom, or None if the
    profile is invalid or there are already MAX_GEOMS."""
    try:
        w, h, r = int(w), int(h), int(r)
    except (TypeError, ValueError):
        return None
    if not (100 <= w <= 2048 and 100 <= h <= 2048 and 0 <= r <= min(w, h) // 2 and panel in ("amoled", "lcd")):
        return None
    g = R.Geom(w, h, r, panel, strip)
    with _lock:
        if g.key in _geoms: return _geoms[g.key]
        if len(_geoms) >= MAX_GEOMS: return None
        _geoms[g.key] = g
    print("[weather] new profile %s -- rendering it" % g.key, flush=True)
    _wake.set()
    return g

def clock(epoch, city):
    """Local clock time for the city being shown. The container runs on UTC, so
    time.strftime() printed UTC -- Canton read 3:00 PM at 11:00 AM local."""
    tz = ZoneInfo(city.get("tz", "America/Chicago"))
    return datetime.fromtimestamp(epoch, tz).strftime("%-I:%M %p")

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=QUALITY, optimize=True); return b.getvalue()

def reading(city):
    """(temp F, humidity %) as strings: from HA entities or the nearest NWS station."""
    city = city.get("home") or city              # a panned view reads its own city's weather
    t = city.get("temp") or {"source": "nws"}
    if t.get("source") == "ha":
        return R.ha_reading(t.get("temp"), t.get("hum"))
    return R.obs_reading(city["lat"], city["lon"])

def clear_air_filter_on(city):
    """Always on for views that ask for it (wide); otherwise on only when the
    city's own current temperature rules out snow."""
    if city.get("suppress_clear_air"):
        return True
    try:
        return float(reading(city)[0]) >= CLEAR_AIR_MIN_F
    except (TypeError, ValueError):
        return False            # no reading: keep the band rather than risk hiding snow

def build_radar(city, maps, g):
    lat, lon = city["lat"], city["lon"]
    suppress = clear_air_filter_on(city)
    bm   = R.darken_for_amoled(R.basemap(lat, lon, city, g))
    O = g.orbit
    OW, OH = g.w + 2 * O, g.view_h + 2 * O
    base = R.decorate(bm.resize((OW, OH), R.Image.LANCZOS), city, g).convert("RGB")
    ox, oy = R.orbit_offset(int(time.time() // RADAR_S), g)

    host = maps["host"]
    want = (maps["radar"]["past"] + maps["radar"].get("nowcast", []))[-N_FRAMES:]
    # NOAA quality control: optional -- if NOAA is unreachable, carry on unmasked
    try:
        qc_avail = R.qc_times()
    except Exception as e:
        qc_avail = []
        print("[radar] NOAA QC index unavailable (%s) -- frames not QC-masked" % str(e)[:60], flush=True)
    qc_used = 0
    layers, stamps = [], []
    for f in want:
        layer = R.radar(host, f["path"], lat, lon, city, g)
        if R.is_watermark(layer):
            raise RuntimeError("RainViewer returned a watermark tile at zoom %d "
                               "-- the free tier caps at 7" % city.get("radar_zoom", R.RADAR_ZOOM))
        if suppress:
            layer = R.suppress_clear_air(layer)      # before resampling blends colours
        layer = layer.resize((OW, OH), R.Image.LANCZOS)
        try:
            mask = R.qc_mask(lat, lon, city, f["time"], qc_avail, OW, OH, g)
            if mask is not None:
                layer = R.apply_mask(layer, mask); qc_used += 1
        except Exception as e:
            print("[radar] %s NOAA QC frame failed (%s) -- left unmasked" % (city["id"], str(e)[:60]), flush=True)
        layers.append(layer); stamps.append(f["time"])

    pts = R.place_pixels(city.get("places", []), lat, lon, OW, OH, city, g)

    def compose(layer, frac, left, right):
        frame = base.copy()
        if layer is not None: frame.paste(layer, (0, 0), layer)
        R.draw_places(frame, pts, crosshair=not city.get("wide"), g=g)
        frame = frame.crop((O + ox, O + oy, O + ox + g.w, O + oy + g.view_h))
        frame = R.progress_bar(frame, frac, left, right, ox, oy, g)
        if city.get("pan_label"): R.offset_pill(frame, city["pan_label"], g)   # also the device's recentre target
        return frame

    # Interpolate the radar layer only, then composite. Fewer in-betweens if the
    # device-format loop would not fit the board's flash slot (a big storm changes
    # most pixels in every frame): 3 per 10 min, then 2, 1, real frames only.
    for tw in sorted({t for t in (TWEENS, 2, 1, 0) if t <= TWEENS}, reverse=True):
        seq, times, keys = [], [], []
        for i, layer in enumerate(layers):
            keys.append(len(seq)); seq.append(layer); times.append(stamps[i])
            if i + 1 < len(layers) and tw > 0:
                n = R.tween_count(stamps[i + 1] - stamps[i], tw)
                for k, twl in enumerate(R.tweens(layer, layers[i + 1], n, TWEEN_MODE)):
                    seq.append(twl)
                    times.append(stamps[i] + (stamps[i + 1] - stamps[i]) * (k + 1) / (n + 1.0))
        t_first, t_last = times[0], times[-1]
        left, right = clock(t_first, city), clock(t_last, city)
        span = float(t_last - t_first) or 1.0
        imgs = [compose(layer, (t - t_first) / span, left, right) for layer, t in zip(seq, times)]
        blob = R.encode_loop(compose(None, 0.0, left, right), imgs, keys)
        if len(blob) <= _budget(g) or tw == 0:
            break
        print("[radar] %s %s loop %.1f MB with %d in-betweens > budget, trying fewer" % (city["id"], g.key, len(blob) / 1e6, tw), flush=True)
    out = [_jpeg(im) for im in imgs]
    return out, [int(t) for t in times], keys, {"clear_air": suppress, "qc_masked": qc_used,
                                                 "real_frames": len(layers), "blob": blob, "tweens": tw}

_temps = {}

def build_status(city, g):
    temp, hum = reading(city)
    _temps[city["id"]] = temp
    stamp = clock(time.time(), city)
    if city.get("pan_label"):            # panned: say how far off the city this is
        stamp = "%s \u00b7 %s" % (city["pan_label"], stamp)
    if city.get("status_label"):          # whose reading this is, when it isn't the view's
        stamp = "%s \u00b7 %s" % (city["status_label"], stamp)
    return _jpeg(R.status_strip(temp, hum, stamp, city["name"], g))

def radar_loop():
    only_new = False                       # woken for a new profile: render just the missing ones
    while True:
        try:
            maps = json.loads(R.fetch("https://api.rainviewer.com/public/weather-maps.json"))
        except Exception as e:
            maps = None
            print("[radar] RainViewer index FAILED: %s" % e, flush=True)
        with _lock: geoms = list(_geoms.values())
        cities = _cities()
        global _maps
        if maps: _maps = maps
        _pan_wake.set()                        # panned views refresh with the same index
        for g in geoms:
            for c in cities:
                with _lock: st = _st(c["id"], g)
                if only_new and st["frames"]:
                    continue
                if maps is None:
                    with _lock: st["radar_err"] = "RainViewer index unavailable"
                    continue
                _render(c, g, maps)
        only_new = _wake.wait(RADAR_S)
        _wake.clear()

def _render(c, g, maps):
    with _lock: st = _st(c["id"], g)
    try:
        frames, times, keys, info = build_radar(c, maps, g)
        with _lock:
            st.update(loop_id=int(time.time()), frames=frames, times=times, keys=keys,
                      clear_air_filtered=info["clear_air"], blob=info["blob"], tweens_used=info["tweens"],
                      qc_masked="%d/%d" % (info["qc_masked"], info["real_frames"]),
                      built=int(time.time()), radar_err=None)
        print("[radar] %s %s ok (%d frames)" % (c["id"], g.key, len(frames)), flush=True)
    except Exception as e:
        with _lock: st["radar_err"] = str(e)[:200]
        print("[radar] %s %s FAILED: %s" % (c["id"], g.key, e), flush=True)

def pan_loop():
    """Panned views on their own thread, so a swipe waits for one loop (~15 s), not for
    a whole pass over every city and profile."""
    while True:
        _pan_wake.wait(60); _pan_wake.clear()
        for c, g in _active_pans():
            with _lock: st = _st(c["id"], g); built = st["built"]
            if time.time() - built < RADAR_S - 30: continue
            maps = _maps
            if maps is None or time.time() - built > RADAR_S * 2:
                try: maps = json.loads(R.fetch("https://api.rainviewer.com/public/weather-maps.json"))
                except Exception as e: print("[radar] RainViewer index FAILED: %s" % e, flush=True); continue
            _render(c, g, maps)

def status_loop():
    while True:
        with _lock: geoms = list(_geoms.values())
        for c, g in [(c, g) for c in _cities() for g in geoms] + _active_pans():
            if not g.strip: continue
            try:
                v = build_status(c, g)
                with _lock: _st(c["id"], g).update(status=v, status_built=int(time.time()), status_err=None)
            except Exception as e:
                with _lock: _st(c["id"], g)["status_err"] = str(e)[:200]
                print("[status] %s %s FAILED: %s" % (c["id"], g.key, e), flush=True)
        time.sleep(STATUS_S)

def view_summary(place, default=False):
    """How a device sees one of its cities (in /device/hello)."""
    return dict({k: place[k] for k in ("id", "name", "lat", "lon")}, default=default)

def start():
    for pr in core.profiles():             # devices already registered: render their panels from the start
        add_geom(pr.get("w"), pr.get("h"), pr.get("r"), pr.get("panel", "amoled"))
    _wake.clear()
    threading.Thread(target=radar_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=pan_loop, daemon=True).start()
    print("[weather] cities in use=%s  (radar every %ds, status every %ds)"
          % (",".join(c["id"] for c in _cities()), RADAR_S, STATUS_S), flush=True)

def health():
    """Healthy when the default profile has a loop and a strip for every city; other
    profiles are listed (a profile that was just added needs a few minutes)."""
    d = R.DEFAULT_GEOM.key
    ids = [c["id"] for c in _cities()]
    with _lock:
        per = {cid: {"frames": len(st["frames"]), "status": bool(st["status"]),
                     "radar_err": st["radar_err"], "status_err": st["status_err"]}
               for (cid, gk), st in _state.items() if gk == d and cid in ids}
        others = {gk: sum(1 for (c, k), st in _state.items() if k == gk and st["frames"])
                  for gk in _geoms if gk != d}
    ok = len(per) == len(ids) and all(v["frames"] and v["status"] for v in per.values())
    return ok, dict(per, profiles={k: "%d/%d cities ready" % (n, len(ids)) for k, n in others.items()})

def preview_png(pid, w, h, r, panel, strip=True):
    """For the admin page: the latest frame and status strip of a city, at a profile."""
    g = add_geom(w, h, r, panel, strip)
    if g is None or not _place(pid): return None
    with _lock: st = dict(_st(pid, g))
    if not st["frames"]: return None
    img = R.Image.new("RGB", (g.w, g.h))
    img.paste(R.Image.open(io.BytesIO(st["frames"][-1])), (0, 0))
    if st["status"]: img.paste(R.Image.open(io.BytesIO(st["status"])), (0, g.view_h))
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()

def _geom(h, q):
    """The profile a request is for (default panel if it names none). Sends the error
    response itself and returns None for a bad or unaccepted profile."""
    if "w" not in q:
        return R.DEFAULT_GEOM
    g = add_geom(q.get("w"), q.get("h"), q.get("r", "0"), q.get("panel", "amoled"), q.get("strip", "1") != "0")
    if g is None: h.json({"error": "bad profile, or too many profiles"}, 400)
    return g

def _view(h, vid, rest, g):
    with _lock: st = dict(_st(vid, g))
    if rest == "manifest.json":
        return h.json({
            "city": vid, "loop_id": st["loop_id"], "frames": len(st["frames"]),
            "w": g.w, "h": g.h, "view_h": g.view_h, "status_h": g.status_h, "profile": g.key,
            "built": st["built"], "status_built": st["status_built"],
            "sizes": [len(f) for f in st["frames"]], "times": st["times"], "keys": st["keys"],
            "tweens_per_10min": TWEENS, "tween_mode": TWEEN_MODE,
            "clear_air_filtered": st.get("clear_air_filtered"),
            "qc_masked": st.get("qc_masked"),       # real frames masked by NOAA QC
            "loop_bin_size": len(st.get("blob") or b""), "tweens_used": st.get("tweens_used"),
            "loop_crc32": zlib.crc32(st.get("blob") or b""),   # devices verify what they wrote to flash
            "radar_err": st["radar_err"], "status_err": st["status_err"]})
    if rest == "loop.bin":                  # device format (see render.encode_loop)
        if not st.get("blob"): return h.json({"error": "not ready"}, 503)
        return h.send(st["blob"], "application/octet-stream")
    if rest == "status.jpg":
        if not st["status"]: return h.json({"error": "not ready"}, 503)
        return h.send(st["status"], "image/jpeg")
    if rest.startswith("frame/") and rest.endswith(".jpg"):
        try: i = int(rest[len("frame/"):-4])
        except ValueError: return h.json({"error": "bad index"}, 400)
        if not (0 <= i < len(st["frames"])): return h.json({"error": "out of range"}, 404)
        return h.send(st["frames"][i], "image/jpeg")
    return False

def _ui(h, name, q, g):
    if name == "picker.jpg":
        try: hl = int(q.get("hl", "0"))
        except ValueError: hl = 0
        # the requesting device's own cities, in its order (?ids=a,b,c); else all in use
        cities = [settings.place(i) for i in q["ids"].split(",")] if q.get("ids") else _cities()
        entries = [(c["id"], c["name"], _temps.get(c["id"])) for c in cities if c]
        if not entries: return h.json({"error": "no cities"}, 404)
        return h.send(_jpeg(R.picker_panel(entries, hl % len(entries), q.get("cur", ""), g)), "image/jpeg")
    if name == "pan.jpg":                 # shown while a panned loop is being rendered
        p = parse_pan("x@" + q.get("pan", "0,0"))
        text = "Loading %s\u2026" % pan_label(p[1], p[2]) if p and p[1:] != (0, 0) else "Loading\u2026"
        return h.send(_jpeg(R.message_pill(text, g)), "image/jpeg")
    if name == "hold.jpg":
        return h.send(_jpeg(R.hold_pill()), "image/jpeg")
    return False

def handle(h, p, q):
    """Serve a weather path (new or legacy); False if it isn't one."""
    parts = p.split("/")
    if p in ("/weather/views.json", "/cities.json"):
        return h.json([view_summary(c) for c in settings.places()])
    ui = (len(parts) == 4 and parts[1:3] == ["weather", "ui"]) or (len(parts) == 3 and parts[1] == "ui")
    view = len(parts) >= 4 and parts[1] in ("weather", "c") and _place(parts[2]) is not None
    if not (ui or view):
        return False
    g = _geom(h, q)                         # ?w=&h=&r=&panel= (none: the default panel)
    if g is None:
        return True
    if ui:
        return _ui(h, parts[-1], q, g)
    # /weather/<id>/... and legacy /c/<id>/...
    if "@" in parts[2] and parse_pan(parts[2])[1:] != (0, 0): _touch_pan(parts[2], g)
    return _view(h, parts[2], "/".join(parts[3:]), g)
    return False
