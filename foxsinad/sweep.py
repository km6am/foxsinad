"""12 dB SINAD sweep: the PicoFox steps its level while the sound-card
analyzer listens to the radio under test.

    python3 -m foxsinad.sweep --pad-db 108.5 --radio TIDH3 --condition bench
    python3 -m foxsinad.sweep --pad-db 108.5 --session summit1 --radio TIDH3 --condition antenna

Chain:
    PicoFox SMA -> fixed attenuators (--pad-db in total, cables included)
                -> radio antenna jack
    radio earphone out -> USB line-in adapter (LINE) -> this computer

Before running: radio on the test frequency, squelch fully OPEN, volume set
so the tone peaks around -12 dBFS at full level (see `audiocheck level`).

Levels come from the PicoFox calibration table (cal/picofox_r3_attcal.json,
measured on the 8935) minus --pad-db. Only with --cabled is that the level
at the radio (accurate to the cal table plus however well --pad-db is
known). Over the air the path loss isn't known, so levels are the PicoFox's
output (into the transmit antenna, after --pad-db): comparable between runs
with the same setup, not absolute.

Output follows hp8935_sinad_serial's run.py: the file name is
<label>_<radio>_<condition>_<freq>MHz (or <session>/<radio>_<condition>_
<freq>MHz), _2, _3 ... rather than overwriting, and the header - written
when the run finishes - carries radio, condition, note and the 12 dB result
(sinad12_dbm, sinad12_uv, sinad12_sigma_db, sinad12_fit_points). Columns are
run.py's plus att and n_discarded. Readings stream to <name>.partial.csv
during the run, so an interrupted run keeps its data.
"""

import argparse
import contextlib
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .analyzer import AUDIO_TOL_DB, CLIP_LEVEL, SinadAnalyzer, peak_dbfs, pick_device, pick_monitor
from .picofox import PicoFox, PicoFoxError, set_cabled

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAL = ROOT / "cal" / "picofox_r3_attcal.json"
RUNS = ROOT / "runs"
TARGET_DB = 12.0

# Seconds per reading: settle after each level change, then average.
# Provisional: not yet measured on hardware (settling time and how fast
# SINAD fluctuates at the 12 dB point decide them).
SETTLE_S = 0.5
AVG_COARSE_S = 0.5
AVG_FINE_S = 1.5
FINE_MARGIN = 4            # ATT steps (1 dB) past the coarse bracket
FIT_BAND_DB = 4.0          # the 12 dB fit uses readings within this of the target
FINE_BEYOND = 2            # a fine-pass side ends after this many readings outside it


class SweepError(RuntimeError):
    """The sweep can't go on; the message says what to change."""


class Stopped(Exception):
    """A stop was requested (the RF is turned off on the way out)."""


@dataclass
class Result:
    label: str
    csv: Path
    point_dbm: float = None     # 12 dB SINAD level (see level_ref), or None
    sigma_db: float = None      # the fit's statistical uncertainty
    fit_points: int = None
    level_ref: str = "radio"


def level_reference(cabled, pad_db):
    """What a level means: (key, phrase, axis label). The level at the
    radio is known only on a cabled path whose external attenuation was
    entered (pad_db None means it wasn't; 0 means none); otherwise a level is
    the PicoFox's output, after any external attenuator."""
    if cabled and pad_db is not None:
        return "radio", "at the radio", "level at the radio (dBm)"
    if pad_db:
        return "antenna", "into the transmit antenna", "level into the transmit antenna (dBm)"
    return "output", "at the PicoFox output", "PicoFox output (dBm)"


# What each level_reference() key means, as written in a sweep's CSV header.
LEVEL_REF_HEADER = {
    "radio": "level at the radio input (cabled: output - external attenuation)",
    "antenna": "PicoFox output into the transmit antenna, after the external "
               "attenuation; over the air, not the level at the radio",
    "output": "PicoFox output, not the level at the radio (over the air, or "
              "cabled with no external attenuation entered)",
}


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


def slug(text):
    """Filesystem-safe fragment of a free-text name (as run.py). '3 pole' -> '3_pole'."""
    import re
    return re.sub(r"[^A-Za-z0-9.-]+", "_", (text or "").strip()).strip("_")


