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
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class _NoSounddevice:
    """Stands in for sounddevice where it can't load (no PortAudio, as on
    Android): the DSP and SinadAnalyzer(None) with feed() still work, and
    anything that needs a sound card says why it can't."""

    def __init__(self, error):
        self._error = error

    def __getattr__(self, name):
        raise ImportError(f"sounddevice is unavailable here ({self._error})")


try:
    import sounddevice
except (ImportError, OSError) as e:  # OSError: sounddevice found no PortAudio library
    sounddevice = _NoSounddevice(e)

_SINAD_METER_DIR = Path(__file__).resolve().parents[1] / "third_party" / "sinad_meter"
sys.path.insert(0, str(_SINAD_METER_DIR))
import filters  # noqa: E402  (sinad_meter's)
import sinad as _sinad  # noqa: E402  (sinad_meter's; returns radio-definition SINAD)

CLIP_LEVEL = 0.99
UNDER_RANGE_PEAK = 0.003  # below this the window is probably silence/cable trouble
NO_AUDIO_S = 2.0  # read() gives up after this long without a block (they come every 10-250 ms)


class NoAudio(RuntimeError):
    """The sound card stopped delivering audio (unplugged, or capture failed)."""


def status_line(text):
    """Rewrite one live status line in place. Trimmed to the terminal's
    width: a line that wraps can't be rewound with \\r, so it would scroll."""
    width = max(20, shutil.get_terminal_size((80, 20)).columns - 1)
    print("\r" + text[:width].ljust(width), end="", flush=True)


def sinad_bar(sinad_db, width=24, full_db=36.0, mark_db=12.0):
    """A text bar for SINAD, 0 to full_db, with '|' at the 12 dB mark."""
    filled = int(max(0.0, min(full_db, sinad_db)) / full_db * width)
    mark = round(mark_db / full_db * width)
    cells = ["#" if i < filled else "." for i in range(width)]
    if 0 <= mark < width and mark >= filled:
        cells[mark] = "|"
    return "[" + "".join(cells) + "]"


def peak_dbfs(peak):
    import math
    return 20 * math.log10(max(peak, 1e-12))


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


class AudioRing:
    """Thread-safe FIFO of samples between an input callback and an output
    callback running on different clocks. Keeps at most max_s of backlog
    (dropping the oldest), and pads with silence when it runs dry."""

    def __init__(self, fs, max_s=0.25):
        self._buf = np.zeros(0, dtype=np.float32)
        self._max = int(fs * max_s)
        self._lock = threading.Lock()

    def push(self, block):
        with self._lock:
            self._buf = np.concatenate([self._buf, block.astype(np.float32)])[-self._max:]

    def pull(self, n):
        with self._lock:
            out, self._buf = self._buf[:n], self._buf[n:]
        return np.pad(out, (0, n - len(out)))


class Monitor:
    """Plays the captured audio on another output (e.g. the computer's
    speakers), so you can listen while measuring."""

    def __init__(self, device, fs=48000, gain=1.0):
        self.gain = gain
        self._ring = AudioRing(fs)
        self._stream = sounddevice.OutputStream(samplerate=fs, device=device, channels=1,
                                                callback=self._callback)

    def _callback(self, outdata, frames, _time, _status):
        outdata[:, 0] = self._ring.pull(frames) * self.gain

    def feed(self, block):
        self._ring.push(block)

    def start(self):
        self._stream.start()

    def stop(self):
        self._stream.stop()
        self._stream.close()


def pick_monitor(spec, input_idx):
    """Output device for --monitor. 'default' means the computer's own
    output: the system default, unless that's the adapter itself (macOS
    often switches to a newly plugged USB adapter), in which case the
    first other output."""
    if spec not in (None, "default"):
        spec = str(spec)
        if spec.isdigit():
            return int(spec)
        for idx, name, _, n_out, _ in list_devices():
            if n_out and spec.lower() in name.lower():
                return idx
        raise LookupError(f"no output device matching {spec!r}")
    adapter = sounddevice.query_devices(input_idx)["name"]
    default_out = sounddevice.default.device[1]
    if default_out is not None and default_out >= 0 \
            and sounddevice.query_devices(default_out)["name"] != adapter:
        return default_out
    for idx, name, _, n_out, _ in list_devices():
        if n_out and name != adapter:
            return idx
    raise LookupError("no output device other than the adapter to monitor on")


