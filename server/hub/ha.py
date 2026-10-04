"""Home Assistant REST access, shared by every app. The hub holds the HA token so
devices never need one."""
import json, os, urllib.request
from datetime import datetime

def ha_entity(eid):
    """(state, last_changed epoch) for any HA entity, or (None, None)."""
    base = os.environ.get("HASS_SERVER", "").rstrip("/"); tok = os.environ.get("HASS_TOKEN", "")
    if not (base and tok):
        return None, None
    try:
        r = urllib.request.Request(base + "/api/states/" + eid, headers={"Authorization": "Bearer " + tok})
        st = json.loads(urllib.request.urlopen(r, timeout=10).read())
        lc = datetime.fromisoformat(st["last_changed"].replace("Z", "+00:00")).timestamp()
        return st["state"], lc
    except Exception as e:
        print("    HA %s unavailable (%s)" % (eid, str(e)[:40]))
        return None, None
