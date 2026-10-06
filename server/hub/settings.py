"""Hub settings: everything that used to live in .env / Dockhand stack variables, in
one SQLite file on the cache volume, edited from the admin page.

  secrets    HA URL + token, OpenSky client id + secret. Write-only from the admin
             API: the page only ever learns whether each one is set.
  places     weather cities: name, position, time zone, temperature source, towns
  air_views  aircraft views: centre, radius, zoom levels
  devices    per device: name, apps, what it starts on, its room's screen policy
             (+ the profile it reported in /device/hello)

On first start the store is seeded from the old environment variables and code
defaults, so a hub upgraded in place keeps working; after that the environment is
not read for any of this.
"""
import json, os, sqlite3, threading, zlib

CACHE = os.environ.get("HUB_CACHE", os.environ.get("RADAR_CACHE", "/tmp/hub-cache"))
DB_PATH = os.path.join(CACHE, "hub.db")

SECRET_KEYS = ("hass_url", "hass_token", "opensky_client_id", "opensky_client_secret",
               "mqtt_url", "mqtt_user", "mqtt_password")
APPS = ("weather", "aircraft", "airlist")    # in firmware order: BOOT steps through them

# A room's screen policy, used for any device that doesn't set its own.
DEFAULT_SCREEN = {
    "mode": "auto",               # auto = the room's sensors decide; on / off = forced (e.g. from HA)
    "occupancy": "binary_sensor.upstairs_office_lwr02_occupancy",
    "lux": "sensor.upstairs_office_lwr02_illuminance",
    "vacant_off_min": 5,          # room empty this long -> screen off
    "bright_min": 140,            # 0-255 panel brightness; 69/255 in a 35 lx room was too dark
    "bright_max": 255,
    "bright_per_lux": 0.6,
}

# Weather cities shipped with the code (seed only). Centres are public city centres.
DEFAULT_PLACES = [
    {"id": "geneva",  "name": "Geneva",    "lat": 41.8875, "lon": -88.3054, "tz": "America/Chicago",
     "temp": {"source": "ha", "temp": "sensor.outdoor_temperature", "hum": "sensor.outdoor_humidity"},
     "places": [["Chicago", 41.8781, -87.6298], ["Rockford", 42.2711, -89.0940],
                ["Joliet", 41.5250, -88.0817], ["DeKalb", 41.9295, -88.7504]]},
    {"id": "stlouis", "name": "St. Louis", "lat": 38.6270, "lon": -90.1994, "tz": "America/Chicago",
     "temp": {"source": "nws"},
     "places": [["St. Charles", 38.7881, -90.4974], ["Alton", 38.8906, -90.1843],
                ["Belleville", 38.5201, -89.9840], ["Festus", 38.2206, -90.3960]]},
    # Toledo is the obvious "south" town for Canton but lands on the progress bar
    {"id": "canton",  "name": "Canton",    "lat": 42.3087, "lon": -83.4822, "tz": "America/Detroit",
     "temp": {"source": "nws"},
     "places": [["Detroit", 42.3314, -83.0458], ["Ann Arbor", 42.2808, -83.7430],
                ["Pontiac", 42.6389, -83.2910], ["Monroe", 41.9164, -83.3977]]},
    # Regional view framing all three cities (St. Louis .. Canton, ~580 mi across). The lower
    # 48 at ~9 km/px was too small to read on a 2.16" panel. A 4th town element of 1 marks
    # one of your own cities (amber marker). "wide" = no rings or crosshair.
    {"id": "midwest", "name": "Midwest", "lat": 40.5, "lon": -86.8, "tz": "America/Chicago",
     "temp": {"source": "ha", "temp": "sensor.outdoor_temperature", "hum": "sensor.outdoor_humidity"},
     "status_label": "Geneva", "wide": True, "suppress_clear_air": True,
     "lon_span": 10.4, "base_zoom": 7, "radar_zoom": 5, "radar_tile": 512,
     "places": [["Geneva", 41.8875, -88.3054, 1], ["St. Louis", 38.6270, -90.1994, 1],
                ["Canton", 42.3087, -83.4822, 1], ["Milwaukee", 43.0389, -87.9065],
                ["Indianapolis", 39.7684, -86.1581], ["Fort Wayne", 41.0793, -85.1394],
                ["Louisville", 38.2527, -85.7585]]},
]

