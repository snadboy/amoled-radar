// Aircraft app: live planes around the hub's view centre.
//
// The hub does the heavy lifting (OpenSky, lookups, the map). This draws only the
// live overlay -- planes, trails, labels, the info panel -- dead-reckoning between
// polls so motion stays smooth. Ported from opensky-amoled's radar_ui.cpp.
//
// Background: the hub's ABN1 bundle (one RGB565 map per zoom, towns/rings/home mark
// baked in) cached in a flash slot and drawn by LVGL straight from mapped flash.
// Data: /aircraft/<view>/states.bin every 5 s (304 when unchanged); a tap asks
// /aircraft/info/<icao> for type, registration, owner and a vetted route.
//
// Touch: tap a plane for details, the range badge to zoom, empty map to toggle labels.
// A swipe pans the map 50 mi (one step each way; the hub's poll box covers it); the
// offset pill, 10 min without a swipe, or the hub (HA) bring it home. A new map is
// staged by the net task and swapped in by the UI tick, so it changes without a restart.
// KEY: short = zoom, hold = the airborne plane nearest home (zoomed to fit).
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "app.h"
#include "board.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "net.h"
#include "store.h"
#include "ui.h"

static const char *TAG = "aircraft";

#define MAX_AIRCRAFT            200
#define MAX_LEVELS              4
#define TRAIL_POINTS            8      // one per poll; only the selected plane draws them all
#define MAX_EXTRAPOLATE_S       60
#define STATES_MS               5000
#define PIXEL_SHIFT_S           180
#define URL_MAX                 256

// ---------------------------------------------------------------- data
typedef struct {
    uint32_t icao;
    char callsign[9], squawk[5];
    float lat, lon, alt_m, speed_ms, track_deg, vrate_ms;   // NAN = unknown
    uint32_t time_position;
    uint8_t category, flags;
} aircraft_t;
#define F_GROUND 1

typedef struct {
    aircraft_t a;
    float trail_lat[TRAIL_POINTS], trail_lon[TRAIL_POINTS];
    uint8_t trail_n, trail_head;
    bool seen, on_screen;
    int16_t sx, sy;
} track_t;

// Server status codes (states header). HUB_DOWN is ours: the hub itself is unreachable.
enum { ST_OK, ST_AUTH, ST_RATE, ST_ERROR, ST_IDLE, ST_HUB_DOWN = 100, ST_NONE };

// A finished poll, handed from the net task to the UI: the net task fills it only
// while `ready` is false; the UI consumes it and clears `ready`.
static struct {
    aircraft_t items[MAX_AIRCRAFT];
    int count;
    uint32_t server_time;
    volatile bool ready;
} s_batch;
static volatile int s_status = ST_NONE;
static volatile int s_credits = -1;

typedef struct { char iata[5], city[24]; } airport_t;
typedef struct {
    uint32_t icao;
    bool pending, done, has_route, is_private;
    airport_t origin, dest;
    char type[40], registration[12], owner[32], country[24];
} info_t;
static info_t s_info;                     // guarded by s_info_mx
static struct { uint32_t icao; char cs[9]; float lat, lon; volatile bool pending; } s_req;
static SemaphoreHandle_t s_info_mx;

// ---------------------------------------------------------------- view + bundle
typedef struct {
    uint16_t range_mi;
    float mpp;                            // Web Mercator metres per pixel
    lv_image_dsc_t img;
} level_t;

static char s_view[16] = "home";
static level_t s_levels[MAX_LEVELS];      // the map on screen (UI side)
static int s_nlevels, s_level, s_slot = -1;
static double s_home_mx, s_home_my, s_home_lat, s_home_lon;   // map centre (Mercator) / home
static bool s_bundle_shown;
static int s_map_dx, s_map_dy;            // the pan of the map on screen

// A mapped bundle, handed from the net task to the UI: the net task fills it only while
// `ready` is false; the UI adopts it and clears `ready`.
typedef struct { level_t lv[MAX_LEVELS]; int n, slot, dx, dy; double mx, my, lat, lon; } mapset_t;
static mapset_t s_stage;
static volatile bool s_stage_ready;
static char s_loaded[16];                 // net task: store key of the bundle staged or on screen

#define PAN_MAX      1
#define PAN_IDLE_MS  (10 * 60 * 1000)
static volatile int s_dx, s_dy;           // wanted pan, in 50 mi steps east / north
static volatile bool s_pan_dirty;         // net task: fetch the new map and states now
static int64_t s_pan_at, s_gesture_at;

// ---------------------------------------------------------------- UI state
static track_t s_tracks[MAX_AIRCRAFT];
static int s_track_count;
static uint32_t s_server_time;
static int64_t s_batch_ms;
static bool s_labels_flip;                // a tap on empty map flips the zoom rule until the next zoom
static int s_labels_mi = 999;             // labels at this zoom (mi) or closer; 0 = never (hub setting)
static int s_trail_pts = 2;               // unselected planes' trail, in polls (~30 s each); hub setting
static char s_types[48];                  // "&types=airline,private" when not every kind is shown
static uint32_t s_selected;
static volatile bool s_active, s_screen_on = true;
static TaskHandle_t s_net;

static lv_obj_t *s_root, *s_map, *s_radar, *s_msg;
static lv_obj_t *s_pills[5];            // range, clock, status, attribution, offset: labels keep out
static lv_obj_t *s_pan_label;
static lv_obj_t *s_range_label, *s_clock_label, *s_wifi_label, *s_status_label;
static lv_obj_t *s_panel, *s_panel_title, *s_panel_route, *s_panel_aircraft, *s_panel_body;

static const double R_EARTH = 6378137.0;
static const double DEGR = M_PI / 180.0;

static int64_t ms(void) { return esp_timer_get_time() / 1000; }

// ---------------------------------------------------------------- geometry
static uint32_t now_unix(void)
{
    time_t t = time(NULL);
    if (t > 1700000000) return (uint32_t)t;
    return s_server_time + (uint32_t)((ms() - s_batch_ms) / 1000);   // no NTP yet: run off the hub's clock
}

static void project(double lat, double lon, int32_t *x, int32_t *y)
{
    double mx = lon * DEGR * R_EARTH;
    double my = log(tan(M_PI / 4 + lat * DEGR / 2)) * R_EARTH;
    double mpp = s_levels[s_level].mpp;
    *x = BOARD.w / 2 + (int32_t)lround((mx - s_home_mx) / mpp);
    *y = BOARD.h / 2 - (int32_t)lround((my - s_home_my) / mpp);
}

// Dead-reckon the reported position forward to now.
static void current_position(const aircraft_t *a, double *lat, double *lon)
{
    *lat = a->lat; *lon = a->lon;
    if (isnan(a->speed_ms) || isnan(a->track_deg)) return;
    int32_t dt = (int32_t)(now_unix() - a->time_position);
    if (dt <= 0) return;
    if (dt > MAX_EXTRAPOLATE_S) dt = MAX_EXTRAPOLATE_S;
    double d = a->speed_ms * dt;
    *lat += d * cos(a->track_deg * DEGR) / 111320.0;
    *lon += d * sin(a->track_deg * DEGR) / (111320.0 * cos(a->lat * DEGR));
}

