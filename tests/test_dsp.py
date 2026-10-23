"""Synthetic-audio checks for the analyzer DSP (plan §3 verification).

Runnable directly (python3 tests/test_dsp.py) or via pytest.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad.analyzer import make_bandpass, sinad_db  # noqa: E402

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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
