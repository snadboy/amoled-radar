"""Device-level endpoints, shared by every app. Read-only for devices: settings are
changed on the admin page (admin.py), never over the LAN port.

  /device/hello?id=<mac>&board=&w=&h=&r=&panel=&psram=&slot=&fw=
                         register a device profile; returns its name, apps, the places
                         and view it shows, where it starts, and a boot-settings version
  /device/<id>/state?app=&view=&on=&bright=
                         the device reports what it shows; the reply has screen on/off +
                         brightness (room sensors, or forced by the screen mode), its name,
                         the boot-settings version (changed -> restart), the poll interval,
                         and at most one pending command (switch app/city, restart, identify),
                         delivered once -- a restart must not be re-sent to the rebooted device
  /devices.json          registered devices, for humans (no settings, no secrets)
  /firmware.json, /firmware.bin   OTA, legacy channel (amoled-radar firmware)
  /firmware/<channel>.json|.bin   OTA per channel ("hub" = C6 boards, "hub-p4" = P4)
  /device.json           legacy alias of /device/<id>/state (amoled-radar firmware)
"""
import hashlib, math, os, re, threading, time

from . import settings
from .ha import ha_entity

CACHE = settings.CACHE
FIRMWARE_DIR = os.environ.get("RADAR_FIRMWARE_DIR", os.path.join(CACHE, "firmware"))

PROFILE_KEYS = ("board", "w", "h", "r", "panel", "psram", "slot", "fw")
_ID_OK = re.compile(r"^[0-9A-Za-z:_.-]{1,40}$")

POLL_S = 5                      # device state poll: quick enough for HA controls to feel live

_lock = threading.Lock()
_seen = {}                      # device id -> {"last_seen", "ip", "app", "view", "on", "bright"}: in memory
_cmds = {}                      # device id -> pending command {"id", ...}; newest wins, delivered once
_cmd_id = int(time.time()) & 0xFFFFFF

def seen(dev_id):
    with _lock: return dict(_seen.get(dev_id, {}))

def _touch(dev_id, ip, q=None):
    with _lock:
        d = _seen.setdefault(dev_id, {})
        d.update(last_seen=int(time.time()), ip=ip)
        for k in ("app", "view"):
            if q and q.get(k): d[k] = q[k][:24]
        for k in ("on", "bright", "bmv", "chg", "usb"):
            if q and q.get(k, "").isdigit(): d[k] = int(q[k])
        if q and q.get("batt", "").lstrip("-").isdigit(): d["batt"] = int(q["batt"])   # -1: no battery

def online(dev_id):
    s = seen(dev_id)
    return bool(s.get("last_seen")) and time.time() - s["last_seen"] < 3 * POLL_S + 30

def command(dev_id, **cmd):
    """Queue a command for the device's next poll (merged into any still pending)."""
    global _cmd_id
    with _lock:
        _cmd_id += 1
        _cmds[dev_id] = dict(_cmds.get(dev_id, {}), id=_cmd_id, **cmd)

def profiles():
    """Display profiles of the registered devices (as reported in /device/hello)."""
    return [d["profile"] for d in settings.devices() if d.get("profile")]