_lock = threading.RLock()
_db = None

def _conn():
    global _db
    if _db is None:
        os.makedirs(CACHE, exist_ok=True)
        new = not os.path.exists(DB_PATH)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS secrets   (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS places    (id TEXT PRIMARY KEY, pos INTEGER, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS air_views (id TEXT PRIMARY KEY, pos INTEGER, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS devices   (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta      (key TEXT PRIMARY KEY, value TEXT);
        """)
        os.chmod(DB_PATH, 0o600)
        if new:
            _seed()
    return _db

def _env(*names, default=""):
    for n in names:
        if os.environ.get(n): return os.environ[n]
    return default

def _seed():
    """First start: carry over the old environment variables and defaults."""
    db = _db
    for key, env in (("hass_url", "HASS_SERVER"), ("hass_token", "HASS_TOKEN"),
                     ("opensky_client_id", "OPENSKY_CLIENT_ID"), ("opensky_client_secret", "OPENSKY_CLIENT_SECRET")):
        if os.environ.get(env):
            db.execute("INSERT INTO secrets VALUES (?, ?)", (key, os.environ[env]))
    places = json.loads(os.environ["RADAR_CITIES"]) if os.environ.get("RADAR_CITIES") else DEFAULT_PLACES
    for i, p in enumerate(places):
        p = dict(p)
        if "temp" not in p:            # RADAR_CITIES used "ha": true
            p["temp"] = ({"source": "ha", "temp": _env("RADAR_TEMP_ENTITY", default="sensor.outdoor_temperature"),
                          "hum": _env("RADAR_HUM_ENTITY", default="sensor.outdoor_humidity")}
                         if p.pop("ha", False) else {"source": "nws"})
        p.pop("default", None)
        db.execute("INSERT INTO places VALUES (?, ?, ?)", (p["id"], i, json.dumps(p)))
    views = json.loads(os.environ["AIR_VIEWS"]) if os.environ.get("AIR_VIEWS") else [
        {"id": "home", "name": "Home", "lat": float(_env("AIR_LAT", default="41.8875")),
         "lon": float(_env("AIR_LON", default="-88.3054")), "radius_mi": 50, "levels": [50, 25, 10]}]
    for i, v in enumerate(views):
        db.execute("INSERT INTO air_views VALUES (?, ?, ?)", (v["id"], i, json.dumps(v)))
    screen = dict(DEFAULT_SCREEN)
    for k, env, cast in (("occupancy", "RADAR_OCC_ENTITY", str), ("lux", "RADAR_LUX_ENTITY", str),
                         ("bright_min", "RADAR_BRIGHT_MIN", int), ("bright_max", "RADAR_BRIGHT_MAX", int),
                         ("bright_per_lux", "RADAR_BRIGHT_PER_LUX", float)):
        if os.environ.get(env): screen[k] = cast(os.environ[env])
    if os.environ.get("RADAR_VACANT_OFF_S"): screen["vacant_off_min"] = int(os.environ["RADAR_VACANT_OFF_S"]) // 60
    try:                                # the old JSON device registry (names, profiles)
        for dev_id, d in json.load(open(os.path.join(CACHE, "devices.json"))).items():
            if dev_id.startswith("ip:"): continue
            doc = {k: d[k] for k in ("name", "profile") if k in d}
            doc["screen"] = screen
            db.execute("INSERT INTO devices VALUES (?, ?)", (dev_id, json.dumps(doc)))
    except (OSError, ValueError):
        pass
    db.execute("INSERT OR REPLACE INTO meta VALUES ('default_screen', ?)", (json.dumps(screen),))
    db.commit()
    print("[settings] new store seeded: %d places, %d aircraft views" % (len(places), len(views)), flush=True)

# ---------------------------------------------------------------- secrets
def secret(key):
    with _lock:
        row = _conn().execute("SELECT value FROM secrets WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""

def secrets_set():
    """Which secrets are set -- never their values."""
    with _lock:
        have = {r[0] for r in _conn().execute("SELECT key FROM secrets WHERE value != ''")}
    return {k: k in have for k in SECRET_KEYS}

def set_secret(key, value):
    if key not in SECRET_KEYS: raise KeyError(key)
    with _lock:
        db = _conn()
        if value: db.execute("INSERT OR REPLACE INTO secrets VALUES (?, ?)", (key, value))
        else: db.execute("DELETE FROM secrets WHERE key=?", (key,))
        db.commit()

# ---------------------------------------------------------------- places / aircraft views
def _docs(table):
    with _lock:
        return [json.loads(r[0]) for r in _conn().execute("SELECT doc FROM %s ORDER BY pos, id" % table)]

def _put(table, doc):
    with _lock:
        db = _conn()
        pos = db.execute("SELECT pos FROM %s WHERE id=?" % table, (doc["id"],)).fetchone()
        if pos is None: pos = (db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM %s" % table).fetchone()[0],)
        db.execute("INSERT OR REPLACE INTO %s VALUES (?, ?, ?)" % table, (doc["id"], pos[0], json.dumps(doc)))
        db.commit()

def _delete(table, doc_id):
    with _lock:
        db = _conn(); db.execute("DELETE FROM %s WHERE id=?" % table, (doc_id,)); db.commit()

# "active" is built in: the weather app moves it to the strongest storm near home (the
# first real place) every radar cycle; it is never stored and can't be edited.
ACTIVE = "active"
AIR_KINDS = ("airline", "business", "private", "other")     # aircraft.lookup.KINDS

def places():
    real = _docs("places")
    home = real[0] if real else {"lat": 41.9, "lon": -88.3, "tz": "America/Chicago"}
    return real + [{"id": ACTIVE, "name": "Active storm", "auto": "storm", "lat": home["lat"], "lon": home["lon"],
                    "tz": home.get("tz", "America/Chicago"), "temp": {"source": "nws"}, "places": []}]
air_views   = lambda: _docs("air_views")
put_place   = lambda doc: _put("places", doc)
put_view    = lambda doc: _put("air_views", doc)
del_place   = lambda pid: _delete("places", pid)
del_view    = lambda vid: _delete("air_views", vid)

def place(pid):
    return next((p for p in places() if p["id"] == pid), None)

def air_view(vid):
    return next((v for v in air_views() if v["id"] == vid), None)

# ---------------------------------------------------------------- devices
def default_screen():
    with _lock:
        row = _conn().execute("SELECT value FROM meta WHERE key='default_screen'").fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT_SCREEN)

def _raw_device(dev_id):
    with _lock:
        row = _conn().execute("SELECT doc FROM devices WHERE id=?", (dev_id,)).fetchone()
    return json.loads(row[0]) if row else None

def device(dev_id):
    """A device's effective settings: what it stored, filled in with defaults (and
    pruned of places / views that no longer exist)."""
    d = _raw_device(dev_id) or {}
    pl, av = [p["id"] for p in places()], [v["id"] for v in air_views()]
    pl_default = [p for p in pl if p != ACTIVE] or pl     # new devices: the real cities
    apps = [a for a in d.get("apps", list(APPS)) if a in APPS] or list(APPS)
    w = d.get("weather", {}); a = d.get("aircraft", {})
    wp = [p for p in w.get("places", pl_default) if p in pl] or pl[:1]
    view = a.get("view") if a.get("view") in av else (av[0] if av else None)
    levels = (air_view(view) or {}).get("levels", [50])
    pr = d.get("profile", {})
    try: small = int(pr.get("w", 480)) < 320
    except ValueError: small = False
    small_labels, small_trail = (5, 0) if small else (10, 60)
    try: is_round = int(pr.get("r", 0)) * 2 >= min(int(pr.get("w", 480)), int(pr.get("h", 480)))
    except ValueError: is_round = False
    return {
        "id": dev_id,
        "name": d.get("name") or "Display " + dev_id.replace(":", "")[-4:].upper(),
        "named": bool(d.get("name")),
        "apps": apps,
        "start_app": d.get("start_app") if d.get("start_app") in apps else apps[0],
        "weather": {"places": wp, "start": w.get("start") if w.get("start") in wp else (wp[0] if wp else None),
                    # the strip under the radar (temperature, humidity, city, time); off by
                    # default on round glass, where it would eat the bottom of the circle
                    "strip": bool(w["strip"]) if "strip" in w else not is_round},
        "aircraft": {"view": view, "start_level": max(0, min(int(a.get("start_level", 0)), len(levels) - 1)),
                     # callsign + altitude beside each plane only at this zoom (mi) or closer;
                     # 0 = never (a tapped plane still gets its label and details)
                     "labels_mi": int(a["labels_mi"]) if str(a.get("labels_mi", "")).isdigit() else small_labels,
                     # trail behind each unselected plane, seconds (0 = none; a tapped plane shows its whole trail)
                     "trail_s": int(a["trail_s"]) if str(a.get("trail_s", "")).isdigit() else small_trail,
                     # which flights to show (aircraft.lookup.kind): airline, business, private, other
                     "types": [k for k in a.get("types", AIR_KINDS) if k in AIR_KINDS] or list(AIR_KINDS)},
        # Aircraft listing: the flights nearest an aircraft view's centre
        "airlist": {"view": d.get("airlist", {}).get("view") if d.get("airlist", {}).get("view") in av else view,
                    "radius_mi": max(5, min(100, int(d.get("airlist", {}).get("radius_mi", 50)))),
                    "types": [k for k in d.get("airlist", {}).get("types", AIR_KINDS) if k in AIR_KINDS] or list(AIR_KINDS)},
        "screen": dict(default_screen(), **d.get("screen", {})),
        "profile": d.get("profile", {}),
    }

def devices():
    with _lock:
        ids = [r[0] for r in _conn().execute("SELECT id FROM devices ORDER BY id")]
    return [device(i) for i in ids]

def put_device(dev_id, changes):
    """Merge admin-page changes into a device's stored settings."""
    with _lock:
        d = _raw_device(dev_id)
        if d is None: raise KeyError(dev_id)
        for k in ("name", "apps", "start_app", "weather", "aircraft", "airlist", "screen"):
            if k in changes: d[k] = changes[k]
        if not d.get("name"): d.pop("name", None)
        db = _conn(); db.execute("INSERT OR REPLACE INTO devices VALUES (?, ?)", (dev_id, json.dumps(d))); db.commit()

def register(dev_id, profile):
    """/device/hello: remember the profile; a new device gets default settings."""
    with _lock:
        d = _raw_device(dev_id)
        if d is not None and d.get("profile") == profile: return
        d = d or {}
        d["profile"] = profile
        db = _conn(); db.execute("INSERT OR REPLACE INTO devices VALUES (?, ?)", (dev_id, json.dumps(d))); db.commit()

def forget(dev_id):
    _delete("devices", dev_id)

def boot_version(dev):
    """Fingerprint of everything a device reads only at boot (apps, starting points and
    the places / views it shows). When it changes, the device restarts to pick it up;
    screen policy and the name apply live and are left out."""
    used = {"apps": dev["apps"], "start_app": dev["start_app"], "weather": dev["weather"], "aircraft": dev["aircraft"],
            "airlist": dev["airlist"], "airlist_view": air_view(dev["airlist"]["view"]),
            "places": [place(p) for p in dev["weather"]["places"]], "view": air_view(dev["aircraft"]["view"])}
    return zlib.crc32(json.dumps(used, sort_keys=True).encode()) & 0x7FFFFFFF

def places_in_use():
    """Places some device shows (all of them when no device is registered yet)."""
    devs = devices()
    if not devs: return places()
    want = {p for d in devs if "weather" in d["apps"] for p in d["weather"]["places"]}
    return [p for p in places() if p["id"] in want]

def views_in_use():
    devs = devices()
    if not devs: return air_views()
    want = {d["aircraft"]["view"] for d in devs if "aircraft" in d["apps"]} | \
           {d["airlist"]["view"] for d in devs if "airlist" in d["apps"]}
    return [v for v in air_views() if v["id"] in want]
