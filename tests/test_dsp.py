"""Synthetic-audio checks for the analyzer DSP (plan §3 verification).

Runnable directly (python3 tests/test_dsp.py) or via pytest.
"""

import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad.analyzer import NoAudio, SinadAnalyzer, make_bandpass, sinad_db  # noqa: E402

FS = 48000
N = FS // 4  # one 250 ms window, as used live


def _tone(freq, amp, n=N):
    return amp * np.sin(2 * np.pi * freq * np.arange(n) / FS)


def test_clean_tone_reads_high():
    bpf = make_bandpass(FS)
    s = sinad_db(_tone(1000, 0.5), FS, bpf)
    assert s > 30, s


def test_known_snr_within_1db():
    rng = np.random.default_rng(42)
    amp, sigma = 0.5, 0.02
    x = _tone(1000, amp) + rng.normal(0, sigma, N)
    # In-band noise power after the 200-3000 Hz bandpass:
    inband = sigma**2 * (3000 - 200) / (FS / 2)
    expected = 10 * np.log10(1 + (amp**2 / 2) / inband)
    bpf = make_bandpass(FS)
    got = sinad_db(x, FS, bpf)
    assert abs(got - expected) < 1.0, (got, expected)


def test_noise_only_reads_low():
    rng = np.random.default_rng(7)
    bpf = make_bandpass(FS)
    s = sinad_db(rng.normal(0, 0.05, N), FS, bpf)
    assert s < 10, s



def _fed_reading(blocks, settle_s=0.0, avg_s=1.0):
    """read() on a SinadAnalyzer with no sound card, fed from another thread
    the way the phone's audio thread feeds it: (samples, overflowed) pairs."""
    an = SinadAnalyzer(None)
    result = {}
    reader = threading.Thread(target=lambda: result.update(r=an.read(settle_s, avg_s)))
    with an:
        reader.start()
        while reader.is_alive():
            for block, overflowed in blocks:
                an.feed(block, overflowed)
            reader.join(0.01)
    return result["r"]


def test_fed_audio_reads_like_the_window_function():
    rng = np.random.default_rng(3)
    x = _tone(1000, 0.5) + rng.normal(0, 0.02, N)
    r = _fed_reading([(x, False)])
    assert r.n == 4 and r.n_discarded == 0
    # feed() keeps float32, as the sound card stream always delivered
    assert abs(r.sinad_db - sinad_db(x.astype(np.float32), FS, make_bandpass(FS))) < 1e-9


def test_fed_overflow_and_clipping_are_discarded():
    clean = _tone(1000, 0.5)
    r = _fed_reading([(clean, False), (clean, True), (_tone(1000, 1.2), False), (clean, False)])
    assert r.n_discarded >= 2 and r.n >= 1
    assert r.peak >= 0.99


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")


def test_read_gives_up_when_the_audio_stops():
    # Unplugged mid-run, or a capture that never started: an error, not a hang.
    an = SinadAnalyzer(None)
    an.no_audio_s = 0.1
    with an, pytest.raises(NoAudio, match="no audio"):
        an.read(0.0, 1.0)
