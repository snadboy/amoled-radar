"""Home Assistant REST access, shared by every app. The hub holds the HA URL and token
(admin page -> settings) so devices never need them."""
import json, urllib.request
from datetime import datetime

from . import settings

def configured():
    return bool(settings.secret("hass_url") and settings.secret("hass_token"))

def _get(path, timeout=10):
    base, tok = settings.secret("hass_url").rstrip("/"), settings.secret("hass_token")
    r = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + tok})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())

def ha_entity(eid):
    """(state, last_changed epoch) for any HA entity, or (None, None)."""
    if not (eid and configured()):
        return None, None
    try:
        st = _get("/api/states/" + eid)
        lc = datetime.fromisoformat(st["last_changed"].replace("Z", "+00:00")).timestamp()
        return st["state"], lc
    except Exception as e:
        print("    HA %s unavailable (%s)" % (eid, str(e)[:40]))
        return None, None

def entities(domain=None):
    """Entity ids (optionally of one domain) for the admin page's pickers."""
    if not configured(): return []
    try:
        return sorted(s["entity_id"] for s in _get("/api/states", 20)
                      if not domain or s["entity_id"].startswith(domain + "."))
    except Exception:
        return []

def test():
    """(ok, message) -- for the admin page's Test button."""
    if not configured(): return False, "URL or token not set"
    try:
        return True, _get("/api/").get("message", "ok")
    except Exception as e:
        return False, str(e)[:120]
