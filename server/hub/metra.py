"""Metra app: one line drawn as a track between two chosen stations, with every train
on that stretch placed where it is now, as a picture.

  Chicago OTC ●──•──•──▶──•──•──◀──•──● Elburn
                       │           │
                West Chicago 2:46  │
                             Geneva 2:51

The stations in between are dots; under each train, its next station and the time it
gets there. Train number and delay sit above it, coloured by delay.

Data: the SB Metra Home Assistant integration (ha-sb-metra), through the hub's HA
connection -- one source of truth, no Metra API use of our own:
  sensor.metra_active_trains  attr lines[<line>] = trains now (next station + ETA, delay)
  metra.schedule action       today's trips with their stations in order: gives each line's
                              stations, the path between two of them, and how long a train
                              takes between stops (so it can be placed between them).

Endpoint
  /metra/<line>/line.jpg?w=&h=&r=&from=<station>&to=<station>
"""
import io, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from . import ha
from .aircraft import basemap

ID = "metra"
TZ = ZoneInfo("America/Chicago")
ACTIVE = "sensor.metra_active_trains"
ACTIVE_S, SCHED_S = 20, 6 * 3600          # sensor refreshes every 2 min; the timetable daily
# GTFS routes.txt: name, colour
LINES = {"BNSF": ("BNSF", "29C233"), "HC": ("Heritage Corridor", "550E0C"), "MD-N": ("Milwaukee North", "CC5500"),
         "MD-W": ("Milwaukee West", "F1AD0E"), "ME": ("Metra Electric", "EB5C00"), "NCS": ("North Central", "9785BC"),
         "RI": ("Rock Island", "E02400"), "SWS": ("SouthWest", "0042A8"), "UP-N": ("Union Pacific North", "008000"),
         "UP-NW": ("Union Pacific Northwest", "FFE600"), "UP-W": ("Union Pacific West", "FE8D81")}
DEFAULT_LINE = "UP-W"
DIM, INK, SUB = (120, 130, 142), (236, 240, 244), (196, 204, 214)
GREEN, AMBER, RED = (64, 200, 100), (255, 183, 3), (240, 70, 60)

_lock = threading.Lock()
_active = (0, None)                       # (fetched, {line: [train]})
_sched = {}                               # line: (fetched, date, [trip])

def _colour(line):
    """The line's colour, lifted until it reads on black (HC maroon, SWS navy)."""
    c = [int(LINES.get(line, ("", "FE8D81"))[1][i:i + 2], 16) for i in (0, 2, 4)]
    while 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2] < 110:
        c = [min(255, int(v * 1.25 + 20)) for v in c]
    return tuple(c)

def _active_trains(line):
    global _active
    with _lock:
        if _active[1] is not None and time.time() - _active[0] < ACTIVE_S: return _active[1].get(line, [])
    a = ha.attributes(ACTIVE)
    lines = (a or {}).get("lines")
    with _lock:
        if lines is not None: _active = (time.time(), lines)
        return (_active[1] or {}).get(line, [])

def trips(line):
    """Today's scheduled trips on a line: [{train, direction, stations: [{station, time}]}]."""
    today = datetime.now(TZ).date()
    with _lock:
        hit = _sched.get(line)
        if hit and hit[1] == today and time.time() - hit[0] < SCHED_S: return hit[2]
    r = ha.call("metra", "schedule", {"line": line})
    t = ((r or {}).get(line) or {}).get("trains")
    with _lock:
        if t: _sched[line] = (time.time(), today, t)
        return t or (hit[2] if hit else [])

def lines():
    with _lock: known = list((_active[1] or {}).keys())
    return [{"id": l, "name": LINES.get(l, (l,))[0]} for l in (known or LINES)]