class SinadAnalyzer:
    """Continuous capture; read() returns a robust SINAD over avg_s.
    With monitor=<output device>, the captured audio is also played there.
    With device=None there is no sound card stream: whoever captures the
    audio (on Android, AudioRecord) supplies it with feed()."""

    def __init__(self, device, fs=48000, window_s=0.25, hpf_hz=200.0, lpf_hz=3000.0,
                 monitor=None, monitor_gain=1.0):
        self.monitor = Monitor(monitor, fs, monitor_gain) if monitor is not None else None
        self.fs = fs
        self.window = round(fs * window_s)
        self.no_audio_s = NO_AUDIO_S
        self._bpf = make_bandpass(fs, hpf_hz, lpf_hz)
        self._q = queue.Queue()
        self._overflowed = False
        self._stream = None if device is None else sounddevice.InputStream(
            samplerate=fs, device=device, channels=1, callback=self._callback
        )

    def _callback(self, indata, _frames, _time, status):
        # PortAudio thread; a callback stream keeps the device drained,
        # which is what prevents overflows while the caller computes.
        self.feed(indata[:, 0].copy(), status.input_overflow)

    def feed(self, block, overflowed=False):
        """Adds captured audio: one channel, float, full scale 1.0, at fs.
        overflowed marks a block after which samples were lost."""
        block = np.asarray(block, dtype=np.float32)
        if self.monitor is not None:
            self.monitor.feed(block)
        self._q.put((block, bool(overflowed)))

    def __enter__(self):
        if self.monitor is not None:
            self.monitor.start()
        if self._stream is not None:
            self._stream.start()
        return self

    def __exit__(self, *_):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self.monitor is not None:
            self.monitor.stop()

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _get(self):
        try:
            return self._q.get(timeout=self.no_audio_s)
        except queue.Empty:
            raise NoAudio(f"no audio from the sound card for {self.no_audio_s:g} s") from None

    def read(self, settle_s=0.5, avg_s=2.0):
        """Discards settle_s of audio, then measures windows for avg_s.
        Raises NoAudio if the audio stops, rather than waiting forever."""
        self._drain()
        # settle: consume and drop
        dropped = 0
        while dropped < settle_s * self.fs:
            block, _ = self._get()
            dropped += len(block)

        kept, discarded = [], 0
        peak = 0.0
        buf = np.zeros(0, dtype=np.float32)
        bad = False  # overflow seen within the current window
        needed = max(1, round(avg_s / (self.window / self.fs)))
        while len(kept) + discarded < needed or not kept:
            block, overflowed = self._get()
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
    p.add_argument("--monitor", action="store_true",
                   help="also play the audio on the computer's speakers")
    p.add_argument("--monitor-device", metavar="OUTPUT",
                   help="play it on this output instead (name or number)")
    p.add_argument("--monitor-gain", type=float, default=1.0)
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
    mon = None
    if args.monitor or args.monitor_device:
        mon = pick_monitor(args.monitor_device or "default", device)
        print(f"monitoring on: [{mon}] {sounddevice.query_devices(mon)['name']}")
    t_end = time.time() + args.seconds if args.seconds else None
    with SinadAnalyzer(device, hpf_hz=args.hpf, lpf_hz=args.lpf,
                       monitor=mon, monitor_gain=args.monitor_gain) as an:
        try:
            while t_end is None or time.time() < t_end:
                r = an.read(settle_s=0.0, avg_s=args.avg)
                flags = []
                if r.under_range:
                    flags.append("NO AUDIO")
                if r.peak >= CLIP_LEVEL:
                    flags.append("CLIP")
                if r.n_discarded:
                    flags.append(f"{r.n_discarded} dropped")
                status_line(f"SINAD {r.sinad_db:5.1f} dB {' '.join(flags)} "
                            f"{sinad_bar(r.sinad_db)} sd {r.std_db:3.1f} "
                            f"peak {peak_dbfs(r.peak):5.1f} dBFS")
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
