# Changelog

## 0.1.0 — unreleased

First public version.

**Measurement**
- 12 dB SINAD sweep: a coarse pass across the attenuator, then a fine pass that
  walks out from the crossing; the point comes from a fit over 12 ± 4 dB SINAD.
- Ballpark and meter modes, and record mode: SINAD every 0.25 s at a fixed
  level, with Morse ID windows flagged and left out of the summary.
- Cabled mode (no Morse ID) for output that goes only into a load or test
  equipment, set explicitly on every run and off at the end.
- Target audio level for setting the radio's volume, recorded with each run.

**App**
- Web UI as a summit workflow: session, radio, levels, sweep, finish. The
  Finish table reads every sweep saved in the session's folder.
- A simulated bench (`--sim`) for trying it, and for the tests, without hardware.
- The engine and page are shared with the Android app (km6am/foxysinad) and
  the rfbench test harness.
