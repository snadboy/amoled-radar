#!/usr/bin/env python3
"""HTTP service feeding the ESP32-C6 AMOLED radar display, for several cities.

Two independent refresh loops, because they have very different natural rates:

  * radar  -- every RADAR_REFRESH_S (600s, RainViewer's cadence). One RainViewer
              index fetch per cycle, then one loop per city.
  * status -- every STATUS_REFRESH_S (60s), so the temperature is never up to ten
              minutes stale. This is why the strip is served apart from the frames.

The device asks /cities.json once, shows the list when the KEY button is pressed,
and for the chosen city polls /c/<id>/manifest.json. When loop_id changes it pulls
the frames into flash and animates locally -- no per-frame WiFi. It can cache every
city's loop, so switching cities is instant.

Endpoints
  /cities.json                 [{id, name, lat, lon, default}]
  /c/<id>/manifest.json        loop_id, frame count, sizes, times, keys (real frames)
  /c/<id>/frame/<n>.jpg        480x424 radar frame
  /c/<id>/status.jpg           480x56 temperature / humidity / city strip
  /healthz
"""
import hashlib, io, json, os, threading, time, urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import render as R   # the renderer, used as a library

PORT       = int(os.environ.get("PORT", "8080"))
RADAR_S    = int(os.environ.get("RADAR_REFRESH_S", "600"))
STATUS_S   = int(os.environ.get("STATUS_REFRESH_S", "60"))
N_FRAMES   = int(os.environ.get("RADAR_FRAMES", "12"))
QUALITY    = int(os.environ.get("RADAR_JPEG_QUALITY", "86"))
# In-between frames per 10 minutes of radar time (0 disables smoothing). The
# manifest lists which frames are real ("keys"), so firmware that measures its
# own decode rate as too slow can play real frames only.
TWEENS     = int(os.environ.get("RADAR_TWEENS", "3"))
TWEEN_MODE = os.environ.get("RADAR_TWEEN_MODE", "motion")
# Clear-air echo (RainViewer's faint tan/grey band) is filtered on city views only
# when the city is at least this warm. Snow can't reach the ground at 40 F, so the
# band can't be snow then; below it, the band is kept in case it is light snow.
CLEAR_AIR_MIN_F = float(os.environ.get("RADAR_CLEAR_AIR_MIN_F", "40"))

# City centres are public, so they can live in the repo. "ha": true means the
# temperature comes from the Home Assistant outdoor sensors instead of the
# nearest NWS station.
DEFAULT_CITIES = [
    {"id": "geneva",  "name": "Geneva",    "lat": 41.8875, "lon": -88.3054, "tz": "America/Chicago", "ha": True, "default": True,
     "places": [["Chicago", 41.8781, -87.6298], ["Rockford", 42.2711, -89.0940],
                ["Joliet", 41.5250, -88.0817], ["DeKalb", 41.9295, -88.7504]]},
    {"id": "stlouis", "name": "St. Louis", "lat": 38.6270, "lon": -90.1994, "tz": "America/Chicago",
     "places": [["St. Charles", 38.7881, -90.4974], ["Alton", 38.8906, -90.1843],
                ["Belleville", 38.5201, -89.9840], ["Festus", 38.2206, -90.3960]]},
    # Toledo is the obvious "south" town for Canton but lands on the progress bar
    {"id": "canton",  "name": "Canton",    "lat": 42.3087, "lon": -83.4822, "tz": "America/Detroit",
     "places": [["Detroit", 42.3314, -83.0458], ["Ann Arbor", 42.2808, -83.7430],
                ["Pontiac", 42.6389, -83.2910], ["Monroe", 41.9164, -83.3977]]},
    # Regional view framing all three cities (St. Louis .. Canton, ~580 mi across).
    # Replaced an "Entire country" view on 2026-10-03: the lower 48 at ~9 km/px was
    # too small to read on a 2.16-inch panel. lon_span fills the width; RainViewer
    # zoom 5 at 512 px is ~1.86 km/px, matching what's shown. No rings or crosshair
    # ("wide"). A 4th element of 1 marks one of your own cities (amber marker).
    {"id": "midwest", "name": "Midwest", "lat": 40.5, "lon": -86.8, "tz": "America/Chicago",
     "ha": True, "status_label": "Geneva", "wide": True, "suppress_clear_air": True,
     "lon_span": 10.4, "base_zoom": 7, "radar_zoom": 5, "radar_tile": 512,
     "places": [["Geneva", 41.8875, -88.3054, 1], ["St. Louis", 38.6270, -90.1994, 1],
                ["Canton", 42.3087, -83.4822, 1], ["Milwaukee", 43.0389, -87.9065],
                ["Indianapolis", 39.7684, -86.1581], ["Fort Wayne", 41.0793, -85.1394],
                ["Louisville", 38.2527, -85.7585]]},
]
# Screen control for the device, decided here so the board never needs an HA token.
OCC_ENTITY   = os.environ.get("RADAR_OCC_ENTITY", "binary_sensor.upstairs_office_lwr02_occupancy")
LUX_ENTITY   = os.environ.get("RADAR_LUX_ENTITY", "sensor.upstairs_office_lwr02_illuminance")
VACANT_OFF_S = int(os.environ.get("RADAR_VACANT_OFF_S", "300"))   # empty this long -> screen off
BRIGHT_MIN     = int(os.environ.get("RADAR_BRIGHT_MIN", "140"))     # 0-255 panel brightness
BRIGHT_MAX     = int(os.environ.get("RADAR_BRIGHT_MAX", "255"))
BRIGHT_PER_LUX = float(os.environ.get("RADAR_BRIGHT_PER_LUX", "0.6"))
FIRMWARE_DIR = os.environ.get("RADAR_FIRMWARE_DIR", os.path.join(R.CACHE, "firmware"))

