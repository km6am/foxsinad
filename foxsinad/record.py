"""SINAD over time at one level: the PicoFox holds a level while the
analyzer records every window.

What it's for: on a summit, desense that comes and goes (a site
transmitter keying up) shows as SINAD dropping at a level that otherwise
holds about 12 dB. On the bench, how fast SINAD fluctuates at the 12 dB
point, and how long it takes to settle after a level change, decide the
sweep's reading times.

Each 0.25 s analyzer window is one row, not averaged. The level can be
changed during the run (requests); it changes between windows, so each
row's att was set for the whole of its window. Windows during a Morse ID
are flagged (id_active) and left out of the summary, not dropped. The
PicoFox reports IDs with its STATUS replies, which whoever drives it polls
about once a second, so an ID's first window or so may go unflagged.

Rows stream to <name>.partial.csv; the header, with the summary, goes in
when the run ends, as for a sweep.
"""

import csv
import math
import queue
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .analyzer import CLIP_LEVEL, peak_dbfs
from .picofox import set_cabled
from .sweep import (RUNS, TARGET_DB, CalTable, _id_in_progress, _say, compose_label,
                    finish_csv, freq_tag, level_reference, slug, unique_label)

WINDOW_S = 0.25
COLUMNS = ["time", "t_s", "att", "gen_dbm", "level_dbm", "sinad_db", "peak_dbfs", "clipped",
           "n_discarded", "id_active"]


@dataclass
class Result:
    label: str
    csv: Path
    summary: list        # one dict per attenuator setting, see summarize()
    level_ref: str


def record_stem(args):
    """record_<radio>_<condition>_<freq>, in the session's folder if there
    is one, so it sits beside that session's sweeps. Never an existing name."""
    runs = Path(getattr(args, "runs_dir", None) or RUNS)
    folder = runs / slug(args.session) if args.session else runs
    folder.mkdir(parents=True, exist_ok=True)
    return unique_label(str(folder / compose_label("record", args.radio, args.condition,
                                                   freq_tag(args.freq))))


def summarize(rows, target=TARGET_DB):
    """Per attenuator setting, in the order first used: windows, seconds,
    median and 10th/90th percentile SINAD, and the share of windows below
    the target. Leaves out ID windows and those with no SINAD (clipped)."""
    groups = {}
    for r in rows:
        if r["id_active"] or math.isnan(r["sinad_db"]):
            continue
        g = groups.setdefault(r["att"], dict(att=r["att"], level_dbm=r["level_dbm"], s=[]))
        g["s"].append(r["sinad_db"])
    out = []
    for g in groups.values():
        s = np.array(g.pop("s"))
        out.append(dict(g, n=len(s), seconds=round(len(s) * WINDOW_S, 2),
                        median_db=round(float(np.median(s)), 2),
                        p10_db=round(float(np.percentile(s, 10)), 2),
                        p90_db=round(float(np.percentile(s, 90)), 2),
                        below_pct=round(100 * float(np.mean(s < target)), 1)))
    return out


def summary_line(g, ref_text):
    return (f"ATT {g['att']} ({g['level_dbm']:.2f} dBm {ref_text}): {g['n']} windows, "
            f"{g['seconds']:g} s, median {g['median_db']:.1f} dB SINAD, "
            f"10-90% {g['p10_db']:.1f}-{g['p90_db']:.1f} dB, "
            f"{g['below_pct']:g}% below {TARGET_DB:g} dB")


def run(args, fox, an, att, on_reading=None, log=_say, stop=None, requests=None):
    """Records until stop (an Event) is set. att is the starting attenuator
    setting (steps); requests, a queue of new settings, changes it during
    the run. on_reading(row, reading) reports each window. RF is always off
    at the end."""
    cal = CalTable(args.cal)
    stem = record_stem(args)
    label = Path(stem).name
    partial, final = Path(f"{stem}.partial.csv"), Path(f"{stem}.csv")
    cabled = getattr(args, "cabled", False)
    ref, ref_text, _ = level_reference(cabled, args.pad_db)
    pad = args.pad_db or 0.0
    started = datetime.now()
    rows = []
    try:
        set_cabled(fox, cabled)
        if args.call:
            fox.set_call(args.call)
        fox.set_freq(args.freq)
        fox.set_att(att)
        fox.tone(args.tone, args.dev)
        log(f"recording at {att * 0.25:.2f} dB (ATT {att}), "
            f"{cal.dbm(att) - pad:.2f} dBm {ref_text}")
        t0 = time.monotonic()
        with open(partial, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            while not (stop is not None and stop.is_set()):
                new = att
                while requests is not None:
                    try:
                        new = requests.get_nowait()
                    except queue.Empty:
                        break
                if new != att:
                    att = new
                    fox.set_att(att)
                    log(f"level now {att * 0.25:.2f} dB (ATT {att}), "
                        f"{cal.dbm(att) - pad:.2f} dBm {ref_text}")
                r = an.read(settle_s=0.0, avg_s=WINDOW_S)
                gen = cal.dbm(att)
                row = dict(time=datetime.now().isoformat(timespec="milliseconds"),
                           t_s=round(time.monotonic() - t0, 3), att=att,
                           gen_dbm=round(gen, 2), level_dbm=round(gen - pad, 2),
                           sinad_db=r.sinad_db, peak_dbfs=round(peak_dbfs(r.peak), 1),
                           clipped=int(r.peak >= CLIP_LEVEL), n_discarded=r.n_discarded,
                           id_active=int(_id_in_progress(fox.events)))
                rows.append(row)
                w.writerow(["" if k == "sinad_db" and math.isnan(v) else
                            round(v, 2) if k == "sinad_db" else v
                            for k, v in row.items()])
                fh.flush()
                if on_reading:
                    on_reading(row, r)
    finally:
        fox.off()
        fox.set_att(127)
        set_cabled(fox, False)

    summary = summarize(rows)
    meta = dict(
        label=label, date=started.isoformat(timespec="seconds"), mode="foxsinad record",
        source="PicoFox r3 via USB", instrument="sound-card SINAD (foxsinad.analyzer)",
        freq_mhz=args.freq, offset_db=-pad, dev_khz=args.dev / 1000, tone_hz=args.tone,
        window_s=WINDOW_S, radio=args.radio or "(none)", condition=args.condition or "(none)",
        note=args.note, station_call=(args.call or "").upper() or "(not set)",
        cabled="yes (no Morse ID)" if cabled else "no", level_ref=ref_text,
        session=args.session or "(none)",
        audio_peak_dbfs=getattr(args, "audio_peak_dbfs", None),
        audio_target_dbfs=getattr(args, "audio_target_dbfs", None),
        cal_table=f"{cal.path.name} ({cal.date}, {cal.freq_mhz} MHz)",
        windows=len(rows), id_windows=sum(r["id_active"] for r in rows),
        **{f"summary_{i}": summary_line(g, ref_text) for i, g in enumerate(summary, 1)})
    finish_csv(partial, final, meta)
    log(f"Saved {final}")
    for g in summary:
        log("  " + summary_line(g, ref_text))
    return Result(label, final, summary, ref)
