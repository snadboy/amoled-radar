#!/usr/bin/env bash
# Flash over USB. On sdevs the board arrives through Proxmox USB passthrough
# (pve-faraday VM 121, usb0: host=303a:1001) as /dev/ttyACM0.
set -euo pipefail
cd "$(dirname "$0")"
PORT=${PORT:-/dev/ttyACM0}
IDF_IMAGE=${IDF_IMAGE:-espressif/idf:v5.5.3}
docker run --rm -v "$PWD":/project -w /project -e HOME=/tmp -u "$(id -u):$(id -g)" \
  --group-add "$(stat -c %g "$PORT")" --device "$PORT" "$IDF_IMAGE" \
  bash -c "cd build && python -m esptool --chip esp32c6 -p $PORT -b 460800 --before default_reset --after hard_reset write_flash @flash_args" 2>&1 \
  | grep -E "Hash of data verified|rror|Hard resetting" || true
