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


def sa_peak_dbm(hp, reads=3):
    hp.scpi("SAN:MARK:PPE")
    level, _ = hp.scpiq_float("MEAS:SAN:MARK:LEV?", n=reads, settle_s=0.3)
    return level


def afan_setup(hp, filt2, sel):
    hp.scpi("DISP AFAN")
    hp.scpi("AFAN:INP 'FM Demod'")
    hp.scpi("AFAN:FILT1 '300Hz HPF'")
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
    path = CAL_DIR / f"{name}_{date.today().isoformat()}.csv"
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
    txpow = hp.scpiq("MEAS:RFR:POW?")
    print(f"ATT 0 TX-power-meter cross-check: {txpow} (compare SA row below)")

    rows = []
    atts = list(range(0, 128, 4)) + [127]
    for att in atts:
        fox.set_att(att)
        time.sleep(args.settle)
        dbm = sa_peak_dbm(hp) + args.cable_loss_db
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
        for att in (0, 64):
            fox.set_att(att)
            fox.carrier()
            time.sleep(args.settle)
            dbm = sa_peak_dbm(hp) + args.cable_loss_db
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
    afan_setup(hp, filt2="15kHz LPF", sel="FM Deviation")

    rows = []
    print("Part A: deviation linearity at 1 kHz tone")
    for dev in range(1000, 10001, 1000):
        fox.tone(1000, dev)
        time.sleep(args.settle)
        khz, sd = hp.scpiq_float("MEAS:AFR:FM?", n=5, settle_s=0.3)
        rows.append(("linearity", 1000, dev, round(khz * 1000, 1), round(sd * 1000, 1)))
        print(f"dev {dev:5d} Hz -> measured {khz*1000:7.1f} Hz (sd {sd*1000:.1f})")

    print("Part B: audio flatness at 3 kHz deviation")
    for hz in (300, 500, 800, 1000, 1500, 2000, 2400):
        fox.tone(hz, 3000)
        time.sleep(args.settle)
        khz, sd = hp.scpiq_float("MEAS:AFR:FM?", n=5, settle_s=0.3)
        afr = hp.scpiq("MEAS:AFR:AFR?")
        rows.append(("flatness", hz, 3000, round(khz * 1000, 1), round(sd * 1000, 1)))
        print(f"tone {hz:4d} Hz -> dev {khz*1000:7.1f} Hz (sd {sd*1000:.1f}) afr {afr}")
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
        dbm = sa_peak_dbm(hp) + args.cable_loss_db
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
        dbm = sa_peak_dbm(hp) + args.cable_loss_db
        t = round(time.time() - t0, 1)
        rows.append((t, round(dbm, 2)))
        print(f"t={t:6.0f}s  {dbm:7.2f} dBm", flush=True)
        time.sleep(30)
    fox.off()
    fox.set_att(127)
    _writerows("t6_warmup", ("t_s", "dbm"), rows)


TESTS = {
    "probe": t_probe,
    "t1-cal": t1_cal,
    "t2-flatness": t2_flatness,
    "t3-deviation": t3_deviation,
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
    p.add_argument("test", choices=sorted(TESTS))
    args = p.parse_args()

    try:
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
