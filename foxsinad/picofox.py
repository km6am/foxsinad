"""The PicoFox driver: foxctl from the foxsinad-firmware repo, not a copy.

The firmware repo (github.com/km6am/foxsinad-firmware) owns the serial
protocol and its driver, tools/foxctl.py. This module loads that file so
the bench tools here always speak the protocol of the firmware checkout
next to them. Set FOXSINAD_FIRMWARE to use a checkout other than
~/foxsinad-firmware.

CLI (same options as foxctl):
    python3 -m foxsinad.picofox --call KM6AM --freq 145.55 --att 64 --tone 1000 3000
    python3 -m foxsinad.picofox --cabled on --freq 145.55 --att 64 --tone 1000 3000
    python3 -m foxsinad.picofox --off
    python3 -m foxsinad.picofox --status
"""

import importlib.util
import os
from pathlib import Path

FIRMWARE_REPO = Path(os.environ.get("FOXSINAD_FIRMWARE", Path.home() / "foxsinad-firmware"))
_FOXCTL = FIRMWARE_REPO / "tools" / "foxctl.py"

if not _FOXCTL.exists():
    raise ImportError(
        f"{_FOXCTL} not found. Clone https://github.com/km6am/foxsinad-firmware "
        "to ~/foxsinad-firmware, or set FOXSINAD_FIRMWARE to your checkout.")

_spec = importlib.util.spec_from_file_location("foxctl", _FOXCTL)
foxctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(foxctl)

PicoFox = foxctl.PicoFox
PicoFoxError = foxctl.PicoFoxError
find_port = foxctl.find_port
main = foxctl.main


def set_cabled(fox, cabled):
    """Puts cabled mode in the state asked for. The firmware keeps it until
    power-off or *RST, so every run sets it explicitly: one run's cabled
    mode must never carry into a run on an antenna."""
    fox.set_cabled(bool(cabled))


if __name__ == "__main__":
    main()