static float miles_from_home(double lat, double lon, float *bearing_deg)
{
    double x = (lon - s_home_lon) * DEGR * cos((lat + s_home_lat) * 0.5 * DEGR);
    double y = (lat - s_home_lat) * DEGR;
    if (bearing_deg) { double b = atan2(x, y) / DEGR; *bearing_deg = b < 0 ? b + 360 : b; }
    return sqrt(x * x + y * y) * 3958.8;
}

// FlightRadar-ish altitude ramp: orange low -> yellow -> green -> cyan -> violet high.
static lv_color_t altitude_color(float alt_m, bool on_ground)
{
    if (on_ground || isnan(alt_m)) return lv_color_hex(0x9a9a9a);
    static const struct { float ft; uint32_t rgb; } stops[] = {
        { 0, 0xff6a2b }, { 5000, 0xffb52b }, { 10000, 0xf2f23c }, { 20000, 0x45f07a },
        { 30000, 0x2ad4ff }, { 40000, 0xb07cff },
    };
    const int n = sizeof(stops) / sizeof(stops[0]);
    float ft = alt_m * 3.28084f;
    if (ft <= stops[0].ft) return lv_color_hex(stops[0].rgb);
    for (int i = 1; i < n; i++)
        if (ft <= stops[i].ft) {
            float t = (ft - stops[i - 1].ft) / (stops[i].ft - stops[i - 1].ft);
            return lv_color_mix(lv_color_hex(stops[i].rgb), lv_color_hex(stops[i - 1].rgb), (uint8_t)(t * 255));
        }
    return lv_color_hex(stops[n - 1].rgb);
}

static void format_altitude(char *buf, size_t n, float alt_m)
{
    if (isnan(alt_m)) { strlcpy(buf, "--", n); return; }
    int ft = (int)lroundf(alt_m * 3.28084f);
    if (ft >= 18000) snprintf(buf, n, "FL%03d", ft / 100);   // flight level above transition altitude
    else snprintf(buf, n, "%d", (ft + 50) / 100 * 100);
}

// ---------------------------------------------------------------- batch merge (UI)
static track_t *find_track(uint32_t icao)
{
    for (int i = 0; i < s_track_count; i++) if (s_tracks[i].a.icao == icao) return &s_tracks[i];
    return NULL;
}

static void merge_batch(void)
{
    for (int i = 0; i < s_track_count; i++) s_tracks[i].seen = false;
    for (int i = 0; i < s_batch.count; i++) {
        const aircraft_t *in = &s_batch.items[i];
        track_t *t = find_track(in->icao);
        if (t) {
            if (in->time_position != t->a.time_position) {
                t->trail_lat[t->trail_head] = t->a.lat;
                t->trail_lon[t->trail_head] = t->a.lon;
                t->trail_head = (t->trail_head + 1) % TRAIL_POINTS;
                if (t->trail_n < TRAIL_POINTS) t->trail_n++;
            }
        } else if (s_track_count < MAX_AIRCRAFT) {
            t = &s_tracks[s_track_count++];
            memset(t, 0, sizeof(*t));
        } else {
            continue;
        }
        t->a = *in;
        t->seen = true;
    }
    int w = 0;
    for (int r = 0; r < s_track_count; r++)
        if (s_tracks[r].seen) { if (w != r) s_tracks[w] = s_tracks[r]; w++; }
    s_track_count = w;
    s_server_time = s_batch.server_time;
    s_batch_ms = ms();
    if (s_selected && !find_track(s_selected)) s_selected = 0;
}

// ---------------------------------------------------------------- drawing
static void draw_text(lv_layer_t *layer, const char *txt, int32_t x, int32_t y, int32_t w,
                      const lv_font_t *font, lv_color_t color)
{
    lv_draw_label_dsc_t d;
    lv_draw_label_dsc_init(&d);
    d.text = txt;
    d.text_local = 1;                     // LVGL copies it; draw tasks run after we return
    d.font = font;
    d.color = color;
    lv_area_t a = { x, y, x + w - 1, y + 2 * font->line_height };   // room for two lines
    lv_draw_label(layer, &d, &a);
}

// Chevron pointing along `track_deg`, scaled by emitter category.
static void draw_plane(lv_layer_t *layer, int32_t x, int32_t y, float track_deg, float scale, lv_color_t color)
{
    static const float shape[4][2] = { { 0, -10 }, { -7, 8 }, { 0, 4 }, { 7, 8 } };   // nose, left, notch, right
    float a = (isnan(track_deg) ? 0 : track_deg) * (float)DEGR, s = sinf(a), c = cosf(a);
    lv_point_precise_t p[4];
    for (int i = 0; i < 4; i++) {
        float px = shape[i][0] * scale, py = shape[i][1] * scale;
        p[i].x = x + px * c - py * s;
        p[i].y = y + px * s + py * c;
    }
    lv_draw_triangle_dsc_t t;
    lv_draw_triangle_dsc_init(&t);
    t.color = color;
    t.opa = LV_OPA_COVER;
    t.p[0] = p[0]; t.p[1] = p[1]; t.p[2] = p[2];
    lv_draw_triangle(layer, &t);
    t.p[0] = p[0]; t.p[1] = p[2]; t.p[2] = p[3];
    lv_draw_triangle(layer, &t);
}

static float category_scale(uint8_t cat)
{
    return cat == 2 ? 0.75f : (cat == 5 || cat == 6) ? 1.25f : 1.0f;   // light / heavy, high-performance
}

// Inside the rounded glass (local coordinates), with a small margin.
static bool in_glass(int32_t x, int32_t y)
{
    int W = BOARD.w, H = BOARD.h, r = BOARD.corner_r, pad = 4;
    if (x < pad || y < pad || x >= W - pad || y >= H - pad) return false;
    int32_t cx = x < r ? r : x >= W - r ? W - r : x, cy = y < r ? r : y >= H - r ? H - r : y;
    int32_t dx = x - cx, dy = y - cy;
    return dx * dx + dy * dy <= (r - pad) * (r - pad);
}

// A label box (local coordinates) that is fully visible and clear of the UI pills.
static bool label_fits(int32_t x0, int32_t y0, int32_t x1, int32_t y1)
{
    if (!in_glass(x0, y0) || !in_glass(x1, y0) || !in_glass(x0, y1) || !in_glass(x1, y1)) return false;
    int32_t rx = lv_obj_get_x(s_root), ry = lv_obj_get_y(s_root);
    for (int i = 0; i < 5; i++) {
        lv_area_t a;
        if (lv_obj_has_flag(s_pills[i], LV_OBJ_FLAG_HIDDEN)) continue;
        lv_obj_get_coords(s_pills[i], &a);
        if (x0 <= a.x2 - rx && x1 >= a.x1 - rx && y0 <= a.y2 - ry && y1 >= a.y1 - ry) return false;
    }
    return true;
}

