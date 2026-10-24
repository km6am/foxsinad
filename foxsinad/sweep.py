"""12 dB SINAD sweep: the PicoFox steps its level while the sound-card
analyzer listens to the radio under test.

    python3 -m foxsinad.sweep --pad-db 100 --label TIDH3_bench

Chain:
    PicoFox SMA -> fixed attenuators (--pad-db in total, cables included)
                -> radio antenna jack
    radio earphone out -> USB line-in adapter (LINE) -> this computer

Before running: radio on the test frequency, squelch fully OPEN, volume set
so the tone peaks around -12 dBFS at full level (see `audiocheck level`).

Levels at the radio come from the PicoFox calibration table
(cal/picofox_r3_attcal.json, measured on the 8935) minus --pad-db, so
their accuracy is the cal table's plus however well --pad-db is known.

Output (runs/, in the hp8935_sinad_serial CSV format plus att and
n_discarded columns): one row per reading, then a plot with the
interpolated 12 dB SINAD point.
"""

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .analyzer import CLIP_LEVEL, SinadAnalyzer, pick_device, pick_monitor
from .picofox import PicoFox, PicoFoxError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAL = ROOT / "cal" / "picofox_r3_attcal.json"
RUNS = ROOT / "runs"
TARGET_DB = 12.0


class CalTable:
    """PicoFox output (dBm) vs attenuator setting, interpolated between
    the measured settings."""

    def __init__(self, path=DEFAULT_CAL):
        cal = json.loads(Path(path).read_text())
        pts = sorted((p["att"], p["dbm"]) for p in cal["points"])
        self.att = np.array([a for a, _ in pts], dtype=float)
        self.dbm_pts = np.array([d for _, d in pts])
        self.freq_mhz = cal["freq_mhz"]
        self.date = cal.get("date", "?")
        self.path = Path(path)

    def dbm(self, att):
        return float(np.interp(att, self.att, self.dbm_pts))


def dbm_to_uv(dbm):
    """Voltage across 50 ohms, microvolts (as in the hp8935 CSVs)."""
    return math.sqrt(10 ** (dbm / 10) / 1000 * 50) * 1e6


def twelve_db_point(levels, sinads, target=TARGET_DB):
    """Level (dBm) where SINAD crosses the target.

    A straight-line fit through the points within 4 dB of the target, if
    there are at least 3; otherwise linear interpolation between the two
    points either side of the first crossing from the strong end. None if
    the data never crosses."""
    pts = sorted(zip(levels, sinads), reverse=True)      # strongest first
    near = [(l, s) for l, s in pts if abs(s - target) <= 4]
    if len(near) >= 3:
        slope, icept = np.polyfit([l for l, _ in near], [s for _, s in near], 1)
        if slope > 0:
            return float((target - icept) / slope)
    for (l1, s1), (l2, s2) in zip(pts, pts[1:]):
        if s1 >= target > s2:
            return l1 + (target - s1) * (l2 - l1) / (s2 - s1)
    return None


def _id_in_progress(events):
    ids = [e for e in events if e.startswith("*ID")]
    return bool(ids) and ids[-1].startswith("*ID START")


def read_clean(fox, an, settle_s, avg_s):
    """One analyzer reading that didn't overlap a Morse ID. The PicoFox
    reports IDs as *ID START / *ID DONE events; STATUS collects them."""
    while True:
        fox.status()
        if _id_in_progress(fox.events):
            print("  (waiting for a Morse ID to finish)", flush=True)
            while _id_in_progress(fox.events):
                time.sleep(0.5)
                fox.status()
        before = len(fox.events)
        r = an.read(settle_s=settle_s, avg_s=avg_s)
        fox.status()
        if not any(e.startswith("*ID") for e in fox.events[before:]):
            return r
        print("  (Morse ID during this reading; measuring again)", flush=True)


