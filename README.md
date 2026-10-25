# foxsinad

Portable 12 dB SINAD / receiver-desense field kit: a **PicoFox rev 3**
(AI6YM) 2 m beacon as the stepped RF source, a USB sound card +
**sinad_meter** (AI6KG) as the analyzer, and a Python sweep tool that finds
a radio's 12 dB SINAD point — on the bench or on a SOTA summit. The
summit-vs-baseline delta is measured desense, ground truth for the
[sota-rf](../sota-rf) risk tiers.

Status: the 2026-10-23 gate passed (PicoFox SINAD ceiling ≥ 23.6 dB through
the 8935). Bench sweeps work, and an untouched setup repeats to 0.10 dB. A
web UI is designed ([docs/plans/2026-10-25-ui-design.md](docs/plans/2026-10-25-ui-design.md))
but not built yet.
Bring-up and sweep instructions: [docs/bringup-audio.md](docs/bringup-audio.md).

## Setup

The PicoFox firmware and its driver live in their own repo,
[km6am/foxsinad-firmware](https://github.com/km6am/foxsinad-firmware),
checked out at `~/foxsinad-firmware` (or set `FOXSINAD_FIRMWARE`).
`foxsinad.picofox` loads that repo's `tools/foxctl.py` rather than keeping
a copy, so protocol changes are made in one place. `firmware/` here is the
frozen 0.1.0 pre-release.

```bash
~/miniconda3/envs/sota-rf/bin/python -m pip install --no-deps -e ~/foxsinad
```

That editable install makes `python -m foxsinad.<tool>` run this checkout
from any directory.

**Cabled mode** (`--cabled` on `sweep` and `ballpark`; firmware 0.3.0): for
output that goes only into a load or test equipment, so no Morse ID is
sent. Every run sets it on or off explicitly and turns it off at the end,
so it never carries into a run on an antenna.

Related repos: [PicoFox](https://github.com/ai6ym/PicoFox) ·
[sinad_meter](https://github.com/charlieh0tel/sinad_meter) ·
`~/Code/hp8935_sinad_serial` (lab-grade reference measurements, output
format shared with this project).
