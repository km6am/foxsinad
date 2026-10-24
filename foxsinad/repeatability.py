"""Repeatability of a set of sweeps: the spread of their 12 dB SINAD points.

    python3 -m foxsinad.repeatability runs/<session> [--ref-csv <8935 run.csv>]

Reads every run CSV in the folder (the 12 dB point and its fit uncertainty
come from each file's header), prints the spread, and plots all the curves
overlaid plus the 12 dB points in run order. With --ref-csv, an
hp8935_sinad_serial run of the same radio is drawn for comparison.
"""

import argparse
import csv
import statistics as st
from pathlib import Path


def read_run(path):
    """(header dict, rows) from a run CSV in the run.py format."""
    meta, body = {}, []
    for line in Path(path).read_text().splitlines():
        if line.startswith("# "):
            k, _, v = line[2:].partition(": ")
            meta[k] = v
        elif line.strip():
            body.append(line)
    return meta, list(csv.DictReader(body))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_order_key(path):
    """name.csv, name_2.csv, ... name_10.csv in the order they were taken."""
    stem = path.stem
    head, _, tail = stem.rpartition("_")
    return (head, int(tail)) if tail.isdigit() and head.endswith("MHz") else (stem, 1)


def summarize(folder):
    runs = []
    for path in sorted(Path(folder).glob("*.csv"), key=run_order_key):
        if path.name.endswith(".partial.csv"):
            continue
        meta, rows = read_run(path)
        runs.append(dict(name=path.stem, meta=meta, rows=rows,
                         p12=_num(meta.get("sinad12_dbm")),
                         sigma=_num(meta.get("sinad12_sigma_db"))))
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder")
    ap.add_argument("--ref-csv", help="hp8935_sinad_serial run of the same radio, to compare")
    args = ap.parse_args()

    runs = summarize(args.folder)
    pts = [r["p12"] for r in runs if r["p12"] is not None]
    print(f"{len(runs)} runs, {len(pts)} with a 12 dB point\n")
    for i, r in enumerate(runs, 1):
        p = "   (none)" if r["p12"] is None else f"{r['p12']:9.2f} dBm"
        s = "" if r["sigma"] is None else f"  fit +/- {r['sigma']:.2f}"
        print(f"  {i:2d}  {r['name']:32s} {p}{s}")
    if len(pts) >= 2:
        mean, sd = st.mean(pts), st.stdev(pts)
        sig = [r["sigma"] for r in runs if r["sigma"] is not None]
        print(f"\n  mean {mean:.2f} dBm   run-to-run sd {sd:.2f} dB   "
              f"range {max(pts) - min(pts):.2f} dB ({min(pts):.2f} to {max(pts):.2f})")
        if sig:
            print(f"  typical single-run fit uncertainty {st.median(sig):.2f} dB "
                  f"(sd/fit ratio {sd / st.median(sig):.1f}: near 1 means the fit's +/- "
                  "describes the scatter; well above 1 means something varies between runs)")
    ref = None
    if args.ref_csv:
        rmeta, rrows = read_run(args.ref_csv)
        ref = (_num(rmeta.get("sinad12_dbm")), rrows, rmeta.get("date", "")[:10])
        if ref[0] is not None and pts:
            print(f"\n  HP 8935 reference ({ref[2]}): {ref[0]:.2f} dBm; "
                  f"foxsinad mean is {st.mean(pts) - ref[0]:+.2f} dB from it")
    plot(Path(args.folder), runs, ref)


def plot(folder, runs, ref):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    SURF, INK, INK2, GRID, GRAY = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e4df", "#9a9892"
    BLUE, ORANGE = "#2a78d6", "#eb6834"
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.8),
                                 gridspec_kw={"width_ratios": [1.6, 1]})
    fig.patch.set_facecolor(SURF)
    for a in (a1, a2):
        a.set_facecolor(SURF)
        a.grid(True, color=GRID, lw=0.8)
        a.tick_params(colors=INK2, labelsize=9)
        for s in a.spines.values():
            s.set_color("#d8d6d0")
        a.spines["top"].set_visible(False)
        a.spines["right"].set_visible(False)

    a1.axhline(12, color=GRAY, ls="--", lw=1)
    if ref and ref[1]:
        rr = [r for r in ref[1] if r.get("sinad_db")]
        a1.plot([float(r["level_dbm"]) for r in rr], [float(r["sinad_db"]) for r in rr], "o",
                ms=3, color=ORANGE, label=f"HP 8935 ({ref[2]})", zorder=3)
    for i, r in enumerate(runs):
        pts = sorted((float(x["level_dbm"]), float(x["sinad_db"])) for x in r["rows"])
        if pts:
            a1.plot(*zip(*pts), "-", lw=0.9, alpha=0.55, color=BLUE,
                    label="foxsinad runs" if i == 0 else None)
    a1.set_xlabel("level at the radio (dBm)", color=INK)
    a1.set_ylabel("SINAD (dB)", color=INK)
    a1.set_title("SINAD vs level at the radio, all runs", loc="left", color=INK, fontsize=11)
    a1.legend(frameon=False, fontsize=8.5, loc="upper left", labelcolor=INK2)

    idx = [i + 1 for i, r in enumerate(runs) if r["p12"] is not None]
    val = [r["p12"] for r in runs if r["p12"] is not None]
    err = [r["sigma"] or 0 for r in runs if r["p12"] is not None]
    a2.errorbar(idx, val, yerr=err, fmt="o", ms=5, color=BLUE, ecolor=BLUE, capsize=3)
    if len(val) >= 2:
        m = st.mean(val)
        a2.axhline(m, color=BLUE, lw=1, alpha=0.6)
        a2.text(max(idx) + 0.3, m, f"mean {m:.2f}", va="center", fontsize=8.5, color=BLUE)
    if ref and ref[0] is not None:
        a2.axhline(ref[0], color=ORANGE, lw=1, ls="--")
        a2.text(max(idx or [1]) + 0.3, ref[0], f"8935 {ref[0]:.2f}", va="center",
                fontsize=8.5, color=ORANGE)
    a2.set_xlim(0.4, max(idx or [1]) + 2.2)
    a2.set_xticks(idx)
    a2.set_xlabel("run", color=INK)
    a2.set_ylabel("12 dB SINAD level (dBm)", color=INK)
    a2.set_title("12 dB SINAD point by run", loc="left", color=INK, fontsize=11)
    fig.tight_layout()
    out = folder / "repeatability.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=SURF)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
