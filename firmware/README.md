# foxsinad_fw — serial-controlled test firmware for the PicoFox r3

A stateless fork of [PicoFox-Firmware](https://github.com/ai6ym/PicoFox)
(CC BY-NC-SA 4.0, see [LICENSE](LICENSE)): no flash filesystem, no USB
mass storage, no `settings.txt` — every parameter arrives over USB
serial (see [PROTOCOL.md](PROTOCOL.md)). RF stays off until a callsign
is set, the attenuator boots at maximum, and a morse ID keys
automatically every 9 minutes of transmission and at RF-off.

What it keeps from upstream, verbatim: the pin map, board-revision
detect, the SI5351 fixed-÷6/modulate-the-PLL scheme and its per-sample
timing, and the BVA1761 bit-bang driver (which borrows the I2C pins, so
all RF hardware access lives on core 1; core 0 only parses serial).

## Build

Requires [arduino-cli](https://arduino.github.io/arduino-cli/) with the
[Arduino-Pico](https://github.com/earlephilhower/arduino-pico) core and
the Etherkit Si5351 library:

```bash
./build.sh setup   # one-time: installs core + library
./build.sh         # compiles; UF2 lands in build/
```

Uses the default Pico SDK USB stack (plain `rpipico` FQBN) — the stock
firmware's Adafruit TinyUSB stack is only needed for the mass-storage
device this fork deliberately drops.

## Flash

Hold BOOTSEL while plugging the PicoFox in, then copy
`build/foxsinad_fw.ino.uf2` onto the `RPI-RP2` drive that appears. To
return the device to fox duty, flash the stock release UF2 from the
upstream repo the same way.

## Smoke test (no RF gear needed)

```bash
python3 -m serial.tools.miniterm /dev/cu.usbmodem* 115200
```

Expect `*BOOT FOXSINAD <ver> REV3`. Then `HELLO`, `CALL <yourcall>`,
`STATUS`, `ATT 64`, `CARRIER ON` (LED lights), `OFF`. A nearby 2 m
receiver on 146.565 MHz should hear the carrier and the morse ID.