def run(args):
    cal = CalTable(args.cal)
    if abs(cal.freq_mhz - args.freq) > 1.0:
        print(f"warning: the cal table was measured at {cal.freq_mhz} MHz, "
              f"you're sweeping at {args.freq} MHz")
    dev = pick_device(args.device)
    mon = pick_monitor(args.monitor, dev) if getattr(args, "monitor", None) else None
    RUNS.mkdir(exist_ok=True)
    label = args.label or f"foxsinad_{args.condition}_{args.freq:g}MHz"
    stamp = datetime.now()
    path = RUNS / f"run_{label}_{stamp.strftime('%Y-%m-%d_%H%M%S')}.csv"

    rows = []
    with PicoFox(args.port) as fox, SinadAnalyzer(dev, monitor=mon) as an:
        print("PicoFox:", fox.hello())
        fox.set_call(args.call)
        fox.set_freq(args.freq)
        fox.set_att(0)
        fox.tone(args.tone, args.dev)
        try:
            r = read_clean(fox, an, 1.5, 1.0)
            top = cal.dbm(0) - args.pad_db
            if r.peak >= CLIP_LEVEL:
                sys.exit("error: the audio clips at full level. Turn the radio's volume down "
                         "(audiocheck level) and run again.")
            if r.under_range:
                sys.exit("error: no audio. Check the cable, the LINE switch, the radio's "
                         "volume, and that its squelch is open.")
            if r.sinad_db < TARGET_DB:
                sys.exit(f"error: SINAD is only {r.sinad_db:.1f} dB at full level "
                         f"({top:.1f} dBm at the radio). Use less fixed attenuation.")

            with open(path, "w", newline="") as fh:
                for k, v in [("label", label), ("date", stamp.isoformat(timespec="seconds")),
                             ("mode", "foxsinad sweep"), ("source", "PicoFox r3 via USB"),
                             ("instrument", "sound-card SINAD (foxsinad.analyzer)"),
                             ("freq_mhz", args.freq), ("offset_db", -args.pad_db),
                             ("dev_khz", args.dev / 1000), ("tone_hz", args.tone),
                             ("settle_s", args.settle), ("avg_coarse_s", args.avg_coarse),
                             ("avg_fine_s", args.avg_fine), ("condition", args.condition),
                             ("note", args.note), ("station_call", args.call),
                             ("af_input", f"audio device {dev}"),
                             ("af_filter", "200-3000 Hz bandpass"),
                             ("cal_table", f"{cal.path.name} ({cal.date}, {cal.freq_mhz} MHz)")]:
                    fh.write(f"# {k}: {v}\n")
                w = csv.writer(fh)
                w.writerow(["phase", "gen_dbm", "level_dbm", "level_uv", "sinad_db",
                            "sinad_sd", "n", "time", "att", "n_discarded"])

                def measure(phase, att, avg):
                    fox.set_att(att)
                    r = read_clean(fox, an, args.settle, avg)
                    gen = cal.dbm(att)
                    lvl = gen - args.pad_db
                    row = dict(phase=phase, att=att, level_dbm=lvl, sinad_db=r.sinad_db)
                    rows.append(row)
                    w.writerow([phase, round(gen, 2), round(lvl, 2), round(dbm_to_uv(lvl), 4),
                                round(r.sinad_db, 2), round(r.std_db, 3), r.n,
                                datetime.now().isoformat(timespec="milliseconds"),
                                att, r.n_discarded])
                    fh.flush()
                    flags = (" CLIP" if r.peak >= CLIP_LEVEL else "") + \
                            (f" ({r.n_discarded} windows discarded)" if r.n_discarded else "")
                    print(f"{phase:6s} ATT {att:3d}  {lvl:8.2f} dBm  SINAD {r.sinad_db:5.1f} dB "
                          f"(sd {r.std_db:.2f}){flags}", flush=True)
                    return r.sinad_db

                # coarse: 2 dB steps until clearly below the target
                weak = 0
                for att in list(range(0, 128, 8)) + [127]:
                    s = measure("coarse", att, args.avg_coarse)
                    weak = weak + 1 if s < TARGET_DB - 3 else 0
                    if weak >= 2:
                        break
                coarse = [r for r in rows if r["phase"] == "coarse"]
                above = [r["att"] for r in coarse if r["sinad_db"] >= TARGET_DB]
                below = [r["att"] for r in coarse if r["sinad_db"] < TARGET_DB]
                if not below:
                    sys.exit(f"SINAD is still above {TARGET_DB:g} dB at ATT 127 "
                             f"({cal.dbm(127) - args.pad_db:.1f} dBm at the radio). "
                             "Add more fixed attenuation and run again.")
                b = min(below)
                stronger = [x for x in above if x < b]
                a = max(stronger) if stronger else 0

                # fine: 0.25 dB steps across the bracket, with a little margin
                for att in range(max(0, a - 4), min(127, b + 4) + 1):
                    measure("fine", att, args.avg_fine)
        finally:
            fox.off()
            fox.set_att(127)

    fine = [r for r in rows if r["phase"] == "fine"]
    point = twelve_db_point([r["level_dbm"] for r in fine], [r["sinad_db"] for r in fine])
    print(f"\nwrote {path}")
    if point is None:
        print("No clean 12 dB crossing in the fine sweep; see the CSV.")
    else:
        print(f"12 dB SINAD: {point:.1f} dBm at the radio ({dbm_to_uv(point):.3f} uV, 50 ohm)")
    plot(path, rows, point)
    return point


