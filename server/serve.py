#!/usr/bin/env python3
"""HTTP service feeding the ESP32-C6 AMOLED radar display.

Two independent refresh loops, because they have very different natural rates:

  * radar  -- every RADAR_REFRESH_S (default 600s, matching RainViewer's cadence)
  * status -- every STATUS_REFRESH_S (default 60s), so the outdoor temperature is
              never up to 10 minutes stale. This is the whole reason the status
              strip is served separately instead of being burned into each frame.

Everything is held in memory; nothing touches disk except the one-time basemap
cache. The device fetches /manifest.json, notices loop_id changed, pulls the
frames into its 16MB flash, and animates locally.
"""
import io, json, os, threading, time
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
TWEEN_MODE = os.environ.get("RADAR_TWEEN_MODE", "motion")   # or "blend"

_lock   = threading.Lock()
_state  = {"loop_id": 0, "frames": [], "status": b"", "built": 0, "status_built": 0,
           "radar_err": None, "status_err": None}

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=QUALITY, optimize=True); return b.getvalue()

def build_radar():
    """Render the loop as radar-only frames (no status strip burned in)."""
    bm   = R.darken_for_amoled(R.basemap())
    OW, OH = R.PANEL + 2*R.ORBIT_PX, R.VIEW_H + 2*R.ORBIT_PX
    base = R.decorate(bm.resize((OW, OH), R.Image.LANCZOS)).convert("RGB")
    ox, oy = R.orbit_offset(int(time.time() // RADAR_S))

    maps = json.loads(R.fetch("https://api.rainviewer.com/public/weather-maps.json"))
    host = maps["host"]
    want = (maps["radar"]["past"] + maps["radar"].get("nowcast", []))[-N_FRAMES:]

    layers, stamps = [], []
    for f in want:
        layer = R.radar(host, f["path"])
        if R.is_watermark(layer):
            raise RuntimeError("RainViewer returned a watermark tile at zoom %d "
                               "-- the free tier caps at 7" % R.RADAR_ZOOM)
        layers.append(layer.resize((OW, OH), R.Image.LANCZOS)); stamps.append(f["time"])

    # interpolate the radar layer only, then composite every layer the same way
    seq, times, keys = [], [], []
    for i, layer in enumerate(layers):
        keys.append(len(seq)); seq.append(layer); times.append(stamps[i])
        if i + 1 < len(layers) and TWEENS > 0:
            n = R.tween_count(stamps[i + 1] - stamps[i], TWEENS)
            for k, tw in enumerate(R.tweens(layer, layers[i + 1], n, TWEEN_MODE)):
                seq.append(tw)
                times.append(stamps[i] + (stamps[i + 1] - stamps[i]) * (k + 1) / (n + 1.0))

    out = []
    for layer in seq:
        frame = base.copy(); frame.paste(layer, (0, 0), layer)
        frame = frame.crop((R.ORBIT_PX + ox, R.ORBIT_PX + oy,
                            R.ORBIT_PX + ox + R.PANEL, R.ORBIT_PX + oy + R.VIEW_H))
        out.append(_jpeg(frame))
    return out, {"times": [int(t) for t in times], "keys": keys}

def build_status():
    temp, hum = R.ha_reading()
    return _jpeg(R.status_strip(temp, hum, time.strftime("%-I:%M %p")))

def _loop(name, fn, period, apply_fn):
    while True:
        try:
            v = fn()
            with _lock: apply_fn(v)
            print("[%s] ok" % name, flush=True)
        except Exception as e:
            with _lock: _state["%s_err" % name] = str(e)[:200]
            print("[%s] FAILED: %s" % (name, e), flush=True)
        time.sleep(period)

def _apply_radar(v):
    frames, meta = v
    _state.update(loop_id=int(time.time()), frames=frames, times=meta["times"],
                  keys=meta["keys"], built=int(time.time()), radar_err=None)

def _apply_status(v):
    _state.update(status=v, status_built=int(time.time()), status_err=None)

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
    def do_GET(self):
        p = self.path.split("?")[0]
        with _lock: st = dict(_state)
        if p in ("/manifest.json", "/"):
            return self._send(json.dumps({
                "loop_id": st["loop_id"], "frames": len(st["frames"]),
                "w": R.PANEL, "h": R.PANEL, "view_h": R.VIEW_H, "status_h": R.STATUS_H,
                "built": st["built"], "status_built": st["status_built"],
                "sizes": [len(f) for f in st["frames"]],
                "times": st.get("times", []),
                "keys": st.get("keys", []),
                "tweens_per_10min": TWEENS, "tween_mode": TWEEN_MODE,
                "radar_err": st["radar_err"], "status_err": st["status_err"],
            }).encode())
        if p == "/status.jpg":
            if not st["status"]: return self._send(b'{"error":"not ready"}', code=503)
            return self._send(st["status"], "image/jpeg")
        if p.startswith("/frame/") and p.endswith(".jpg"):
            try: i = int(p[len("/frame/"):-4])
            except ValueError: return self._send(b'{"error":"bad index"}', code=400)
            if not (0 <= i < len(st["frames"])):
                return self._send(b'{"error":"out of range"}', code=404)
            return self._send(st["frames"][i], "image/jpeg")
        if p == "/healthz":
            ok = bool(st["frames"]) and bool(st["status"])
            return self._send(json.dumps({"ok": ok, "frames": len(st["frames"]),
                                          "radar_err": st["radar_err"],
                                          "status_err": st["status_err"]}).encode(),
                              code=200 if ok else 503)
        self._send(b'{"error":"not found"}', code=404)

if __name__ == "__main__":
    threading.Thread(target=_loop, args=("radar", build_radar, RADAR_S, _apply_radar), daemon=True).start()
    threading.Thread(target=_loop, args=("status", build_status, STATUS_S, _apply_status), daemon=True).start()
    print("serving on :%d  (radar every %ds, status every %ds, %d frames)"
          % (PORT, RADAR_S, STATUS_S, N_FRAMES), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
