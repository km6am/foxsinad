"""8935 calibration & characterization session (docs/8935-cal-session.md).

Drives the PicoFox (foxsinad_fw, our serial driver) and the HP 8935
through the hp8935_sinad_serial SINADCTL IBASIC program's SCPI/SCPIQ
passthrough. The user launches SINADCTL on the 8935 first (their
OPERATING.md procedure) and runs each test from the command line; this
module never opens the 8935 port except as invoked.

SCPIQ replies echo the query (SCPIQ,<echo>,<answer>) and the echo MUST be
checked: a mismatch means the readback queue is lagging by one
(hp8935_sinad_serial PROTOCOL.md §2/§10). On mismatch we read again once,
then abort the point.
"""

import argparse
import csv
import glob
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

import serial

from .picofox import PicoFox

CAL_DIR = Path(__file__).resolve().parents[1] / "cal"
DEFAULT_FREQ_MHZ = 145.55
HP8935_CONFIG = Path.home() / ".config" / "hp8935_sinad" / "config.json"


def hp8935_remembered():
    """(port, callsign) from the hp8935_sinad tools' own config, else Nones."""
    try:
        cfg = json.loads(HP8935_CONFIG.read_text())
    except (OSError, ValueError):
        return None, None
    return cfg.get("port"), (cfg.get("station") or {}).get("call")


class Hp8935Error(RuntimeError):
    pass


class Sinadctl:
    """Minimal SINADCTL client: PING, SCPI, SCPIQ with echo verification."""

    def __init__(self, port, baud=9600, timeout_s=10.0):
        self._ser = serial.Serial(port, baud, timeout=0.5)
        self.timeout_s = timeout_s
        time.sleep(0.2)
        self._ser.reset_input_buffer()

    def close(self):
        self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _readline(self, timeout_s=None):
        deadline = time.time() + (timeout_s or self.timeout_s)
        while time.time() < deadline:
            line = self._ser.readline().decode(errors="replace").strip()
            if line:
                return line
        raise Hp8935Error("timeout waiting for SINADCTL reply")

    def _send(self, line):
        self._ser.write((line + "\r\n").encode())

    PING_HELP = """\
No reply to PING on {port}.

Please ensure SINADCTL is running on the 8935 and the IBASIC CONTROLLER
screen is up:

  1. On the 8935: Software Menu -> TESTS (IBASIC CONTROLLER). If SINADCTL
     is already in memory, press the `Run` softkey; if not, load it:
         cd ~/Code/hp8935_sinad_serial && ~/miniconda3/envs/vnafit/bin/python load.py
  2. If it was running and the 8935 was power-cycled: Serial In is stuck
     on `IBASIC` with nothing running (the OPERATING.md trap). Front
     panel: I/O CONFIGURE -> Serial In -> `Inst`, then Run or reload.
  3. Not sure which state it's in? This observes rather than guesses:
         cd ~/Code/hp8935_sinad_serial && ~/miniconda3/envs/vnafit/bin/python run.py state
     (want: `SINADCTL running`)
  4. Also worth a glance: right adapter ({port}), 9600 8-N-1, and that
     nothing else has the port open."""

    def ping(self):
        self._send("PING")
        try:
            reply = self._readline()
        except Hp8935Error:
            raise Hp8935Error(self.PING_HELP.format(port=self._ser.port)) from None
        if not reply.startswith("OK SINADCTL"):
            if reply.strip().upper() == "PING":
                # Our own characters came back: IBASIC Echo On is echoing
                # keystrokes, so they went to the front panel, not to a
                # running program.
                raise Hp8935Error(
                    "The 8935 echoed the command back - characters are going\n"
                    "to the front panel, so SINADCTL is not running. On the\n"
                    "IBASIC CONTROLLER screen press the `Run` softkey (or load\n"
                    "it with hp8935_sinad_serial's load.py), then retry."
                )
            raise Hp8935Error(
                f"PING answered {reply!r} instead of 'OK SINADCTL <ver>'.\n"
                "Something other than SINADCTL is on the line - if a stale\n"
                "program or half-typed command is involved, QUIT/Run it per\n"
                "hp8935_sinad_serial OPERATING.md section 4."
            )
        return reply

    def scpi(self, cmd):
        """Tunnelled command; raises if the instrument logged errors."""
        self._send("SCPI " + cmd)
        reply = self._readline()
        if not reply.startswith("OK SCPI"):
            raise Hp8935Error(f"SCPI {cmd!r} got {reply!r}")
        nerr = int(reply.split()[2])
        if nerr:
            detail = self._readline()  # SCPIE,<cmd>,<instrument words>
            raise Hp8935Error(f"SCPI {cmd!r}: {detail}")

    def scpiq(self, query, retries=1):
        """Tunnelled query with the mandatory echo check."""
        self._send("SCPIQ " + query)
        for _ in range(retries + 1):
            reply = self._readline()
            if reply.upper().startswith("ERR"):
                raise Hp8935Error(f"SCPIQ {query!r}: {reply}")
            parts = reply.split(",", 2)
            if len(parts) == 3 and parts[0] == "SCPIQ":
                if parts[1].strip().upper() == query.strip().upper():
                    return parts[2].strip().strip('"')
                # lagged reply from an abandoned earlier query: discard,
                # the next line should be ours
                continue
        raise Hp8935Error(f"SCPIQ {query!r}: echo never matched (lagging queue)")

    def scpiq_float(self, query, n=1, settle_s=0.0):
        vals = []
        for _ in range(n):
            if settle_s:
                time.sleep(settle_s)
            vals.append(float(self.scpiq(query)))
        return statistics.median(vals), (statistics.stdev(vals) if n > 1 else 0.0)


