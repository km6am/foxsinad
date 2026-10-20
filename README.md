# foxsinad

Portable 12 dB SINAD / receiver-desense field kit: a **PicoFox rev 3**
(AI6YM) 2 m beacon as the stepped RF source, a USB sound card +
**sinad_meter** (AI6KG) as the analyzer, and a Python sweep tool that finds
a radio's 12 dB SINAD point — on the bench or on a SOTA summit. The
summit-vs-baseline delta is measured desense, ground truth for the
[sota-rf](../sota-rf) risk tiers.

Status: **scaffold + firmware fork built (compiles; bench smoke test pending).** See
[docs/plans/2026-10-20-foxsinad-design.md](docs/plans/2026-10-20-foxsinad-design.md)
for the design and
[docs/plans/2026-10-20-foxsinad-implementation.md](docs/plans/2026-10-20-foxsinad-implementation.md)
for the build order. The first task is a go/no-go measurement: whether the
PicoFox's SI5351-based FM is clean enough (SINAD ceiling ≥ ~20 dB vs the
HP 8935). Nothing else gets built until that passes.

Related repos: [PicoFox](https://github.com/ai6ym/PicoFox) ·
[sinad_meter](https://github.com/charlieh0tel/sinad_meter) ·
`~/Code/hp8935_sinad_serial` (lab-grade reference measurements, output
format shared with this project).