def device_state(screen):
    """What the panel should do, from its room's sensors (a device's "screen" settings).
    Empty for vacant_off_min -> off. Brightness follows the room's light: the panel is
    never brighter than the room needs, the second-biggest burn-in lever after being off."""
    mode = screen.get("mode", "auto")
    occ, since = ha_entity(screen.get("occupancy")) if mode == "auto" else (None, None)
    lux_s, _ = ha_entity(screen.get("lux"))
    try: lux = float(lux_s)
    except (TypeError, ValueError): lux = None
    display, reason = "on", "occupied"
    if mode == "off":
        display, reason = "off", "screen mode off"
    elif mode == "on":
        reason = "screen mode on"
    elif occ == "off" and since and time.time() - since >= 60 * float(screen.get("vacant_off_min", 5)):
        display, reason = "off", "room empty %d min" % ((time.time() - since) // 60)
    elif occ is None:
        reason = "occupancy unknown -- staying on"
    lo, hi, k = int(screen["bright_min"]), int(screen["bright_max"]), float(screen["bright_per_lux"])
    bright = int((lo + hi) / 2) if lux is None else int(max(lo, min(hi, lo + lux * k)))
    return {"display": display, "brightness": max(1, min(255, bright)), "reason": reason,
            "lux": lux, "occupancy": occ}

def hello_reply(dev, apps):
    """A device's apps and settings as its firmware reads them."""
    by_id = {a.ID: a for a in apps}
    out = []
    for app_id in dev["apps"]:
        if app_id == "weather" and "weather" in by_id:
            out.append({"id": "weather", "views": [dict(by_id["weather"].view_summary(p, p["id"] == dev["weather"]["start"]),
                                                        strip=dev["weather"]["strip"])     # per view: the firmware's app API
                                                   for p in map(settings.place, dev["weather"]["places"]) if p]})
        elif app_id == "aircraft" and "aircraft" in by_id:
            v = settings.air_view(dev["aircraft"]["view"])
            out.append({"id": "aircraft", "views": [by_id["aircraft"].view_summary(v, dev["aircraft"]["start_level"],
                                                                                       dev["aircraft"]["labels_mi"], dev["aircraft"]["trail_s"],
                                                                                       dev["aircraft"]["types"])] if v else []})
        elif app_id == "airlist" and "airlist" in by_id:
            v = settings.air_view(dev["airlist"]["view"])
            out.append({"id": "airlist", "views": [by_id["airlist"].view_summary(v, dev["airlist"]["radius_mi"], dev["airlist"]["types"])] if v else []})
        elif app_id == "metra" and "metra" in by_id:
            out.append({"id": "metra", "views": [by_id["metra"].view_summary(dev["metra"])]})
    return {"id": dev["id"], "name": dev["name"], "default_app": dev["start_app"],
            "sv": settings.boot_version(dev), "apps": out}

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
        prof = {k: q[k] for k in PROFILE_KEYS if k in q}
        if "fw" in prof: prof["fw"] = prof["fw"].replace(" ", "+")     # "+" in a query string arrives as a space
        settings.register(dev_id, prof)
        _touch(dev_id, ip)
        return h.json(hello_reply(settings.device(dev_id), apps))
    parts = p.split("/")                         # ['', 'device', '<id>', 'state']
    if len(parts) == 4 and parts[1] == "device" and parts[3] == "state" and _ID_OK.match(parts[2]):
        _touch(parts[2], ip, q)
        dev = settings.device(parts[2])
        reply = dict(device_state(dev["screen"]), name=dev["name"], sv=settings.boot_version(dev), poll_s=POLL_S)
        with _lock:
            if parts[2] in _cmds: reply["cmd"] = _cmds.pop(parts[2])
        return h.json(reply)
    if p == "/device.json":                      # legacy amoled-radar firmware: no id
        return h.json(device_state(settings.default_screen()))
    if p == "/devices.json":
        return h.json({d["id"]: dict(name=d["name"], profile=d["profile"], **seen(d["id"])) for d in settings.devices()})
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


# ---------------------------------------------------------------- panned views
# A device can move its view off centre in PAN_MI steps (swipe, or HA): the view id is
# then "<view>@<dx>,<dy>" (steps east, north). Weather allows 3 steps, aircraft 1.
PAN_MI = 50
_PAN_RE = re.compile(r"^([a-z0-9_-]+)@(-?\d+),(-?\d+)$")

def parse_pan(vid, max_steps):
    """(base id, dx, dy); dx = dy = 0 for a plain id; None if malformed or too far."""
    m = _PAN_RE.match(vid or "")
    if not m: return None if "@" in (vid or "") else (vid, 0, 0)
    base, dx, dy = m.group(1), int(m.group(2)), int(m.group(3))
    if abs(dx) > max_steps or abs(dy) > max_steps: return None
    return base, dx, dy

def pan_label(dx, dy):
    """"50 mi NE", "100 mi N, 50 mi E" """
    ns = "N" if dy > 0 else "S"; ew = "E" if dx > 0 else "W"
    if dx and dy and abs(dx) == abs(dy): return "%d mi %s%s" % (abs(dy) * PAN_MI, ns, ew)
    parts = (["%d mi %s" % (abs(dy) * PAN_MI, ns)] if dy else []) + (["%d mi %s" % (abs(dx) * PAN_MI, ew)] if dx else [])
    return ", ".join(parts)

def pan_centre(lat, lon, dx, dy):
    return (lat + dy * PAN_MI / 69.05, lon + dx * PAN_MI / (69.17 * math.cos(math.radians(lat))))
