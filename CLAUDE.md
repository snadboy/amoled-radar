# amoled-radar

Animated weather radar + outdoor temp/humidity on a **Waveshare ESP32-C6-Touch-AMOLED-2.16**.

**Status:** server-side renderer working and validated against live data. Firmware not started.
**Started:** 2026-10-02

---

## What it does

A 480×480 AMOLED on the desk showing a 50-mile-radius animated radar loop over a dark
basemap, with a one-line outdoor temp/humidity strip across the bottom.

```
┌────────────────────────────┐
│                            │  480 × 424  radar viewport
│      radar + basemap       │  50 mi radius fits the SHORT axis, so the
│      25/50 mi rings        │  full radius is visible in every direction
│      centre crosshair      │  380 m/px displayed
│                            │
├────────────────────────────┤
│  52°F      10:50 PM   84%  │  480 × 56  status strip
└────────────────────────────┘
```

---

## The hardware constraint, and the design choice

**The ESP32-C6 has no PSRAM.** The chip has no external PSRAM interface at all — just
512 KB HP SRAM + 16 KB LP SRAM. A single 480×480 RGB565 framebuffer is **450 KB**, and
the WiFi stack wants 60–80 KB. So the device can never hold a whole frame: whatever
draws to the panel must work in horizontal bands (~40 KB working set).

**That rules out a framebuffer, not on-device compositing.** A self-contained device is
feasible: basemap pre-rendered into flash, RainViewer tiles fetched and PNG-decoded
row-by-row, resampled and alpha-blended band by band. An earlier version of this file
called the server a "necessity" — that was wrong.

**The server is a deliberate choice (confirmed by the owner 2026-10-03)**, because:

