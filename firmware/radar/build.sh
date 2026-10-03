#!/usr/bin/env bash
# Build the firmware in Espressif's container. WiFi credentials come from the
# shareables .env and go ONLY into the gitignored secrets.defaults -- never into
# the repo. Usage: ./build.sh            (server = bedrock, the default)
#                  RADAR_SERVER_URL=http://192.168.86.220:8098 ./build.sh   (dev server)
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=${RADAR_ENV:-/mnt/shareables/.claude/.env}
IDF_IMAGE=${IDF_IMAGE:-espressif/idf:v5.5.3}

python3 - "$ENV_FILE" <<'PY'
import os, shlex, subprocess, sys
env = {}
out = subprocess.run(["bash", "-c", 'set -a; source "$1" >/dev/null 2>&1; env -0', "_", sys.argv[1]],
                     capture_output=True, check=True).stdout
for item in out.split(b"\0"):
    if b"=" in item:
        k, v = item.split(b"=", 1); env[k.decode()] = v.decode()
need = ["RADAR_WIFI_SSID", "RADAR_WIFI_PASSWORD"]
missing = [k for k in need if not env.get(k)]
if missing:
    sys.exit("missing in %s: %s" % (sys.argv[1], ", ".join(missing)))
def kc(v):                      # sdkconfig string escaping
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
lines = ["CONFIG_RADAR_WIFI_SSID=" + kc(env["RADAR_WIFI_SSID"]),
         "CONFIG_RADAR_WIFI_PASSWORD=" + kc(env["RADAR_WIFI_PASSWORD"])]
if os.environ.get("RADAR_SERVER_URL"):
    lines.append("CONFIG_RADAR_SERVER_URL=" + kc(os.environ["RADAR_SERVER_URL"]))
old = os.umask(0o077)
open("secrets.defaults", "w").write("\n".join(lines) + "\n")
os.umask(old)
PY

rm -f sdkconfig            # regenerate from defaults so secrets/server changes take effect
docker run --rm -v "$PWD":/project -w /project -e HOME=/tmp -u "$(id -u):$(id -g)" \
  -e SDKCONFIG_DEFAULTS="sdkconfig.defaults;secrets.defaults" "$IDF_IMAGE" \
  bash -c 'idf.py set-target esp32c6 >/dev/null && idf.py build' 2>&1 \
  | grep -E "error|warning:|Project build complete|binary size" | grep -v "^Checking" || true
test -f build/amoled_radar.bin && ls -l build/amoled_radar.bin | awk '{print "built " $5 " bytes"}'
