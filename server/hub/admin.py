"""Admin page and its JSON API: the only way to change hub settings.

Served on its own port (ADMIN_PORT, 8081), which the stack exposes ONLY through the
DockTail VIP (https://displays.swallow-spectrum.ts.net) -- never on the LAN port the
displays use, so nothing on the LAN can read or change settings. Tailscale is the
access control; there is no separate login.

  GET    /                          the page (admin.html)
  GET    /api/state                 everything the page shows (secrets only as set / not set)
  POST   /api/secrets               {"hass_url": "...", ...}; null clears one; omitted = unchanged
  POST   /api/test/ha | opensky     try the connection
  GET    /api/ha/entities?domain=   entity ids for the pickers
  PUT    /api/places/<id>           create or replace a place      DELETE removes it
  PUT    /api/views/<id>            create or replace an aircraft view   DELETE removes it
  PUT    /api/devices/<id>          change a device's settings     DELETE forgets it
  GET    /api/devices/<id>/preview.png   what the device shows when it starts
"""
import json, os, re, time
from zoneinfo import ZoneInfo

from . import core, ha, settings

PAGE = os.path.join(os.path.dirname(__file__), "admin.html")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,12}$")      # store keys are "w:<id>", 16 bytes max

class Bad(Exception): pass

def _num(v, lo, hi, what):
    try: v = float(v)
    except (TypeError, ValueError): raise Bad("%s must be a number" % what)
    if not lo <= v <= hi: raise Bad("%s must be between %g and %g" % (what, lo, hi))
    return v

def _clean_place(pid, d):
    if not _SLUG.match(pid): raise Bad("id: lowercase letters, digits and -, at most 13")
    out = {"id": pid, "name": str(d.get("name", "")).strip()[:24] or pid,
           "lat": _num(d.get("lat"), -85, 85, "latitude"), "lon": _num(d.get("lon"), -180, 180, "longitude")}
    try: ZoneInfo(d.get("tz", "")); out["tz"] = d["tz"]
    except Exception: raise Bad("time zone: an IANA name such as America/Chicago")
    t = d.get("temp") or {}
    if t.get("source") == "ha":
        if not (t.get("temp") and t.get("hum")): raise Bad("temperature from HA needs both entities")
        out["temp"] = {"source": "ha", "temp": t["temp"], "hum": t["hum"]}
    else:
        out["temp"] = {"source": "nws"}
    towns = []
    for row in d.get("places", []):
        if not (isinstance(row, list) and len(row) >= 3): raise Bad("towns: name, lat, lon per line")
        town = [str(row[0])[:24], _num(row[1], -85, 85, "town latitude"), _num(row[2], -180, 180, "town longitude")]
        if len(row) > 3 and row[3]: town.append(1)
        towns.append(town)
    out["places"] = towns
    # advanced, as in the seeded regional view
    for k in ("wide", "suppress_clear_air"):
        if d.get(k): out[k] = True
    for k, lo, hi in (("radius_mi", 5, 500), ("lon_span", 0.5, 70)):
        if d.get(k) not in (None, ""): out[k] = _num(d[k], lo, hi, k)
    for k, lo, hi in (("base_zoom", 3, 12), ("radar_zoom", 2, 7), ("radar_tile", 256, 512)):
        if d.get(k) not in (None, ""): out[k] = int(_num(d[k], lo, hi, k))
    if d.get("status_label"): out["status_label"] = str(d["status_label"])[:24]
    return out

def _clean_view(vid, d):
    if not _SLUG.match(vid): raise Bad("id: lowercase letters, digits and -, at most 13")
    levels = sorted({int(_num(x, 2, 250, "zoom level")) for x in d.get("levels", [])}, reverse=True)
    if not 1 <= len(levels) <= 4: raise Bad("1 to 4 zoom levels (miles from centre to edge)")
    return {"id": vid, "name": str(d.get("name", "")).strip()[:24] or vid,
            "lat": _num(d.get("lat"), -85, 85, "latitude"), "lon": _num(d.get("lon"), -180, 180, "longitude"),
            "radius_mi": int(_num(d.get("radius_mi", levels[0]), 5, 250, "radius")), "levels": levels}

