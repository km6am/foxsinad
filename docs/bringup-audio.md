# Bringing up the audio / SINAD / RF chain

From an unplugged USB audio adapter to a first 12 dB SINAD sweep, in
steps that each check one link. Commands assume `cd ~/foxsinad` and
`PY=~/miniconda3/envs/sota-rf/bin/python`.

```
PicoFox SMA ─ fixed attenuators (total = --pad-db) ─ radio antenna jack
radio earphone out ─ 3.5 mm to RCA ─ UFO202 LINE IN ─ USB ─ Mac
```

**You need:**
- the UFO202, with its switch on **LINE** (PHONO adds RIAA equalization
  and silently skews SINAD)
- a 3.5 mm-to-RCA cable from the radio's **earphone** jack. Not a
  speaker output: some radios drive the speaker from both sides (bridged),
  and grounding one side can damage the radio.
- an RCA-to-RCA cable, for the loopback test
- fixed attenuators of known value, and a way to shield the radio or the
  PicoFox (see step 4)

## 1. Find the adapter

```bash
$PY -m foxsinad.audiocheck devices
```

This lists every audio device and says which one was auto-detected as the
USB line-in adapter. If it doesn't find the UFO202, note its number in the
list and add `--device <number>` to the commands below. It never picks the
built-in microphone: macOS voice processing removes test tones from it.

## 2. The analyzer's own floor (loopback)

Connect the UFO202's line **OUT** to its line **IN** with the RCA cable.
No RCA cable? Use the radio cable instead: its 3.5 mm plug into the
UFO202's **headphone** jack (knob near the middle), and its white RCA
into line **IN**. The test plays on the left channel only.

```bash
$PY -m foxsinad.audiocheck loopback
```

It plays a 1 kHz tone at −12 dBFS and measures it on the input. The SINAD
it reports is the best the analyzer can read; a radio measured through it
can't read higher. It should be far above 12 dB (the PicoFox itself tops
out at 23.6 dB), so anything near or below ~30 dB means a cabling, level
or software problem to fix first. Record the number.

## 3. Radio audio, at a strong signal

Connect the full chain above, with the step attenuator set low enough
that the tone is clearly audible on the radio. Set the radio to 145.55 MHz with the
**squelch fully open**. Start a strong tone:

```bash
$PY -m foxsinad.picofox --call KM6AM --freq 145.55 --att 0 --tone 1000 3000
$PY -m foxsinad.audiocheck level
```

To hear the radio on the computer's speakers at the same time, add
`--monitor`, e.g. `audiocheck level --monitor`. It works in `ballpark`,
`sweep` and the live `analyzer` too, and helps when finding the 12 dB point
by ear. To hear it somewhere else, use `--monitor-device <name or number>`.

Adjust the radio's volume until the peaks sit between **−20 and −6 dBFS**,
then leave the volume alone for the rest of the session. Ctrl-C the
level meter, then watch the live SINAD:

```bash
$PY -m foxsinad.analyzer
```