def stations(line):
    """The line's stations in route order (the longest trip, others' extra stops slotted in
    after their predecessor; a branch lands as one block after the trunk), as in ha-sb-metra."""
    seqs = sorted(([s["station"] for s in t["stations"]][::-1 if t.get("direction") == "inbound" else 1]
                   for t in trips(line)), key=len, reverse=True)     # all read outbound: downtown first
    if not seqs: return []
    out = list(seqs[0])
    for seq in seqs[1:]:
        for i, s in enumerate(seq):
            if s in out: continue
            anchor = next((x for x in reversed(seq[:i]) if x in out), None)
            out.insert(out.index(anchor) + 1 if anchor else len(out), s)
    return out

def path(line, a, b):
    """Stations from a to b along the line: the stops of the fullest trip serving both
    (so a branch is followed, not the trunk). [] when no train runs between them."""
    best = []
    for t in trips(line):
        seq = [s["station"] for s in t["stations"]]
        if a in seq and b in seq and a != b:
            i, j = seq.index(a), seq.index(b)
            seg = seq[i:j + 1] if i < j else seq[j:i + 1][::-1]
            if len(seg) > len(best): best = seg
    return best

def _mins(hm):
    h, m = hm.split(":"); return int(h) * 60 + int(m)

def _hm12(hm):
    h, m = map(int, hm.split(":")); return "%d:%02d" % ((h - 1) % 12 + 1, m)

def placed(line, path_, now=None):
    """[(position along path 0..len-1, heading +1 toward the far end / -1, train)] for
    the trains between the path's ends now."""
    if len(path_) < 2: return []
    now = now or datetime.now(TZ)
    nowm = now.hour * 60 + now.minute + now.second / 60
    pos = {s: i for i, s in enumerate(path_)}
    by_num = {t["train"]: [s for s in t["stations"]] for t in trips(line)}
    out = []
    for t in _active_trains(line):
        nxt = t.get("next_station")
        if nxt not in pos or t.get("eta", "?") == "?": continue
        sched = by_num.get(t["train"], [])
        names = [s["station"] for s in sched]
        k = names.index(nxt) if nxt in names else -1
        prev = sched[k - 1] if k > 0 else None
        if prev is None:                                  # not left its first stop yet
            if nxt in (path_[0], path_[-1]): continue     # waiting at an end: off the stretch
            out.append((pos[nxt], 0, t)); continue
        if prev["station"] not in pos: continue           # still short of the stretch
        run = (_mins(sched[k]["time"]) - _mins(prev["time"])) % 1440 or 1
        left = (_mins(t["eta"]) - nowm + 720) % 1440 - 720
        f = max(0.0, min(1.0, 1 - left / run))
        p0, p1 = pos[prev["station"]], pos[nxt]
        out.append((p0 + (p1 - p0) * f, 1 if p1 > p0 else -1, t))
    out.sort(key=lambda x: x[0])
    return out

def _delay_col(t):
    d = t.get("delay_min") or 0
    return RED if d >= 10 else AMBER if d >= 3 else GREEN

def _fit(d, text, font, room):
    if d.textlength(text, font=font) <= room: return text
    while text and d.textlength(text + "…", font=font) > room: text = text[:-1]
    return text + "…"

def view_summary(cfg):
    import urllib.parse
    return {"id": cfg["line"], "name": LINES.get(cfg["line"], (cfg["line"],))[0],
            "query": urllib.parse.urlencode({"from": cfg["from"], "to": cfg["to"]})}

def start(): pass

def health():
    return True, {}