# ---------------------------------------------------------------- 8935 setups
def sa_setup(hp, freq_mhz):
    hp.scpi("DISP SAN")
    hp.scpi("SAN:INP 'RF In'")
    hp.scpi("SAN:ATT:MODE 'Auto'")
    hp.scpi(f"SAN:CFR {freq_mhz} MHZ")
    time.sleep(1.0)


def _as_mhz(value):
    """Marker frequency in MHz. The manual doesn't state the query's units;
    in the 2 m band Hz, kHz and MHz readings can't be confused."""
    if value > 1e7:
        return value / 1e6
    if value > 1e4:
        return value / 1e3
    return value


def sa_peak_dbm(hp, expect_mhz, tol_khz=5.0, agree_db=0.2,
                sweep_wait_s=0.7, max_tries=15):
    """Peak level of the carrier at expect_mhz, verified rather than trusted.

    A peak search only looks at the trace as it stands. Just after a
    re-center or RF-on, the trace can still be the old one (seen on
    hardware 2026-10-23: marker parked off the carrier, read -35.79 dBm
    while the screen showed +12). So: wait, peak-search, check that the
    marker frequency is on the carrier, and only accept a level once two
    consecutive on-carrier reads agree.
    """
    last = None
    seen = []
    for _ in range(max_tries):
        time.sleep(sweep_wait_s)
        hp.scpi("SAN:MARK:PEAK")  # manual's own example spelling (p319)
        mhz = _as_mhz(float(hp.scpiq("MEAS:SAN:MARK:FREQ?")))
        level = float(hp.scpiq("MEAS:SAN:MARK:LEV?"))
        seen.append((round(mhz, 5), round(level, 2)))
        if abs(mhz - expect_mhz) * 1e3 > tol_khz:
            last = None
            continue
        if last is not None and abs(level - last) <= agree_db:
            return (level + last) / 2
        last = level
    raise Hp8935Error(
        f"SA marker never settled on the carrier at {expect_mhz} MHz "
        f"(+/-{tol_khz} kHz) with two reads within {agree_db} dB; "
        f"(MHz, dBm) seen: {seen}")


def afan_setup(hp, filt2, sel, filt1="300Hz HPF"):
    hp.scpi("DISP AFAN")
    hp.scpi("AFAN:INP 'FM Demod'")
    hp.scpi(f"AFAN:FILT1 '{filt1}'")
    hp.scpi(f"AFAN:FILT2 '{filt2}'")
    hp.scpi(f"MEAS:AFR:SEL '{sel}'")
    time.sleep(1.0)


