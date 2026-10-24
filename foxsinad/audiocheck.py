"""Audio bring-up checks for the sound-card SINAD analyzer.

    python3 -m foxsinad.audiocheck devices     # list audio devices; show which is auto-picked
    python3 -m foxsinad.audiocheck level       # live input level, for setting the radio's volume
    python3 -m foxsinad.audiocheck level --monitor   # ...and hear it on the computer's speakers
    python3 -m foxsinad.audiocheck loopback    # the analyzer's own floor: adapter output -> input

loopback plays a 1 kHz tone out of the adapter's line output and measures
it on its line input (connect OUT to IN with an RCA cable; LINE switch,
not PHONO). With no radio in the chain, the SINAD it reports is the best
this analyzer can read: anything measured later is limited by it.
"""

import argparse
import sys

import numpy as np
import sounddevice

from .analyzer import CLIP_LEVEL, UNDER_RANGE_PEAK, Monitor, list_devices, make_bandpass, \
    output_twin, pick_device, pick_monitor, sinad_db, status_line

FS = 48000


def dbfs(x):
    return 20 * np.log10(max(float(x), 1e-12))


def analyze(rec, fs=FS, window_s=0.25):
    """Median SINAD over 250 ms windows (200-3000 Hz), plus level and clipping."""
    n = round(fs * window_s)
    bpf = make_bandpass(fs)
    vals = [sinad_db(rec[i:i + n], fs, bpf) for i in range(0, len(rec) - n + 1, n)]
    peak = float(np.abs(rec).max()) if len(rec) else 0.0
    return {
        "sinad_db": float(np.median(vals)) if vals else float("nan"),
        "sinad_sd": float(np.std(vals)) if vals else float("nan"),
        "windows": len(vals),
        "peak_dbfs": dbfs(peak),
        "rms_dbfs": dbfs(np.sqrt(np.mean(rec ** 2))) if len(rec) else -240.0,
        "clipped": peak >= CLIP_LEVEL,
        "under_range": peak < UNDER_RANGE_PEAK,
    }


def cmd_devices(_args):
    for idx, name, n_in, n_out, sr in list_devices():
        print(f"  [{idx}] {name}  (inputs {n_in}, outputs {n_out}, {sr:.0f} Hz)")
    try:
        pick_device()
    except LookupError:
        print("\nNo USB line-in adapter recognized. Plug in the UFO202 (LINE switch), "
              "or pass --device to the other commands.")


def cmd_level(args):
    dev = pick_device(args.device)
    mon = None
    if args.monitor or args.monitor_device:
        out = pick_monitor(args.monitor_device or "default", dev)
        mon = Monitor(out, FS, args.monitor_gain)
        print(f"monitoring on: [{out}] {sounddevice.query_devices(out)['name']}")
    print("Live input level. Aim for peaks between -20 and -6 dBFS with the test tone on.\n"
          "Ctrl-C to stop.\n")
    block = FS // 8
    try:
        if mon:
            mon.start()
        with sounddevice.InputStream(samplerate=FS, device=dev, channels=1) as s:
            while True:
                x, _overflow = s.read(block)
                x = x[:, 0]
                if mon:
                    mon.feed(x)
                pk, rms = dbfs(np.abs(x).max()), dbfs(np.sqrt(np.mean(x ** 2)))
                bar = "#" * max(0, min(20, int((pk + 60) / 3)))
                flag = " CLIPPING: volume down" if pk > dbfs(CLIP_LEVEL) else \
                       " too quiet" if pk < -40 else ""
                status_line(f"peak {pk:6.1f} dBFS  rms {rms:6.1f} |{bar:<20}|{flag}")
    except KeyboardInterrupt:
        print()
    finally:
        if mon:
            mon.stop()


def cmd_loopback(args):
    dev = pick_device(args.device)
    out = output_twin(dev)
    secs = args.seconds
    t = np.arange(int(FS * (secs + 0.5))) / FS
    tone = (10 ** (args.level_dbfs / 20) * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    print(f"Playing 1 kHz at {args.level_dbfs} dBFS on [{out}] and recording [{dev}] for "
          f"{secs:g} s.\nConnect line OUT to line IN; LINE switch on.")
    rec = sounddevice.playrec(tone, samplerate=FS, channels=1, device=(dev, out), blocking=True)
    rec = rec[int(FS * 0.5):, 0]          # drop the first 0.5 s (start-up)
    r = analyze(rec)
    print(f"\nlevel:  peak {r['peak_dbfs']:.1f} dBFS, rms {r['rms_dbfs']:.1f} dBFS")
    print(f"SINAD:  {r['sinad_db']:.1f} dB (sd {r['sinad_sd']:.2f}, {r['windows']} windows, "
          "200-3000 Hz)")
    if r["under_range"]:
        print("No signal came back: check the cable (OUT -> IN) and the LINE switch.")
    elif r["clipped"]:
        print("The input clipped: lower --level-dbfs.")
    else:
        print("This is the analyzer's floor. A radio measured through it can't read higher.")


def main():
    p = argparse.ArgumentParser(description="Audio bring-up checks for the SINAD analyzer.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device", help="audio input, index or name (default: auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list audio devices", parents=[common])
    lv = sub.add_parser("level", help="live input level meter", parents=[common])
    lv.add_argument("--monitor", action="store_true",
                    help="also play the audio on the computer's speakers")
    lv.add_argument("--monitor-device", metavar="OUTPUT",
                    help="play it on this output instead (name or number)")
    lv.add_argument("--monitor-gain", type=float, default=1.0)
    lb = sub.add_parser("loopback", help="measure the analyzer's own SINAD floor",
                        parents=[common])
    lb.add_argument("--seconds", type=float, default=3.0)
    lb.add_argument("--level-dbfs", type=float, default=-12.0)
    args = p.parse_args()
    try:
        {"devices": cmd_devices, "level": cmd_level, "loopback": cmd_loopback}[args.cmd](args)
    except LookupError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