CITIES = json.loads(os.environ["RADAR_CITIES"]) if os.environ.get("RADAR_CITIES") else DEFAULT_CITIES
BY_ID  = {c["id"]: c for c in CITIES}

_lock  = threading.Lock()
_state = {c["id"]: {"loop_id": 0, "frames": [], "times": [], "keys": [], "status": b"",
                    "built": 0, "status_built": 0, "radar_err": None, "status_err": None}
          for c in CITIES}

def clock(epoch, city):
    """Local clock time for the city being shown. The container runs on UTC, so
    time.strftime() printed UTC -- Canton read 3:00 PM at 11:00 AM local."""
    tz = ZoneInfo(city.get("tz", "America/Chicago"))
    return datetime.fromtimestamp(epoch, tz).strftime("%-I:%M %p")

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=QUALITY, optimize=True); return b.getvalue()

def reading(city):
    return R.ha_reading() if city.get("ha") else R.obs_reading(city["lat"], city["lon"])

def clear_air_filter_on(city):
    """Always on for views that ask for it (wide); otherwise on only when the
    city's own current temperature rules out snow."""
    if city.get("suppress_clear_air"):
        return True
    try:
        return float(reading(city)[0]) >= CLEAR_AIR_MIN_F
    except (TypeError, ValueError):
        return False            # no reading: keep the band rather than risk hiding snow

