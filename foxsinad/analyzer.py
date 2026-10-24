"""SINAD analyzer: sound-card capture + sinad_meter DSP, hardened.

This is the seam the phase-2 Kotlin port replaces: nothing above this
module touches sinad_meter or the sound card directly.

Hardening beyond sinad_meter (per docs/plans implementation plan §3):
windows that overflowed the input buffer or clipped the ADC are DISCARDED
(sinad_meter only warns), aggregation is the median so one surviving
glitch cannot bias a point, and each reading reports how much was thrown
away. Default passband 200-3000 Hz keeps the PicoFox's 4 kHz sampling
image (5 kS/s modulation) out of the measurement.

CLI (live meter):
    python3 -m foxsinad.analyzer --device UFO202            # name substring
    python3 -m foxsinad.analyzer --list-devices
"""

import queue
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice

_SINAD_METER_DIR = Path(__file__).resolve().parents[1] / "third_party" / "sinad_meter"
sys.path.insert(0, str(_SINAD_METER_DIR))
import filters  # noqa: E402  (sinad_meter's)
import sinad as _sinad  # noqa: E402  (sinad_meter's; returns radio-definition SINAD)

CLIP_LEVEL = 0.99
UNDER_RANGE_PEAK = 0.003  # below this the window is probably silence/cable trouble


def make_bandpass(fs, hpf_hz=200.0, lpf_hz=3000.0):
    return filters.make_audio_filter(fs, hpf_hz, lpf_hz)


def sinad_db(samples, fs, bandpass=None):
    """SINAD (dB, (S+N+D)/(N+D)) of one record; pure function for tests."""
    if bandpass is not None:
        bandpass.reset()
        samples = bandpass(samples)
    value, _ = _sinad.measure(samples, fs)
    return value


@dataclass
class Reading:
    sinad_db: float      # median over kept windows
    std_db: float
    n: int               # windows kept
    n_discarded: int     # overflowed or clipped windows
    peak: float          # max |sample| seen (pre-filter)
    under_range: bool    # capture suspiciously quiet throughout


class SinadAnalyzer:
    """Continuous capture; read() returns a robust SINAD over avg_s."""

    def __init__(self, device, fs=48000, window_s=0.25, hpf_hz=200.0, lpf_hz=3000.0):
        self.fs = fs
        self.window = round(fs * window_s)
        self._bpf = make_bandpass(fs, hpf_hz, lpf_hz)
        self._q = queue.Queue()
        self._overflowed = False
        self._stream = sounddevice.InputStream(
            samplerate=fs, device=device, channels=1, callback=self._callback
        )

    def _callback(self, indata, _frames, _time, status):
        # PortAudio thread; a callback stream keeps the device drained,
        # which is what prevents overflows while the caller computes.
        self._q.put((indata[:, 0].copy(), bool(status.input_overflow)))

    def __enter__(self):
        self._stream.start()
        return self

    def __exit__(self, *_):
        self._stream.stop()
        self._stream.close()

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def read(self, settle_s=0.5, avg_s=2.0):
        """Discards settle_s of audio, then measures windows for avg_s."""
        self._drain()
        # settle: consume and drop
        dropped = 0
        while dropped < settle_s * self.fs:
            block, _ = self._q.get()
            dropped += len(block)

        kept, discarded = [], 0
        peak = 0.0
        buf = np.zeros(0, dtype=np.float32)
        bad = False  # overflow seen within the current window
        needed = max(1, round(avg_s / (self.window / self.fs)))
        while len(kept) + discarded < needed or not kept:
            block, overflowed = self._q.get()
            bad = bad or overflowed
            buf = np.concatenate([buf, block])
            while len(buf) >= self.window:
                win, buf = buf[: self.window], buf[self.window :]
                wpeak = float(np.abs(win).max())
                peak = max(peak, wpeak)
                if bad or wpeak >= CLIP_LEVEL:
                    discarded += 1
                    bad = False
                    continue
                kept.append(sinad_db(win, self.fs, self._bpf))

        arr = np.array(kept)
        return Reading(
            sinad_db=float(np.median(arr)),
            std_db=float(arr.std()),
            n=len(kept),
            n_discarded=discarded,
            peak=peak,
            under_range=peak < UNDER_RANGE_PEAK,
        )


