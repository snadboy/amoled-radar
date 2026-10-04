"""Aircraft details and routes, shared by every device.

  adsbdb  /aircraft/<icao24>?callsign=<cs>: type, registration, owner and the
          callsign's route, in one request.
  hexdb   /route/icao/<cs> (possibly multi-leg) + /airport/icao/<code>: route fallback.

Both route databases are crowd-sourced and often stale for reused flight numbers
(about 60% were wrong in a live check), so a route is shown only when flying
origin -> plane -> destination is a modest detour over flying it direct. Route
candidates are cached per callsign and vetted against the plane's position on
every request, because the plane keeps moving.

Lookups for planes in view are prefetched in the background, one request per
AIR_LOOKUP_GAP_S, so a tap usually hits a warm cache.
"""
import json, math, os, queue, re, threading, time, urllib.error, urllib.request

UA = "snadboy-display-hub/1.0 (personal homelab display)"
AIRCRAFT_TTL = 7 * 86400
ROUTE_TTL    = 6 * 3600          # flight numbers get reused; don't trust a route for long
GAP_S        = float(os.environ.get("AIR_LOOKUP_GAP_S", "1.0"))

_lock = threading.Lock()
_aircraft = {}     # icao -> (fetched, {type, registration, owner} or None)
_routes   = {}     # callsign -> (fetched, [(origin, dest, source), ...])
_airports = {}     # ICAO code -> airport dict or None (hexdb)
_hexdb_done = {}   # callsign -> fetched (hexdb consulted for it)
_queue = queue.Queue()
_queued = set()

def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def _gc_mi(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dp = math.radians(lon2 - lon1), p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))

def route_fits(o, d, lat, lon):
    direct = _gc_mi(o["lat"], o["lon"], d["lat"], d["lon"])
    via = _gc_mi(o["lat"], o["lon"], lat, lon) + _gc_mi(lat, lon, d["lat"], d["lon"])
    return via - direct < 0.15 * direct + 60

def is_private(cs):
    """US private flights use the registration as callsign; no route exists."""
    return bool(re.match(r"^N\d", cs or ""))

def _adsbdb(icao, cs):
    """(aircraft dict or None, route candidate or None)."""
    base = "https://api.adsbdb.com/v0/aircraft/%06X" % icao
    urls = [base + "?callsign=" + cs, base] if cs and not is_private(cs) else [base]
    for url in urls:          # an unknown callsign 404s the whole request, so retry without it
        try:
            resp = _get_json(url).get("response") or {}
            break
        except urllib.error.HTTPError as e:
            if e.code != 404: raise
    else:
        return None, None
    ac, info = resp.get("aircraft"), None
    if isinstance(ac, dict):
        mfr, typ = ac.get("manufacturer") or "", ac.get("type") or ""
        # adsbdb types often already start with the manufacturer ("Cessna 172N")
        full = "%s %s" % (mfr, typ) if mfr and not typ.lower().startswith(mfr.lower()) else typ
        info = {"type": full.strip(), "registration": ac.get("registration") or "",
                "owner": ac.get("registered_owner") or ""}
    fr, cand = resp.get("flightroute"), None
    if isinstance(fr, dict):
        def ap(j):
            j = j or {}
            if j.get("latitude") is None: return None
            return {"iata": j.get("iata_code") or "", "city": j.get("municipality") or "",
                    "lat": float(j["latitude"]), "lon": float(j["longitude"])}
        o, d = ap(fr.get("origin")), ap(fr.get("destination"))
        if o and d: cand = (o, d, "adsbdb")
    return info, cand

def _short_name(name):
    """"Chicago O'Hare International Airport" -> "Chicago O'Hare"."""
    for s in (" International Airport", " Regional Airport", " Municipal Airport",
              " Airport", " International", " Intl"):
        if s in name: return name[:name.index(s)]
    return name

def _hexdb_airport(code):
    if code not in _airports:
        try:
            j = _get_json("https://hexdb.io/api/v1/airport/icao/" + code)
            _airports[code] = None if j.get("latitude") is None else {
                "iata": j.get("iata") or code, "city": _short_name(j.get("airport") or ""),
                "lat": float(j["latitude"]), "lon": float(j["longitude"])}
        except Exception:
            return None          # don't cache a transient failure
    return _airports[code]

def _hexdb_routes(cs):
    """Consecutive legs of hexdb's (possibly multi-leg) route as candidates."""
    try:
        route = _get_json("https://hexdb.io/api/v1/route/icao/" + cs).get("route") or ""
    except urllib.error.HTTPError as e:
        if e.code == 404: return []
        raise
    aps = [_hexdb_airport(c) for c in route.split("-")[:6] if c]
    if not aps or None in aps: return []
    return [(a, b, "hexdb") for a, b in zip(aps, aps[1:])]

def _fresh(entry, ttl):
    return entry is not None and time.time() - entry[0] < ttl

def _fit(cands, lat, lon):
    if lat is None or lon is None: return None
    for o, d, src in cands:
        if route_fits(o, d, lat, lon):
            return {"origin": o, "dest": d, "source": src}
    return None

def lookup(icao, cs, lat, lon, fetch=True):
    """Info dict for a plane, or None if it isn't cached and fetch is False."""
    cs = (cs or "").strip()
    with _lock:
        a, r = _aircraft.get(icao), _routes.get(cs)
    need_cs = bool(cs) and not is_private(cs)
    if not (_fresh(a, AIRCRAFT_TTL) and (not need_cs or _fresh(r, ROUTE_TTL))):
        if not fetch: return None
        info, cand = _adsbdb(icao, cs)
        now = time.time()
        with _lock:
            _aircraft[icao] = a = (now, info)
            if need_cs: _routes[cs] = r = (now, [cand] if cand else [])
    cands = list(r[1]) if need_cs and r else []
    route = _fit(cands, lat, lon)
    # adsbdb's route missing or wrong for where the plane is: try hexdb, once per TTL
    if route is None and need_cs and lat is not None:
        done = _hexdb_done.get(cs)
        if done is None or time.time() - done >= ROUTE_TTL:
            if not fetch: return None
            extra = _hexdb_routes(cs)
            with _lock:
                _hexdb_done[cs] = time.time()
                _routes[cs] = (r[0], [c for c in cands if c[2] != "hexdb"] + extra)
            cands = _routes[cs][1]
            route = _fit(cands, lat, lon)
    info = a[1] or {}
    return {"icao": "%06x" % icao, "callsign": cs, "private": is_private(cs),
            "type": info.get("type", ""), "registration": info.get("registration", ""),
            "owner": info.get("owner", ""), "route": route}

def prefetch(aircraft):
    """Queue lookups for planes whose info isn't cached yet."""
    for ac in aircraft:
        key = (ac["icao"], ac["callsign"])
        if key in _queued: continue
        if lookup(ac["icao"], ac["callsign"], ac["lat"], ac["lon"], fetch=False) is None:
            _queued.add(key); _queue.put(ac)

def _worker():
    while True:
        ac = _queue.get()
        try:
            lookup(ac["icao"], ac["callsign"], ac["lat"], ac["lon"])
        except Exception as e:
            print("[aircraft] lookup %06x %s failed: %s" % (ac["icao"], ac["callsign"], str(e)[:80]), flush=True)
        _queued.discard((ac["icao"], ac["callsign"]))
        time.sleep(GAP_S)

def start():
    threading.Thread(target=_worker, daemon=True).start()