def build_radar(city, maps):
    lat, lon = city["lat"], city["lon"]
    suppress = clear_air_filter_on(city)
    bm   = R.darken_for_amoled(R.basemap(lat, lon, city))
    OW, OH = R.PANEL + 2 * R.ORBIT_PX, R.VIEW_H + 2 * R.ORBIT_PX
    base = R.decorate(bm.resize((OW, OH), R.Image.LANCZOS), city).convert("RGB")
    ox, oy = R.orbit_offset(int(time.time() // RADAR_S))

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
        layer = R.radar(host, f["path"], lat, lon, city)
        if R.is_watermark(layer):
            raise RuntimeError("RainViewer returned a watermark tile at zoom %d "
                               "-- the free tier caps at 7" % city.get("radar_zoom", R.RADAR_ZOOM))
        if suppress:
            layer = R.suppress_clear_air(layer)      # before resampling blends colours
        layer = layer.resize((OW, OH), R.Image.LANCZOS)
        try:
            mask = R.qc_mask(lat, lon, city, f["time"], qc_avail, OW, OH)
            if mask is not None:
                layer = R.apply_mask(layer, mask); qc_used += 1
        except Exception as e:
            print("[radar] %s NOAA QC frame failed (%s) -- left unmasked" % (city["id"], str(e)[:60]), flush=True)
        layers.append(layer); stamps.append(f["time"])

    # interpolate the radar layer only, then composite every layer the same way
    seq, times, keys = [], [], []
    for i, layer in enumerate(layers):
        keys.append(len(seq)); seq.append(layer); times.append(stamps[i])
        if i + 1 < len(layers) and TWEENS > 0:
            n = R.tween_count(stamps[i + 1] - stamps[i], TWEENS)
            for k, tw in enumerate(R.tweens(layer, layers[i + 1], n, TWEEN_MODE)):
                seq.append(tw)
                times.append(stamps[i] + (stamps[i + 1] - stamps[i]) * (k + 1) / (n + 1.0))

    pts = R.place_pixels(city.get("places", []), lat, lon, OW, OH, city)
    out = []
    t_first, t_last = times[0], times[-1]
    left, right = clock(t_first, city), clock(t_last, city)
    for layer, t in zip(seq, times):
        frame = base.copy(); frame.paste(layer, (0, 0), layer)
        R.draw_places(frame, pts, crosshair=not city.get("wide"))
        frame = frame.crop((R.ORBIT_PX + ox, R.ORBIT_PX + oy,
                            R.ORBIT_PX + ox + R.PANEL, R.ORBIT_PX + oy + R.VIEW_H))
        frac = (t - t_first) / float(t_last - t_first) if t_last > t_first else 1.0
        out.append(_jpeg(R.progress_bar(frame, frac, left, right, ox, oy)))
    return out, [int(t) for t in times], keys, {"clear_air": suppress, "qc_masked": qc_used, "real_frames": len(layers)}

_temps = {}

def build_status(city):
    temp, hum = reading(city)
    _temps[city["id"]] = temp
    stamp = clock(time.time(), city)
    if city.get("status_label"):          # whose reading this is, when it isn't the view's
        stamp = "%s \u00b7 %s" % (city["status_label"], stamp)
    return _jpeg(R.status_strip(temp, hum, stamp, city["name"]))

def radar_loop():
    while True:
        try:
            maps = json.loads(R.fetch("https://api.rainviewer.com/public/weather-maps.json"))
        except Exception as e:
            maps = None
            print("[radar] RainViewer index FAILED: %s" % e, flush=True)
        for c in CITIES:
            if maps is None:
                with _lock: _state[c["id"]]["radar_err"] = "RainViewer index unavailable"
                continue
            try:
                frames, times, keys, info = build_radar(c, maps)
                with _lock:
                    _state[c["id"]].update(loop_id=int(time.time()), frames=frames, times=times,
                                           keys=keys, clear_air_filtered=info["clear_air"],
                                           qc_masked="%d/%d" % (info["qc_masked"], info["real_frames"]),
                                           built=int(time.time()), radar_err=None)
                print("[radar] %s ok (%d frames)" % (c["id"], len(frames)), flush=True)
            except Exception as e:
                with _lock: _state[c["id"]]["radar_err"] = str(e)[:200]
                print("[radar] %s FAILED: %s" % (c["id"], e), flush=True)
        time.sleep(RADAR_S)

def status_loop():
    while True:
        for c in CITIES:
            try:
                v = build_status(c)
                with _lock: _state[c["id"]].update(status=v, status_built=int(time.time()), status_err=None)
            except Exception as e:
                with _lock: _state[c["id"]]["status_err"] = str(e)[:200]
                print("[status] %s FAILED: %s" % (c["id"], e), flush=True)
        time.sleep(STATUS_S)

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        # a client hanging up mid-response (health checks, a device losing WiFi)
        # is routine -- don't dump a traceback into the container log for it
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

def device_state():
    """What the panel should do. Occupancy off for VACANT_OFF_S -> off. Brightness
    follows the room: the panel is never brighter than the room needs, which is the
    second-biggest burn-in lever after not being on at all."""
    occ, since = R.ha_entity(OCC_ENTITY)
    lux_s, _ = R.ha_entity(LUX_ENTITY)
    try: lux = float(lux_s)
    except (TypeError, ValueError): lux = None
    display, reason = "on", "occupied"
    if occ == "off" and since and time.time() - since >= VACANT_OFF_S:
        display, reason = "off", "room empty %d min" % ((time.time() - since) // 60)
    elif occ is None:
        reason = "occupancy unknown -- staying on"
    # Floor raised from 50 to 140 after the owner found 69/255 (35 lx room) too dark.
    # Occupancy blanking is the main burn-in protection; brightness is secondary.
    lo, hi, k = BRIGHT_MIN, BRIGHT_MAX, BRIGHT_PER_LUX
    bright = int((lo + hi) / 2) if lux is None else int(max(lo, min(hi, lo + lux * k)))
    return {"display": display, "brightness": bright, "reason": reason,
            "lux": lux, "occupancy": occ}

def firmware_info():
    try:
        ver = open(os.path.join(FIRMWARE_DIR, "version.txt")).read().strip()
        path = os.path.join(FIRMWARE_DIR, "firmware.bin")
        data = open(path, "rb").read()
        return {"version": ver, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except OSError:
        return None

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), code=code)

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/") or "/"
        q = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
        if p == "/device.json":
            return self._json(device_state())
        if p == "/ui/picker.jpg":
            try: hl = int(q.get("hl", "0"))
            except ValueError: hl = 0
            entries = [(c["id"], c["name"], _temps.get(c["id"])) for c in CITIES]
            return self._send(_jpeg(R.picker_panel(entries, hl % len(entries), q.get("cur", ""))), "image/jpeg")
        if p == "/ui/hold.jpg":
            return self._send(_jpeg(R.hold_pill()), "image/jpeg")
        if p == "/firmware.json":
            fi = firmware_info()
            return self._json(fi if fi else {"error": "no firmware published"}, 200 if fi else 404)
        if p == "/firmware.bin":
            try: return self._send(open(os.path.join(FIRMWARE_DIR, "firmware.bin"), "rb").read(), "application/octet-stream")
            except OSError: return self._json({"error": "no firmware published"}, 404)
        if p in ("/", "/cities.json"):
            return self._json([dict({k: c[k] for k in ("id", "name", "lat", "lon")},
                                    default=bool(c.get("default"))) for c in CITIES])
        if p == "/healthz":
            with _lock:
                per = {cid: {"frames": len(st["frames"]), "status": bool(st["status"]),
                             "radar_err": st["radar_err"], "status_err": st["status_err"]}
                       for cid, st in _state.items()}
            ok = all(v["frames"] and v["status"] for v in per.values())
            return self._json({"ok": ok, "cities": per}, 200 if ok else 503)

        parts = p.split("/")          # ['', 'c', '<id>', ...]
        if len(parts) >= 4 and parts[1] == "c" and parts[2] in BY_ID:
            with _lock: st = dict(_state[parts[2]])
            rest = "/".join(parts[3:])
            if rest == "manifest.json":
                return self._json({
                    "city": parts[2], "loop_id": st["loop_id"], "frames": len(st["frames"]),
                    "w": R.PANEL, "h": R.PANEL, "view_h": R.VIEW_H, "status_h": R.STATUS_H,
                    "built": st["built"], "status_built": st["status_built"],
                    "sizes": [len(f) for f in st["frames"]], "times": st["times"], "keys": st["keys"],
                    "tweens_per_10min": TWEENS, "tween_mode": TWEEN_MODE,
                    "clear_air_filtered": st.get("clear_air_filtered"),
                    "qc_masked": st.get("qc_masked"),       # real frames masked by NOAA QC
                    "radar_err": st["radar_err"], "status_err": st["status_err"]})
            if rest == "status.jpg":
                if not st["status"]: return self._json({"error": "not ready"}, 503)
                return self._send(st["status"], "image/jpeg")
            if rest.startswith("frame/") and rest.endswith(".jpg"):
                try: i = int(rest[len("frame/"):-4])
                except ValueError: return self._json({"error": "bad index"}, 400)
                if not (0 <= i < len(st["frames"])): return self._json({"error": "out of range"}, 404)
                return self._send(st["frames"][i], "image/jpeg")
        self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    threading.Thread(target=radar_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    print("serving on :%d  cities=%s  (radar every %ds, status every %ds)"
          % (PORT, ",".join(c["id"] for c in CITIES), RADAR_S, STATUS_S), flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()