# ---------------------------------------------------------------------- tests
def t_probe(hp, fox, args):
    print("8935:", hp.ping())
    print("8935 FILT2:", hp.scpiq("AFAN:FILT2?"))
    print("PicoFox:", fox.hello())
    for k, v in fox.status().items():
        print(f"  {k}={v}")


def _writerows(name, header, rows):
    CAL_DIR.mkdir(exist_ok=True)
    # Timestamped so a same-day rerun never overwrites an earlier run.
    path = CAL_DIR / f"{name}_{time.strftime('%Y-%m-%d_%H%M%S')}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {path}")
    return path


def t1_cal(hp, fox, args):
    f = args.freq
    sa_setup(hp, f)
    fox.set_freq(f)
    fox.set_att(0)
    fox.carrier()
    time.sleep(1.0)
    # Absolute cross-check against the TX power meter (reads the RF
    # ANALYZER screen's TX Power field, so tune the analyzer first).
    # Non-fatal: at +18 dBm-cable the power meter is near its floor and
    # may read invalid.
    try:
        hp.scpi(f"RFAN:FREQ {f} MHZ")
        hp.scpi("RFAN:INP 'RF In'")
        time.sleep(1.0)
        txpow = hp.scpiq("MEAS:RFR:POW?") or "(blank)"
    except Hp8935Error as e:
        txpow = f"unavailable ({e})"
    print(f"ATT 0 TX-power-meter cross-check: {txpow} (compare SA row below)")

    rows = []
    atts = list(range(0, 128, 4)) + [127]
    for att in atts:
        fox.set_att(att)
        time.sleep(args.settle)
        dbm = sa_peak_dbm(hp, f) + args.cable_loss_db
        rows.append((att, att * 0.25, round(dbm, 2)))
        print(f"ATT {att:3d} (-{att*0.25:5.2f} dB) -> {dbm:7.2f} dBm")
    fox.off()
    fox.set_att(127)

    _writerows("t1_attcal", ("att", "nominal_db", "dbm"), rows)
    CAL_DIR.mkdir(exist_ok=True)
    cal = {
        "device": "picofox_r3",
        "freq_mhz": f,
        "date": date.today().isoformat(),
        "cable_loss_db_added": args.cable_loss_db,
        "tx_power_meter_at_att0": txpow,
        "points": [{"att": a, "dbm": d} for a, _, d in rows],
    }
    path = CAL_DIR / "picofox_r3_attcal.json"
    path.write_text(json.dumps(cal, indent=1))
    print(f"wrote {path}")


def t2_flatness(hp, fox, args):
    rows = []
    for f in (144.10, 145.55, 146.50, 147.90):
        sa_setup(hp, f)
        fox.set_freq(f)
        for att in (0, 64, 127):
            fox.set_att(att)
            fox.carrier()
            time.sleep(args.settle)
            dbm = sa_peak_dbm(hp, f) + args.cable_loss_db
            rows.append((f, att, round(dbm, 2)))
            print(f"{f:.2f} MHz ATT {att:3d} -> {dbm:7.2f} dBm")
    fox.off()
    fox.set_att(127)
    _writerows("t2_flatness", ("freq_mhz", "att", "dbm"), rows)