def freq_tag(mhz):
    """'145.55MHz', trailing zeros trimmed (as run.py)."""
    return f"{float(mhz):g}MHz"


def compose_label(*parts):
    """Join the non-empty parts into one file-name stem (as run.py)."""
    return "_".join(p for p in (slug(x) for x in parts) if p) or "run"


def unique_label(stem):
    """Never overwrite a saved run: append _2, _3, ... (as run.py)."""
    if not Path(f"{stem}.csv").exists():
        return stem
    n = 2
    while Path(f"{stem}_{n}.csv").exists():
        n += 1
    print(f"  {Path(stem).name}.csv already exists, saving this run as {Path(stem).name}_{n}")
    return f"{stem}_{n}"


def dbm_to_uv(dbm):
    """Voltage across 50 ohms, microvolts (as in the hp8935 CSVs)."""
    return math.sqrt(10 ** (dbm / 10) / 1000 * 50) * 1e6


def twelve_db_fit(levels, sinads, target=TARGET_DB):
    """(level, sigma, n_points) where SINAD crosses the target, or None.

    A straight-line fit through the points within 4 dB of the target, if
    there are at least 3; sigma is the fit's statistical uncertainty in the
    crossing level. Otherwise linear interpolation between the two points
    either side of the first crossing from the strong end (sigma None)."""
    pts = sorted(zip(levels, sinads), reverse=True)      # strongest first
    near = [(l, s) for l, s in pts if abs(s - target) <= FIT_BAND_DB]
    if len(near) >= 3:
        x = np.array([l for l, _ in near])
        y = np.array([s for _, s in near])
        if len(near) > 3:
            (slope, icept), cov = np.polyfit(x, y, 1, cov=True)
        else:                       # 3 points: no covariance estimate
            (slope, icept), cov = np.polyfit(x, y, 1), None
        if slope > 0:
            level = (target - icept) / slope
            sigma = None
            if cov is not None:
                # d(level) = -(d icept + level d slope) / slope
                g = np.array([-level / slope, -1 / slope])
                sigma = float(np.sqrt(g @ cov @ g))
            return float(level), sigma, len(near)
    for (l1, s1), (l2, s2) in zip(pts, pts[1:]):
        if s1 >= target > s2:
            return l1 + (target - s1) * (l2 - l1) / (s2 - s1), None, 2
    return None


def twelve_db_point(levels, sinads, target=TARGET_DB):
    """Level (dBm) where SINAD crosses the target, or None."""
    fit = twelve_db_fit(levels, sinads, target)
    return None if fit is None else fit[0]


def fine_pass(a, sa, b, sb, measure):
    """The fine pass: 0.25 dB steps outward from where the coarse pass
    crossed 12 dB, between ATT a (SINAD sa, above) and b (sb, below). Each
    side - stronger first - ends after FINE_BEYOND readings in a row outside
    the fit band, which the fit would ignore, or a little past the bracket.
    measure(att) returns the SINAD."""
    lo, hi = max(0, a - FINE_MARGIN), min(127, b + FINE_MARGIN)
    start = round(a + (sa - TARGET_DB) / (sa - sb) * (b - a)) if sa != sb else a
    start = min(max(start, lo), hi)
    for att_range in (range(start, lo - 1, -1), range(start + 1, hi + 1)):
        outside = 0
        for att in att_range:
            outside = outside + 1 if abs(measure(att) - TARGET_DB) > FIT_BAND_DB else 0
            if outside >= FINE_BEYOND:
                break


def _id_in_progress(events):
    ids = [e for e in events if e.startswith("*ID")]
    return bool(ids) and ids[-1].startswith("*ID START")


def _say(msg):
    print(msg, flush=True)