# Names a USB line-in adapter may show up under. Measured: the UFO202
# appears on macOS as "USB Audio CODEC" (its codec chip's name), split into
# an input-only device and an output-only device. pick_device() prints what
# it chose, and --device overrides it.
AUTO_NAMES = ("UFO202", "USB Audio CODEC", "USB AUDIO CODEC", "USB Audio Device")


def list_devices():
    """(index, name, max_in, max_out, default_samplerate) for every device."""
    return [(d["index"], d["name"], d["max_input_channels"], d["max_output_channels"],
             d["default_samplerate"]) for d in sounddevice.query_devices()]


def find_device(name_fragment, need_output=False):
    for idx, name, n_in, n_out, _ in list_devices():
        if n_in and (n_out or not need_output) and name_fragment.lower() in name.lower():
            return idx
    raise LookupError(f"no input device matching {name_fragment!r}")


def output_twin(input_idx):
    """The output-only device with the same name as an input-only one:
    macOS splits USB audio adapters like the UFO202 into two devices."""
    name = sounddevice.query_devices(input_idx)["name"]
    for idx, n, _, n_out, _ in list_devices():
        if n_out and n == name:
            return idx
    raise LookupError(f"no output device named {name!r} to pair with input [{input_idx}]")


def pick_device(spec=None, need_output=False):
    """Device index from an explicit index/name, or auto-detected from
    AUTO_NAMES. Never falls back to a built-in microphone: its voice
    processing removes test tones (measured on a MacBook Pro)."""
    if spec is not None:
        spec = str(spec)
        return int(spec) if spec.isdigit() else find_device(spec, need_output)
    for name in AUTO_NAMES:
        try:
            idx = find_device(name, need_output)
        except LookupError:
            continue
        print(f"audio device: [{idx}] {sounddevice.query_devices(idx)['name']} (auto-detected)")
        return idx
    lines = "\n".join(f"  [{i}] {n}  (in {a}, out {b})" for i, n, a, b, _ in list_devices())
    raise LookupError("no USB line-in adapter found. Plug in the UFO202 (switch on LINE),\n"
                      "or name a device with --device. Devices seen:\n" + lines)


def main():
    import argparse
    import time

    p = argparse.ArgumentParser(description="Live SINAD meter")
    p.add_argument("--device", help="input device index or name substring "
                   "(default: auto-detect the USB line-in adapter)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--hpf", type=float, default=200.0)
    p.add_argument("--lpf", type=float, default=3000.0)
    p.add_argument("--avg", type=float, default=1.0, help="seconds per reading")
    p.add_argument("--seconds", type=float, default=0,
                   help="stop after this long (default: run until ^C)")
    args = p.parse_args()

    if args.list_devices:
        for d in sounddevice.query_devices():
            if d["max_input_channels"]:
                print(f"{d['index']:3} {d['name']}")
        return

    device = pick_device(args.device)
    t_end = time.time() + args.seconds if args.seconds else None
    with SinadAnalyzer(device, hpf_hz=args.hpf, lpf_hz=args.lpf) as an:
        try:
            while t_end is None or time.time() < t_end:
                r = an.read(settle_s=0.0, avg_s=args.avg)
                flags = []
                if r.n_discarded:
                    flags.append(f"{r.n_discarded} discarded")
                if r.under_range:
                    flags.append("UNDER-RANGE")
                if r.peak >= CLIP_LEVEL:
                    flags.append("CLIP")
                print(f"SINAD {r.sinad_db:5.1f} dB  std {r.std_db:4.1f}  "
                      f"peak {r.peak:.3f}  {' '.join(flags)}", flush=True)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
