"""Firmware: CI releases on GitHub -> the displays' OTA channels and the install page.

CI (.github/workflows/firmware.yml) builds every board on each firmware push and
publishes a release "firmware-<sha>" with, per board, display-hub-<board>.bin (the
app, for OTA) and display-hub-<board>-full.bin (bootloader + table + app, for the
install page), plus version.txt. The images carry no secrets.

Nothing reaches a display until the admin page publishes a release to a channel:
the app image is then copied to FIRMWARE_DIR/<channel>/, where devices on that
channel find it (/firmware/<channel>.json, checked 90 s after boot and every 6 h).
New boards installed from the install page get the release published to their
board's channel (or the newest, if none is published yet).
"""
import json, os, re, threading, time, urllib.request

from . import core

REPO = os.environ.get("HUB_FIRMWARE_REPO", "snadboy/display-hub")
UA = "snadboy-display-hub/1.0"
BOARDS = {"c6": {"channel": "hub", "chip": "ESP32-C6", "name": "2.16\" AMOLED (ESP32-C6)"},
          "p4": {"channel": "hub-p4", "chip": "ESP32-P4", "name": "3.5\" LCD (ESP32-P4)"},
          "esp32": {"channel": "hub-esp32", "chip": "ESP32", "name": "1.28\" round LCD (ESP32, DeskRadar build)"}}
_TAG = re.compile(r"^firmware-[0-9a-f]{7}$")

_lock = threading.Lock()
_cache = {"at": 0, "releases": []}

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout).read()

def releases(refresh=False):
    """Newest first: [{"tag", "version", "date", "assets": {name: url}}]."""
    with _lock:
        if not refresh and time.time() - _cache["at"] < 600:
            return _cache["releases"]
    try:
        out = []
        for r in json.loads(_get("https://api.github.com/repos/%s/releases?per_page=15" % REPO)):
            if not _TAG.match(r.get("tag_name", "")): continue
            assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
            ver = (r.get("name") or "").replace("Firmware ", "").strip()
            out.append({"tag": r["tag_name"], "version": ver, "date": r.get("published_at", ""), "assets": assets})
        with _lock: _cache.update(at=time.time(), releases=out)
    except Exception as e:
        print("[firmware] GitHub releases unavailable: %s" % str(e)[:80], flush=True)
    with _lock: return _cache["releases"]

def _release(tag):
    return next((r for r in releases() if r["tag"] == tag), None)

def published(board):
    """{"version", "tag"} on a board's channel, or None."""
    d = core._fw_dir(BOARDS[board]["channel"])
    info = core.firmware_info(BOARDS[board]["channel"])
    if not info: return None
    try: tag = open(os.path.join(d, "tag.txt")).read().strip()
    except OSError: tag = ""
    return {"version": info["version"], "tag": tag}

def _download(url, path):
    data = _get(url, timeout=120)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f: f.write(data)
    os.replace(path + ".tmp", path)
    return data

def publish(board, tag):
    """Put a release's app image on a board's OTA channel."""
    r = _release(tag)
    if r is None: raise ValueError("unknown release %s" % tag)
    name = "display-hub-%s.bin" % board
    if name not in r["assets"] or "version.txt" not in r["assets"]:
        raise ValueError("%s has no %s image" % (tag, board))
    d = core._fw_dir(BOARDS[board]["channel"])
    ver = _get(r["assets"]["version.txt"]).decode().strip()
    _download(r["assets"][name], os.path.join(d + ".new", "firmware.bin"))     # stage, then swap
    with open(os.path.join(d + ".new", "version.txt"), "w") as f: f.write(ver + "\n")
    with open(os.path.join(d + ".new", "tag.txt"), "w") as f: f.write(tag + "\n")
    if os.path.isdir(d): os.replace(d, d + ".old")
    os.replace(d + ".new", d)
    if os.path.isdir(d + ".old"):
        for f in os.listdir(d + ".old"): os.remove(os.path.join(d + ".old", f))
        os.rmdir(d + ".old")
    print("[firmware] %s -> channel %s (%s)" % (tag, BOARDS[board]["channel"], ver), flush=True)
    return ver

def install_release(board):
    """The release a new board of this kind gets: the one its channel runs, else the newest."""
    p = published(board)
    if p and p["tag"] and _release(p["tag"]): return _release(p["tag"])
    return next((r for r in releases() if "display-hub-%s-full.bin" % board in r["assets"]), None)

def install_image(board):
    """Bytes of the full install image for a board (cached per release)."""
    r = install_release(board)
    if r is None: return None
    path = os.path.join(core.FIRMWARE_DIR, "install", r["tag"], "display-hub-%s-full.bin" % board)
    if os.path.exists(path): return open(path, "rb").read()
    return _download(r["assets"]["display-hub-%s-full.bin" % board], path)

def manifest():
    """ESP Web Tools manifest: one build per chip; the tool picks by the chip it finds."""
    builds, vers = [], []
    for board, b in BOARDS.items():
        r = install_release(board)
        if r is None: continue
        vers.append(r["version"])
        builds.append({"chipFamily": b["chip"], "parts": [{"path": "/install/%s-full.bin" % board, "offset": 0}]})
    return {"name": "Display Hub", "version": vers[0] if vers else "none", "new_install_prompt_erase": True,
            "new_install_improv_wait_time": 30, "builds": builds}