def t3_deviation(hp, fox, args):
    f = args.freq
    fox.set_freq(f)
    fox.set_att(40)
    hp.scpi(f"RFAN:FREQ {f} MHZ")
    # FM deviation is not a MEAS:AFR:SEL menu entry - it is the AF
    # analyzer's own level field whenever AF Anl In = FM Demod, read
    # directly with MEAS:AFR:FM?. Select 'AF Freq' so the Part-B
    # frequency readback (MEAS:AFR:AFR?) is the displayed measurement.
    afan_setup(hp, filt2="15kHz LPF", sel="AF Freq")

    rows = []
    print("Part A: deviation linearity at 1 kHz tone")
    for dev in range(1000, 10001, 1000):
        fox.tone(1000, dev)
        time.sleep(args.settle)
        # MEAS:AFR:FM? returns Hz (verified on hardware 2026-10-23: the
        # 300 Hz-tone reading matched the 0.951x sampled-peak prediction
        # only in Hz), despite the screen field being labeled in kHz.
        hz_meas, sd = hp.scpiq_float("MEAS:AFR:FM?", n=5, settle_s=0.3)
        rows.append(("linearity", 1000, dev, round(hz_meas, 1), round(sd, 1)))
        print(f"dev {dev:5d} Hz -> measured {hz_meas:7.1f} Hz (sd {sd:.1f})")

    print("Part B: audio flatness at 3 kHz deviation")
    for hz in (300, 500, 800, 1000, 1500, 2000, 2400):
        fox.tone(hz, 3000)
        time.sleep(args.settle)
        hz_meas, sd = hp.scpiq_float("MEAS:AFR:FM?", n=5, settle_s=0.3)
        afr = hp.scpiq("MEAS:AFR:FREQ?")
        rows.append(("flatness", hz, 3000, round(hz_meas, 1), round(sd, 1)))
        print(f"tone {hz:4d} Hz -> dev {hz_meas:7.1f} Hz (sd {sd:.1f}) afr {afr}")
    fox.off()
    fox.set_att(127)
    _writerows("t3_deviation",
               ("part", "tone_hz", "dev_cmd_hz", "dev_meas_hz", "sd_hz"), rows)


def t4_ceiling(hp, fox, args):
    f = args.freq
    fox.set_freq(f)
    hp.scpi(f"RFAN:FREQ {f} MHZ")
    afan_setup(hp, filt2="3kHz LPF", sel="SINAD")
    fox.tone(1000, 3000)

    rows = []
    for att in (0, 40, 80):
        fox.set_att(att)
        time.sleep(args.settle)
        sinad, sd = hp.scpiq_float("MEAS:AFR:SINAD?", n=16, settle_s=0.25)
        rows.append((att, round(sinad, 2), round(sd, 2)))
        print(f"ATT {att:3d} -> SINAD {sinad:5.1f} dB (sd {sd:.2f})")
    fox.off()
    fox.set_att(127)
    _writerows("t4_ceiling", ("att", "sinad_db", "sd_db"), rows)

    worst = min(r[1] for r in rows)
    print(f"\nGATE: worst ceiling {worst:.1f} dB -> "
          + ("PASS (>=20)" if worst >= 20 else
               "MARGINAL (15-20)" if worst >= 15 else "FAIL (<15): stop and rethink"))


def t5_sinad_vs_power(hp, fox, args):
    f = args.freq
    sa_setup(hp, f)
    fox.set_freq(f)
    fox.set_att(0)
    fox.tone(1000, 3000)

    rows = []
    for att in list(range(0, 128, 8)) + [127]:
        fox.set_att(att)
        time.sleep(args.settle)
        hp.scpi("DISP SAN")
        time.sleep(0.5)
        dbm = sa_peak_dbm(hp, f) + args.cable_loss_db
        afan_setup(hp, filt2="3kHz LPF", sel="SINAD")
        sinad, sd = hp.scpiq_float("MEAS:AFR:SINAD?", n=8, settle_s=0.25)
        rows.append((att, round(dbm, 2), round(sinad, 2), round(sd, 2)))
        print(f"ATT {att:3d} -> {dbm:7.2f} dBm  SINAD {sinad:5.1f} dB (sd {sd:.2f})")
    fox.off()
    fox.set_att(127)
    _writerows("t5_sinad_vs_power", ("att", "dbm", "sinad_db", "sd_db"), rows)


def t6_warmup(hp, fox, args):
    f = args.freq
    sa_setup(hp, f)
    fox.set_freq(f)
    fox.set_att(0)
    fox.carrier()
    print("logging SA level every 30 s for 20 min (start from a COLD PicoFox)")
    rows = []
    t0 = time.time()
    while time.time() - t0 < 20 * 60:
        dbm = sa_peak_dbm(hp, f) + args.cable_loss_db
        t = round(time.time() - t0, 1)
        rows.append((t, round(dbm, 2)))
        print(f"t={t:6.0f}s  {dbm:7.2f} dBm", flush=True)
        time.sleep(30)
    fox.off()
    fox.set_att(127)
    _writerows("t6_warmup", ("t_s", "dbm"), rows)


