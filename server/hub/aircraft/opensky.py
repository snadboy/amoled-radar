"""OpenSky Network client: OAuth2 client credentials + /states/all for a box.

One poller per view, shared by every device showing it. It polls only while some
device has asked for states recently, so an idle hub spends no credits (a standard
account gets 4,000 a day; a ~100 mi box costs 1 per call, so 30 s = 2,880/day).
"""
import json, math, os, threading, time, urllib.error, urllib.parse, urllib.request

from .. import settings

TOKEN_URL  = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"
UA = "snadboy-display-hub/1.0 (personal homelab display)"

POLL_S         = int(os.environ.get("AIR_POLL_S", "30"))
IDLE_AFTER_S   = int(os.environ.get("AIR_IDLE_AFTER_S", "120"))   # no device asked this long -> stop polling
HIDE_ON_GROUND = os.environ.get("AIR_HIDE_GROUND", "1") == "1"    # ORD alone parks ~100 aircraft

# Status codes, also sent to devices in the states header.
ST_OK, ST_AUTH, ST_RATE, ST_ERROR, ST_IDLE = 0, 1, 2, 3, 4
ST_NAMES = {ST_OK: "ok", ST_AUTH: "auth failed", ST_RATE: "rate limited", ST_ERROR: "error", ST_IDLE: "idle"}

class AuthError(Exception): pass
class RateLimited(Exception):
    def __init__(self, retry_s): super().__init__("rate limited"); self.retry_s = retry_s

_token = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()

def _fetch_token(cid, sec):
    """(token, expires_in) for a client id + secret."""
    if not (cid and sec):
        raise AuthError("OpenSky client id / secret not set (admin page)")
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": sec}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        raise AuthError("token HTTP %d" % e.code)
    return r["access_token"], int(r.get("expires_in", 1800))

def _token_value():
    with _token_lock:
        if _token["value"] and _token["expires"] - time.time() > 60:
            return _token["value"]
        tok, exp = _fetch_token(settings.secret("opensky_client_id"), settings.secret("opensky_client_secret"))
        _token.update(value=tok, expires=time.time() + exp)
        return _token["value"]

def test(cid=None, sec=None):
    """(ok, message) -- the admin page's Test button: fetch a token with the values
    typed on the page (blank ones fall back to the saved settings)."""
    try:
        _fetch_token(cid or settings.secret("opensky_client_id"), sec or settings.secret("opensky_client_secret"))
        return True, "token ok"
    except Exception as e:
        return False, str(e)[:120]

def _drop_token():
    with _token_lock: _token.update(value=None, expires=0.0)

def miles_between(lat0, lon0, lat, lon):
    """Equirectangular approximation; plenty accurate at 50 mi."""
    x = math.radians(lon - lon0) * math.cos(math.radians((lat + lat0) / 2))
    return math.hypot(x, math.radians(lat - lat0)) * 3958.8

def _num(v):
    return None if v is None else float(v)

def _parse(row, view):
    """One /states/all row -> dict, or None if it should be skipped."""
    if len(row) < 17 or row[5] is None or row[6] is None:
        return None
    ground = bool(row[8])
    if HIDE_ON_GROUND and ground:
        return None
    lat, lon = float(row[6]), float(row[5])
    if miles_between(view["lat"], view["lon"], lat, lon) > poll_radius(view):
        return None
    alt = _num(row[7])
    return {"icao": int(row[0], 16), "callsign": (row[1] or "").strip(), "country": row[2] or "",
            "squawk": (row[14] or "").strip(), "lat": lat, "lon": lon,
            "alt_m": alt if alt is not None else _num(row[13]),
            "speed_ms": _num(row[9]), "track_deg": _num(row[10]), "vrate_ms": _num(row[11]),
            "time_position": int(row[3] if row[3] is not None else row[4]),
            "category": int(row[17]) if len(row) > 17 and row[17] is not None else 0,
            "on_ground": ground}

