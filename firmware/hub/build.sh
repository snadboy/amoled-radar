#!/usr/bin/env bash
# Build the firmware in Espressif's container. Release builds carry NO secrets: boards
# get WiFi from the install page (Improv over USB) and keep it on the board. One board
# per target; BOARD picks it (default c6):
#   ./build.sh                                   C6 AMOLED 2.16", hub = bedrock (the default)
#   BOARD=p4 ./build.sh                          P4 LCD 3.5"
#   HUB_SERVER_URL=http://192.168.86.220:8099 ./build.sh   any board, another hub
#   DEV_WIFI=1 ./build.sh                        developer build: also seeds WiFi from the
#                                                shareables .env (RADAR_WIFI_SSID/PASSWORD)
#                                                for a board with none saved -- never publish it
# Output: build-<board>/display_hub.bin (+ version.txt, shared). Overrides go ONLY into the
# gitignored secrets.defaults.
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=${RADAR_ENV:-/mnt/shareables/.claude/.env}
IDF_IMAGE=${IDF_IMAGE:-espressif/idf:v5.5.3}

python3 - "$ENV_FILE" <<'PY'
import os, subprocess, sys
def kc(v):                      # sdkconfig string escaping
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
lines = []
if os.environ.get("DEV_WIFI") == "1":
    env = {}
    out = subprocess.run(["bash", "-c", 'set -a; source "$1" >/dev/null 2>&1; env -0', "_", sys.argv[1]],
                         capture_output=True, check=True).stdout
    for item in out.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1); env[k.decode()] = v.decode()
    missing = [k for k in ("RADAR_WIFI_SSID", "RADAR_WIFI_PASSWORD") if not env.get(k)]
    if missing:
        sys.exit("missing in %s: %s" % (sys.argv[1], ", ".join(missing)))
    lines += ["CONFIG_HUB_WIFI_SSID=" + kc(env["RADAR_WIFI_SSID"]),
              "CONFIG_HUB_WIFI_PASSWORD=" + kc(env["RADAR_WIFI_PASSWORD"])]
if os.environ.get("HUB_SERVER_URL"):
    lines.append("CONFIG_HUB_SERVER_URL=" + kc(os.environ["HUB_SERVER_URL"]))
old = os.umask(0o077)
open("secrets.defaults", "w").write("\n".join(lines) + "\n")
os.umask(old)
PY

BOARD=${BOARD:-c6}
case "$BOARD" in
  c6) TARGET=esp32c6 ;;
  p4) TARGET=esp32p4 ;;
  *) echo "BOARD must be c6 or p4" >&2; exit 1 ;;
esac
B=build-$BOARD; SDK=sdkconfig.$BOARD
rm -f $SDK                # regenerate from defaults so secrets/server changes take effect
# Firmware version = commit + build time. OTA installs whenever the server's differs.
# CI passes FW_VERSION (the commit's own time) so every board's build of a commit agrees.
echo "${FW_VERSION:-$(git rev-parse --short HEAD)$(git diff --quiet -- . || echo +)-$(date -u +%m%d%H%M)}" > version.txt
docker run --rm -v "$PWD":/project -w /project -e HOME=/tmp -u "$(id -u):$(id -g)" \
  -e SDKCONFIG_DEFAULTS="sdkconfig.defaults;secrets.defaults" "$IDF_IMAGE" \
  bash -c "idf.py -B $B -D SDKCONFIG=$SDK set-target $TARGET >/dev/null && idf.py -B $B -D SDKCONFIG=$SDK build" 2>&1 \
  | grep -E "error|warning:|Project build complete|binary size" | grep -v "^Checking" || true
test -f $B/display_hub.bin && ls -l $B/display_hub.bin | awk -v b=$BOARD '{print "built " b ": " $5 " bytes"}'