def _clean_device(d):
    out = {}
    if "name" in d: out["name"] = str(d["name"] or "").strip()[:24]
    if "apps" in d:
        out["apps"] = [a for a in d["apps"] if a in settings.APPS]
        if not out["apps"]: raise Bad("a device needs at least one app")
    for k in ("start_app", "weather", "aircraft"):
        if k in d: out[k] = d[k]
    if "screen" in d:
        s = d["screen"]
        out["screen"] = {"occupancy": str(s.get("occupancy") or ""), "lux": str(s.get("lux") or ""),
                         "vacant_off_min": _num(s.get("vacant_off_min", 5), 0, 1440, "minutes empty"),
                         "bright_min": int(_num(s.get("bright_min", 140), 1, 255, "minimum brightness")),
                         "bright_max": int(_num(s.get("bright_max", 255), 1, 255, "maximum brightness")),
                         "bright_per_lux": _num(s.get("bright_per_lux", 0.6), 0, 50, "brightness per lux")}
    return out

def _state():
    now = time.time()
    devs = []
    for d in settings.devices():
        s = core.seen(d["id"])
        devs.append(dict(d, ip=s.get("ip"), last_seen=s.get("last_seen"),
                         online=bool(s.get("last_seen")) and now - s["last_seen"] < 90))
    return {"secrets": settings.secrets_set(), "places": settings.places(), "views": settings.air_views(),
            "devices": devs, "apps": list(settings.APPS)}

def _preview(dev, apps):
    """PNG of what the device shows when it starts, rendered at its own profile."""
    pr = dev.get("profile") or {}
    w, hgt, r, panel = pr.get("w", "480"), pr.get("h", "480"), pr.get("r", "56"), pr.get("panel", "amoled")
    by_id = {a.ID: a for a in apps}
    if dev["start_app"] == "aircraft":
        v = settings.air_view(dev["aircraft"]["view"])
        if not v: return None
        return by_id["aircraft"].preview_png(v, int(w), int(hgt), int(r), dev["aircraft"]["start_level"])
    return by_id["weather"].preview_png(dev["weather"]["start"], w, hgt, r, panel)

def handle(h, method, p, q, body, apps):
    """Serve an admin path; False if it isn't one. Errors come back as {"error": ...}."""
    try:
        if method == "GET" and p == "/":
            return h.send(open(PAGE, "rb").read(), "text/html; charset=utf-8")
        if not p.startswith("/api/"):
            return False
        parts = p.split("/")[2:]                    # after /api/
        data = json.loads(body or b"{}") if method in ("POST", "PUT") else {}
        if method == "GET" and parts == ["state"]:
            return h.json(_state())
        if method == "POST" and parts == ["secrets"]:
            for k, v in data.items():
                if k not in settings.SECRET_KEYS: raise Bad("unknown setting %s" % k)
                settings.set_secret(k, (v or "").strip() if v is not None else "")
            return h.json(_state())
        if method == "POST" and parts[:1] == ["test"] and len(parts) == 2:
            if parts[1] == "ha": ok, msg = ha.test()
            elif parts[1] == "opensky":
                from .aircraft import opensky
                ok, msg = opensky.test()
            else: return False
            return h.json({"ok": ok, "message": msg})
        if method == "GET" and parts == ["ha", "entities"]:
            return h.json(ha.entities(q.get("domain") or None))
        if len(parts) == 2 and parts[0] in ("places", "views"):
            put, delete, clean = ((settings.put_place, settings.del_place, _clean_place) if parts[0] == "places"
                                  else (settings.put_view, settings.del_view, _clean_view))
            if method == "PUT": put(clean(parts[1], data)); return h.json(_state())
            if method == "DELETE": delete(parts[1]); return h.json(_state())
        if len(parts) >= 2 and parts[0] == "devices":
            dev_id = parts[1]
            if dev_id not in {d["id"] for d in settings.devices()}:
                return h.json({"error": "unknown device"}, 404)
            if method == "PUT" and len(parts) == 2:
                settings.put_device(dev_id, _clean_device(data)); return h.json(_state())
            if method == "DELETE" and len(parts) == 2:
                settings.forget(dev_id); return h.json(_state())
            if method == "GET" and parts[2:] == ["preview.png"]:
                png = _preview(settings.device(dev_id), apps)
                return h.send(png, "image/png") if png else h.json({"error": "nothing to show yet"}, 503)
        return h.json({"error": "not found"}, 404)
    except Bad as e:
        return h.json({"error": str(e)}, 400)
    except (ValueError, KeyError) as e:
        return h.json({"error": "bad request: %s" % e}, 400)