def render(line, a, b, w, h, r, now=None):
    now = now or datetime.now(TZ)
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    s = min(w, h) / 480.0
    F = lambda sz, bold=False: basemap.font(max(11, int(sz * s)), bold)
    f_hd, f_end, f_num, f_lab, f_ft = F(24, True), F(20, True), F(18, True), F(20), F(14)
    t4 = int(time.time() // 120) % 4                       # AMOLED: drift a few px over time
    ox, oy = (0, 2, 2, 0)[t4], (0, 0, 2, 2)[t4]
    side = max(16, int(r * 0.45)) + ox
    col = _colour(line)
    # header: line name, clock
    top = max(14, int(r * 0.45)) + oy
    d.text((side, top), LINES.get(line, (line,))[0], font=f_hd, fill=col, anchor="la")
    d.text((w - side, top), now.strftime("%-I:%M"), font=f_hd, fill=INK, anchor="ra")
    p = path(line, a, b)
    if not ha.configured() or not p:
        msg = ("Home Assistant not set up" if not ha.configured() else
               "Waiting for Metra data…" if not trips(line) else "No trains run %s → %s" % (a, b))
        d.text((w // 2, h // 2), _fit(d, msg, f_lab, w - 2 * side), font=f_lab, fill=SUB, anchor="mm")
        return img
    # track: ends named above it, every stop a dot
    x0, x1 = side + 8, w - side - 8
    y_end = top + int(48 * s)
    y = y_end + int(84 * s)
    X = lambda i: x0 + (x1 - x0) * i / (len(p) - 1)
    room = x1 - x0 + 16 - 20                                 # both names on one row, shortest kept whole
    wa, wb = d.textlength(p[0], font=f_end), d.textlength(p[-1], font=f_end)
    ra = room - min(wb, room / 2) if wa > wb else min(wa, room / 2) if wa + wb > room else wa
    d.text((x0 - 8, y_end), _fit(d, p[0], f_end, ra), font=f_end, fill=INK, anchor="la")
    d.text((x1 + 8, y_end), _fit(d, p[-1], f_end, room - min(wa, ra)), font=f_end, fill=INK, anchor="ra")
    lw = max(4, int(6 * s))
    d.line((x0, y, x1, y), fill=col, width=lw)
    for i in range(len(p)):
        rr = int((9 if i in (0, len(p) - 1) else 4) * s)
        d.ellipse((X(i) - rr, y - rr, X(i) + rr, y + rr), fill=col if i in (0, len(p) - 1) else (0, 0, 0),
                  outline=col, width=max(2, int(2 * s)))
    trains = placed(line, p, now)
    live = {t["train"]: t for t in _active_trains(line)}
    on = {t["train"] for _, _, t in trains}
    # bottom: the next train to set off from each end toward the other
    foot_y = h - max(14, int(r * 0.45)) + oy
    nxt_lines = []
    for here, there in ((p[0], p[-1]), (p[-1], p[0])):
        n = next_departure(line, here, there, now, on)
        if n:
            tm, num = n
            dl = (live.get(num) or {}).get("delay_min") or 0
            nxt_lines.append(("Next from %s" % here, "%s  #%s" % (_hm12(tm), num) + ("  +%d" % dl if dl >= 1 else ""),
                              RED if dl >= 10 else AMBER if dl >= 3 else INK))
    next_top = foot_y - int(22 * s) - len(nxt_lines) * int(27 * s)
    for i, (lhs, rhs, c) in enumerate(nxt_lines):
        ny = next_top + i * int(27 * s)
        rw = d.textlength(rhs, font=f_lab)
        d.text((w - side, ny), rhs, font=f_lab, fill=c, anchor="ra")
        d.text((side, ny), _fit(d, lhs, f_lab, w - 2 * side - rw - 12), font=f_lab, fill=DIM, anchor="la")
    # labels below the track: next station + time, in as many rows as needed so none overlap
    row_h, lab_top = int(28 * s), y + int(34 * s)
    max_rows = max(1, (next_top - int(8 * s) - lab_top) // row_h)
    rows_end, num_end, labs = [], [], []
    for fpos, head, t in trains:
        x = X(fpos)
        nm = "%s  %s" % (t["next_station"], _hm12(t["eta"]))
        tw = d.textlength(nm, font=f_lab)
        lx = max(side, min(w - side - tw, x - tw / 2))
        row = next((i for i, e in enumerate(rows_end) if lx > e + 10), len(rows_end))
        if row >= max_rows: continue
        if row == len(rows_end): rows_end.append(0)
        rows_end[row] = lx + tw
        labs.append((x, lx, row, nm, t, head))
    for x, lx, row, nm, t, head in labs:                   # leaders first, under everything
        d.line((x, y + int(12 * s), x, lab_top + row * row_h - 2), fill=(70, 78, 88), width=1)
    for x, lx, row, nm, t, head in labs:
        ly = lab_top + row * row_h                         # black behind: hides leaders passing under
        d.rectangle((lx - 4, ly - 2, lx + d.textlength(nm, font=f_lab) + 4, ly + row_h - 6), fill=(0, 0, 0))
        d.text((lx, ly), nm, font=f_lab, fill=SUB, anchor="la")
    for fpos, head, t in trains:
        x = X(fpos)
        # marker on the track: an arrow the way it's going (a disc while it waits)
        c, m = _delay_col(t), int(11 * s)
        if head:
            d.polygon([(x + head * m, y), (x - head * m * 0.7, y - m), (x - head * m * 0.7, y + m)], fill=c, outline=(0, 0, 0))
        else:
            d.ellipse((x - m * 0.8, y - m * 0.8, x + m * 0.8, y + m * 0.8), fill=c, outline=(0, 0, 0))
        # number + delay above, in rows so neighbours don't collide
        tag = t["train"] + ("  +%d" % t["delay_min"] if (t.get("delay_min") or 0) >= 1 else "")
        tw = d.textlength(tag, font=f_num)
        nx = max(side, min(w - side - tw, x - tw / 2))
        nrow = next((i for i, e in enumerate(num_end) if nx > e + 8), len(num_end))
        if nrow >= 2: continue
        if nrow == len(num_end): num_end.append(0)
        num_end[nrow] = nx + tw
        d.text((nx, y - int((18 + 24 * nrow) * s)), tag, font=f_num, fill=c, anchor="lb")
    if not trains:
        d.text((w // 2, lab_top + row_h), "No trains between these stations now", font=f_lab, fill=DIM, anchor="mm")
    hidden = len(trains) - len(labs)
    foot = "%d train%s on this stretch" % (len(trains), "" if len(trains) == 1 else "s") + (" (%d not labelled)" % hidden if hidden else "")
    d.text((w // 2 + ox, foot_y), foot, font=f_ft, fill=DIM, anchor="mb")
    return img

def next_departure(line, here, there, now, skip=()):
    """(HH:MM, train) of the next scheduled train leaving `here` for `there`, or None."""
    nowm = now.hour * 60 + now.minute
    best = None
    for t in trips(line):
        seq = [s["station"] for s in t["stations"]]
        if here not in seq or there not in seq or seq.index(here) > seq.index(there) or t["train"] in skip: continue
        tm = t["stations"][seq.index(here)]["time"]
        ahead = (_mins(tm) - nowm) % 1440
        if ahead <= 720 and (best is None or ahead < best[0]): best = (ahead, tm, t["train"])
    return best[1:] if best else None

def _jpeg(img):
    b = io.BytesIO(); img.save(b, "JPEG", quality=88); return b.getvalue()

def preview_png(cfg, w, h, r):
    b = io.BytesIO(); render(cfg["line"], cfg["from"], cfg["to"], w, h, r).save(b, "PNG"); return b.getvalue()

def handle(h, p, q):
    parts = p.split("/")                        # ['', 'metra', <line>, 'line.jpg']
    if len(parts) != 4 or parts[1] != ID or parts[3] != "line.jpg":
        return False
    try:
        w, hh, r = int(q.get("w", 480)), int(q.get("h", 480)), int(q.get("r", 56))
    except ValueError:
        return h.json({"error": "bad parameters"}, 400)
    if not (100 <= w <= 2048 and 100 <= hh <= 2048):
        return h.json({"error": "bad size"}, 400)
    return h.send(_jpeg(render(parts[2], q.get("from", ""), q.get("to", ""), w, hh, r)), "image/jpeg")