def t3b_deviation_corrected(hp, fox, args):
    """Part B repeated with ZOH pre-compensation, measured through the
    3 kHz LPF so the reading is the tone component, not staircase edges.

    The firmware's 5 kS/s zero-order hold scales the tone component by
    sinc(f/5000); command dev = target/sinc so the delivered tone-component
    deviation is the target at every tone frequency. Prediction: flat
    3000 Hz for tones up to ~1500 Hz. Above that, the first sampling
    image (5000-f) falls inside the 3 kHz measurement filter (3000 Hz at
    f=2000, 2600 Hz at f=2400) and beats with the tone, so the peak
    detector should read high again there - if it does, that is the
    image, not the modulator.
    """
    import math

    f = args.freq
    target = 3000.0
    fox.set_freq(f)
    fox.set_att(40)
    hp.scpi(f"RFAN:FREQ {f} MHZ")
    # 20 Hz HPF (manual's FILT1 list): the 300 Hz HPF used in the first
    # t3b run took ~25% off the 300 Hz tone.
    afan_setup(hp, filt2="3kHz LPF", sel="AF Freq", filt1="20Hz HPF")

    rows = []
    for hz in (300, 500, 800, 1000, 1500, 2000, 2400):
        zoh = math.sin(math.pi * hz / 5000) / (math.pi * hz / 5000)
        cmd = round(target / zoh)
        fox.tone(hz, cmd)
        time.sleep(args.settle)
        meas, sd = hp.scpiq_float("MEAS:AFR:FM?", n=5, settle_s=0.3)
        rows.append((hz, round(zoh, 4), cmd, round(meas, 1), round(sd, 1)))
        print(f"tone {hz:4d} Hz  zoh {zoh:.3f}  cmd {cmd:5d} Hz -> "
              f"measured {meas:7.1f} Hz ({meas/target - 1:+.1%} vs target)")
    fox.off()
    fox.set_att(127)
    _writerows("t3b_deviation_corrected_hpf20",
               ("tone_hz", "zoh_gain", "dev_cmd_hz", "dev_meas_hz", "sd_hz"), rows)




# ------------------------------------------------- T7: frequency vs warm-up
def rfan_counter_setup(hp, freq_mhz):
    """RF analyzer counting the carrier frequency on RF IN/OUT.

    Every string here is from the 8935 manual or proven code: CONF:RFD
    'Freq' must precede RFAN:FREQ (manual; SINADCTL's FREQ verb sends the
    same), and 'Frequency' is MEAS:RFR:SEL's absolute-frequency choice.
    """
    hp.scpi("CONF:RFD 'Freq'")
    hp.scpi("DISP RFAN")
    hp.scpi("RFAN:INP 'RF In'")
    hp.scpi(f"RFAN:FREQ {freq_mhz} MHZ")
    hp.scpi("MEAS:RFR:SEL 'Frequency'")
    time.sleep(1.0)


def _fox_ports():
    return sorted(glob.glob("/dev/cu.usbmodem*"))


