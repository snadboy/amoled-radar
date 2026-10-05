#!/usr/bin/env bash
# Flash over USB: BOARD=c6|p4 PORT=<resolved by-id path> ./flash.sh
# On sdevs the boards arrive through Proxmox USB passthrough (pve-faraday VM 121,
# pinned by physical port -- see CLAUDE.md).
set -euo pipefail
cd "$(dirname "$0")"
BOARD=${BOARD:-c6}
case "$BOARD" in c6) CHIP=esp32c6 ;; p4) CHIP=esp32p4 ;; *) echo "BOARD must be c6 or p4" >&2; exit 1 ;; esac
# Always pass PORT resolved from the board's USB serial (/dev/serial/by-id/...): with
# several boards on sdevs, a ttyACM number says nothing about which board it is.
PORT=${PORT:?set PORT, e.g. PORT=\$(readlink -f /dev/serial/by-id/*<serial>*)}
IDF_IMAGE=${IDF_IMAGE:-espressif/idf:v5.5.3}
docker run --rm -v "$PWD":/project -w /project -e HOME=/tmp -u "$(id -u):$(id -g)" \
  --group-add "$(stat -c %g "$PORT")" --device "$PORT" "$IDF_IMAGE" \
  bash -c "cd build-$BOARD && python -m esptool --chip $CHIP -p $PORT -b 460800 --before default_reset --after hard_reset write_flash @flash_args" 2>&1 \
  | grep -E "Hash of data verified|rror|Hard resetting" || true