- **The upstreams are fragile.** Three silent failures surfaced in the first evening
  (RainViewer zoom cap, its watermark tiles, CARTO's new key requirement — see below).
  Server-side, each fix is a Python edit + redeploy. On-device, each is a firmware
  rebuild + OTA, discovered only because the screen looks wrong.
- **Firmware stays trivial:** fetch JPEG -> decode in bands -> blit. No PNG streaming,
  resampling, blending, tile staging or watermark detection in C.
- **Better output:** Pillow LANCZOS resampling and the AMOLED darkening LUT.
- **Cost:** one more moving part. If bedrock or the container is down, the display keeps
  animating its last cached loop and goes stale. (Serverless still needs internet + HA,
  so it would only drop the bedrock dependency.)

The board does have **16 MB flash**, which is the saving grace: the whole animation loop
(~12 frames × ~25 KB JPEG ≈ 300 KB) caches locally, so the device animates from flash and
only re-fetches when the radar updates (every 10 min). No per-frame WiFi.

---

## Hardware

| | |
|---|---|
| Board | Waveshare ESP32-C6-Touch-AMOLED-2.16 |
| Panel | 480×480 AMOLED, **CO5300** driver over QSPI, 16 bpp |
| Touch | **CST9220** over I²C (unused so far) |
| Also onboard | AXP2101 PMU, QMI8658 6-axis IMU, PCF85063 RTC, audio codec, dual mics |
| Memory | 512 KB SRAM, **no PSRAM**, 16 MB flash |

### Pinout — from Waveshare's own `user_config.h`, NOT from a web search

```
I2C    SCL = GPIO7   SDA = GPIO8      AXP2101 @0x34, CST9220, QMI8658, PCF85063
QSPI   CS  = GPIO5   PCLK = GPIO0     D0..D3 = GPIO1, GPIO2, GPIO3, GPIO4
       BSP_LCD_RST       = NC   <-- panel reset is via the AXP2101, not a GPIO
       BSP_LCD_BACKLIGHT = NC   <-- AMOLED: brightness is a panel command, no PWM
Touch  RST = GPIO11  INT = GPIO15
SD     CLK = GPIO0   MOSI = GPIO1  MISO = GPIO2  CS = GPIO6   (shares LCD data lines)
I2S    MCLK=19 SCLK=20 LCLK=22 DOUT=23 DSIN=21
```

⚠️ **`LCD_RST = NC` means the AXP2101 must be initialised before the panel will light.**
This is the single most likely cause of a dead screen on first bring-up.

⚠️ A web search confidently returned a pinout with `SCLK=38`. **ESP32-C6 only has
GPIO0–30**, so that pin does not exist — it was an ESP32-S3 pinout. Always take pins from
`02_Example/ESP-IDF-v5.5.3/*/main/user_config.h` in
[waveshareteam/ESP32-C6-Touch-AMOLED-2.16](https://github.com/waveshareteam/ESP32-C6-Touch-AMOLED-2.16).

---

## Data sources — and their real limits

### RainViewer (radar) — free, no key

`https://api.rainviewer.com/public/weather-maps.json` → 12 past frames at **600 s**
intervals (2 h of history). Tiles:
`{host}{path}/{size}/{z}/{x}/{y}/{color}/{smooth}_{snow}.png`

Two limits found the hard way, both verified by experiment:

1. **Zoom is capped at 7.** z ≥ 8 returns a *"Zoom Level Not Supported"* watermark tile
   rather than an HTTP error — a black box `rgba(0,0,0,140)` with white text, which
   composites silently and looks like weird terrain. Detect it, don't trust HTTP 200.
   **Workaround:** `size=512` tiles at z=7 → 455 m/px, which is already at NEXRAD's native
   resolution (~250 m–1 km), so nothing real is lost.
2. **`color` and `snow` are ignored.** All 9 palettes return byte-identical tiles
   (same md5). Only `smooth` has any effect. You get one fixed palette: light rain in
   blues, heavy cells **yellow -> orange -> red**, plus semi-transparent tan at the
   lightest edges. It already reads as weather radar, so no remap is needed (an
   earlier note here called it "a blue ramp"; that came from a light-rain sample).

### Basemap — Esri, free, no key, **not** CARTO

`https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`
— note the **z/y/x** order, not z/x/y.

CARTO's `basemaps.cartocdn.com` now returns *"API KEY REQUIRED"* placeholder tiles with
HTTP 200. Same silent-failure trap as RainViewer. Esri has no zoom cap, so the basemap
renders at z=9 (228 m/px) under z=7 radar — sharp map, appropriately soft radar.

The location is fixed, so the basemap is fetched **once** (12 tiles) and cached forever.

Esri "Dark Gray" is still mid-grey, which on an AMOLED wastes power (lit pixels) and
muddies the radar. `darken_for_amoled()` applies a gamma+scale LUT to crush midtones
toward true black while keeping roads and coastline legible.

### Home Assistant (temp/humidity)

`sensor.outdoor_temperature` / `sensor.outdoor_humidity`.
**Not** the FP300s — `outside_tsr_*` and `outside_garage_*` read ~10 °F warm because
both sit in sheltered spaces (three-season room, garage).

---

## Server

`server/render.py` — composites frames; run it with `HASS_SERVER`/`HASS_TOKEN` set.

```bash
cd server
RADAR_OUT=./out RADAR_CACHE=./cache RADAR_FRAMES=12 python3 render.py
```

Env: `RADAR_LAT`/`RADAR_LON` (centre), `RADAR_FRAMES`, `RADAR_OUT`, `RADAR_CACHE`,
`RADAR_TEMP_ENTITY`/`RADAR_HUM_ENTITY`, `RADAR_DARK_GAMMA`/`RADAR_DARK_SCALE`.

Output: ~24–45 KB JPEG per 480×480 frame (bigger when there's heavy precipitation).

**Intended home:** `bedrock` (11 containers, load 0.03, 4.2/16 GB) as a dockhand stack.

---

## Smoothing (in-between frames)

Real frames are 10 min apart; a 30 mph storm jumps ~5 mi (~20 px) per frame, which
reads as a cut. The device cannot blend (it never holds a whole frame), so the server
renders in-betweens and the device just plays more frames.

- **Motion, not crossfade.** `render.tweens()` uses OpenCV Farneback optical flow and
  moves the echoes along the measured path. Crossfade double-exposes storms and blends
  yellow over blue into **olive -- a colour not on the intensity scale**, so it misstates
  intensity. Measured motion on a real storm: median 7.8 px, p90 14 px per 10 min.
- **Interpolate the radar layer only**, then composite over the basemap; flow on
  finished frames would warp coastlines. Work in **premultiplied alpha** -- transparent
  radar pixels carry junk RGB (e.g. `rgba(71,112,76,0)`) that otherwise bleeds in.
- `RADAR_TWEENS` (default 3) per 10 min, scaled by real time (`tween_count`), so a
  skipped RainViewer frame (20-min gap -> 7 in-betweens) plays at the same speed.
- 12 real frames -> 49 total, ~2.2 MB per loop (flash budget 16 MB).
- Manifest carries `keys` (indices of real frames) and `times`. **Device frame rate is
  unknown** -- the C6 has no JPEG hardware; ~4 fps is a guess. If it is slow, firmware
  plays `keys` only.

## Burn-in

**This design is unusually exposed, because the radar returns are the ONLY thing that
moves.** Everything else sits on identical pixels forever: the 25/50 mi range rings, the
centre crosshair, the "50 mi" label, the status divider, the degree/percent glyphs, every
coastline and county line in the basemap, and the leading digit of the temperature.

Organic emitters age by cumulative luminance x time, roughly L^1.5-2, blue fastest. This
panel has **none** of the compensation phones and TVs ship (no pixel-shift, no uniformity
compensation, no logo dimming). Small AMOLEDs are typically rated ~10-30k h to 50%
luminance at full drive; 24/7 is 8,760 h/yr.

Measured from HA over 7 days:

| | |
|---|---|
| Office occupied | 47.6 h of 168 h = **28.3%** of the week |
| Ambient illuminance | median 97 lx, p75 129, max 297 — never a bright room |

Mitigations, strongest first:

1. **Blank on vacancy — 72% reduction, measured.** `binary_sensor.upstairs_office_lwr02_occupancy`.
   A ~3.5x life extension on its own. *(firmware + HA — not yet built)*
2. **Run dim.** Wear is superlinear in brightness and the room's median is only 97 lx, so
   25-40% looks fine and more than halves wear. CO5300 brightness is DCS `0x51`; drive it
   from `sensor.upstairs_office_lwr02_illuminance`. *(firmware — not yet built)*
3. **Pixel orbit — DONE.** The window is rendered oversized by `ORBIT_PX` and the crop
   walks a 24-position path (+/-6 px). ⚠️ The offset is **per refresh cycle, not per
   frame** — deriving it per frame makes the animation visibly judder (that bug was
   written and caught here; the regression check is comparing a static basemap corner
   across frames in a loop, which must be byte-identical).
4. **Dim static chrome — DONE.** Ring alpha 85 -> 55, crosshair 190 -> 110 and off pure
   white, "50 mi" label 200 -> 130. All tunable via `RADAR_RING_ALPHA`,
   `RADAR_CROSS_ALPHA`, `RADAR_ORBIT_PX`.
5. **True black everywhere else — DONE** via `darken_for_amoled()`. Black pixels are
   genuinely off and age zero.

## Next steps

1. Package the renderer as a container + HTTP endpoint on `bedrock` via dockhand.
   Endpoints: `/radar/{n}.jpg` (loop, refreshed ~10 min) and `/status.jpg`
   (bottom strip, refreshed ~60 s so the temperature isn't 10 min stale).
2. Firmware (ESP-IDF, from Waveshare's examples as the base): AXP2101 → CO5300 → WiFi →
   HTTP fetch loop into flash → banded blit. No LVGL needed.
3. Flash it. **Requires physical USB access** — the board is not reachable from sdevs.
4. Decide whether touch does anything (pause/resume, cycle zoom). Not specified.

## Open questions

- Enclosure/orientation, and whether the panel runs 24/7 or sleeps overnight.
- Whether to keep RainViewer's blue palette or remap it server-side to a classic
  green→yellow→red NEXRAD ramp. Remapping is easy here but risks misrepresenting
  intensity, so it is deliberately not done yet.
