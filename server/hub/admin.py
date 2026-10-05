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
  POST   /api/places                create a place (its id is made from the name)
  PUT    /api/places/<id>           change a place                 DELETE removes it
  POST   /api/views, PUT|DELETE /api/views/<id>    the same for aircraft views
  GET    /api/geocode?q=Geneva, IL  places matching a name (Open-Meteo): name, lat, lon, time zone
  GET    /api/towns?lat=&lon=&radius_mi=   towns worth labelling around a place (OpenStreetMap)
  PUT    /api/devices/<id>          change a device's settings     DELETE forgets it
  GET    /api/devices/<id>/preview.png   what the device shows when it starts
  GET    /api/firmware              CI releases, what each board's channel runs, device versions
  POST   /api/firmware/publish      {"board": "c6"|"p4", "tag": "firmware-<sha>"}
  GET    /install                   install page (ESP Web Tools: flash + WiFi over USB)
  GET    /install/manifest.json, /install/<board>-full.bin
"""
import json, os, re, time, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

from . import core, firmware, ha, settings

PAGE = os.path.join(os.path.dirname(__file__), "admin.html")
INSTALL = os.path.join(os.path.dirname(__file__), "install.html")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,12}$")      # store keys are "w:<id>", 16 bytes max

class Bad(Exception): pass

US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia"}

def geocode(q):
    """Places matching "Name" or "Name, region" (a US state code or name, a country...)."""
    name, _, where = (x.strip() for x in q.partition(","))
    if len(name) < 2: return []
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name, "count": 20 if where else 10, "language": "en", "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "snadboy-display-hub/1.0"})
    res = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("results") or []
    w = US_STATES.get(where.upper(), where).lower()
    out = []
    for r in res:
        region = ", ".join(x for x in (r.get("admin1"), r.get("country")) if x)
        if w and w not in region.lower() and w != (r.get("country_code") or "").lower(): continue
        out.append({"name": r["name"], "region": region, "lat": round(r["latitude"], 4), "lon": round(r["longitude"], 4),
                    "tz": r.get("timezone", ""), "population": r.get("population")})
    return out[:10]

def suggest_towns(lat, lon, radius_mi=50, n=4):
    """The biggest towns around a place, spread round the compass (labels bunched on one
    side collide on the radar), between a quarter and nine-tenths of the radius out."""
    import math
    from .aircraft import basemap
    towns = basemap.places(settings.CACHE, {"lat": lat, "lon": lon, "levels": [radius_mi]})
    picks = []
    for pop, name, tlat, tlon in towns:                  # biggest first
        dx = math.radians(tlon - lon) * math.cos(math.radians((tlat + lat) / 2))
        dy = math.radians(tlat - lat)
        dist, brg = math.hypot(dx, dy) * 3958.8, math.degrees(math.atan2(dx, dy)) % 360
        if not (radius_mi * 0.25 <= dist <= radius_mi * 0.9): continue
        if any(min(abs(brg - b), 360 - abs(brg - b)) < 60 for b in (p[3] for p in picks)): continue
        picks.append((name, round(tlat, 4), round(tlon, 4), brg))
        if len(picks) == n: break
    return [list(p[:3]) for p in picks]

def _new_id(name, taken):
    """A short, unique, stable id from a name ("St. Louis" -> "st-louis"). Ids are internal --
    the firmware's flash store keys are "w:<id>", so they stay within 13 characters."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:13].strip("-") or "place"
    cand, n = base, 2
    while cand in taken:
        sfx = "-%d" % n; cand = base[:13 - len(sfx)].rstrip("-") + sfx; n += 1
    return cand

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
        out["screen"] = {"mode": s.get("mode") if s.get("mode") in ("auto", "on", "off") else "auto",
                         "occupancy": str(s.get("occupancy") or ""), "lux": str(s.get("lux") or ""),
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
        devs.append(dict(d, ip=s.get("ip"), last_seen=s.get("last_seen"), online=core.online(d["id"]),
                         showing=dict(app=s.get("app"), view=s.get("view"), on=s.get("on"), bright=s.get("bright"))))
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
        if method == "GET" and p == "/install":
            return h.send(open(INSTALL, "rb").read(), "text/html; charset=utf-8")
        if method == "GET" and p == "/install/manifest.json":
            return h.json(firmware.manifest())
        m = re.match(r"^/install/(c6|p4)-full\.bin$", p)
        if method == "GET" and m:
            img = firmware.install_image(m.group(1))
            return h.send(img, "application/octet-stream") if img else h.json({"error": "no release yet"}, 404)
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
            v = lambda k: (data.get(k) or "").strip() or None     # typed on the page; blank = saved
            if parts[1] == "ha": ok, msg = ha.test(v("hass_url"), v("hass_token"))
            elif parts[1] == "mqtt":
                from . import mqtt
                ok, msg = mqtt.test(v("mqtt_url"), v("mqtt_user"), v("mqtt_password"))
            elif parts[1] == "opensky":
                from .aircraft import opensky
                ok, msg = opensky.test(v("opensky_client_id"), v("opensky_client_secret"))
            else: return False
            return h.json({"ok": ok, "message": msg})
        if method == "GET" and parts == ["firmware"]:
            rel = firmware.releases(refresh=q.get("refresh") == "1")
            fw = {}
            for d in settings.devices():
                board = "p4" if d["profile"].get("board", "").startswith("ws-p4") else "c6"
                fw.setdefault(board, []).append({"name": d["name"], "fw": d["profile"].get("fw", "")})
            return h.json({"releases": [{k: r[k] for k in ("tag", "version", "date")} for r in rel],
                           "boards": [dict(b, board=k, published=firmware.published(k), devices=fw.get(k, []))
                                      for k, b in firmware.BOARDS.items()]})
        if method == "POST" and parts == ["firmware", "publish"]:
            if data.get("board") not in firmware.BOARDS: raise Bad("board must be c6 or p4")
            try: ver = firmware.publish(data["board"], str(data.get("tag", "")))
            except ValueError as e: raise Bad(str(e))
            return h.json({"ok": True, "version": ver})
        if method == "GET" and parts == ["geocode"]:
            try: return h.json(geocode(q.get("q", "")))
            except Exception as e: return h.json({"error": "lookup failed: %s" % str(e)[:80]}, 502)
        if method == "GET" and parts == ["towns"]:
            try:
                return h.json(suggest_towns(_num(q.get("lat"), -85, 85, "latitude"), _num(q.get("lon"), -180, 180, "longitude"),
                                            _num(q.get("radius_mi", 50), 5, 500, "radius")))
            except Bad: raise
            except Exception as e: return h.json({"error": "town lookup failed: %s" % str(e)[:80]}, 502)
        if method == "POST" and len(parts) == 1 and parts[0] in ("places", "views"):
            existing = settings.places() if parts[0] == "places" else settings.air_views()
            new_id = _new_id(str(data.get("name", "")), {x["id"] for x in existing})
            if parts[0] == "places": settings.put_place(_clean_place(new_id, data))
            else: settings.put_view(_clean_view(new_id, data))
            return h.json(_state())
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
