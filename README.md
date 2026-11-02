# foxsinad

Portable 12 dB SINAD / receiver-desense field kit: a **PicoFox rev 3**
(AI6YM) 2 m beacon as the stepped RF source, a USB sound card +
**sinad_meter** (AI6KG) as the analyzer, and a Python sweep tool that finds
a radio's 12 dB SINAD point — on the bench or on a SOTA summit. The
summit-vs-baseline delta is measured desense, ground truth for the
[sota-rf](https://github.com/km6am/sota-rf) risk tiers.

Status: the 2026-10-23 gate passed (PicoFox SINAD ceiling ≥ 23.6 dB through
the 8935). Bench sweeps work, and an untouched setup repeats to 0.10 dB. The
web UI is a step-by-step summit workflow (session, radio, levels, sweep,
finish): `python -m foxsinad.app` (add `--sim` to try it without hardware).
The same page and engine run on an Android phone in
[km6am/foxysinad](https://github.com/km6am/foxysinad).
Bring-up and sweep instructions: [docs/bringup-audio.md](docs/bringup-audio.md).

## Setup

The PicoFox firmware and its driver live in their own repo,
[km6am/foxsinad-firmware](https://github.com/km6am/foxsinad-firmware),
checked out at `~/foxsinad-firmware` (or set `FOXSINAD_FIRMWARE`).
`foxsinad.picofox` loads that repo's `tools/foxctl.py` rather than keeping
a copy, so protocol changes are made in one place. Since firmware 0.4.0
the PicoFox speaks SCPI; `python -m foxsinad.picofox --scpi "FREQ?"` sends
any command.

```bash
git clone --recursive https://github.com/km6am/foxsinad   # sinad_meter is a submodule
git clone https://github.com/km6am/foxsinad-firmware
python3 -m pip install -e ./foxsinad                      # Python 3.10 or newer
```

That editable install makes `python3 -m foxsinad.<tool>` run this checkout
from any directory. Tests: `python3 -m pytest -q tests` (they use the
simulated bench, no hardware).

**Cabled mode** (`--cabled` on `sweep` and `ballpark`; firmware 0.4.0): for
output that goes only into a load or test equipment, so no Morse ID is
sent. Every run sets it on or off explicitly and turns it off at the end,
so it never carries into a run on an antenna.

Related repos: [PicoFox](https://github.com/ai6ym/PicoFox) ·
[sinad_meter](https://github.com/charlieh0tel/sinad_meter) ·
hp8935_sinad_serial (lab-grade reference measurements, output format shared
with this project; not public).

## License

MIT for this repo's code (see [LICENSE](LICENSE)). sinad_meter is included as
a submodule under its own MIT license. The PicoFox firmware is a derivative of
AI6YM's PicoFox-Firmware and keeps its CC BY-NC-SA 4.0 license, in
[foxsinad-firmware](https://github.com/km6am/foxsinad-firmware).

SINAD figures from this kit are estimates from a sound-card analyzer, checked
against an HP 8935 on the bench; they are not calibrated laboratory
measurements.
