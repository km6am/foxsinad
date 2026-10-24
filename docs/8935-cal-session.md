# 8935 calibration & characterization session (plan task 2 — the gate)

Six tests, T1–T6. T4 is the go/no-go gate for the whole project. Every
test is automated by `foxsinad/cal8935.py`; **you** run each command (the
8935 serial port stays user-driven), the script drives both instruments
and writes the output files. All SCPI goes through SINADCTL's
`SCPI`/`SCPIQ` passthrough with the echo check your PROTOCOL.md §2
mandates.

Commands below assume `cd ~/foxsinad` and
`PY=~/miniconda3/envs/sota-rf/bin/python`. Every test accepts
`--port-8935 <dev>` (defaults to the hp8935 tools' remembered port) and
`--port-fox <dev>` (default: first `/dev/cu.usbmodem*`).

## T0 — setup (once)

1. **RF**: PicoFox SMA → short known coax → **RF IN/OUT** port (never
   ANT IN: the PicoFox makes ~+18 dBm ≈ 63 mW and ANT IN is a small-
   signal port). Note the coax loss if you know it; pass `--cable-loss-db`
   to fold it in, else 0 is assumed and the table is "at the 8935 end of
   this cable."
2. **PicoFox**: USB to the Mac, power switch on, foxsinad_fw flashed
   (it is, from the smoke test). It boots RF-off at max attenuation.
3. **8935**: your normal SINADCTL startup per hp8935_sinad_serial's
   OPERATING.md (load/run SINADCTL, Serial In → IBASIC). The script
   starts every run with `PING` and refuses to continue without a reply.
4. Preflight probe (checks both links, sets nothing):
   ```bash
   $PY -m foxsinad.cal8935 probe
   ```
   It prints HELLO from the PicoFox, PING/STATUS from SINADCTL, and one
   `SCPIQ AFAN:FILT2?` as the echo-check canary.

## T1 — cal table: SA marker power vs ATT  *(your "pull power off the SA")*

PicoFox on unmodulated carrier at 145.550; attenuator stepped 0→127
(every 4 counts = 1 dB, plus 127); each point: spectrum-analyzer peak
search, marker level read.

8935 config the script sends: `DISP SAN` · `SAN:INP 'RF In'` ·
`SAN:CFR 145.55 MHZ` · `SAN:ATT:MODE 'Auto'`; per point
`SAN:MARK:PPE` then `SCPIQ MEAS:SAN:MARK:LEV?` (median of 3 reads).

```bash
$PY -m foxsinad.cal8935 t1-cal
```

- Output: `cal/picofox_r3_attcal.json` (ATT → dBm) + CSV alongside.
- Sanity row: at ATT 0 it also reads `MEAS:RFR:POW?` (the TX power
  meter) as an absolute cross-check — SA marker and power meter should
  agree within ~1.5 dB (SA amplitude spec). **Relative** step accuracy
  is the BVA1761's (good); **absolute** accuracy is the SA's — the cal
  table records both readings so the uncertainty stays visible.
- Expect: monotonic ~0.25 dB/count, total span ≈ 31.75 dB. Any step
  where the level *rises* with increasing ATT = attenuator bit problem;
  watch the 32/64/96 binary boundaries.

## T2 — flatness vs frequency

Carrier at ATT 0 and ATT 64 on 144.10 / 145.55 / 146.50 / 147.90 MHz
(SA re-centered each time, same marker read).

```bash
$PY -m foxsinad.cal8935 t2-flatness
```

Expect < ~1 dB variation; if larger, the cal table becomes per-frequency
(the JSON already stores its frequency).

## T3 — modulation characterization: FM deviation of the PicoFox

*(I read your "tiny filter" as the PicoFox — "tiny fox". If you meant
inserting the physical tinyfilt from the hp8935 repo inline, that's just
T1/T4 re-run with it in the path; say so and we add it.)*

AF analyzer on FM Demod, **wide** filters for this test
(`AFAN:FILT2 '15kHz LPF'`) so the real modulation incl. the 4 kHz
sampling image is captured; `MEAS:AFR:SEL 'FM Deviation'`, read
`MEAS:AFR:FM?`.

Part A — deviation linearity: `TONE 1000 <dev>` for dev = 1000…10000
step 1000, at ATT 40.
Part B — audio flatness: `TONE <hz> 3000` for hz = 300…2400, read
deviation and `MEAS:AFR:AFR?` (demodulated audio frequency readback).

```bash
$PY -m foxsinad.cal8935 t3-deviation
```

Expect: measured dev ≈ 0.951 × commanded at 1 kHz (the 5 kS/s sample
grid misses the sine peak — computed, now measured), linear in the
command; flat ±1 dB across 300–2400 Hz. The 8935 reads **peak** deviation
in kHz — confirm the units subfield on screen at the first point.

## T4 — SINAD ceiling through the 8935's own demod  **(GATE)**

No radio in the loop: the 8935 demodulates the PicoFox directly, so this
isolates the source's modulation purity — the SI5351 I2C-FM question the
whole design hangs on.

AF analyzer: `AFAN:INP 'FM Demod'` · `AFAN:FILT1 '300Hz HPF'` ·
`AFAN:FILT2 '3kHz LPF'` *(your 3 kHz upper cutoff — same passband our
sound-card analyzer defaults to)* · `MEAS:AFR:SEL 'SINAD'`. PicoFox:
`TONE 1000 3000`. Read `MEAS:AFR:SINAD?` ×16, median + sd, at ATT 0,
40, 80.

```bash
$PY -m foxsinad.cal8935 t4-ceiling
```

**PASS: ≥ ~20 dB at every level.** Comfortably above 20 → the 12 dB
crossing will be measurable with margin; 15–20 → marginal, usable but
thin; below ~15 → stop, rethink the source before building the sweep.

## T5 — SINAD vs power, well above threshold

Your requested map: `TONE 1000 3000` held on, ATT stepped 0→127 every 8
counts (2 dB); each point reads the SA marker level **and** SINAD
(8 reads, median). Since the 8935's demod is far more sensitive than any
HT, every point is "well above threshold" — the curve should be a flat
plateau at the T4 ceiling. Any slope says modulation quality depends on
level (it shouldn't) or the attenuator is doing something non-passive.

```bash
$PY -m foxsinad.cal8935 t5-sinad-vs-power
```

Output: CSV `att, level_dbm, sinad_db, sinad_sd` — this is also the
bench reference the field sweep format is checked against.

## T6 — warm-up drift (replaces the battery test; USB-powered by design)

From a **cold** PicoFox (powered off ≥30 min, then on): carrier at
ATT 0, SA marker level logged every 30 s for 20 min.

```bash
$PY -m foxsinad.cal8935 t6-warmup
```

Expect < a few tenths of a dB after warm-up; whatever it is becomes the
cal table's stated level uncertainty. (Frequency drift is the TCXO's
problem and irrelevant to FM reception.)

## Order and time

T0 probe (2 min) → T4 first — it's the gate; if it fails nothing else
matters (10 min) → T1 (15 min) → T3 (10 min) → T5 (10 min) → T2 (5 min)
→ T6 whenever a cold start is convenient (20 min unattended).

## Verify-on-bench notes (things the manual + protocol can't settle)

- First `MEAS:SAN:MARK:LEV?`: confirm the number matches the marker
  readout on screen (units/offset sanity).
- First `MEAS:AFR:FM?`: confirm kHz and that the Pk/RMS detector
  subfield is where you want it (script assumes peak).
- If any SCPIQ echo mismatches, the script re-reads once and aborts the
  point on a second mismatch (PROTOCOL.md §10 behavior).
