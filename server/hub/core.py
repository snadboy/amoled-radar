"""Device-level endpoints, shared by every app.

  /device/hello?id=<mac>&board=&w=&h=&r=&panel=&psram=&slot=&fw=
                         register a device profile; returns its apps and views
  /device/<id>/state     screen on/off + brightness (same rules for every app)
  /device/<id>/name?set=<name>   name a device; it shows the name on its identity card
  /devices.json          registered devices, for humans
  /firmware.json, /firmware.bin   OTA, legacy channel (amoled-radar firmware)
  /firmware/<channel>.json|.bin   OTA per channel (hub firmware: "hub")
  /device.json           legacy alias of /device/<id>/state (amoled-radar firmware)
"""
import hashlib, json, os, re, threading, time

from .ha import ha_entity

CACHE = os.environ.get("HUB_CACHE", os.environ.get("RADAR_CACHE", "/tmp/hub-cache"))

# Screen control, decided here so the board never needs an HA token. The env names
# keep their RADAR_ prefix so an existing .env carries over unchanged.
OCC_ENTITY   = os.environ.get("RADAR_OCC_ENTITY", "binary_sensor.upstairs_office_lwr02_occupancy")
LUX_ENTITY   = os.environ.get("RADAR_LUX_ENTITY", "sensor.upstairs_office_lwr02_illuminance")
VACANT_OFF_S = int(os.environ.get("RADAR_VACANT_OFF_S", "300"))   # empty this long -> screen off
BRIGHT_MIN     = int(os.environ.get("RADAR_BRIGHT_MIN", "140"))     # 0-255 panel brightness
BRIGHT_MAX     = int(os.environ.get("RADAR_BRIGHT_MAX", "255"))
BRIGHT_PER_LUX = float(os.environ.get("RADAR_BRIGHT_PER_LUX", "0.6"))
FIRMWARE_DIR = os.environ.get("RADAR_FIRMWARE_DIR", os.path.join(CACHE, "firmware"))

PROFILE_KEYS = ("board", "w", "h", "r", "panel", "psram", "slot", "fw")
DEVICES_FILE = os.path.join(CACHE, "devices.json")
_ID_OK = re.compile(r"^[0-9A-Za-z:_.-]{1,40}$")

_lock = threading.Lock()
try:
    _devices = json.load(open(DEVICES_FILE))
except (OSError, ValueError):
    _devices = {}

def _save():
    os.makedirs(CACHE, exist_ok=True)
    tmp = DEVICES_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(_devices, f, indent=1, sort_keys=True)
    os.replace(tmp, DEVICES_FILE)

def _seen(dev_id, ip, profile=None):
    with _lock:
        d = _devices.setdefault(dev_id, {})
        d.update(last_seen=int(time.time()), ip=ip)
        if profile is not None and d.get("profile") != profile:
            d["profile"] = profile
            _save()

def device_name(dev_id):
    """Its given name, else 'Display' + the last 4 hex digits of its MAC."""
    with _lock: name = _devices.get(dev_id, {}).get("name")
    return name or "Display " + dev_id.replace(":", "")[-4:].upper()

def device_state():
    """What the panel should do. Occupancy off for VACANT_OFF_S -> off. Brightness
    follows the room: the panel is never brighter than the room needs, which is the
    second-biggest burn-in lever after not being on at all."""
    occ, since = ha_entity(OCC_ENTITY)
    lux_s, _ = ha_entity(LUX_ENTITY)
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

def _fw_dir(channel):
    """The legacy root channel is FIRMWARE_DIR itself (amoled-radar firmware); named
    channels live below it, so a build can't reach boards it isn't meant for."""
    return FIRMWARE_DIR if channel is None else os.path.join(FIRMWARE_DIR, channel)

def firmware_info(channel=None):
    try:
        ver = open(os.path.join(_fw_dir(channel), "version.txt")).read().strip()
        data = open(os.path.join(_fw_dir(channel), "firmware.bin"), "rb").read()
        return {"version": ver, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except OSError:
        return None

def handle(h, p, q, apps):
    """Serve a core path; False if it isn't one."""
    ip = h.client_address[0]
    if p == "/device/hello":
        dev_id = q.get("id", "")
        if not _ID_OK.match(dev_id):
            return h.json({"error": "bad or missing id"}, 400)
        _seen(dev_id, ip, {k: q[k] for k in PROFILE_KEYS if k in q})
        return h.json({"id": dev_id, "name": device_name(dev_id), "default_app": apps[0].ID,
                       "apps": [{"id": a.ID, "views": a.views()} for a in apps]})
    parts = p.split("/")                         # ['', 'device', '<id>', 'state']
    if len(parts) == 4 and parts[1] == "device" and parts[3] == "state" and _ID_OK.match(parts[2]):
        _seen(parts[2], ip)
        return h.json(device_state())
    if len(parts) == 4 and parts[1] == "device" and parts[3] == "name" and _ID_OK.match(parts[2]):
        name = q.get("set", "").strip()[:24]
        with _lock:
            if parts[2] not in _devices: return h.json({"error": "unknown device"}, 404)
            if name: _devices[parts[2]]["name"] = name
            else: _devices[parts[2]].pop("name", None)
            _save()
        return h.json({"id": parts[2], "name": device_name(parts[2])})
    if p == "/device.json":                      # legacy amoled-radar firmware: no id, key by IP
        _seen("ip:" + ip, ip)
        return h.json(device_state())
    if p == "/devices.json":
        with _lock: return h.json(_devices)
    # /firmware.{json,bin} (legacy root channel) and /firmware/<channel>.{json,bin}
    m = re.match(r"^/firmware(?:/([a-z0-9-]{1,24}))?\.(json|bin)$", p)
    if m:
        channel, ext = m.groups()
        if ext == "json":
            fi = firmware_info(channel)
            return h.json(fi if fi else {"error": "no firmware published"}, 200 if fi else 404)
        try: return h.send(open(os.path.join(_fw_dir(channel), "firmware.bin"), "rb").read(), "application/octet-stream")
        except OSError: return h.json({"error": "no firmware published"}, 404)
    return False
