# display-hub (formerly amoled-radar)

One server for every small display: weather radar today, aircraft next (ported from
`~/projects/git/opensky-amoled`, local-only repo). **Design agreed 2026-10-04: `docs/DESIGN.md`.**

- Repo: https://github.com/snadboy/display-hub (renamed from amoled-radar 2026-10-04; old URL redirects)
- Decisions: evolve this repo; buttons PWR = screen, BOOT = next app, KEY = app action;
  bedrock host port **8098**, DockTail VIP **`displays`**.
- **Step 1 DONE** (6981bc2): `server/hub/` package; legacy radar paths still aliased.
- **Step 2 DONE** (638bbc6): `server/hub/aircraft/` -- OpenSky poller (only while a device fetched
  states in the last 2 min), adsbdb/hexdb lookups with prefetch, ABN1 basemap bundles, AST1 binary
  states, previews. Needs `OPENSKY_CLIENT_*` and `AIR_LAT/AIR_LON` (exact home, NOT in the public
  repo) in the environment.
- **Step 3 DONE** (`firmware/hub/`, 1.51 MB of the 2 MB app slot): core (hello + names, screen
  policy, PWR/BOOT, OTA channel `hub` at `/firmware/hub.{json,bin}`), board `boards/ws_c6_216.c`
  (CST9220 touch, BOOT/KEY GPIO, PWR via AXP2101 IRQ 0x41/0x49 bit3), LVGL in the board's DMA
  strips (`core/ui.c`), aircraft app (`apps/aircraft.c`). Store: 6 slots, keys `a:<view>` /
  `w:<view>`, ordered by commit seq; mapped slots are never erased.
- **Step 4 DONE**: weather app (`apps/weather.c` + `rdl.c` + `jpeg_draw.c`; `app_t.raw` keeps LVGL
  paused). Server loop budget 2.06 MB. Both boards run the hub firmware; all buttons, touch info
  and app switching verified by the owner 2026-10-04.