def read_clean(fox, an, settle_s, avg_s, log=_say):
    """One analyzer reading that didn't overlap a Morse ID. The PicoFox
    reports IDs as *ID START / *ID DONE events; STATUS collects them."""
    while True:
        fox.status()
        if _id_in_progress(fox.events):
            log("  (waiting for a Morse ID to finish)")
            while _id_in_progress(fox.events):
                time.sleep(0.5)
                fox.status()
        before = len(fox.events)
        r = an.read(settle_s=settle_s, avg_s=avg_s)
        if math.isnan(r.sinad_db):
            raise SweepError("the audio clipped throughout the reading: turn the radio's volume down")
        st = fox.status()
        # Also check the ID is over: another reader (the UI's status poll) may
        # have taken the "ID started" event between the checks above.
        if (not any(e.startswith("*ID") for e in fox.events[before:])
                and not _id_in_progress(fox.events) and st.get("id_active", "0") != "1"):
            return r
        log("  (Morse ID during this reading; measuring again)")


def finish_csv(partial, final, meta):
    """The finished file: the # key: value header (known only at the end, as
    run.py writes it), then the readings streamed to partial."""
    with open(final, "w", newline="") as fh:
        for k, v in meta.items():
            fh.write(f"# {k}: {v}\n")
        fh.write(partial.read_text())
    partial.unlink()


def read_header(path):
    """A finished run's # key: value header, as strings."""
    meta = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("# "):
                break
            k, _, v = line[2:].rstrip("\r\n").partition(": ")
            meta[k] = v
    return meta


def run_stem(args, runs=None):
    """File stem for the next run, named as hp8935_sinad_serial's run.py: a
    session is a folder holding <radio>_<condition>_<freq>; otherwise
    <label>_<radio>_<condition>_<freq>. Never an existing name."""
    runs = Path(runs or RUNS)
    runs.mkdir(parents=True, exist_ok=True)
    if args.session:
        folder = runs / slug(args.session)
        folder.mkdir(exist_ok=True)
        return unique_label(str(folder / compose_label(args.radio, args.condition,
                                                       freq_tag(args.freq))))
    return unique_label(str(runs / compose_label(args.label, args.radio, args.condition,
                                                 freq_tag(args.freq))))


