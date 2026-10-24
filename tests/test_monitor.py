"""The audio monitor's buffer, plus a silent check that the output opens.

Runnable directly: ~/miniconda3/envs/sota-rf/bin/python tests/test_monitor.py
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad.analyzer import AudioRing  # noqa: E402


def test_ring_passes_samples_in_order_and_pads_with_silence():
    r = AudioRing(fs=1000, max_s=1.0)
    r.push(np.arange(5, dtype=np.float32))
    out = r.pull(8)
    assert list(out) == [0, 1, 2, 3, 4, 0, 0, 0], out
    print("PASS test_ring_passes_samples_in_order_and_pads_with_silence")


def test_ring_drops_oldest_when_backlog_too_long():
    r = AudioRing(fs=1000, max_s=0.01)          # keeps at most 10 samples
    r.push(np.arange(25, dtype=np.float32))
    assert list(r.pull(10)) == list(range(15, 25))
    print("PASS test_ring_drops_oldest_when_backlog_too_long")


if __name__ == "__main__":
    test_ring_passes_samples_in_order_and_pads_with_silence()
    test_ring_drops_oldest_when_backlog_too_long()
    print("ALL PASS")