- **Bedrock deploy DONE (2026-10-04):** Dockhand git stack 31 `amoled-radar` (env 11, docker-homelab
  `amoled-radar/docker-compose.yml`, manual sync) runs image `display-hub:8cd4601` as container
  `display-hub` on 192.168.86.135:8098 (image tag: see docker-homelab); humans:
  https://displays.swallow-spectrum.ts.net. Stack,
  service and volume keep the amoled-radar name on purpose (in-place replace, cache kept).
  Stack variables (Dockhand): HASS_SERVER/TOKEN, OPENSKY_CLIENT_ID/SECRET, AIR_LAT/AIR_LON.
  Firmware is published in bedrock's volume `amoled-radar_amoled-radar-cache`, `firmware/` (root
  channel) and `firmware/hub/` (both boards' channel); `firmware/radar-backup/` = the old radar fw.
  Both boards run 32d42ef-10042116 (points at bedrock). `radar-dev` on sdevs is retired;
  `~/radar-dev-cache` kept as a backup.
  **To ship a server change:** push -> CI builds ghcr.io/snadboy/display-hub:<sha> -> bump the tag
  in docker-homelab -> deploy stack 31 via the Dockhand REST API (owner's OK first):
  `curl -X POST -H "Authorization: Bearer $DOCKHAND_API_TOKEN" -d '{}'
  https://dockhand.swallow-spectrum.ts.net/api/git/stacks/31/deploy` (async job; poll
  GET .../stacks/31 for syncStatus, then the container swaps ~30 s later). Token in shareables .env. 
  **To ship firmware:** push firmware/hub -> CI release `firmware-<sha>` -> admin page Firmware tab
  (or POST /api/firmware/publish {"board","tag"} on the VIP) publishes it to a board's channel.
  Boards check every 6 h and 90 s after boot.
- opensky-amoled archived 2026-10-04: private, read-only https://github.com/snadboy/opensky-amoled
  (local copy still at ~/projects/git/opensky-amoled). Old amoled-radar images removed from sdevs
  and bedrock.
- **Step 5 IN PROGRESS (2026-10-04):** target board = Waveshare **ESP32-P4-WIFI6-Touch-LCD-3.5**
  (arrives 2026-10-05): ST7796 SPI LCD 320x480 (MOSI 20, CLK 21, CS 23, DC 26, RST 27, backlight
  PWM 28), FT6336 touch I2C (RST 29, INT 50), I2C SCL 8 / SDA 7, AXP2101, buttons BOOT/PWR/RESET
  only (no KEY), 32 MB PSRAM, WiFi via the C6 co-processor over SDIO (esp_wifi_remote/esp-hosted),
  separate sdkconfig for chip rev <3 vs 3.x. Waveshare BSP: waveshare/esp32_p4_wifi6_touch_lcd_3_5
  2.0.2 (pins/init cmds there). Planned orientation: landscape 480x320 (not confirmed by owner).
  Done: weather server renders per profile (`render.Geom`; default 480x480 byte-identical;
  480x320 = 272 px radar + 48 px strip, no orbit on LCD), loops for registered/requested
  profiles (MAX_GEOMS 4), `?w=&h=&r=&panel=` on weather paths; firmware weather app takes the
  loop height from RDL1 (store v6), sends its profile, tap opens/steps the picker.
  Fixed: store claimed only one "writing" slot (two sync tasks -> a slot erased mid-download,
  headers ANDed together); net_stream shared one static buffer between both tasks.
  CRC: manifests carry loop_crc32 / bundle_crc32; firmware reads the slot back and checks
  before committing (store v7). Found because Display 2 kept drawing a map cached by the
  shared-buffer build: header and id fine, 30% of the bytes wrong (owner confirmed clean after).
  Slow downloads (15-58 s) seen once with the screen off: not reproduced after the fixes.
  P4 build DONE (compiles, 1.38 MB): `boards/ws_p4_35.c` (landscape 480x320; rotation flags
  PANEL_*/TOUCH_* unverified), `apps/jpeg_draw_p4.c` (P4 ROM has no TJpgDec -> hardware JPEG
  decoder into PSRAM; miniz IS in the P4 ROM), target-specific sdkconfig.defaults.esp32{c6,p4},
  per-board build dirs (`BOARD=p4 ./build.sh` -> build-p4/), OTA channel `hub-p4`. ESP-IDF's
  default chip revision is 3.x only (CONFIG_ESP32P4_REV_MIN_301): read the real one with
  esptool before flashing. esp_hosted SDIO defaults: CMD 19, CLK 18, D0-D3 14-17, C6 reset 54.
  2026-10-05 (later): bedrock runs display-hub:760e931 (deployed via the Dockhand API); settings
  in hub.db; both displays run CI firmware 760e931-10051234 (channel `hub`). Test hubs removed.
  The displays need only power now (any USB charger) -- sdevs USB is for first flash, rescue
  and logs.
- Builds: `[BOARD=c6|p4] ./build.sh` (hub = bedrock by default; `HUB_SERVER_URL=...` for another)
  -> `build-<board>/display_hub.bin`. Flash by SERIAL, never by ttyACM number:
  `BOARD=c6 PORT=$(readlink -f /dev/serial/by-id/*20:6E:F1:16:A1:00*) ./flash.sh`.
- **Settings + install (2026-10-05), retiring .env:** decisions: boot default (no remembered
  app/city), admin page Tailscale-only (no login), WiFi set on the board.
  * `server/hub/settings.py`: SQLite `hub.db` in the cache volume -- secrets (write-only),
    places, air_views, devices (name, apps, start_app, weather places/start, aircraft
    view/start_level, room screen policy). Seeded once from env + devices.json. Only places /
    views some device uses are rendered / polled.
  * `admin.py` + `admin.html` on ADMIN_PORT 8081 (DockTail VIP only; LAN 8080 stays read-only):
    Devices (cards + start preview), Weather places, Aircraft views, Firmware, Connections.
  * Devices: `/device/hello` = own apps/places/view/start; `/device/<id>/state` = room screen
    policy + name (live) + `sv` boot-settings version (changed -> device restarts).
  * Firmware: no secrets; WiFi in NVS, set via Improv over USB (`core/improv.c`; C6 USB-JTAG,
    P4 UART0). `DEV_WIFI=1 ./build.sh` seeds WiFi for dev builds only.
  * CI `.github/workflows/firmware.yml` -> GitHub release `firmware-<sha>` (per board: app +
    `-full` merged image, version.txt). Hub `firmware.py`: Firmware tab publishes a release to a
    board's channel (`hub` = c6, `hub-p4` = p4); `/install` = ESP Web Tools page (needs https:
    use the VIP).
  * Verified on Display 2 (test hub :8099/:8199): seeding, live rename, restart on start-app
    change, NVS-erased board -> set-up screen -> Improv -> boot.
- **HA over MQTT (step 3, 2026-10-05):** `server/hub/mqtt.py` (paho-mqtt) publishes discovery once
  Connections -> MQTT is set (broker = HA Mosquitto add-on, mqtt://host-ha.isnadboy.com:1883, its
  own HA user `display-hub`). Per display: Online, App + City selects (switch now), Screen select
  (Auto/On/Off -> settings screen.mode), Brightness, Firmware, IP, Restart, Identify. Hub: aircraft
  in range + closest per view, OpenSky credits, Weather loops problem. Devices poll every 5 s
  (`poll_s` in the state reply; HA reads cached 5 s), report app/view/on/bright, and get one-shot
  commands in the reply. Tested end to end with a local Mosquitto + Display 2. Deployed:
  hub 760e931 (docker-homelab bf1c430, via the Dockhand API); both displays on firmware
  760e931-10051234.
  Verified in HA 2026-10-05: devices `Display 1/2` + `Display Hub`; select.display_2_app switched
  the display both ways in < 10 s. Admin Test buttons now test the typed values (blank = saved),
  hub f1ac99a (docker-homelab 9f0287a).
- **DeskRadar board: tried and DROPPED (2026-10-05).** A classic ESP32 devkit (4 MB, CH340, MAC
  b4:bf:e9:60:60:fc) + 1.28" round GC9A01 with NO touch and one button ran the hub firmware for
  a day (commit 99e19ef..67d315b had `BOARD=esp32`), but the owner found it too limited (can't
  swipe). Removed in the next commit; its original Arduino firmware was restored from
  `/mnt/shareables/firmware-backups/deskradar-b4bfe96060fc-20261005.bin` (verify OK) and the
  hub forgot it. Kept from that work: the per-device **Status strip** setting (`weather.strip`,
  `&strip=0`, profile key `_nostrip`), round-glass layout in render.py (`Geom.round`), and
  `store_begin(key, urgent)` (the view on screen may evict the oldest unmapped bundle).
- **Panning (2026-10-05, hub 63a5ea4, firmware d701880 via OTA):**
  view ids `<view>@<dx>,<dy>` (50 mi steps east/north; helpers `core.parse_pan/pan_label/
  pan_centre`). Weather: up to 3 steps, rendered on demand per profile on `pan_loop` (~20 s),
  kept 15 min after the last request; offset pill burned into frames (tap the top 80 px =
  home); `/weather/ui/pan.jpg` "Loading..." pill over the dimmed last frame. Aircraft: 1 step;
  poller box = radius + 71 mi while <= 25 sq deg (still 1 OpenSky credit), states cut to each
  view's radius (`_within`); panned maps: home mark, no rings, header lat/lon = real home.
  Firmware: weather swipes judged on release (50 px); aircraft LVGL LV_EVENT_GESTURE on
  s_radar (GESTURE_BUBBLE cleared) + amber offset pill, maps staged by the net task and
  adopted by the UI tick (live swap, no restart). Auto home after 10 min; city change resets.
  HA: per-display Pan select (Centred + 8 directions at 50 mi). Store keys for pans:
  `w:~<vi><dx+'d'><dy+'d'>`, `a:~<dx><dy><view>`. Verified via HA on Display 60FC (weather
  and aircraft); swipes on the C6 touch boards not yet tried by the owner.
- **Later 2026-10-05 (hub 67d315b, firmware 67d315b):**
  * Aircraft per-display settings: `labels_mi` (callsign/alt labels at that zoom or closer;
    0 never, 999 always; default 5 small / 10 large; a tap on empty map flips until next
    zoom), `trail_s` (unselected trails 0-240 s, one point per ~30 s poll; default 0 small /
    60 large), `types` (lookup.kind: airline / business (fractional+charter list) / private
    (N-reg) / other; sent as `states.bin?types=`, filtered on the hub). OpenSky's category is
    ~always 0, hence callsign-based. The "no ORD after pan" report was just the zoom level.
  * Weather "Active storm": built-in place id `active` (settings.places() appends it; not
    stored, admin can't edit it). `weather/storm.py` scans the latest RainViewer frame
    (z5 tiles) within STORM_SEARCH_MI (800) of the first real place, weights colours
    (tans + light cyan clear-air rings = 0, darker blue <= 0.7, yellow 8-12, red 25,
    pink 35), best 100 mi window by summed-area table; hysteresis KEEP 0.6, QUIET 300
    -> home + "No active storms"; centre snapped to 0.25 deg. Label pill in the frames +
    status strip. RainViewer ignores the colour-scheme parameter now (one palette).
- **2026-10-05/06 (hub 75520d0, firmware 75520d0 on both C6):**
  * Views show 10% beyond radius/range (`VIEW_MARGIN` 1.1 in weather/render.py and
    aircraft/basemap.py) so the outer ring clears the bezel; strip-off progress bar lifted
    6 px with times inset `r*0.6` from the rounded corners.
  * NOAA QC mask: never on Active; `apply_mask` returns None (frame left unmasked) when it
    would remove >25% of REAL rain -- opaque and not light cyan (b > 200, b > r), because
    the clear-air rings round radar sites are opaque light cyan and are exactly what the
    mask removes (counting them disabled QC for St. Louis).
  * Busy loops: over budget -> 64 colours -> fewer real frames keeping the in-betweens ->
    fewer in-betweens. Radar layer upscaled with NEAREST. Firmware caps a frame at 112 ms
    (FRAME_MAX_MS) so a short loop plays at full-loop pace instead of stretching to 5 s.
  * Battery: AXP2101 detection/ADC/fuel gauge enabled in pmu_init; `board_power()` read every
    60 s, sent as `&batt=&bmv=&chg=&usb=` (batt -1 = none); admin page line + HA Battery /
    Charging / Battery voltage (only if batt >= 0) and USB power entities. John's display
    has a cell (100%, 4.08 V on first read -- gauge uncalibrated); Robert's has none.
  * Admin preview: "Preview is being rendered..." placeholder + 10 s retries (a new profile
    renders on first request; after a hub restart loops take minutes).
  * Deploy gotchas: GitHub hosted runners once failed to start a job ("not acquired");
    fallback = `docker build ./server` + push to ghcr with `gh auth token` (has
    write:packages). Dockhand once reported "synced" without swapping the container:
    redeploy and check `docker inspect` on bedrock.
  * HA MQTT outage 2026-10-05 19:07: HA's own broker login rejected ("not authorised"),
    every MQTT entity unavailable; a Mosquitto restart did NOT fix it -- HA had a pending
    MQTT *reauth* flow that the owner completed in the UI (snadboy login).
- **2026-10-06 (hub + firmware 92392fd):** third app **Aircraft listing** (`airlist`):
  `server/hub/airlist.py` renders `/airlist/<view>/list.jpg?w=&h=&r=&mi=&page=` (nearest
  first, two lines per flight, page wraps, drifts 2 px every 2 min for burn-in) from the
  aircraft app's poller (`aircraft._poller(view)`, touched per request) and cached
  `lookup.lookup(..., fetch=False)`; `firmware/hub/main/apps/airlist.c` is a raw app that
  fetches it every 5 s (KEY/tap = next page, page 1 again after 30 s). Device settings
  `airlist: {view, radius_mi}`; in `settings.APPS`, hello, boot_version, views_in_use.
  "Aircraft" renamed "Aircraft radar" (admin APP_NAMES, mqtt APP_LABEL -> HA select option,
  firmware app name). Admin editor: app settings on tabs (`appTab`), an app not ticked
  under Apps -> tab struck through + `<fieldset disabled>`. Place / view dialogs: a
  "Look up..." button beside Name opens `#ldlg` (wireLookup on `l_find`).
- **Open items:** P4 3.5" board bring-up when it arrives (install page first; chip revision,
  rotation flags unverified); owner to rename the displays and delete the six Dockhand stack
  variables (hub.db has them now); rotate DOCKHAND_API_TOKEN (it was pasted into a chat).
- **USB on sdevs** (pve-faraday VM 121): both pinned by physical port 2026-10-04 --
  `usb0: host=3-1.4.1` (weather board), `usb1: host=3-1.3` (aircraft board).
  | Hub name | Started as | USB serial = MAC | Host port | sdevs | IP |
  |---|---|---|---|---|---|
  | **Display 1** | weather board | D4:05:92:B8:F9:0C | 3-1.4.1 | /dev/ttyACM0 | .227 |
  | **Display 2** | aircraft board | 20:6E:F1:16:A1:00 | 3-1.3 | /dev/ttyACM1 | .221 |
  Both run the hub firmware (Display 1 migrated by OTA 2026-10-04). Names live in the hub's
  devices.json (`/device/<id>/name?set=...`); each board shows an identity card (name, MAC, IP,
  fw) for 3 s at boot and on a BOOT hold. Say "Display 1/2" to the owner, never "#1/#2".
  Buttons verified on BOTH boards 2026-10-04 (every press is logged as `hub: button X short|long`):
  BOOT short = next app, BOOT hold = identity card, KEY short = zoom / city picker, KEY hold =
  closest plane (+ info lookup), PWR = screen. Earlier "KEY does nothing" was a mix-up over
  which button was pressed, not hardware.
  Opening the serial port can reset a board. Logs: a pyserial read inside the IDF container
  (the ports are root:dialout and snadboy is not in dialout).

The weather notes below predate the hub and still describe the running system.

---

# amoled-radar (weather)

Animated weather radar + outdoor temp/humidity on a **Waveshare ESP32-C6-Touch-AMOLED-2.16**.

**Status:** server and firmware running on board #1 (see Firmware section).
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

### ⚠️ Pin conflict between Waveshare's own sources — resolve on bring-up

| | ESP-IDF examples (`user_config.h`) | XiaoZhi firmware (`boards/waveshare/esp32-c6-touch-amoled-2.16/config.h`) |
|---|---|---|
| Panel CS  | **GPIO5**  | **GPIO15** |
| Touch INT | **GPIO15** | **GPIO5**  |

Everything else agrees (QSPI CLK=0, D0-3=1-4, I2C SDA=8 SCL=7, touch RST=11, panel RST
via AXP2101). One source has CS and touch-INT swapped. The wrong CS just gives a blank
panel, so try GPIO5 first and swap if dark. **RESOLVED 2026-10-03: CS = GPIO15, touch INT = GPIO5. The XiaoZhi config is right;
the ESP-IDF examples' `user_config.h` is WRONG.** An earlier note here said the opposite,
because `09_LVGL_V9_Test` (CS=5) lit the panel -- but it only works BY ACCIDENT: its touch
init drives GPIO15 (which it thinks is touch INT) low and leaves it there, holding the real
panel chip-select asserted. Our own firmware with CS=5 never touched GPIO15 and the panel
stayed black through four builds, while every draw call reported success. Setting CS=15
lit it immediately (colour-bar test pattern held until KEY is pressed).
**Lesson: "the vendor demo works" proves the hardware, not the demo's pin map.**

### Buttons — the enclosure has three on top

| Button | Pin | Use |
|---|---|---|
| BOOT | **GPIO9** (XiaoZhi config) | download mode if held at power-on; XiaoZhi uses it as a runtime button, so it is a safe fallback |
| KEY  | **GPIO10**, active LOW (needs pull-up) -- found 2026-10-03 with `firmware/bringup-buttons` | **opens the city picker; hold = screen off/on** |
| PWR  | AXP2101 PWRON: IRQ reg 0x49 (bit1 falling, bit0 rising, bit3 SHORT, bit2 LONG), **and GPIO18 goes HIGH while held** | power -- long press powers off |

Enclosure top, left to right: **Boot, Power, Key** (labelled). Confirmed by pressing each
while `bringup-buttons` logged GPIO 6/9/10/14/18 and the AXP2101 IRQ register.

## Cities and the picker

Four views, defined in `server/hub/weather/__init__.py` (`DEFAULT_CITIES`; override with `RADAR_CITIES` JSON).
City centres are public coordinates, so no home position is stored anywhere.

| id | City | Centre | Temp/humidity source |
|---|---|---|---|
| `geneva`  | Geneva, IL    | 41.8875, -88.3054 | **HA** `sensor.outdoor_temperature` / `_humidity` (the owner's own sensor) |
| `stlouis` | St. Louis, MO | 38.6270, -90.1994 | NWS nearest station (**KCPS** Downtown Airport), Open-Meteo fallback |
| `canton`  | Canton, MI    | 42.3087, -83.4822 | NWS nearest station (**KYIP** Willow Run), Open-Meteo fallback |

NWS observations are METAR-derived and report **whole degrees C**, so station temps step in
~2 F increments, and two stations can report identical values (KCPS and KYIP both read
12 C / dewpoint 8 C on 2026-10-03, giving the same humidity to 12 decimal places -- looks
like a caching bug, isn't). NWS wants a contact in the User-Agent.

**KEY button behaviour (firmware), agreed 2026-10-03:**

| Input | Screen on | Screen off |
|---|---|---|
| Short press (< 1 s) | open picker / next city | **wake the screen** (no picker) |
| Hold >= 1 s | screen off (closes picker, no city change) | screen on |

After 250 ms of holding, show a "Hold to turn off" pill with a bar filling to the 1 s mark,
so releasing early still counts as a short press. Turn the panel off with the standard
MIPI DCS display-off / sleep-in commands rather than cutting the AXP2101 rail, so it wakes
instantly without re-initialising. **A manual off overrides occupancy blanking**: walking
into the office must not wake a screen that was turned off by hand.

**Picker behaviour (firmware):** KEY opens a list with the current city highlighted; each
further KEY press moves the highlight; 3 s without a press picks the highlighted city;
tapping a row on the touchscreen picks it immediately. Radar is paused and dimmed behind
it. All three loops (~1-2.5 MB each) fit in flash together, so switching is instant.

The status strip now carries the city name (centre, above the time).

**Local time per city.** Each city has a `tz` (Canton is `America/Detroit`). The
container runs on UTC, and `time.strftime()` originally printed UTC -- Canton read
3:00 PM at 11:00 AM local. All clocks go through `weather.clock(epoch, city)`.

**Progress bar** (burned into each radar frame, `render.progress_bar`): loop start time
left, latest time right, amber fill + playhead at this frame's position. Dark gradient band
and black-outlined labels keep it readable over heavy returns. Nudged by the orbit offset.

**Towns** (`places` per city, `render.draw_places`): 4 per city, drawn ON TOP of the radar
so they stay readable in storms, and before the orbit crop so they drift with it. A label
flips to the left of its dot if it would hit the centre crosshair, another label or the
edge (Canton's "Ann Arbor" ran into the crosshair otherwise). Toledo was left out of Canton
because it lands on the progress bar.

**Entire country** (`id: us`): the lower 48 via `lon_span: 61` (fills the panel width)
instead of `radius_mi`; basemap zoom 5, RainViewer zoom 3 at 512 px (~7.8 km/px, close to
the ~9 km/px shown). No rings or crosshair. Places include your three cities flagged with a
4th element `1` (amber markers) plus six large cities. No national temperature exists, so
the strip shows Geneva's HA reading labelled "Geneva · time" (`status_label`).

**Clear-air suppression** (national only, `suppress_clear_air`): RainViewer's lowest band
is a ramp of semi-transparent tans/greys, ~rgba(117,112,98,52) to rgba(222,208,151,190) --
43.6% of all national returns, mostly clear-air echo (insects/birds) ringing radar sites in
the evening plus a halo round real storms. Filtered on the raw tile before resampling
(after it, colours blend and the test fails).

On **city views** it is **temperature-gated** (`RADAR_CLEAR_AIR_MIN_F`, default 40): filtered
when that city's own current reading is >= 40 F, kept when colder. Whether RainViewer's
faint band also carries light snow is unverified, and snow can't reach the ground at 40 F,
so this hides clear-air echo for most of the year without risking hiding snow in winter.
No reading -> keep the band. The manifest reports `clear_air_filtered`.

Found because St. Louis showed a tan field ringing the KLSX radar over ~1/3 of the view
at 27% humidity: 62,403 tan pixels before, 29 after. A light-blue patch remained -- that
is in the rain band and can't be filtered without hiding real light rain (at that humidity
it is likely virga or October bird migration).

**NOAA quality control mask** (`render.qc_mask`, all views): RainViewer appears to serve
near-raw reflectivity, so it shows birds, insects and clutter that weather apps remove. NOAA/
NCEP's quality-controlled CONUS base-reflectivity mosaic (dual-pol filtered) is a free WMS:
`https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows`, frames every 1-2 min,
~2 h kept (60 frames), any EPSG:3857 bbox. It is blockier than RainViewer and uses a
different palette, so it is a MASK: RainViewer pixels survive only where NOAA (nearest frame
within 10 min) has echo, dilated by `RADAR_QC_DILATE_KM` (4 km) for its coarser grid and the
timestamp skew. Applied to real frames before tweening. NOAA unreachable -> frames go out
unmasked rather than failing (manifest `qc_masked` = "masked/real").

Found 2026-10-03: a light-blue patch east of St. Louis at 27-29% humidity that the owner's
weather app didn't show. NOAA had ZERO returns there (October bird migration along the
Mississippi flyway, beside KLSX). Validated both ways before shipping: St. Louis radar pixels
-92%, the Texas storm -1%, national -25% (Plains specks; all real systems kept).
Side effect: anything outside US radar coverage (Mexico, Caribbean) is blanked on the national
view. The tan clear-air filter is now largely redundant but harmless, and is kept.

**Ring scale fixed:** rings were scaled from the oversized canvas, which includes the orbit
bleed, so the "50 mi" ring was drawn ~4% large. Now divided out (verified: Chicago measures
34 mi vs 35 true).

⚠️ `decorate()` runs on the oversized canvas, so anything drawn there must sit
`2*ORBIT_PX` in from the edges -- the "50 mi" label was being clipped to "50 m" at some
orbit positions until that was fixed.

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

`server/hub/weather/render.py` — composites frames; run it with `HASS_SERVER`/`HASS_TOKEN` set.

```bash
cd server
RADAR_OUT=./out RADAR_CACHE=./cache RADAR_FRAMES=12 python3 -m hub.weather.render
# the whole service:
HUB_CACHE=./cache python3 -m hub
```

Env: `RADAR_LAT`/`RADAR_LON` (centre), `RADAR_FRAMES`, `RADAR_OUT`, `RADAR_CACHE`,
`RADAR_TEMP_ENTITY`/`RADAR_HUM_ENTITY`, `RADAR_DARK_GAMMA`/`RADAR_DARK_SCALE`.

Output: ~24–45 KB JPEG per 480×480 frame (bigger when there's heavy precipitation).

Endpoints (hub layout, step 1 done 2026-10-04): core `/device/hello`, `/device/<id>/state`,
`/devices.json`, `/firmware.{json,bin}`, `/healthz`; weather `/weather/views.json`,
`/weather/<id>/{manifest.json,loop.bin,status.jpg,frame/<n>.jpg}`, `/weather/ui/{picker,hold}.jpg`.
Legacy aliases kept for the running firmware: `/cities.json`, `/c/<id>/...`, `/ui/...`, `/device.json`
(legacy devices show in `/devices.json` as `ip:<addr>`).

⚠️ The basemap cache key includes the location (`base_z9_<lat>_<lon>_o<orbit>.png`). It was
once plain `base_z9.png`, which every city would have silently shared.

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

## Firmware (firmware/radar) -- running on the board since 2026-10-03

ESP-IDF 5.5.3 in Espressif's container (`./build.sh`, `./flash.sh`); nothing installed on
sdevs. Board reaches sdevs via Proxmox USB passthrough: **pve-faraday VM 121
`usb0: host=303a:1001`** -> `/dev/ttyACM0` (hot-plugged, no reboot).

* **Display:** CO5300 via `espressif/esp_lcd_sh8601` 1.0.0, QSPI 40 MHz, **CS GPIO15**.
  Panel reset = power-cycle AXP2101 ALDO3. Brightness is in the init sequence itself.
  1 s colour bars at boot prove the panel path (they would have exposed the CS bug at once).
* **Frame format RDL1 (replaced JPEG, 2026-10-04):** JPEG decode took 240 ms/frame and,
  with no framebuffer, the panel showed every frame as a visible downward WIPE. Now the
  server sends `/c/<id>/loop.bin`: the bare map once as raw RGB565, plus per frame a
  zlib-compressed 8-bit layer (0 = keep map pixel, 1..255 = palette). The board copies map
  rows from a memory-mapped slot and patches only changed pixels, inflating with the ROM's
  `tinfl_decompress`. Measured on the board: **66 ms/frame** (map copy 17, inflate+patch 44,
  panel 4). Loops are also SMALLER than JPEG (Geneva 415 KB vs 1148 KB). The server lowers
  in-betweens (3 -> 2 -> 1 -> 0 per 10 min) until a loop fits `RADAR_LOOP_BUDGET` (2.4 MB).
  Loop paced at ~5 s + 1.5 s dwell regardless of frame count.
  Next lever if the wipe is still visible: per-frame "dirty strip" flags so unchanged
  16-row strips are skipped entirely (clear day ~3 ms/frame).
* **Decode (status strip, picker only now):** C6 **ROM** TJpgDec (`esp32c6/rom/tjpgd.h`, RGB888 out), drawn in 16-row strips
  with ping-pong DMA buffers. **~240 ms per 480x424 frame = ~4.2 fps** -- the hard limit
  with JPEG on this chip. A faster "map from flash + changed pixels" format is the option
  if that is ever not good enough.
* **Flash cache:** `frames` partition, 5 slots x 2.375 MB for 4 views + a spare; header
  written last so a loop is complete or invisible. Current view checked every 60 s, others
  every 2 h. Slots of views the server no longer lists count as free (`store_set_views`) --
  without that a retired view (the old national one) would hold a slot forever.
* **KEY (GPIO10):** tap = picker (server-rendered JPEG; KEY steps, 3 s idle chooses, view
  saved in NVS); hold 1 s = screen off (hint pill from 250 ms); any press wakes. Manual off
  overrides occupancy; manual wake in an empty room lasts 10 min.
* **Server-decided screen:** `/device.json` -- off after the office LWR02 reports empty for
  5 min; brightness `140 + 0.6*lux`, max 255 (floor raised from 50: 69/255 was too dark).
* **OTA:** device fetches `/firmware.json` 90 s after boot and every 6 h; installs if the
  version string differs. **Rollback enabled**: an image must draw a frame and call
  `ota_mark_good()` or the bootloader reverts. Verified end to end 2026-10-03.
  The binary embeds the WiFi password, so it is published ONLY to the server's local
  volume (`/cache/firmware/{firmware.bin,version.txt}`), never to GitHub.
  Publish: `./build.sh && cp build/amoled_radar.bin <cache>/firmware/firmware.bin && cp version.txt <cache>/firmware/`.

**Rounded glass:** the panel's corners are rounded (~50-56 px radius, measured from a photo).
`render.CORNER_R = 56`; status text sits `SIDE_INSET = 40` from the sides, the "50 mi" label
is inset, and town labels treat edges/corners as HARD limits and overlaps as SOFT: your own
cities always get a label; a reference town that can't fit is dropped (Columbus was, so the
Midwest view uses Fort Wayne).

**Midwest view** replaced "Entire country" (too small at ~9 km/px on 2.16"): lon_span 10.4
around 40.5N 86.8W, RainViewer z5/512 (~1.86 km/px), basemap z7. Flag `wide` = no rings.

**Dev server:** while bedrock awaits a Dockhand Sync, a copy runs on sdevs
(`docker run ... -p 8098:8080 -v ~/radar-dev-cache:/cache amoled-radar:dev`) and the board
is built with `RADAR_SERVER_URL=http://192.168.86.220:8098`.

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