def run(args, fox=None, an=None, on_start=None, on_point=None, log=_say, stop=None):
    """One sweep. Opens the PicoFox and the analyzer unless they're passed
    in. on_start(label) and on_point(row) report progress; stop (an Event)
    ends the run early with Stopped. Raises SweepError for problems the user
    has to fix. RF is always off at the end."""
    cal = CalTable(args.cal)
    if abs(cal.freq_mhz - args.freq) > 1.0:
        log(f"warning: the cal table was measured at {cal.freq_mhz} MHz, "
            f"you're sweeping at {args.freq} MHz")
    stem = run_stem(args, getattr(args, "runs_dir", None))
    label = Path(stem).name
    if on_start:
        on_start(label)
    partial = Path(f"{stem}.partial.csv")
    final = Path(f"{stem}.csv")
    columns = ["phase", "gen_dbm", "level_dbm", "level_uv", "sinad_db", "sinad_sd", "n",
               "time", "att", "n_discarded"]
    started = datetime.now()

    rows = []
    cabled = getattr(args, "cabled", False)
    ref, ref_text, xlabel = level_reference(cabled, args.pad_db)
    pad = args.pad_db or 0.0      # None: not entered, so levels are the PicoFox output
    dev = None
    live = LivePlot(f"{label}  (pad {pad:g} dB)", xlabel) \
        if getattr(args, "live", False) else None
    with contextlib.ExitStack() as opened:
        if fox is None:
            fox = opened.enter_context(PicoFox(args.port))
        if an is None:
            dev = pick_device(args.device)
            want = getattr(args, "monitor", False) or getattr(args, "monitor_device", None)
            mon = pick_monitor(getattr(args, "monitor_device", None) or "default", dev) \
                if want else None
            an = opened.enter_context(SinadAnalyzer(dev, monitor=mon))
        log(f"PicoFox: {fox.hello()}")
        try:
            set_cabled(fox, cabled)
            if args.call:
                fox.set_call(args.call)
            fox.set_freq(args.freq)
            fox.set_att(0)
            fox.tone(args.tone, args.dev)
            r = read_clean(fox, an, 1.5, 1.0, log)
            top = cal.dbm(0) - pad
            if r.peak >= CLIP_LEVEL:
                raise SweepError("the audio clips at full level. Turn the radio's volume down "
                                 "(audiocheck level) and run again.")
            if r.under_range:
                raise SweepError("no audio. Check the cable, the LINE switch, the radio's "
                                 "volume, and that its squelch is open.")
            target = getattr(args, "audio_target_dbfs", None)
            if target is not None and abs(peak_dbfs(r.peak) - target) > AUDIO_TOL_DB:
                log(f"warning: the tone peaks at {peak_dbfs(r.peak):.1f} dBFS at full level, "
                    f"{target:g} dBFS is the target (set the radio's volume in Audio)")
            if r.sinad_db < TARGET_DB:
                raise SweepError(f"SINAD is only {r.sinad_db:.1f} dB at full level "
                                 f"({top:.1f} dBm {ref_text}). Use less fixed attenuation.")

            with open(partial, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(columns)

                def measure(phase, att, avg):
                    if stop is not None and stop.is_set():
                        raise Stopped()
                    fox.set_att(att)
                    r = read_clean(fox, an, args.settle, avg, log)
                    gen = cal.dbm(att)
                    lvl = gen - pad
                    row = [phase, round(gen, 2), round(lvl, 2), round(dbm_to_uv(lvl), 4),
                           round(r.sinad_db, 2), round(r.std_db, 3), r.n,
                           datetime.now().isoformat(timespec="milliseconds"),
                           att, r.n_discarded]
                    rows.append(dict(zip(columns, row)))
                    if live:
                        live.add(phase, lvl, r.sinad_db)
                    if on_point:
                        on_point(dict(rows[-1], clipped=r.peak >= CLIP_LEVEL))
                    w.writerow(row)
                    fh.flush()
                    flags = (" CLIP" if r.peak >= CLIP_LEVEL else "") + \
                            (f" ({r.n_discarded} windows discarded)" if r.n_discarded else "")
                    log(f"{phase:6s} {att * 0.25:5.2f} dB (ATT {att:3d})  {lvl:8.2f} dBm  "
                        f"SINAD {r.sinad_db:5.1f} dB "
                        f"(sd {r.std_db:.2f}){flags}")
                    return r.sinad_db

                # coarse: 2 dB steps on down into the tail, until SINAD is at
                # the noise (below --tail-db twice running) or ATT runs out
                weak = 0
                for att in list(range(0, 128, 8)) + [127]:
                    s = measure("coarse", att, args.avg_coarse)
                    weak = weak + 1 if s < args.tail_db else 0
                    if weak >= 2:
                        break
                coarse = [r for r in rows if r["phase"] == "coarse"]
                above = [r["att"] for r in coarse if r["sinad_db"] >= TARGET_DB]
                below = [r["att"] for r in coarse if r["sinad_db"] < TARGET_DB]
                if not below:
                    raise SweepError(f"SINAD is still above {TARGET_DB:g} dB at ATT 127 "
                             f"({cal.dbm(127) - pad:.1f} dBm {ref_text}). "
                             "Add more fixed attenuation and run again. "
                             f"(Readings so far: {partial})")
                b = min(below)
                stronger = [x for x in above if x < b]
                a = max(stronger) if stronger else 0
                by_att = {r["att"]: r["sinad_db"] for r in coarse}
                fine_pass(a, by_att.get(a, TARGET_DB), b, by_att[b],
                          lambda att: measure("fine", att, args.avg_fine))
        finally:
            fox.off()
            fox.set_att(127)
            set_cabled(fox, False)
            if live and not rows:
                live.plt.close(live.fig)

    fine = [r for r in rows if r["phase"] == "fine"]
    fit = twelve_db_fit([r["level_dbm"] for r in fine], [r["sinad_db"] for r in fine])
    point, sigma, npts = fit if fit else (None, None, None)

    # The header goes in once the result is known, as run.py does.
    meta = dict(
        label=label, date=started.isoformat(timespec="seconds"), mode="foxsinad sweep",
        source="PicoFox r3 via USB", instrument="sound-card SINAD (foxsinad.analyzer)",
        freq_mhz=args.freq, offset_db=-pad, dev_khz=args.dev / 1000,
        tone_hz=args.tone, avg_s=args.avg_fine, avg_coarse_s=args.avg_coarse,
        settle_s=args.settle, radio=args.radio or "(none)",
        audio_peak_dbfs=getattr(args, "audio_peak_dbfs", None),
        audio_target_dbfs=getattr(args, "audio_target_dbfs", None),
        condition=args.condition or "(none)", note=args.note,
        station_call=(args.call or "").upper() or "(not set)",
        cabled="yes (no Morse ID)" if cabled else "no",
        level_ref=LEVEL_REF_HEADER[ref],
        session=args.session or "(none)",
        af_input=getattr(args, "af_input", None) or f"audio device {dev}", af_filter1="200Hz HPF (software)",
        af_filter2="3kHz LPF (software)", af_deemphasis="(radio's own)",
        af_detector="SINAD from FFT residual (sinad_meter)",
        cal_table=f"{cal.path.name} ({cal.date}, {cal.freq_mhz} MHz)",
        sinad12_dbm=None if point is None else round(point, 2),
        sinad12_uv=None if point is None else round(dbm_to_uv(point), 4),
        sinad12_sigma_db=None if sigma is None else round(sigma, 3),
        sinad12_fit_points=npts)
    finish_csv(partial, final, meta)

    log(f"\nSaved {final}")
    if point is None:
        log("No clean 12 dB crossing in the fine sweep; see the CSV.")
    else:
        pm = "" if sigma is None else f" +/- {sigma:.2f}"
        log(f"  => {TARGET_DB:g} dB SINAD at {point:.2f} dBm{pm}   "
            f"({dbm_to_uv(point):.3f} uV) {ref_text}")
        if sigma is not None:
            log(f"     +/- is the fit's statistical uncertainty ({npts} points); it "
                "excludes the cal table and --pad-db.")
    plot(final, rows, point, log, xlabel)
    if live:
        live.finish(point, hold_s=None if getattr(args, "hold", False) else 3.0)
    return Result(label, final, point, sigma, npts, ref)


class LivePlot:
    """A window that draws each reading as it's taken (like run.py's live
    plot). Closes itself at the end of the run unless hold_s is None."""

    def __init__(self, title, xlabel="level at the radio (dBm)"):
        import matplotlib.pyplot as plt
        self.plt = plt
        plt.ion()
        self.fig, self.ax = plt.subplots(figsize=(7.6, 4.4))
        self.fig.canvas.manager.set_window_title(title)
        self.ax.axhline(TARGET_DB, color="#9a9892", ls="--", lw=1)
        self.lines = {
            "coarse": self.ax.plot([], [], "o", ms=4, mfc="none", color="#2a78d6",
                                   label="coarse")[0],
            "fine": self.ax.plot([], [], "o", ms=4, color="#2a78d6", label="fine")[0],
        }
        self.data = {"coarse": [], "fine": []}
        self.ax.set_xlabel(xlabel)
        self.ax.set_ylabel("SINAD (dB)")
        self.ax.set_title(title, loc="left", fontsize=10)
        self.ax.grid(alpha=0.3)
        self.ax.legend(frameon=False, fontsize=8.5, loc="upper left")
        self.fig.tight_layout()
        self.plt.pause(0.05)

    def add(self, phase, level, sinad):
        self.data[phase].append((level, sinad))
        self.lines[phase].set_data(*zip(*self.data[phase]))
        self.ax.relim()
        self.ax.autoscale_view()
        self.plt.pause(0.01)

    def finish(self, point, hold_s=3.0):
        if point is not None:
            self.ax.axvline(point, color="#eb6834", lw=1)
            self.ax.annotate(f"12 dB SINAD: {point:.1f} dBm", xy=(point, TARGET_DB),
                             xytext=(6, 8), textcoords="offset points",
                             color="#eb6834", fontsize=9)
        if hold_s is None:
            self.plt.ioff()
            self.plt.show()          # stays until you close the window
        else:
            self.plt.pause(hold_s)
            self.plt.close(self.fig)


def plot(path, rows, point, log=_say, xlabel="level at the radio (dBm)"):
    """Writes <path>.png, or nothing where matplotlib isn't installed (the
    phone): the CSV is the result, the picture is a convenience."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError:
        log("no PNG plot: matplotlib isn't installed here (the CSV has every reading)")
        return

    fig = Figure(figsize=(7.6, 4.4))
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    for phase, style in (("coarse", dict(mfc="none")), ("fine", dict())):
        pts = sorted((r["level_dbm"], r["sinad_db"]) for r in rows if r["phase"] == phase)
        if pts:
            ax.plot(*zip(*pts), "o", ms=4, color="#2a78d6", label=phase, **style)
    ax.axhline(TARGET_DB, color="#9a9892", ls="--", lw=1)
    if point is not None:
        ax.axvline(point, color="#eb6834", lw=1)
        ax.annotate(f"12 dB SINAD: {point:.1f} dBm", xy=(point, TARGET_DB), xytext=(6, 8),
                    textcoords="offset points", color="#eb6834", fontsize=9)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("SINAD (dB)")
    ax.set_title(f"SINAD vs {xlabel.replace(' (dBm)', '')}", loc="left")
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, fontsize=8.5)
    fig.tight_layout()
    png = path.with_suffix(".png")
    fig.savefig(png, dpi=150)
    log(f"wrote {png}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pad-db", type=float, required=True,
                   help="total fixed attenuation between the PicoFox and the radio, dB")
    p.add_argument("--label", default="run", help="run prefix (default 'run', as run.py)")
    p.add_argument("--session", help="session name: runs go in runs/<session>/, "
                   "named <radio>_<condition>_<freq>")
    p.add_argument("--radio", default="", help="radio name; goes in the file name")
    p.add_argument("--condition", default="", help="e.g. bench, antenna, summit; "
                   "goes in the file name")
    p.add_argument("--note", default="", help="free text, metadata only")
    p.add_argument("--call", default="KM6AM")
    p.add_argument("--cabled", action="store_true",
                   help="output into a load or test equipment only, never an antenna: "
                        "no Morse ID")
    p.add_argument("--freq", type=float, default=145.55)
    p.add_argument("--tone", type=int, default=1000)
    p.add_argument("--dev", type=int, default=3000, help="commanded peak deviation, Hz")
    p.add_argument("--settle", type=float, default=SETTLE_S, help="seconds after each level change")
    p.add_argument("--tail-db", type=float, default=3.0,
                   help="coarse pass continues until SINAD is below this twice (default 3)")
    p.add_argument("--avg-coarse", type=float, default=AVG_COARSE_S, help="seconds per coarse reading")
    p.add_argument("--avg-fine", type=float, default=AVG_FINE_S, help="seconds per fine reading")
    p.add_argument("--device", help="audio input (default: auto-detect the USB adapter)")
    p.add_argument("--port", help="PicoFox serial port (default: auto)")
    p.add_argument("--cal", default=str(DEFAULT_CAL))
    p.add_argument("--monitor", action="store_true",
                   help="also play the audio on the computer's speakers")
    p.add_argument("--monitor-device", metavar="OUTPUT",
                   help="play it on this output instead (name or number)")
    p.add_argument("--live", action="store_true",
                   help="live plot window, closed 3 s after each run (see --hold)")
    p.add_argument("--hold", action="store_true",
                   help="with --live: keep the plot open until you close it")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="run the sweep N times in a row (stops at the first error)")
    args = p.parse_args()
    try:
        for i in range(1, args.repeat + 1):
            if args.repeat > 1:
                print(f"\n=== run {i} of {args.repeat}, {datetime.now().strftime('%H:%M:%S')}")
            run(args)
    except (LookupError, PicoFoxError, SweepError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\nstopped (RF was turned off)")
    if args.repeat > 1 and args.session:
        print()
        from .repeatability import main as summarize_main
        sys.argv = ["repeatability", str(RUNS / slug(args.session))]
        summarize_main()


if __name__ == "__main__":
    main()
