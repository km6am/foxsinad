"""Set the external attenuation before a 12 dB SINAD sweep.

    python3 -m foxsinad.ballpark

The PicoFox's own attenuator covers only ~32 dB, so an external step
attenuator has to put the radio's 12 dB SINAD point inside that window.
This parks the PicoFox in the middle of its range (ATT 64, so the sweep
has 16 dB of room either way), transmits the test tone, and shows the
radio's SINAD live while you turn the step attenuator:

  1. Start with the step attenuator at MAXIMUM, so the radio sees almost
     nothing.
  2. Reduce it until SINAD reads about 12 dB.
  3. Press Ctrl-C, then enter the total external attenuation (step
     attenuator + fixed pads + cables). The sweep command to run next is
     printed for you.

RF is always turned off at the end (with the Morse ID).
"""

import argparse
import sys

from .analyzer import CLIP_LEVEL, SinadAnalyzer, pick_device, pick_monitor, status_line
from .picofox import PicoFox, PicoFoxError
from .sweep import CalTable, DEFAULT_CAL, read_clean


def hint(r):
    """What to do next, from one reading."""
    if r.under_range:
        return "no audio: check the cable, the radio's volume, and that squelch is open"
    if r.peak >= CLIP_LEVEL:
        return "audio clipping: turn the radio's volume down"
    s = r.sinad_db
    if s >= 20:
        return "far too strong: add a lot of attenuation (tens of dB)"
    if s > 14:
        return "too strong: add attenuation"
    if s >= 10:
        return "GOOD: about 12 dB. Press Ctrl-C."
    if s >= 4:
        return "too weak: remove attenuation"
    return "nothing yet (noise only): remove attenuation"


def main():
    p = argparse.ArgumentParser(description="Set the external attenuation before a sweep.")
    p.add_argument("--att", type=int, default=64, help="PicoFox attenuator setting (default 64)")
    p.add_argument("--call", default="KM6AM")
    p.add_argument("--freq", type=float, default=145.55)
    p.add_argument("--tone", type=int, default=1000)
    p.add_argument("--dev", type=int, default=3000)
    p.add_argument("--device", help="audio input (default: auto-detect the USB adapter)")
    p.add_argument("--port", help="PicoFox serial port (default: auto)")
    p.add_argument("--monitor", nargs="?", const="default", metavar="OUTPUT",
                   help="also play the radio's audio on the computer's output")
    p.add_argument("--monitor-gain", type=float, default=1.0)
    args = p.parse_args()

    cal = CalTable(DEFAULT_CAL)
    last = None
    try:
        dev = pick_device(args.device)
        mon = pick_monitor(args.monitor, dev) if args.monitor else None
        with PicoFox(args.port) as fox, SinadAnalyzer(dev, monitor=mon,
                                                      monitor_gain=args.monitor_gain) as an:
            print("PicoFox:", fox.hello())
            fox.set_call(args.call)
            fox.set_freq(args.freq)
            fox.set_att(args.att)
            out = cal.dbm(args.att)
            print("\nStep attenuator at MAXIMUM first. Then reduce it until SINAD reads "
                  "about 12 dB.")
            input(f"Ready? Press Enter to start the tone (PicoFox at ATT {args.att}, "
                  f"about {out:+.1f} dBm out) ... ")
            try:
                fox.tone(args.tone, args.dev)
                while True:
                    r = read_clean(fox, an, settle_s=0.0, avg_s=1.0)
                    last = r
                    status_line(f"SINAD {r.sinad_db:5.1f} dB  {hint(r)}")
            except KeyboardInterrupt:
                print("\nturning RF off ...")
            finally:
                fox.off()           # sends the Morse ID
                fox.set_att(127)
    except (LookupError, PicoFoxError) as e:
        sys.exit(f"error: {e}")

    try:
        pad = float(input("\nTotal external attenuation, dB "
                          "(step attenuator + fixed pads + cables): "))
    except (ValueError, EOFError, KeyboardInterrupt):
        pad = None

    if pad is None:
        print("No attenuation entered. Run the sweep with --pad-db <total dB>.")
        return
    if last is not None and 10 <= last.sinad_db <= 14:
        print(f"\nThe radio is near 12 dB SINAD at about {out - pad:.1f} dBm "
              "(a rough figure; the sweep measures it properly).")
    print("\nNext:")
    print(f"  ~/miniconda3/envs/sota-rf/bin/python -m foxsinad.sweep --pad-db {pad:g} "
          f"--label <radio>_<condition>")


if __name__ == "__main__":
    main()
