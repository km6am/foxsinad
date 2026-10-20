# foxsinad_fw serial protocol

USB CDC serial, 115200 baud (rate is cosmetic over CDC). One command per
line, CR/LF tolerant, case-insensitive verbs. Every command gets exactly
one reply line: `OK ...` or `ERR <reason>`. Lines starting with `*` are
asynchronous events, not replies — a host driver must tolerate them at
any time.

| Command | Reply | Notes |
|---|---|---|
| `HELLO` (or `VER`) | `OK FOXSINAD <ver> REV<n>` | identify firmware + board revision |
| `CALL <sign>` | `OK CALL <SIGN>` | required before any RF-on command; letters/digits/`/`, ≤11 chars |
| `FREQ <MHz>` | `OK FREQ <MHz>` | 144–148 MHz minus a 20 kHz deviation margin; applies live if transmitting |
| `ATT <0-127>` | `OK ATT <n>` | BVA1761 step attenuator, 0.25 dB/step; r3 boards only; applies live |
| `TONE <hz> <dev_hz>` | `OK TONE <hz> <dev>` | FM sine, 100–2400 Hz tone, 100–10000 Hz peak deviation; standard SINAD test is `TONE 1000 3000` |
| `TONE OFF` / `CARRIER OFF` / `OFF` | `OK OFF` | RF off — sends an end-of-transmission morse ID first if anything was transmitted since the last ID |
| `CARRIER ON` | `OK CARRIER` | unmodulated carrier (level cal, desense source) |
| `ID` | `OK ID` | key the morse ID now (MCW, 600 Hz, 20 WPM) |
| `ABORT` | `OK ABORT` | immediate RF kill, **no** end-of-transmission ID; bench/emergency only |
| `STATUS` | `OK STATUS mode=… freq=… att=… tone=… dev=… call=… rev=… since_id=… up=…` | one line, key=value |

Async events:

- `*BOOT FOXSINAD <ver> REV<n>` — printed once at startup.
- `*ID START` / `*ID DONE` — brackets every morse ID, including the
  automatic one fired after 9 minutes of continuous transmission.
  **SINAD readings taken between these two events are garbage** (the
  modulation is morse, not the test tone); the host driver must discard
  them, and should expect replies to block for the ID duration (~10 s),
  since commands are applied by the RF core only between IDs.

Facts a host driver should rely on:

- `ATT` during TONE/CARRIER **blanks the RF for a few milliseconds**: per
  the designer, the attenuator bit-bang is not I2C-compatible with the
  Si5351 while transmitting, so the firmware disables the RF output,
  programs the attenuator, re-tunes the carrier (restoring PLL registers)
  and re-enables. It does **not** change the mode and **never triggers a
  morse ID** — ID fires only on the 9-min auto timer, on an RF-off
  command, or on an explicit `ID`. A sweep therefore holds the tone on
  for the whole run and steps `ATT`; discard a settle window after each
  step (the receiver's AGC/squelch needs one after the blank anyway).
  `FREQ` applies live without a blank.

- Boot state: RF off, attenuator at maximum (127), no callsign — the
  device cannot transmit until `CALL` is set.
- Replies are synchronous: `OK` means the RF core has *applied* the
  change, not merely queued it. Use a ≥15 s serial timeout to survive a
  reply that lands behind an automatic ID.
- The firmware is stateless: nothing survives a power cycle, so a driver
  must (re)configure everything, every session, in this order:
  `HELLO` → `CALL` → `FREQ` → `ATT` → `TONE`/`CARRIER`.
- Tone sample rate is 5 kS/s, so a tone at f0 has its first sampling
  image at 5000−f0 Hz (e.g. 4 kHz for the standard 1 kHz tone),
  zero-order-hold-attenuated ≈13 dB. Keep the analyzer's SINAD passband
  below it (e.g. 200–3000 Hz) so the image never counts as distortion.