def plot(path, rows, point):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    for phase, style in (("coarse", dict(mfc="none")), ("fine", dict())):
        pts = sorted((r["level_dbm"], r["sinad_db"]) for r in rows if r["phase"] == phase)
        if pts:
            ax.plot(*zip(*pts), "o", ms=4, color="#2a78d6", label=phase, **style)
    ax.axhline(TARGET_DB, color="#9a9892", ls="--", lw=1)
    if point is not None:
        ax.axvline(point, color="#eb6834", lw=1)
        ax.annotate(f"12 dB SINAD: {point:.1f} dBm", xy=(point, TARGET_DB), xytext=(6, 8),
                    textcoords="offset points", color="#eb6834", fontsize=9)
    ax.set_xlabel("level at the radio (dBm)")
    ax.set_ylabel("SINAD (dB)")
    ax.set_title("SINAD vs level at the radio", loc="left")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8.5)
    fig.tight_layout()
    png = path.with_suffix(".png")
    fig.savefig(png, dpi=150)
    print(f"wrote {png}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pad-db", type=float, required=True,
                   help="total fixed attenuation between the PicoFox and the radio, dB")
    p.add_argument("--label", help="name for the run (used in the file name)")
    p.add_argument("--condition", default="bench", help="e.g. bench, antenna, summit")
    p.add_argument("--note", default="")
    p.add_argument("--call", default="KM6AM")
    p.add_argument("--freq", type=float, default=145.55)
    p.add_argument("--tone", type=int, default=1000)
    p.add_argument("--dev", type=int, default=3000, help="commanded peak deviation, Hz")
    p.add_argument("--settle", type=float, default=1.0, help="seconds after each level change")
    p.add_argument("--avg-coarse", type=float, default=1.0, help="seconds per coarse reading")
    p.add_argument("--avg-fine", type=float, default=3.0, help="seconds per fine reading")
    p.add_argument("--device", help="audio input (default: auto-detect the USB adapter)")
    p.add_argument("--port", help="PicoFox serial port (default: auto)")
    p.add_argument("--cal", default=str(DEFAULT_CAL))
    p.add_argument("--monitor", nargs="?", const="default", metavar="OUTPUT",
                   help="also play the radio's audio on the computer's output")
    args = p.parse_args()
    try:
        run(args)
    except (LookupError, PicoFoxError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
