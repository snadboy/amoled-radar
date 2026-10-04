# display-hub design

Agreed 2026-10-04. One server does the heavy lifting for every small display in the house;
devices only draw, take touch and read buttons. Grown out of amoled-radar (weather), with
opensky-amoled (aircraft) ported in.

## Goals

- Varied ESP32 hardware, one firmware. Heavy work and secrets live on the server.
- A smaller codebase that drifts less: fixes to fragile upstreams are a Python edit + redeploy.
- Reliance on bedrock is acceptable. If it is down, the device keeps showing its cached data.

## Repo layout (target)

```
server/hub/        one Python service, one container
  core.py          device registry, per-device screen state (HA occupancy/lux), firmware/OTA
  weather/         RainViewer + NOAA QC, RDL1 loops, status strip (was render.py/serve.py)
  aircraft/        OpenSky poller, adsbdb/hexdb lookups, basemap bundles, previews
firmware/          one ESP-IDF 5.5 project
  boards/          board_ws_c6_216.c (+ future P4 boards); each fills a device profile
  apps/            weather.c, aircraft.c
  core/            net, store, ota, keys, app switcher, LVGL glue
```

## Server

Container `display-hub`, image `ghcr.io/snadboy/display-hub`, port 8080 inside, published on
bedrock as **8098** for LAN devices (no TLS on the device). DockTail VIP **`displays`** for
humans: a page listing registered devices with previews.

| Path | Purpose |
|---|---|
| `GET /device/hello?id=<mac>&board=&w=&h=&r=&panel=&psram=&slot=&fw=` | Register the device profile. Returns enabled apps, default app, views. |
| `GET /device/<id>/state` | Screen on/off + brightness for every app (was `/device.json`; replaces opensky's hard-coded night dimming). |
| `GET /firmware.json`, `/firmware.bin` | OTA, unchanged. |
| `/weather/<view>/manifest.json`, `loop.bin`, `status.jpg` | Was `/c/<id>/...`. Old paths stay as aliases until both boards are migrated. |
| `/aircraft/<view>/bundle.bin` | Basemaps for every zoom level in one bundle, rings and towns baked in, sized to the profile's shape and safe area. |
| `/aircraft/<view>/states.bin?since=<seq>` | Fixed 32-byte binary records, pre-filtered to radius + airborne. 304 if unchanged. `?fmt=json` for humans. |
| `/aircraft/info/<icao>?cs=&lat=&lon=` | adsbdb, hexdb fallback, route-fits-path check, shared cache. Small JSON. |
| `/aircraft/<view>/preview.png?profile=` | What a device should show. Verify the server before touching firmware. |

- **One OpenSky poll per view, regardless of device count.** Polling runs only while some
  device fetched `states` in the last ~2 min (replaces the device's `g_paused`); the
  4,000-credit daily budget is shared.
- **Lookups prefetched** for planes in view; each `states` record flags "info cached", so a
  tap is one LAN round trip to a warm cache.
- **Source behind an interface**, so a local ADS-B receiver could replace OpenSky later.
- States are binary, not JSON: cJSON on ~200 aircraft would take ~150 KB of the C6's heap.

## Device

- **Apps** implement `enter / leave / tick / key / touch` and own the screen while active.
  - *Aircraft* is an LVGL app. Its background is an LVGL image pointing at a memory-mapped
    flash slot; planes, trails, labels and the info panel are drawn live, with dead-reckoning
    on the device. Port of opensky's `radar_ui.cpp`.
  - *Weather* draws straight to the panel (`rdl.c`). LVGL's draw buffers reuse the board's
    two DMA strip buffers, so the two paths don't double the RAM.
- **Store of bundles.** Radar's slot store generalised: the header's offset/length table
  names sections (maps, palette, frames). An RDL1 loop is one kind of bundle, aircraft
  basemaps another. `hello` reports the slot size and the server sizes bundles to fit
  (as it already does with `RADAR_LOOP_BUDGET`). Budget: 6 slots x ~2.07 MB = 4 weather
  views + 1 aircraft view + 1 spare.
- **Partition table is frozen** at radar's layout (2 x 2 MB OTA apps + 12.4 MB `frames`),
  so board #1 can take the hub firmware over OTA. **The LVGL build must stay under 2 MB.**
- **No TLS on the device.** OAuth, `certs.h` and the streaming parser move to the server,
  freeing roughly 40+ KB of the C6's ~172 KB heap.
- **Profiles.** A Kconfig choice picks the board; it reports resolution, corner radius,
  panel type, PSRAM and slot size in `hello`. P4 boards with PSRAM can hold backgrounds in
  RAM, which makes smooth zoom/pan possible later.

### Buttons

| Button | Short press | Hold |
|---|---|---|
| **PWR** | Screen off/on (manual off overrides occupancy) | Power off (fixed in the AXP2101) |
| **BOOT** | Next app, with a name toast | -- |
| **KEY** | App action: weather opens the city picker, aircraft cycles zoom | Aircraft: select the closest plane |

This moves weather's screen-off from a KEY hold to PWR.

## Order of work

1. Restructure the server; keep legacy radar paths as aliases so the deployed weather board
   keeps working.
2. Aircraft on the server: poller, `states`, `info`, bundles, `preview.png`. Verify with curl
   and previews, no firmware involved.
3. Firmware: radar's IDF project + LVGL component + CST9220 touch (keep opensky's fix for
   Waveshare's X-coordinate bug) + app interface + aircraft app. USB-flash board #2; delete
   the Arduino sketch.
4. Weather as an app + app switching. Check the build is < 2 MB, then OTA board #1.
5. Device profiles, then a bigger screen.
