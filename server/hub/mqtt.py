"""Home Assistant over MQTT: discovery entities for the hub and every display.

Per display (an HA device): Online, App + City selects (switch what it shows now --
the boot defaults stay on the admin page), Screen select (Auto = room sensors,
On / Off = forced; saved in its settings), Brightness, Firmware and IP sensors,
Restart and Identify buttons. For the hub: aircraft in range and the closest one,
OpenSky credits left, and a Weather loops problem sensor.

Broker and login come from the admin page (Connections: mqtt_url like
mqtt://host-ha.isnadboy.com:1883, user, password). With no URL set this does nothing.
Topics: homeassistant/<component>/<node>/<object>/config (retained discovery),
display-hub/status (LWT), display-hub/<node>/state (JSON), display-hub/<node>/<what>/set|press.
"""
import json, threading, time, urllib.parse

import paho.mqtt.client as mqtt

from . import core, settings

BASE = "display-hub"
DISC = "homeassistant"
STATUS = BASE + "/status"
ADMIN_URL = "https://displays.swallow-spectrum.ts.net"
APP_LABEL = {"weather": "Weather radar", "aircraft": "Aircraft"}
SCREEN = {"auto": "Auto", "on": "On", "off": "Off"}

_lock = threading.Lock()
_client, _conf, _published = None, None, set()
_apps = []                      # app modules (aircraft for its pollers, weather for health)

def _node(dev_id): return "dh_" + dev_id.replace(":", "")

def _conf_now():
    return tuple(settings.secret(k) for k in ("mqtt_url", "mqtt_user", "mqtt_password"))

def _parse(url):
    u = urllib.parse.urlparse(url if "://" in url else "mqtt://" + url)
    return u.hostname, u.port or 1883

# ---------------------------------------------------------------- discovery
HUB_DEV = {"identifiers": ["display_hub_hub"], "name": "Display Hub", "manufacturer": "snadboy",
           "model": "display-hub", "configuration_url": ADMIN_URL}

def _configs():
    """{topic: payload} for every entity that should exist now."""
    out, avail = {}, [{"topic": STATUS}]
    def ent(component, node, obj, dev, name, **kw):
        out["%s/%s/%s/%s/config" % (DISC, component, node, obj)] = dict(
            name=name, unique_id="%s_%s" % (node, obj), device=dev, availability=avail, **kw)
    for d in settings.devices():
        node, pr = _node(d["id"]), d["profile"]
        st = "%s/%s/state" % (BASE, node)
        dev = {"identifiers": ["display_hub_" + node], "name": d["name"], "manufacturer": "Waveshare",
               "model": pr.get("board", "display"), "sw_version": pr.get("fw", ""),
               "connections": [["mac", d["id"]]], "via_device": "display_hub_hub", "configuration_url": ADMIN_URL}
        ent("binary_sensor", node, "online", dev, "Online", state_topic=st, device_class="connectivity",
            value_template="{{ 'ON' if value_json.online else 'OFF' }}", entity_category="diagnostic")
        ent("select", node, "app", dev, "App", state_topic=st, value_template="{{ value_json.app }}",
            command_topic="%s/%s/app/set" % (BASE, node), options=[APP_LABEL[a] for a in d["apps"]], icon="mdi:apps")
        if "weather" in d["apps"]:
            names = [p["name"] for p in map(settings.place, d["weather"]["places"]) if p]
            ent("select", node, "city", dev, "City", state_topic=st, value_template="{{ value_json.city }}",
                command_topic="%s/%s/city/set" % (BASE, node), options=names, icon="mdi:weather-pouring")
        ent("select", node, "screen", dev, "Screen", state_topic=st, value_template="{{ value_json.screen }}",
            command_topic="%s/%s/screen/set" % (BASE, node), options=list(SCREEN.values()), icon="mdi:monitor")
        ent("sensor", node, "brightness", dev, "Brightness", state_topic=st, value_template="{{ value_json.brightness }}",
            state_class="measurement", icon="mdi:brightness-6")
        ent("sensor", node, "firmware", dev, "Firmware", state_topic=st, value_template="{{ value_json.fw }}",
            entity_category="diagnostic", icon="mdi:chip")
        ent("sensor", node, "ip", dev, "IP address", state_topic=st, value_template="{{ value_json.ip }}",
            entity_category="diagnostic", icon="mdi:ip-network")
        ent("button", node, "restart", dev, "Restart", command_topic="%s/%s/restart/press" % (BASE, node),
            device_class="restart", entity_category="config")
        ent("button", node, "identify", dev, "Identify", command_topic="%s/%s/identify/press" % (BASE, node),
            device_class="identify", entity_category="config")
    hs = BASE + "/hub/state"
    for v in settings.air_views():
        vid = v["id"]
        ent("sensor", "dh_hub", "aircraft_" + vid, HUB_DEV, "Aircraft in range (%s)" % v["name"], state_topic=hs,
            value_template="{{ value_json.aircraft['%s'].count }}" % vid, state_class="measurement", icon="mdi:airplane")
        ent("sensor", "dh_hub", "closest_" + vid, HUB_DEV, "Closest aircraft (%s)" % v["name"], state_topic=hs,
            value_template="{{ value_json.aircraft['%s'].closest }}" % vid,
            json_attributes_topic=hs, json_attributes_template="{{ value_json.aircraft['%s'] | tojson }}" % vid,
            icon="mdi:airplane-marker")
    ent("sensor", "dh_hub", "opensky_credits", HUB_DEV, "OpenSky credits left", state_topic=hs,
        value_template="{{ value_json.credits }}", state_class="measurement", icon="mdi:counter", entity_category="diagnostic")
    ent("binary_sensor", "dh_hub", "weather_problem", HUB_DEV, "Weather loops", state_topic=hs, device_class="problem",
        value_template="{{ 'OFF' if value_json.weather_ok else 'ON' }}")
    return out