// Callsign + altitude beside the plane: right if it fits, else left, else not at all
// (a selected plane always gets one). Near the rounded corners or under a pill, a
// label was being cut off.
static void draw_label(lv_layer_t *layer, const track_t *t, int32_t x, int32_t y, int32_t ox, int32_t oy, bool selected)
{
    char alt[16], buf[32];
    format_altitude(alt, sizeof(alt), t->a.alt_m);
    snprintf(buf, sizeof(buf), "%s\n%s", t->a.callsign[0] ? t->a.callsign : "?", alt);
    const lv_font_t *f = &lv_font_montserrat_14;
    lv_point_t sz;
    lv_text_get_size(&sz, buf, f, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    int32_t lx = x + 11, ly = y - 6;
    if (!label_fits(lx, ly, lx + sz.x, ly + sz.y)) {
        lx = x - 11 - sz.x;
        if (!label_fits(lx, ly, lx + sz.x, ly + sz.y)) {
            if (!selected) return;
            lx = x + 11;
        }
    }
    draw_text(layer, buf, lx + ox, ly + oy, sz.x + 4, f, lv_color_hex(0xd8d8d8));
}

static void radar_draw_cb(lv_event_t *e)
{
    if (!s_bundle_shown) return;
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_draw_line_dsc_t trail;
    lv_draw_line_dsc_init(&trail);
    trail.width = 2;
    trail.round_start = trail.round_end = 1;
    int W = BOARD.w, H = BOARD.h;
    // Screen positions are kept in map coordinates (hit-testing undoes the pixel
    // shift); drawing adds the shift back so planes stay on the map's towns.
    lv_area_t rc;
    lv_obj_get_coords(s_radar, &rc);
    int32_t ox = rc.x1, oy = rc.y1;

    track_t *sel = NULL;
    bool labels = s_nlevels && (s_levels[s_level].range_mi <= s_labels_mi) != s_labels_flip;
    for (int i = 0; i < s_track_count; i++) {
        track_t *t = &s_tracks[i];
        double lat, lon;
        current_position(&t->a, &lat, &lon);
        int32_t x, y;
        project(lat, lon, &x, &y);
        t->sx = x; t->sy = y;
        t->on_screen = x > -20 && x < W + 20 && y > -20 && y < H + 20;
        if (!t->on_screen) continue;

        lv_color_t col = altitude_color(t->a.alt_m, t->a.flags & F_GROUND);
        bool selected = t->a.icao == s_selected;
        if (selected) sel = t;

        // Breadcrumbs, oldest first, ending at the current position.
        trail.color = col;
        trail.opa = selected ? LV_OPA_80 : LV_OPA_30;
        int shown = selected ? t->trail_n : (t->trail_n < s_trail_pts ? t->trail_n : s_trail_pts);
        int32_t px = 0, py = 0;
        for (int k = t->trail_n - shown; k < t->trail_n; k++) {
            int idx = (t->trail_head + TRAIL_POINTS - t->trail_n + k) % TRAIL_POINTS;
            int32_t qx, qy;
            project(t->trail_lat[idx], t->trail_lon[idx], &qx, &qy);
            if (k > t->trail_n - shown) {
                trail.p1 = (lv_point_precise_t){ px + ox, py + oy };
                trail.p2 = (lv_point_precise_t){ qx + ox, qy + oy };
                lv_draw_line(layer, &trail);
            }
            px = qx; py = qy;
        }
        if (shown) {
            trail.p1 = (lv_point_precise_t){ px + ox, py + oy };
            trail.p2 = (lv_point_precise_t){ x + ox, y + oy };
            lv_draw_line(layer, &trail);
        }

        draw_plane(layer, x + ox, y + oy, t->a.track_deg, category_scale(t->a.category), col);
        if (labels || selected) draw_label(layer, t, x, y, ox, oy, selected);
    }

    if (sel) {
        lv_draw_arc_dsc_t d;
        lv_draw_arc_dsc_init(&d);
        d.center.x = sel->sx + ox; d.center.y = sel->sy + oy;
        d.radius = 16; d.start_angle = 0; d.end_angle = 360; d.width = 2;
        d.color = lv_color_white();
        lv_draw_arc(layer, &d);
    }
}

// ---------------------------------------------------------------- info panel
static info_t info_get(void)
{
    xSemaphoreTake(s_info_mx, portMAX_DELAY);
    info_t r = s_info;
    xSemaphoreGive(s_info_mx);
    return r;
}

static void info_request(const aircraft_t *a, double lat, double lon)
{
    xSemaphoreTake(s_info_mx, portMAX_DELAY);
    memset(&s_info, 0, sizeof(s_info));
    s_info.icao = a->icao; s_info.pending = true;
    s_req.icao = a->icao; strlcpy(s_req.cs, a->callsign, sizeof(s_req.cs));
    s_req.lat = lat; s_req.lon = lon; s_req.pending = true;
    xSemaphoreGive(s_info_mx);
    if (s_net) xTaskNotifyGive(s_net);
}

static void update_lookup_labels(const aircraft_t *a, double lat, double lon)
{
    info_t f = info_get();
    if (f.icao != a->icao) { info_request(a, lat, lon); f.pending = true; f.done = false; }
    lv_color_t dim = lv_color_hex(0x7f8a94);
    if (!f.done) {
        lv_label_set_text(s_panel_route, "Looking up...");
        lv_obj_set_style_text_color(s_panel_route, dim, 0);
        lv_obj_add_flag(s_panel_aircraft, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    if (f.has_route) {
        lv_label_set_text_fmt(s_panel_route, "%s " LV_SYMBOL_RIGHT " %s   #7f8a94 %s " LV_SYMBOL_RIGHT " %s#",
                              f.origin.iata, f.dest.iata, f.origin.city, f.dest.city);
        lv_obj_set_style_text_color(s_panel_route, lv_color_hex(0x3fb7a0), 0);
    } else {
        lv_label_set_text(s_panel_route, f.is_private ? "Private flight" : "Route unknown");
        lv_obj_set_style_text_color(s_panel_route, dim, 0);
    }
    // "Boeing 737-824  -  N12345  -  United Airlines"
    char line[110] = "";
    const char *parts[] = { f.type, f.registration, f.owner };
    for (int i = 0; i < 3; i++) {
        if (!*parts[i] || (i == 1 && !strcmp(f.registration, a->callsign))) continue;
        if (line[0]) strlcat(line, "  -  ", sizeof(line));
        strlcat(line, parts[i], sizeof(line));
    }
    if (line[0]) { lv_label_set_text(s_panel_aircraft, line); lv_obj_remove_flag(s_panel_aircraft, LV_OBJ_FLAG_HIDDEN); }
    else lv_obj_add_flag(s_panel_aircraft, LV_OBJ_FLAG_HIDDEN);
}

static void update_panel(void)
{
    track_t *t = s_selected ? find_track(s_selected) : NULL;
    if (!t) { lv_obj_add_flag(s_panel, LV_OBJ_FLAG_HIDDEN); return; }
    const aircraft_t *a = &t->a;
    double lat, lon;
    current_position(a, &lat, &lon);
    float brg, dist = miles_from_home(lat, lon, &brg);
    static const char *dirs[] = { "N", "NE", "E", "SE", "S", "SW", "W", "NW" };
    info_t f = info_get();

    char title[48], body[200], alt[16], spd[12], hdg[12], vs[16];
    snprintf(title, sizeof(title), "%s  #7f8a94 %06lX#", a->callsign[0] ? a->callsign : "(no callsign)", (unsigned long)a->icao);
    format_altitude(alt, sizeof(alt), a->alt_m);
    if (isnan(a->speed_ms)) strlcpy(spd, "--", sizeof(spd));
    else snprintf(spd, sizeof(spd), "%d kt", (int)lroundf(a->speed_ms * 1.94384f));
    if (isnan(a->track_deg)) strlcpy(hdg, "--", sizeof(hdg));
    else snprintf(hdg, sizeof(hdg), "%03d" "\xC2\xB0", (int)lroundf(a->track_deg) % 360);
    if (isnan(a->vrate_ms) || fabsf(a->vrate_ms) < 0.5f) strlcpy(vs, "level", sizeof(vs));
    else snprintf(vs, sizeof(vs), "%+d fpm", (int)lroundf(a->vrate_ms * 196.85f / 10) * 10);
    snprintf(body, sizeof(body), "Alt %s ft   Spd %s   Hdg %s\n%s   Squawk %s\n%.1f mi %s of home   %s",
             alt, spd, hdg, vs, a->squawk[0] ? a->squawk : "--",
             dist, dirs[(int)((brg + 22.5f) / 45) % 8], f.icao == a->icao ? f.country : "");
    lv_label_set_text(s_panel_title, title);
    lv_label_set_text(s_panel_body, body);
    update_lookup_labels(a, lat, lon);

    // Keep the panel out of the way of the plane it describes.
    bool low = t->sy > BOARD.h * 5 / 8;
    lv_obj_align(s_panel, low ? LV_ALIGN_TOP_MID : LV_ALIGN_BOTTOM_MID, 0, low ? 56 : -22);
    lv_obj_remove_flag(s_panel, LV_OBJ_FLAG_HIDDEN);
}

// ---------------------------------------------------------------- events
static void set_level(int level)
{
    if (!s_nlevels) return;
    s_level = level % s_nlevels;
    s_labels_flip = false;
    lv_image_set_src(s_map, &s_levels[s_level].img);
    lv_label_set_text_fmt(s_range_label, "%d mi", s_levels[s_level].range_mi);
    lv_obj_invalidate(s_radar);
}

static void range_click_cb(lv_event_t *e) { set_level(s_level + 1); }

static void pan_text(char *out, size_t n, int dx, int dy)
{
    snprintf(out, n, "50 mi %s%s", dy > 0 ? "N" : dy < 0 ? "S" : "", dx > 0 ? "E" : dx < 0 ? "W" : "");
}

static void pan_to(int dx, int dy)        // any task: the net task and the UI tick pick it up
{
    dx = dx < -PAN_MAX ? -PAN_MAX : dx > PAN_MAX ? PAN_MAX : dx;
    dy = dy < -PAN_MAX ? -PAN_MAX : dy > PAN_MAX ? PAN_MAX : dy;
    s_pan_at = ms();
    if (dx == s_dx && dy == s_dy) return;
    s_dx = dx; s_dy = dy; s_pan_dirty = true;
    ESP_LOGI(TAG, "pan -> %d,%d", dx, dy);
    if (s_net) xTaskNotifyGive(s_net);
}

static void pan_pill_cb(lv_event_t *e) { pan_to(0, 0); }

static void gesture_cb(lv_event_t *e)
{
    lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());
    s_gesture_at = ms();
    if (dir == LV_DIR_LEFT) pan_to(s_dx + 1, s_dy);          // the map follows the finger
    else if (dir == LV_DIR_RIGHT) pan_to(s_dx - 1, s_dy);
    else if (dir == LV_DIR_TOP) pan_to(s_dx, s_dy - 1);
    else if (dir == LV_DIR_BOTTOM) pan_to(s_dx, s_dy + 1);
}

static void radar_click_cb(lv_event_t *e)
{
    if (ms() - s_gesture_at < 400) return;                  // the end of a swipe, not a tap
    lv_point_t p;
    lv_indev_get_point(lv_indev_active(), &p);
    p.x -= lv_obj_get_x(s_root);          // undo pixel shift
    p.y -= lv_obj_get_y(s_root);
    int best = -1;
    int32_t best_d2 = 28 * 28;
    for (int i = 0; i < s_track_count; i++) {
        if (!s_tracks[i].on_screen) continue;
        int32_t dx = s_tracks[i].sx - p.x, dy = s_tracks[i].sy - p.y, d2 = dx * dx + dy * dy;
        if (d2 < best_d2) { best_d2 = d2; best = i; }
    }
    if (best >= 0) s_selected = s_tracks[best].a.icao;
    else if (s_selected) s_selected = 0;
    else s_labels_flip = !s_labels_flip;
    update_panel();
    lv_obj_invalidate(s_radar);
}

// Select the airborne plane nearest home and zoom in as far as still shows it.
static void select_closest(void)
{
    int best = -1;
    float best_mi = 1e9f;
    for (int i = 0; i < s_track_count; i++) {
        if (s_tracks[i].a.flags & F_GROUND) continue;
        double lat, lon;
        current_position(&s_tracks[i].a, &lat, &lon);
        float mi = miles_from_home(lat, lon, NULL);
        if (mi < best_mi) { best_mi = mi; best = i; }
    }
    if (best < 0) return;
    s_selected = s_tracks[best].a.icao;
    int level = 0;                        // levels run from widest to tightest
    for (int i = 0; i < s_nlevels; i++) if (s_levels[i].range_mi >= best_mi * 1.25f) level = i;
    ESP_LOGI(TAG, "closest %s, %.1f mi", s_tracks[best].a.callsign, best_mi);
    set_level(level);
    // The panel picks top/bottom from the plane's screen position, which the next
    // draw would set; compute it now for the new zoom.
    track_t *t = &s_tracks[best];
    double lat, lon;
    current_position(&t->a, &lat, &lon);
    int32_t x, y;
    project(lat, lon, &x, &y);
    t->sx = x; t->sy = y;
    update_panel();
}

static void update_status(void)
{
    int visible = 0;
    for (int i = 0; i < s_track_count; i++) visible += s_tracks[i].on_screen;
    uint32_t age = s_server_time ? now_unix() - s_server_time : 0;
    lv_color_t wifi_col = lv_color_hex(0x45f07a);
    switch (s_status) {
    case ST_NONE:     lv_label_set_text(s_status_label, "Connecting..."); wifi_col = lv_color_hex(0x9a9a9a); break;
    case ST_HUB_DOWN: lv_label_set_text_fmt(s_status_label, "%d aircraft  -  hub down", visible); wifi_col = lv_color_hex(0xff4040); break;
    case ST_AUTH:     lv_label_set_text(s_status_label, "OpenSky login failed"); wifi_col = lv_color_hex(0xff4040); break;
    case ST_RATE:     lv_label_set_text(s_status_label, "OpenSky credits used up"); wifi_col = lv_color_hex(0xffb52b); break;
    case ST_ERROR:    lv_label_set_text_fmt(s_status_label, "%d aircraft  -  stale %lus", visible, (unsigned long)age); wifi_col = lv_color_hex(0xffb52b); break;
    case ST_IDLE:     lv_label_set_text(s_status_label, "Waking up..."); break;
    default:          lv_label_set_text_fmt(s_status_label, "%d aircraft  -  %lus", visible, (unsigned long)age); break;
    }
    lv_obj_set_style_text_color(s_wifi_label, wifi_col, 0);
    struct tm tm;
    time_t t = time(NULL);
    if (t > 1700000000 && localtime_r(&t, &tm))
        lv_label_set_text_fmt(s_clock_label, "%d:%02d", tm.tm_hour % 12 ? tm.tm_hour % 12 : 12, tm.tm_min);
}

static void pixel_shift(void)
{
    static const int8_t offs[][2] = { { 0, 0 }, { 2, 0 }, { 2, 2 }, { 0, 2 }, { -2, 2 },
                                      { -2, 0 }, { -2, -2 }, { 0, -2 }, { 2, -2 } };
    static uint8_t i = 0;
    i = (i + 1) % (sizeof(offs) / sizeof(offs[0]));
    lv_obj_set_pos(s_root, offs[i][0], offs[i][1]);
}

// Take the net task's staged map: new levels, centre and home; the old slot is released.
static void adopt_map(void)
{
    int old = s_slot;
    memcpy(s_levels, s_stage.lv, sizeof(s_levels));
    s_nlevels = s_stage.n; s_slot = s_stage.slot;
    s_home_mx = s_stage.mx; s_home_my = s_stage.my; s_home_lat = s_stage.lat; s_home_lon = s_stage.lon;
    s_map_dx = s_stage.dx; s_map_dy = s_stage.dy;
    s_stage_ready = false;
    s_bundle_shown = true;
    lv_obj_add_flag(s_msg, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(s_map, LV_OBJ_FLAG_HIDDEN);
    set_level(s_level < s_nlevels ? s_level : 0);           // points the image at the new levels
    if (old >= 0 && old != s_slot) store_unmap(old);
    ESP_LOGI(TAG, "map %d,%d from slot %d", s_map_dx, s_map_dy, s_slot);
}

static void update_pan_ui(void)
{
    if ((s_dx || s_dy) && ms() - s_pan_at > PAN_IDLE_MS) pan_to(0, 0);      // back home after 10 min
    char t[24];
    if (s_dx || s_dy) {
        pan_text(t, sizeof(t), s_dx, s_dy);
        lv_label_set_text_fmt(s_pan_label, "%s  " LV_SYMBOL_CLOSE, t);
        lv_obj_remove_flag(s_pills[4], LV_OBJ_FLAG_HIDDEN);
    } else lv_obj_add_flag(s_pills[4], LV_OBJ_FLAG_HIDDEN);
    if (s_bundle_shown && (s_map_dx != s_dx || s_map_dy != s_dy)) {      // the new map is on its way
        if (s_dx || s_dy) lv_label_set_text_fmt(s_msg, "Loading %s...", t);
        else lv_label_set_text(s_msg, "Loading home...");
        lv_obj_remove_flag(s_msg, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(s_msg);
    }
}

static void tick_cb(lv_timer_t *timer)
{
    static uint32_t ticks;
    if (!s_active) return;
    ticks++;
    if (s_stage_ready) adopt_map();
    update_pan_ui();
    if (s_batch.ready) { merge_batch(); s_batch.ready = false; }
    if (ticks % PIXEL_SHIFT_S == 0) pixel_shift();
    update_status();
    update_panel();
    lv_obj_invalidate(s_radar);
    if (s_bundle_shown) hub_drawn();
}

// ---------------------------------------------------------------- net task
typedef struct { int slot; uint32_t off; } sink_t;
static esp_err_t to_flash(void *ctx, const uint8_t *d, size_t n)
{
    sink_t *s = (sink_t *)ctx;
    esp_err_t e = store_write(s->slot, s->off, d, n);
    s->off += n;
    return e;
}

static uint32_t id32(const char *bundle_id)
{
    char head[9];                         // first 8 hex digits are plenty; strtoul on all 16
    strlcpy(head, bundle_id, sizeof(head));   // saturates at 0xffffffff, so every id would match
    uint32_t v = (uint32_t)strtoul(head, NULL, 16);
    return v ? v : 1;
}

// Map a stored ABN1 bundle into s_stage. Only called while s_stage_ready is false.
static bool map_bundle(int slot, int dx, int dy)
{
    const uint8_t *p = store_map(slot);
    if (!p) return false;
    p += STORE_DATA_OFF;
    uint16_t ver, n, w, h;
    if (memcmp(p, "ABN1", 4)) return false;
    memcpy(&ver, p + 4, 2); memcpy(&n, p + 6, 2); memcpy(&w, p + 8, 2); memcpy(&h, p + 10, 2);
    if (ver != 1 || !n || n > MAX_LEVELS || w != BOARD.w || h != BOARD.h) {
        ESP_LOGE(TAG, "bundle v%u %u levels %ux%u does not fit this board", ver, n, w, h);
        return false;
    }
    float lat, lon;
    memcpy(&s_stage.mx, p + 16, 8); memcpy(&s_stage.my, p + 24, 8);
    memcpy(&lat, p + 32, 4); memcpy(&lon, p + 36, 4);
    s_stage.lat = lat; s_stage.lon = lon;
    for (int i = 0; i < n; i++) {
        const uint8_t *e = p + 40 + 16 * i;
        uint16_t rng; float mpp; uint32_t off, len;
        memcpy(&rng, e, 2); memcpy(&mpp, e + 4, 4); memcpy(&off, e + 8, 4); memcpy(&len, e + 12, 4);
        if (len != (uint32_t)w * h * 2 || off + len > store_slot_capacity()) return false;
        level_t *L = &s_stage.lv[i];
        L->range_mi = rng; L->mpp = mpp;
        memset(&L->img, 0, sizeof(L->img));
        L->img.header.magic = LV_IMAGE_HEADER_MAGIC;
        L->img.header.cf = LV_COLOR_FORMAT_RGB565;
        L->img.header.w = w; L->img.header.h = h; L->img.header.stride = w * 2;
        L->img.data_size = len;
        L->img.data = p + off;
    }
    s_stage.n = n; s_stage.slot = slot; s_stage.dx = dx; s_stage.dy = dy;
    return true;
}

// Hub id and store key of the view at a pan ("home@1,0", "a:~ed" + view).
static void vid_of(int dx, int dy, char out[24])
{
    if (dx || dy) snprintf(out, 24, "%s@%d,%d", s_view, dx, dy);
    else strlcpy(out, s_view, 24);
}
static void key_of(int dx, int dy, char out[16])
{
    if (dx || dy) snprintf(out, 16, "a:~%c%c%.10s", 'd' + dx, 'd' + dy, s_view);
    else snprintf(out, 16, "a:%.13s", s_view);   // store keys are 15 chars max
}

// Make sure the current bundle for this view, pan and profile is in flash and staged.
static void sync_bundle(void)
{
    char url[URL_MAX], id[24], key[16]; uint8_t *js; size_t len;
    int dx = s_dx, dy = s_dy;
    vid_of(dx, dy, id); key_of(dx, dy, key);
    snprintf(url, sizeof(url), "%s/aircraft/%s/manifest.json?w=%d&h=%d&r=%d", hub_url(), id, BOARD.w, BOARD.h, BOARD.corner_r);
    uint32_t want = 0, crc = 0; size_t size = 0;
    if (net_get(url, &js, &len, 8192) == ESP_OK) {
        cJSON *m = cJSON_Parse((char *)js);
        free(js);
        const cJSON *id = cJSON_GetObjectItem(m, "bundle_id"), *sz = cJSON_GetObjectItem(m, "bundle_size"),
                    *cr = cJSON_GetObjectItem(m, "bundle_crc32");
        if (cJSON_IsNumber(cr)) crc = (uint32_t)cr->valuedouble;                 // absent: an older hub
        if (cJSON_IsString(id)) want = id32(id->valuestring);
        if (cJSON_IsNumber(sz)) size = (size_t)sz->valuedouble;
        cJSON_Delete(m);
    }
    loop_hdr_t cur; int cs;
    bool have = store_get(key, &cur, &cs);
    if (have && (!want || cur.loop_id == want)) {           // current, or the hub is down: use what we have
        if (strcmp(s_loaded, key) && !s_stage_ready && map_bundle(cs, dx, dy)) {
            strlcpy(s_loaded, key, sizeof(s_loaded)); s_stage_ready = true;
            ESP_LOGI(TAG, "bundle %s from slot %d", key, cs);
        }
        return;
    }
    if (!want) return;
    if (!size || size > store_slot_capacity()) { ESP_LOGW(TAG, "bundle size %u unusable", (unsigned)size); return; }
    int slot = store_begin(key, true);
    if (slot < 0) { ESP_LOGW(TAG, "no free slot for the bundle"); return; }
    snprintf(url, sizeof(url), "%s/aircraft/%s/bundle.bin?w=%d&h=%d&r=%d", hub_url(), id, BOARD.w, BOARD.h, BOARD.corner_r);
    sink_t sk = { .slot = slot, .off = STORE_DATA_OFF };
    size_t got;
    int64_t t0 = ms();
    if (net_stream(url, to_flash, &sk, &got) != ESP_OK || got != size) { ESP_LOGW(TAG, "bundle download failed"); return; }
    if (crc && store_crc32(slot, STORE_DATA_OFF, size) != crc) {
        ESP_LOGE(TAG, "bundle failed its check after writing -- not used, will fetch again");
        return;
    }
    loop_hdr_t hdr = {0};
    strlcpy(hdr.view, key, sizeof(hdr.view));
    hdr.loop_id = want;
    hdr.nframes = 1;
    store_commit(slot, &hdr);
    ESP_LOGI(TAG, "bundle %08lx cached in slot %d (%u KB, %lld ms)", (unsigned long)want, slot, (unsigned)(got / 1024), ms() - t0);
    if (dx != s_dx || dy != s_dy) return;                  // panned again meanwhile: the next sync
    while (s_stage_ready && s_active) vTaskDelay(pdMS_TO_TICKS(50));   // the UI is adopting the last one
    if (!s_stage_ready && map_bundle(slot, dx, dy)) { strlcpy(s_loaded, key, sizeof(s_loaded)); s_stage_ready = true; }
}

static bool have_seq;                     // cleared on a pan: the new view's states differ
static void fetch_states(void)
{
    static uint32_t seq;
    char url[URL_MAX], id[24]; uint8_t *b; size_t len; int status;
    vid_of(s_dx, s_dy, id);
    char since[12] = "";                   // empty: send everything
    if (have_seq) snprintf(since, sizeof(since), "%lu", (unsigned long)seq);
    snprintf(url, sizeof(url), "%s/aircraft/%s/states.bin?since=%s%s", hub_url(), id, since, s_types);
    if (net_fetch(url, &b, &len, 20 + 48 * 256, &status) != ESP_OK) {
        if (status != 304) s_status = ST_HUB_DOWN;
        return;
    }
    uint32_t s_seq, t; int32_t credits; uint16_t n;
    if (len < 20 || memcmp(b, "AST1", 4)) { free(b); return; }
    memcpy(&s_seq, b + 4, 4); memcpy(&t, b + 8, 4); memcpy(&credits, b + 12, 4); memcpy(&n, b + 16, 2);
    s_status = b[18]; s_credits = credits;
    if (len != 20 + 48u * n) { free(b); return; }
    seq = s_seq; have_seq = true;
    if (s_batch.ready) { have_seq = false; free(b); return; }   // UI hasn't taken the last one: refetch next time
    int count = 0;
    for (int i = 0; i < n && count < MAX_AIRCRAFT; i++) {
        const uint8_t *r = b + 20 + 48 * i;
        aircraft_t *a = &s_batch.items[count++];
        memcpy(&a->icao, r, 4);
        memcpy(&a->lat, r + 4, 4); memcpy(&a->lon, r + 8, 4); memcpy(&a->alt_m, r + 12, 4);
        memcpy(&a->speed_ms, r + 16, 4); memcpy(&a->track_deg, r + 20, 4); memcpy(&a->vrate_ms, r + 24, 4);
        memcpy(&a->time_position, r + 28, 4);
        memcpy(a->callsign, r + 32, 8); a->callsign[8] = 0;
        memcpy(a->squawk, r + 40, 4); a->squawk[4] = 0;
        a->category = r[44]; a->flags = r[45];
    }
    free(b);
    s_batch.count = count;
    s_batch.server_time = t;
    s_batch.ready = true;
    static char logged[24];                // first states for each view (start, every pan)
    vid_of(s_dx, s_dy, id);
    if (strcmp(logged, id)) { strlcpy(logged, id, sizeof(logged)); ESP_LOGI(TAG, "first states for %s: seq %lu, %d aircraft, hub status %d", id, (unsigned long)seq, count, s_status); }
}

static void str_item(char *dst, size_t n, const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItem(o, key);
    strlcpy(dst, cJSON_IsString(v) ? v->valuestring : "", n);
}

static void fetch_info(void)
{
    xSemaphoreTake(s_info_mx, portMAX_DELAY);
    uint32_t icao = s_req.icao; char cs[9]; float lat = s_req.lat, lon = s_req.lon;
    strlcpy(cs, s_req.cs, sizeof(cs)); s_req.pending = false;
    xSemaphoreGive(s_info_mx);

    char url[URL_MAX]; uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/aircraft/info/%06lx?cs=%s&lat=%.4f&lon=%.4f", hub_url(), (unsigned long)icao, cs, lat, lon);
    info_t f = { .icao = icao, .done = true };
    if (net_get(url, &js, &len, 4096) == ESP_OK) {
        cJSON *j = cJSON_Parse((char *)js);
        free(js);
        str_item(f.type, sizeof(f.type), j, "type");
        str_item(f.registration, sizeof(f.registration), j, "registration");
        str_item(f.owner, sizeof(f.owner), j, "owner");
        str_item(f.country, sizeof(f.country), j, "country");
        f.is_private = cJSON_IsTrue(cJSON_GetObjectItem(j, "private"));
        const cJSON *r = cJSON_GetObjectItem(j, "route");
        if (cJSON_IsObject(r)) {
            const cJSON *o = cJSON_GetObjectItem(r, "origin"), *d = cJSON_GetObjectItem(r, "dest");
            str_item(f.origin.iata, sizeof(f.origin.iata), o, "iata"); str_item(f.origin.city, sizeof(f.origin.city), o, "city");
            str_item(f.dest.iata, sizeof(f.dest.iata), d, "iata");     str_item(f.dest.city, sizeof(f.dest.city), d, "city");
            f.has_route = true;
        }
        cJSON_Delete(j);
    }
    ESP_LOGI(TAG, "info %06lx %s: %s, route %s-%s", (unsigned long)icao, cs, f.type, f.has_route ? f.origin.iata : "?", f.has_route ? f.dest.iata : "?");
    xSemaphoreTake(s_info_mx, portMAX_DELAY);
    if (s_info.icao == icao && !s_req.pending) s_info = f;   // unless something else was tapped meanwhile
    xSemaphoreGive(s_info_mx);
}

static void net_task(void *arg)
{
    int64_t states_at = 0, bundle_at = 0;
    for (;;) {
        if (!s_active || !s_screen_on) { ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000)); states_at = 0; continue; }
        int64_t now = ms();
        if (s_pan_dirty) { s_pan_dirty = false; bundle_at = 0; states_at = 0; have_seq = false; }
        char key[16]; key_of(s_dx, s_dy, key);
        bool current = !strcmp(s_loaded, key);
        // Until it's staged, retry the bundle every 3 s; then check for a new one hourly.
        if (!bundle_at || now - bundle_at > (current ? 3600 * 1000 : 3000)) { sync_bundle(); bundle_at = ms(); }
        if (s_req.pending) fetch_info();
        if (!states_at || now - states_at > STATES_MS) { fetch_states(); states_at = ms(); }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(200));
    }
}

// ---------------------------------------------------------------- construction
static lv_obj_t *make_pill(lv_obj_t *parent, lv_align_t align, int32_t x, int32_t y)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(o, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_60, 0);
    lv_obj_set_style_radius(o, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_hor(o, 10, 0);
    lv_obj_set_style_pad_ver(o, 4, 0);
    lv_obj_align(o, align, x, y);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static void build_ui(void)
{
    int W = BOARD.w, H = BOARD.h, in = BOARD.corner_r / 2 > 12 ? BOARD.corner_r / 2 : 12;   // clear of rounded corners
    lv_obj_t *scr = lv_screen_active();
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    s_root = lv_obj_create(scr);
    lv_obj_remove_style_all(s_root);
    lv_obj_set_size(s_root, W, H);
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);

    s_map = lv_image_create(s_root);
    lv_obj_set_pos(s_map, 0, 0);
    lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);

    s_msg = lv_label_create(s_root);
    lv_label_set_text(s_msg, "Loading map...");
    lv_obj_set_style_text_font(s_msg, &lv_font_montserrat_18, 0);   // an amber pill, like weather's:
    lv_obj_set_style_text_color(s_msg, lv_color_hex(0xe7ebf0), 0); // plain grey text got lost on the map
    lv_obj_set_style_bg_color(s_msg, lv_color_hex(0x101418), 0);
    lv_obj_set_style_bg_opa(s_msg, LV_OPA_90, 0);
    lv_obj_set_style_border_color(s_msg, lv_color_hex(0xffb703), 0);
    lv_obj_set_style_border_width(s_msg, 2, 0);
    lv_obj_set_style_radius(s_msg, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_hor(s_msg, 18, 0);
    lv_obj_set_style_pad_ver(s_msg, 10, 0);
    lv_obj_center(s_msg);

    s_radar = lv_obj_create(s_root);
    lv_obj_remove_style_all(s_radar);
    lv_obj_set_size(s_radar, W, H);
    lv_obj_add_flag(s_radar, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_radar, radar_draw_cb, LV_EVENT_DRAW_MAIN, NULL);
    lv_obj_add_event_cb(s_radar, radar_click_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_remove_flag(s_radar, LV_OBJ_FLAG_GESTURE_BUBBLE);    // swipes come here, not to the screen
    lv_obj_add_event_cb(s_radar, gesture_cb, LV_EVENT_GESTURE, NULL);

    // Range badge (tap to zoom). These pill positions match the hub's
    // basemap.ui_boxes(), which keeps town labels out from under them.
    lv_obj_t *range = s_pills[0] = make_pill(s_root, LV_ALIGN_TOP_LEFT, in, 14);
    lv_obj_add_flag(range, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(range, 12);
    lv_obj_add_event_cb(range, range_click_cb, LV_EVENT_CLICKED, NULL);
    s_range_label = lv_label_create(range);
    lv_label_set_text(s_range_label, "-- mi");
    lv_obj_set_style_text_font(s_range_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_range_label, lv_color_white(), 0);

    lv_obj_t *clock = s_pills[1] = make_pill(s_root, LV_ALIGN_TOP_RIGHT, -in, 14);
    lv_obj_set_flex_flow(clock, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(clock, 8, 0);
    s_wifi_label = lv_label_create(clock);
    lv_label_set_text(s_wifi_label, LV_SYMBOL_WIFI);
    lv_obj_set_style_text_font(s_wifi_label, &lv_font_montserrat_16, 0);
    s_clock_label = lv_label_create(clock);
    lv_label_set_text(s_clock_label, "--:--");
    lv_obj_set_style_text_font(s_clock_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_clock_label, lv_color_white(), 0);

    lv_obj_t *status = s_pills[2] = make_pill(s_root, LV_ALIGN_BOTTOM_LEFT, in, -14);
    s_status_label = lv_label_create(status);
    lv_obj_set_style_text_font(s_status_label, &lv_font_montserrat_16, 0);   // 12 was too small to read
    lv_obj_set_style_text_color(s_status_label, lv_color_hex(0xd8d8d8), 0);
    lv_label_set_text(s_status_label, "Starting...");

    lv_obj_t *attrib = s_pills[3] = lv_label_create(s_root);
    lv_label_set_text(attrib, "Esri, OSM | OpenSky");
    lv_obj_set_style_text_font(attrib, &lv_font_montserrat_10, 0);
    lv_obj_set_style_text_color(attrib, lv_color_hex(0x707070), 0);
    lv_obj_align(attrib, LV_ALIGN_BOTTOM_RIGHT, -in - 4, -18);

    // Offset pill while panned (tap = home). Top centre: below the range/clock pills on
    // a small panel.
    lv_obj_t *pan = s_pills[4] = make_pill(s_root, LV_ALIGN_TOP_MID, 0, W >= 400 ? 14 : 48);
    lv_obj_set_style_border_color(pan, lv_color_hex(0xffb703), 0);
    lv_obj_set_style_border_width(pan, 2, 0);
    lv_obj_add_flag(pan, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_ext_click_area(pan, 12);
    lv_obj_add_event_cb(pan, pan_pill_cb, LV_EVENT_CLICKED, NULL);
    s_pan_label = lv_label_create(pan);
    lv_obj_set_style_text_font(s_pan_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_pan_label, lv_color_hex(0xffcd5a), 0);

    // Selected-aircraft panel.
    s_panel = lv_obj_create(s_root);
    lv_obj_remove_style_all(s_panel);
    lv_obj_set_size(s_panel, W - 50, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(s_panel, lv_color_hex(0x101418), 0);
    lv_obj_set_style_bg_opa(s_panel, LV_OPA_90, 0);
    lv_obj_set_style_radius(s_panel, 14, 0);
    lv_obj_set_style_border_color(s_panel, lv_color_hex(0x3fb7a0), 0);
    lv_obj_set_style_border_width(s_panel, 1, 0);
    lv_obj_set_style_pad_all(s_panel, 12, 0);
    lv_obj_set_flex_flow(s_panel, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(s_panel, 4, 0);
    lv_obj_remove_flag(s_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_panel, LV_OBJ_FLAG_HIDDEN);
    s_panel_title = lv_label_create(s_panel);
    lv_label_set_recolor(s_panel_title, true);
    lv_obj_set_style_text_font(s_panel_title, &lv_font_montserrat_26, 0);
    lv_obj_set_style_text_color(s_panel_title, lv_color_white(), 0);
    s_panel_route = lv_label_create(s_panel);
    lv_label_set_recolor(s_panel_route, true);
    lv_obj_set_width(s_panel_route, lv_pct(100));
    lv_label_set_long_mode(s_panel_route, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_font(s_panel_route, &lv_font_montserrat_18, 0);
    s_panel_aircraft = lv_label_create(s_panel);
    lv_obj_set_width(s_panel_aircraft, lv_pct(100));
    lv_label_set_long_mode(s_panel_aircraft, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_font(s_panel_aircraft, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_panel_aircraft, lv_color_hex(0xa8b0b8), 0);
    s_panel_body = lv_label_create(s_panel);
    lv_obj_set_style_text_font(s_panel_body, &lv_font_montserrat_18, 0);
    lv_obj_set_style_text_color(s_panel_body, lv_color_hex(0xc8d0d8), 0);

    lv_timer_create(tick_cb, 1000, NULL);
}

// ---------------------------------------------------------------- app interface
static void app_init(const cJSON *views)
{
    const cJSON *v = cJSON_GetArrayItem(views, 0), *id = cJSON_GetObjectItem(v, "id"),
                *sl = cJSON_GetObjectItem(v, "start_level");
    if (cJSON_IsString(id)) strlcpy(s_view, id->valuestring, sizeof(s_view));
    if (cJSON_IsNumber(sl) && sl->valueint >= 0 && sl->valueint < MAX_LEVELS) s_level = sl->valueint;   // the hub's starting zoom
    const cJSON *lm = cJSON_GetObjectItem(v, "labels_mi");
    if (cJSON_IsNumber(lm)) s_labels_mi = lm->valueint;
    const cJSON *tr = cJSON_GetObjectItem(v, "trail_s");      // the hub polls OpenSky every ~30 s
    const cJSON *ty = cJSON_GetObjectItem(v, "types"), *k;
    if (cJSON_IsArray(ty) && cJSON_GetArraySize(ty) < 4) {   // the hub filters: airline, business, private, other
        strlcpy(s_types, "&types=", sizeof(s_types));
        cJSON_ArrayForEach(k, ty) if (cJSON_IsString(k)) {
            if (s_types[7]) strlcat(s_types, ",", sizeof(s_types));
            strlcat(s_types, k->valuestring, sizeof(s_types));
        }
    }
    if (cJSON_IsNumber(tr)) s_trail_pts = tr->valueint / 30 > TRAIL_POINTS ? TRAIL_POINTS : tr->valueint / 30;
    s_info_mx = xSemaphoreCreateMutex();
    ui_lock(0);
    build_ui();
    ui_unlock();
    xTaskCreate(net_task, "aircraft", 6144, NULL, 4, &s_net);
    ESP_LOGI(TAG, "view %s", s_view);
}

static void app_enter(void)
{
    ui_lock(0);
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_HIDDEN);
    ui_unlock();
    s_active = true;
    xTaskNotifyGive(s_net);
}

static void app_leave(void)
{
    s_active = false;
    ui_lock(0);
    lv_obj_add_flag(s_root, LV_OBJ_FLAG_HIDDEN);
    ui_unlock();
}

static void app_key(key_ev_t ev)
{
    if (ev.btn != BTN_KEY) return;
    ui_lock(0);
    if (ev.type == KEY_SHORT) { set_level(s_level + 1); ESP_LOGI(TAG, "zoom %d mi", s_levels[s_level].range_mi); }
    else select_closest();
    ui_unlock();
}

static void app_screen(bool on)
{
    s_screen_on = on;                     // off: stop polling, so the hub stops spending credits
    if (on) xTaskNotifyGive(s_net);
}

static const char *app_current(void)
{
    static char id[24];
    vid_of(s_dx, s_dy, id);
    return id;
}

// One view per device; "<view>@<dx>,<dy>" pans it (HA's Pan select).
static void app_show(const char *view)
{
    size_t n = strlen(s_view);
    int dx = 0, dy = 0;
    if (strncmp(view, s_view, n) || (view[n] && (view[n] != '@' || sscanf(view + n + 1, "%d,%d", &dx, &dy) != 2))) return;
    pan_to(dx, dy);
}

const app_t APP_AIRCRAFT = {
    .id = "aircraft", .name = "Aircraft", .store_prefix = 'a',
    .init = app_init, .enter = app_enter, .leave = app_leave, .key = app_key, .screen = app_screen,
    .current = app_current, .show = app_show,
};
