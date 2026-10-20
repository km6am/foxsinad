#!/bin/sh
# Build foxsinad_fw with arduino-cli. "./build.sh setup" installs the
# Arduino-Pico core and the Si5351 library first (one-time, ~500 MB).
set -e
cd "$(dirname "$0")"

FQBN="rp2040:rp2040:rpipico"
PICO_URL="https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json"

if [ "$1" = "setup" ]; then
    arduino-cli config init --overwrite >/dev/null
    arduino-cli config add board_manager.additional_urls "$PICO_URL"
    arduino-cli core update-index
    arduino-cli core install rp2040:rp2040
    arduino-cli lib install "Etherkit Si5351"
    exit 0
fi

arduino-cli compile --fqbn "$FQBN" --output-dir build foxsinad_fw
echo
echo "UF2: $(pwd)/build/foxsinad_fw.ino.uf2"