def _publish_discovery(c):
    global _published
    cfg = _configs()
    for t, payload in cfg.items():
        c.publish(t, json.dumps(payload), qos=1, retain=True)
    for t in _published - set(cfg):        # entities that went away (forgotten device, app removed)
        c.publish(t, "", qos=1, retain=True)
    _published = set(cfg)

# ---------------------------------------------------------------- state
def _device_state(d):
    s = core.seen(d["id"])
    place = settings.place(s.get("view", "")) if s.get("app") == "weather" else None
    return {"online": core.online(d["id"]), "app": APP_LABEL.get(s.get("app"), ""),
            "city": place["name"] if place else "", "screen": SCREEN.get(d["screen"].get("mode", "auto"), "Auto"),
            "brightness": s.get("bright"), "fw": d["profile"].get("fw", ""), "ip": s.get("ip", ""),
            "screen_on": bool(s.get("on"))}

def _hub_state():
    by_id = {a.ID: a for a in _apps}
    air, credits = {}, None
    if "aircraft" in by_id:
        for vid, snap in by_id["aircraft"].snapshots().items():
            v = settings.air_view(vid) or {}
            near = None
            for a in snap["aircraft"]:
                if a["on_ground"]: continue
                mi = by_id["aircraft"].miles_from(v, a)
                if near is None or mi < near[0]: near = (mi, a)
            air[vid] = {"count": len(snap["aircraft"]), "closest": near[1]["callsign"] if near else "none",
                        "distance_mi": round(near[0], 1) if near else None,
                        "altitude_ft": round(near[1]["alt_m"] * 3.28084) if near and near[1]["alt_m"] is not None else None,
                        "status": snap["status_name"]}
            if snap["credits"] >= 0: credits = snap["credits"]
        for v in settings.air_views():       # views nobody is polling right now
            air.setdefault(v["id"], {"count": 0, "closest": "none", "distance_mi": None, "altitude_ft": None, "status": "idle"})
    ok = by_id["weather"].health()[0] if "weather" in by_id else True
    return {"aircraft": air, "credits": credits, "weather_ok": ok}