def _wait_for_fox(port_hint, timeout_s=1800):
    """Wait for the PicoFox's USB port to appear, then open it once the
    firmware answers (USB enumerates a moment before the firmware is ready)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ports = [port_hint] if port_hint and Path(port_hint).exists() else _fox_ports()
        for port in ports:
            try:
                fox = PicoFox(port)
                fox.hello()
                return fox
            except Exception:
                try:
                    fox.close()
                except Exception:
                    pass
        time.sleep(0.2)
    raise Hp8935Error("the PicoFox never appeared on USB - is SW3 on and the cable in?")


def t7_freq_drift(args):
    """Carrier frequency from cold power-up, against a warmed-up 8935.

    Run order: start this with the PicoFox switched OFF; it sets up the
    8935, then asks you to switch the PicoFox on, programs it at once
    (unmodulated carrier) and logs the frequency every --interval seconds
    for --minutes. The time axis is seconds since the PicoFox powered up,
    from its own uptime counter. Samples taken while a Morse ID was being
    sent are flagged and left out of the plot. Ctrl-C stops early; RF is
    always turned off at the end.
    """
    if _fox_ports() and not args.already_on:
        raise Hp8935Error(
            "The PicoFox is already powered (its USB port is present), so this\n"
            "would not be a cold start. Switch it off with SW3, leave it off\n"
            "for 30 minutes, then start this again. (Or add --already-on to\n"
            "measure a warm start anyway.)")

    f = args.freq
    target_hz = f * 1e6
    CAL_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    csv_path = CAL_DIR / f"t7_freq_drift_{stamp}.csv"

    with Sinadctl(args.port_8935) as hp:
        print("8935:", hp.ping())
        rfan_counter_setup(hp, f)
        gate = hp.scpiq("RFAN:FREQ:GTIM?")
        print(f"RF counter gate time (RF Cnt Gate, as set on the 8935): {gate}")
        print(f"\n>>> Switch the PicoFox ON now (SW3). Waiting for it on USB ...")
        fox = _wait_for_fox(args.port_fox)
        t_seen = time.time()
        try:
            print("PicoFox:", fox.hello())
            fox.set_call(args.call)
            fox.set_freq(f)
            fox.set_att(args.att)
            fox.carrier()
            st = fox.status()
            boot_host_t = time.time() - float(st["up"])
            print(f"carrier on at {f} MHz, ATT {args.att}, "
                  f"{t_seen - boot_host_t:.1f} s after power-up\n")

            rows, in_id, seen_events = [], False, 0
            end = time.time() + args.minutes * 60
            with open(csv_path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["t_since_powerup_s", "freq_hz", "error_hz", "error_ppm",
                            "during_id", "gate_time_reply"])
                while time.time() < end:
                    t0 = time.time()
                    fox.status()  # also collects *ID events
                    id_seen = False
                    for ev in fox.events[seen_events:]:
                        if ev.startswith("*ID"):
                            id_seen = True
                            in_id = ev.startswith("*ID START")
                    seen_events = len(fox.events)
                    reply = hp.scpiq("MEAS:RFR:FREQ:ABS?")
                    t = time.time() - boot_host_t
                    try:
                        hz = _as_mhz(float(reply)) * 1e6
                    except ValueError:
                        print(f"t={t:7.1f} s  no frequency reading ({reply!r}); skipped",
                              flush=True)
                        time.sleep(max(0.0, args.interval - (time.time() - t0)))
                        continue
                    err = hz - target_hz
                    during_id = int(in_id or id_seen)
                    row = (round(t, 1), round(hz, 1), round(err, 1),
                           round(err / target_hz * 1e6, 4), during_id, gate)
                    rows.append(row)
                    w.writerow(row)
                    fh.flush()
                    print(f"t={t:7.1f} s  {hz/1e6:.6f} MHz  {err:+8.1f} Hz  "
                          f"({err/target_hz*1e6:+.3f} ppm){'  [ID]' if during_id else ''}",
                          flush=True)
                    time.sleep(max(0.0, args.interval - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nstopped early")
        finally:
            try:
                fox.off()
                fox.set_att(127)
            except Exception:
                pass
            fox.close()
    print(f"wrote {csv_path}")
    _plot_freq_drift(csv_path, f)


def _plot_freq_drift(csv_path, freq_mhz):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = list(csv.DictReader(open(csv_path)))
    if not rows:
        return
    t = [float(r["t_since_powerup_s"]) / 60 for r in rows]
    e = [float(r["error_hz"]) for r in rows]
    ok = [r["during_id"] == "0" for r in rows]
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    ax.plot([x for x, k in zip(t, ok) if k], [y for y, k in zip(e, ok) if k],
            "o-", ms=3, lw=1.2, color="#2a78d6", label="carrier")
    ax.plot([x for x, k in zip(t, ok) if not k], [y for y, k in zip(e, ok) if not k],
            "o", ms=4, mfc="none", color="#9a9892", label="during Morse ID (excluded)")
    ax.set_xlabel("time since PicoFox power-up (min)")
    ax.set_ylabel(f"frequency error vs {freq_mhz} MHz (Hz)")
    ax.set_title("PicoFox carrier frequency error vs time since power-up", loc="left")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8.5)
    fig.tight_layout()
    png = csv_path.with_suffix(".png")
    fig.savefig(png, dpi=150)
    print(f"wrote {png}")


# ------------------------------------------- T8: watch a reference warm up
def _to_hz(value, expect_hz):
    """An 8935 frequency reply in Hz, kHz or MHz, as Hz: whichever scaling
    lands nearest the expected frequency (works at any frequency)."""
    return min((value * k for k in (1.0, 1e3, 1e6)), key=lambda hz: abs(hz - expect_hz))


def _clock_time_today(hhmm):
    """Epoch seconds for HH:MM today (or yesterday, if that's in the future)."""
    import datetime as dt
    h, m = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t > now:
        t -= dt.timedelta(days=1)
    return t.timestamp()


def t8_ref_warmup(args):
    """Frequency of an external source (e.g. a 10 MHz GPSDO) vs time after
    switch-on, counted by the 8935's RF analyzer on its internal reference.
    No PicoFox involved."""
    f = args.ref_mhz
    target = f * 1e6
    CAL_DIR.mkdir(exist_ok=True)
    csv_path = CAL_DIR / f"t8_ref_warmup_{time.strftime('%Y-%m-%d_%H%M%S')}.csv"

    with Sinadctl(args.port_8935) as hp:
        print("8935:", hp.ping())
        sel = hp.scpiq("CONF:REF:INP:SEL?").upper()
        print(f"8935 Ref Select: {sel}")
        if "EXT" in sel:
            raise Hp8935Error(
                "The 8935 is locked to an external reference, so it would be\n"
                "measuring the source against itself. Set INSTRUMENT CONFIGURE >\n"
                "Ref Select to Internal, then run this again.")
        if "AUTO" in sel:
            print("  (Auto: make sure nothing is connected to the 8935's external\n"
                  "   reference input, or it will lock to it.)")
        try:
            rfan_counter_setup(hp, f)
        except Hp8935Error as e:
            raise Hp8935Error(f"the RF analyzer wouldn't tune to {f} MHz:\n{e}")
        tuned = _to_hz(float(hp.scpiq("RFAN:FREQ?")), target)
        if abs(tuned - target) > 1e3:
            raise Hp8935Error(f"the RF analyzer reads back {tuned/1e6:.6f} MHz, "
                              f"not {f} MHz - probably outside its range")
        gate = hp.scpiq("RFAN:FREQ:GTIM?")
        print(f"RF analyzer tuned to {tuned/1e6:.6f} MHz; RF Cnt Gate reply: {gate}")
        print(f"(At {f:g} MHz, 1 Hz of error is {1/f:.3g} ppm.)")
        if args.started:
            t_on = _clock_time_today(args.started)
            print(f"\nTiming from {args.started} ({(time.time() - t_on) / 60:.1f} min ago). "
                  "Source must already be on RF IN/OUT.")
        else:
            input("\n>>> Source output to RF IN/OUT. Switch the source ON, then press Enter ... ")
            t_on = time.time()

        end = t_on + args.minutes * 60
        try:
            with open(csv_path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["t_since_on_s", "freq_hz", "error_hz", "error_ppm", "gate_time_reply"])
                while time.time() < end:
                    t0 = time.time()
                    reply = hp.scpiq("MEAS:RFR:FREQ:ABS?")
                    t = time.time() - t_on
                    try:
                        hz = _to_hz(float(reply), target)
                    except ValueError:
                        print(f"t={t:7.1f} s  no reading yet ({reply!r})", flush=True)
                    else:
                        err = hz - target
                        w.writerow((round(t, 1), round(hz, 1), round(err, 1),
                                    round(err / target * 1e6, 4), gate))
                        fh.flush()
                        print(f"t={t:7.1f} s  {hz/1e6:.6f} MHz  {err:+8.1f} Hz  "
                              f"({err/target*1e6:+.3f} ppm)", flush=True)
                    time.sleep(max(0.0, args.interval - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\nstopped early")
    print(f"wrote {csv_path}")
    _plot_ref_warmup(csv_path, f)


def _plot_ref_warmup(csv_path, freq_mhz):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = list(csv.DictReader(open(csv_path)))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    ax.plot([float(r["t_since_on_s"]) / 60 for r in rows], [float(r["error_hz"]) for r in rows],
            "o-", ms=3, lw=1.2, color="#2a78d6")
    ax.axhline(0, color="#9a9892", lw=1.0, ls="--")
    ax.set_xlabel("time since switch-on (min)")
    ax.set_ylabel(f"frequency error vs {freq_mhz:g} MHz (Hz)")
    ax.set_title(f"{freq_mhz:g} MHz source frequency error vs time since switch-on", loc="left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    png = csv_path.with_suffix(".png")
    fig.savefig(png, dpi=150)
    print(f"wrote {png}")

TESTS = {
    "probe": t_probe,
    "t1-cal": t1_cal,
    "t2-flatness": t2_flatness,
    "t3-deviation": t3_deviation,
    "t3b-deviation-corrected": t3b_deviation_corrected,
    "t4-ceiling": t4_ceiling,
    "t5-sinad-vs-power": t5_sinad_vs_power,
    "t6-warmup": t6_warmup,
}


def main():
    saved_port, saved_call = hp8935_remembered()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port-8935", default=saved_port, required=saved_port is None,
                   help="8935 serial adapter (default: the hp8935_sinad tools' "
                        f"remembered port{f', {saved_port}' if saved_port else ''})")
    p.add_argument("--port-fox", help="PicoFox port (default: first usbmodem)")
    p.add_argument("--call", default=saved_call or "KM6AM")
    p.add_argument("--freq", type=float, default=DEFAULT_FREQ_MHZ)
    p.add_argument("--cable-loss-db", type=float, default=0.0,
                   help="added to every SA reading (loss of the test cable)")
    p.add_argument("--settle", type=float, default=1.0,
                   help="seconds after each source change before measuring")
    p.add_argument("--minutes", type=float, default=30.0,
                   help="t7/t8: how long to log (default 30)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="t7/t8: seconds between readings (default 5)")
    p.add_argument("--att", type=int, default=40,
                   help="t7-freq-drift: PicoFox attenuator setting (default 40)")
    p.add_argument("--already-on", action="store_true",
                   help="t7-freq-drift: run even though the PicoFox is already powered")
    p.add_argument("--started", metavar="HH:MM",
                   help="t8-ref-warmup: clock time the warming-up device was switched "
                        "on (e.g. the 8935 itself); times from then, no prompt")
    p.add_argument("--ref-mhz", type=float, default=10.0,
                   help="t8-ref-warmup: source frequency, MHz (default 10)")
    p.add_argument("test", choices=sorted([*TESTS, "t7-freq-drift", "t8-ref-warmup"]))
    args = p.parse_args()

    try:
        if args.test == "t7-freq-drift":
            t7_freq_drift(args)  # starts with the PicoFox off, so not via _run
        elif args.test == "t8-ref-warmup":
            t8_ref_warmup(args)  # no PicoFox at all
        else:
            _run(args)
    except Hp8935Error as e:
        print(f"\n8935 problem:\n{e}", file=sys.stderr)
        sys.exit(1)


def _run(args):
    with Sinadctl(args.port_8935) as hp, PicoFox(args.port_fox) as fox:
        print("8935:", hp.ping())
        print("PicoFox:", fox.hello())
        fox.set_call(args.call)
        try:
            TESTS[args.test](hp, fox, args)
        finally:
            try:
                fox.off()
                fox.set_att(127)
            except Exception:
                pass
        for ev in fox.events:
            print(ev)


if __name__ == "__main__":
    main()