# The box also covers a one-step pan each way (the device's swipe, 50 mi, diagonals too)
# while it stays within OpenSky's 1-credit size (25 square degrees); devices get the
# aircraft within radius_mi of whatever centre they show.
PAN_COVER_MI = 71

def poll_radius(view):
    r = view["radius_mi"] + PAN_COVER_MI
    dlat, dlon = r / 69.0, r / (69.17 * math.cos(math.radians(view["lat"])))
    return r if (2 * dlat) * (2 * dlon) <= 25 else view["radius_mi"]

def fetch_states(view):
    """(server_time, [aircraft], credits_left) for the view's box."""
    lat0, lon0, r = view["lat"], view["lon"], poll_radius(view)
    dlat, dlon = r / 69.0, r / (69.17 * math.cos(math.radians(lat0)))
    q = urllib.parse.urlencode({"lamin": "%.4f" % (lat0 - dlat), "lomin": "%.4f" % (lon0 - dlon),
                                "lamax": "%.4f" % (lat0 + dlat), "lomax": "%.4f" % (lon0 + dlon),
                                "extended": 1})
    req = urllib.request.Request(STATES_URL + "?" + q, headers={
        "User-Agent": UA, "Authorization": "Bearer " + _token_value()})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(int(e.headers.get("X-Rate-Limit-Retry-After-Seconds") or 60))
        if e.code == 401:
            _drop_token()
            raise AuthError("states HTTP 401")
        raise
    credits = int(resp.headers.get("X-Rate-Limit-Remaining") or -1)
    js = json.loads(resp.read())
    out = [a for a in (_parse(row, view) for row in (js.get("states") or [])) if a]
    return int(js.get("time") or time.time()), out, credits

class Poller:
    """Polls one view while devices are interested. `on_batch(aircraft)` runs after
    each successful poll (used to prefetch lookups)."""
    def __init__(self, view, on_batch=None):
        self.view, self.on_batch = view, on_batch
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.aircraft, self.seq, self.server_time = [], 0, 0
        self.status, self.credits, self.err = ST_IDLE, -1, None
        self.last_ok = 0.0
        self.interest = 0.0          # last time a device asked
        self.next_due = 0.0

    def touch(self):
        """A device wants this view's states: keep polling, or start again now."""
        self.interest = time.time()
        self.wake.set()

    def snapshot(self):
        with self.lock:
            return {"seq": self.seq, "server_time": self.server_time, "status": self.status,
                    "credits": self.credits, "err": self.err, "last_ok": self.last_ok,
                    "aircraft": self.aircraft}

    def _poll(self):
        wait = POLL_S
        try:
            t, acs, credits = fetch_states(self.view)
            with self.lock:
                self.aircraft, self.server_time, self.credits = acs, t, credits
                self.seq += 1
                self.status, self.err, self.last_ok = ST_OK, None, time.time()
            if self.on_batch:
                self.on_batch(acs)
        except RateLimited as e:
            wait = max(e.retry_s, 60)
            with self.lock: self.status, self.err = ST_RATE, "retry in %ds" % e.retry_s
        except AuthError as e:
            wait = 60
            with self.lock: self.status, self.err = ST_AUTH, str(e)
        except Exception as e:
            with self.lock: self.status, self.err = ST_ERROR, str(e)[:200]
        if self.status != ST_OK:
            print("[aircraft] %s poll: %s (%s)" % (self.view["id"], ST_NAMES[self.status], self.err), flush=True)
        self.next_due = time.time() + wait

    def run(self):
        while True:
            now = time.time()
            if now - self.interest > IDLE_AFTER_S:
                with self.lock:
                    if self.status == ST_OK: self.status = ST_IDLE
                self.wake.wait(); self.wake.clear()
                continue
            if now < self.next_due:
                self.wake.wait(self.next_due - now); self.wake.clear()
                continue
            self._poll()