At a strong signal it should read high, though never above about 23.6 dB
(the PicoFox's ceiling). Change the level and watch it follow:

```bash
$PY -m foxsinad.picofox --att 127
```

(or turn the step attenuator up).

Finish with `$PY -m foxsinad.picofox --off`.

## 4. Leak test (don't skip)

At the levels a 12 dB SINAD test needs (around −120 dBm), signal leaking
around the attenuators matters. The PicoFox puts out +12 dBm, so a leak
130 dB down is already as strong as the test signal.

With the tone on at `ATT 0`, **disconnect the attenuator chain at the
radio** and cap or terminate the radio's input. The analyzer's SINAD must
fall to the noise reading: under about 1 dB (computed: on pure noise this
analyzer reads 0.2 dB typically, 0.5 dB at most). If it stays higher,
signal is getting in some other way: radiating from the
PicoFox or its USB cable, or leaking through the radio's case. Move them
apart, shield the PicoFox, or both, until it stays at the bottom.
Otherwise every sweep will read optimistically.

### Cable arrangement matters more than anything else

*Measured 2026-10-24 (UV-K5, session `uvk5_cabletouch`):* moving the audio
cable, the radio or the PicoFox changed the 12 dB SINAD point by 2–7 dB
per move, a 9.6 dB spread over four runs. Once the cabling was tidied and
left alone, five runs agreed within **0.10 dB**. Inferred cause: the audio
cable to the USB adapter and laptop carries noise into the radio, and
signal can leak around the attenuators, both depending on placement.
- Fix the arrangement before measuring, and don't touch it during a
  session.
- Re-run the leak test (step 4) after any change to the layout.
- A result *better* than an independent reference is the warning sign of
  leakage.

## 5. Set the step attenuator (ballpark)

The PicoFox's attenuator covers only about 32 dB, from +12 to −20 dBm. A
manual step attenuator between the PicoFox and the radio has to place
the radio's 12 dB SINAD point inside that window. The best place is the
middle, so the sweep has room either way.

```bash
$PY -m foxsinad.ballpark
```

1. **Set the step attenuator to MAXIMUM first**, then press Enter. The
   PicoFox transmits the tone at `ATT 64`, the middle of its range (about
   −4 dBm).
2. Reduce the step attenuator while watching the live SINAD and its hint.
   It reads "too strong", "too weak", or **"GOOD: about 12 dB"**.
3. At "GOOD", press **Ctrl-C**. RF turns off (with the Morse ID).
4. Enter the **total** external attenuation: the step attenuator setting
   plus any fixed pads plus cable loss.
5. It prints the exact sweep command, with that value as `--pad-db`.

The sweep can only be as accurate as that total. A step attenuator's own
accuracy, and the cable losses, go straight into every level it reports.

## 6. First sweep

Run the command `ballpark` printed, for example:

```bash
$PY -m foxsinad.sweep --pad-db 119 --radio uvk5 --condition bench
```

**What the sweep does:**
1. A sanity reading at full level. It stops early, saying what to fix, if
   the audio clips, there's no audio, or the reading is already below
   12 dB.
2. A coarse pass in 2 dB steps, from full level down into the tail, until
   SINAD is at the noise (below 3 dB twice; `--tail-db` changes that) or
   the attenuator runs out.
3. A fine pass in 0.25 dB steps across the crossing.
4. A straight-line fit through the fine points near 12 dB gives the
   **12 dB SINAD level at the radio**.

**While it runs:**
- Readings that overlap a Morse ID are discarded and repeated.
- The tone is turned off at the end.

**Output** goes to `runs/`, named and formatted as hp8935_sinad_serial's
`run.py` does it:
- `run_<radio>_<condition>_<freq>MHz.csv`, or with `--session NAME`,
  `runs/NAME/<radio>_<condition>_<freq>MHz.csv`
- a repeat gets `_2`, `_3`, never overwriting
- the header includes the 12 dB result (`sinad12_dbm`, `sinad12_uv`,
  `sinad12_sigma_db`, `sinad12_fit_points`)
- columns are run.py's, plus `att` and `n_discarded`
- readings stream to a `.partial.csv` while it runs, so a stopped run
  keeps its data

**Options:**
- `--live` opens a plot that fills in as readings come (closes 3 s after
  the run; `--hold` keeps it open).
- `--repeat N` runs it N times; with `--session`, a repeatability summary
  is printed at the end.
- `--settle`, `--avg-coarse`, `--avg-fine` override the per-reading
  times (defaults in `sweep.py`, provisional until measured on hardware).

**Antenna to antenna:** the path loss between the antennas isn't known, so
set `--pad-db` to the attenuators only. The "level at the radio" is then
really the level at the PicoFox's antenna, a fixed offset from the truth.
Runs remain comparable with each other, but not with conducted results.

**Accuracy:** the level at the radio is the T1 cal-table value for each
setting minus `--pad-db`. So it's only as good as the cal table (measured
at 145.55 MHz) and as your knowledge of the attenuators and cables.

## 7. Cross-check against the 8935

For a radio you've already measured with hp8935_sinad_serial, the two
12 dB points should agree within about 2 dB. A larger gap means one of
these:
- the `--pad-db` figure
- a leak (step 4)
- the filter difference: this analyzer uses 200–3000 Hz, while the 8935
  runs used its own filter settings, recorded in their CSV headers

A tighter check of the analyzer alone: split the radio's audio with a
Y-cable to both the 8935's AUDIO IN and the UFO202. Then compare the two
SINAD readings on the same audio at a few levels.

## Status

- **Measured 2026-10-24, loopback** (UFO202 headphone jack into its line
  in): **78.5 dB SINAD** (sd 0.31 dB, peaks −20 dBFS). The adapter's
  output stage is in that path too, so the input alone is at least that
  good. With the input open it sits at −86 dBFS rms.
- The radio steps (3–7) were written and checked only against a simulated radio
  (`tests/test_sweep.py`: it finds a simulated −118.0 dBm point at
  −117.9).
- Nothing in this chain has run on real audio hardware yet.