def _publish_state(c):
    for d in settings.devices():
        c.publish("%s/%s/state" % (BASE, _node(d["id"])), json.dumps(_device_state(d)), retain=True)
    c.publish(BASE + "/hub/state", json.dumps(_hub_state()), retain=True)

# ---------------------------------------------------------------- commands
def _on_message(c, userdata, msg):
    try:
        if msg.topic == DISC + "/status":
            if msg.payload == b"online": _publish_discovery(c); _publish_state(c)     # HA restarted
            return
        parts = msg.topic.split("/")             # display-hub/<node>/<what>/<set|press>
        dev = next((d for d in settings.devices() if _node(d["id"]) == parts[1]), None)
        if dev is None: return
        val = msg.payload.decode()
        what = parts[2]
        if what == "app":
            app = next((k for k, l in APP_LABEL.items() if l == val), None)
            if app in dev["apps"]: core.command(dev["id"], app=app)
        elif what == "city":
            place = next((p for p in map(settings.place, dev["weather"]["places"]) if p and p["name"] == val), None)
            if place: core.command(dev["id"], app="weather", view=place["id"])
        elif what == "screen":
            mode = next((k for k, l in SCREEN.items() if l == val), None)
            if mode:
                settings.put_device(dev["id"], {"screen": dict(dev["screen"], mode=mode)})
                _publish_state(c)
        elif what == "restart": core.command(dev["id"], restart=True)
        elif what == "identify": core.command(dev["id"], identify=True)
    except Exception as e:
        print("[mqtt] command %s failed: %s" % (msg.topic, e), flush=True)

def _on_connect(c, userdata, flags, reason, props=None):
    if reason.is_failure:
        print("[mqtt] connect refused: %s" % reason, flush=True); return
    print("[mqtt] connected", flush=True)
    c.publish(STATUS, "online", qos=1, retain=True)
    c.subscribe([(BASE + "/+/+/set", 1), (BASE + "/+/+/press", 1), (DISC + "/status", 1)])
    _publish_discovery(c); _publish_state(c)

# ---------------------------------------------------------------- lifecycle
def _connect(conf):
    url, user, pw = conf
    host, port = _parse(url)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="display-hub")
    if user: c.username_pw_set(user, pw)
    c.will_set(STATUS, "offline", qos=1, retain=True)
    c.on_connect, c.on_message = _on_connect, _on_message
    c.reconnect_delay_set(2, 60)
    c.connect_async(host, port, keepalive=60)
    c.loop_start()
    return c

def _loop():
    global _client, _conf
    last_disc = ""
    while True:
        conf = _conf_now()
        if conf != _conf:                        # first start, or changed on the admin page
            if _client: _client.loop_stop(); _client.disconnect(); _client = None
            _conf = conf
            if conf[0]: _client = _connect(conf); print("[mqtt] connecting to %s" % _parse(conf[0])[0], flush=True)
        if _client and _client.is_connected():
            sig = json.dumps(_configs(), sort_keys=True)
            if sig != last_disc:                 # a device renamed, added, apps or places changed
                _publish_discovery(_client); last_disc = sig
            _publish_state(_client)
        time.sleep(10)

def start(apps):
    _apps.extend(apps)
    threading.Thread(target=_loop, daemon=True).start()

def test(url=None, user=None, pw=None):
    """(ok, message): try a broker and login once -- the values typed on the admin page,
    blank ones falling back to the saved settings."""
    saved = _conf_now()
    url, user, pw = url or saved[0], user or saved[1], pw or saved[2]
    if not url: return False, "broker URL not set"
    host, port = _parse(url)
    done, res = threading.Event(), {}
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user: c.username_pw_set(user, pw)
    c.on_connect = lambda cl, u, f, rc, p=None: (res.update(rc=rc), done.set())
    try:
        c.connect(host, port, keepalive=10); c.loop_start()
        done.wait(8); c.loop_stop(); c.disconnect()
    except Exception as e:
        return False, str(e)[:120]
    rc = res.get("rc")
    if rc is None: return False, "no answer from %s:%d" % (host, port)
    return (not rc.is_failure), ("connected" if not rc.is_failure else str(rc))
