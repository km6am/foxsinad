"""Dry-run checks for the 12 dB SINAD sweep, against a simulated radio.

Runnable directly: ~/miniconda3/envs/sota-rf/bin/python tests/test_sweep.py
"""

import math
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad import sweep  # noqa: E402

TRUE_12DB_DBM = -118.0   # the simulated radio's 12 dB SINAD point
PAD_DB = 128.0


def radio_sinad(level_dbm):
    """SINAD of a simulated FM radio: rises ~3 dB per dB near threshold,
    capped by the PicoFox's 23.6 dB ceiling (noise powers add)."""
    x = 12.0 + 3.0 * (level_dbm - TRUE_12DB_DBM)
    n_rx = 1 / max(10 ** (x / 10) - 1, 1e-6)
    n_src = 1 / (10 ** (23.6 / 10) - 1)
    return 10 * math.log10(1 + 1 / (n_rx + n_src))


@dataclass
class Reading:
    sinad_db: float
    std_db: float = 0.1
    n: int = 8
    n_discarded: int = 0
    peak: float = 0.3
    under_range: bool = False


class StubFox:
    def __init__(self, port=None):
        self.att, self.events, self.statuses = 127, [], 0
        self.cabled_calls = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def hello(self): return "FOXSINAD,PicoFox r3 (stub),0,0.4.0"
    def set_call(self, c): pass
    def set_cabled(self, on): self.cabled_calls.append(on)
    def set_freq(self, f): pass
    def set_att(self, a): self.att = a
    def tone(self, hz, dev): pass
    def off(self): self.att_at_off = self.att
    def status(self):
        # read_clean calls STATUS before (odd) and after (even) each reading.
        self.statuses += 1
        if self.statuses == 8:                   # an ID overlaps a reading: redo it
            self.events += ["*ID START", "*ID DONE"]
        if self.statuses == 15:                  # an ID already under way: wait it out
            self.events.append("*ID START")
        if self.statuses == 17:
            self.events.append("*ID DONE")
        return {}


class StubAnalyzer:
    def __init__(self, device): self.reads = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, settle_s, avg_s):
        self.reads += 1
        lvl = CAL.dbm(FOX.att) - PAD_DB
        return Reading(radio_sinad(lvl))


def test_finds_the_12db_point():
    global CAL, FOX
    CAL = sweep.CalTable()
    FOX = StubFox()
    sweep.PicoFox = lambda port=None: FOX
    sweep.SinadAnalyzer = StubAnalyzer
    sweep.pick_device = lambda spec=None: 99
    sweep.RUNS = Path(tempfile.mkdtemp())
    args = sweep.argparse.Namespace(
        pad_db=PAD_DB, label="run", session=None, radio="stub", condition="bench",
        note="", call="N0CALL",
        freq=145.55, tone=1000, dev=3000, settle=0.0, avg_coarse=0.0, avg_fine=0.0,
        device=None, port=None, cal=str(sweep.DEFAULT_CAL), monitor=None, tail_db=3.0)
    an = StubAnalyzer(None)
    sweep.SinadAnalyzer = lambda device, **kw: an
    point = sweep.run(args).point_dbm
    assert point is not None and abs(point - TRUE_12DB_DBM) < 0.3, point
    rows = [l for l in (sweep.RUNS / "run_stub_bench_145.55MHz.csv").read_text().splitlines()
            if l.startswith(("coarse", "fine"))]
    # 1 sanity reading + one per row + exactly one redo for the overlapping ID
    assert an.reads == len(rows) + 2, (an.reads, len(rows))
    assert not sweep._id_in_progress(FOX.events)
    assert FOX.att == 127, "the sweep must leave the attenuator at 127"
    assert FOX.cabled_calls == [False, False], "cabled mode set explicitly, and off at the end"
    out = sweep.RUNS / "run_stub_bench_145.55MHz.csv"          # run.py's naming
    csv_text = out.read_text()
    assert "phase,gen_dbm,level_dbm,level_uv,sinad_db,sinad_sd,n,time,att,n_discarded" in csv_text
    for key in ("# label: run_stub_bench_145.55MHz", "# radio: stub", "# condition: bench",
                "# sinad12_dbm: ", "# sinad12_sigma_db: ", "# sinad12_fit_points: "):
        assert key in csv_text, key
    assert not list(sweep.RUNS.glob("*.partial.csv")), "partial file must be removed"
    coarse = [l for l in csv_text.splitlines() if l.startswith("coarse")]
    assert float(coarse[-1].split(",")[4]) < 3.0, "coarse pass must reach the tail"
    print(f"PASS test_finds_the_12db_point: found {point:.2f} dBm (true {TRUE_12DB_DBM})")


def test_cabled_never_outlives_a_run():
    from foxsinad import picofox
    fox = StubFox()
    picofox.set_cabled(fox, True)
    picofox.set_cabled(fox, 0)
    assert fox.cabled_calls == [True, False]

    class Transmitting(StubFox):
        def set_cabled(self, on):
            raise picofox.PicoFoxError("'OUTP:CABL OFF': -221 Settings conflict;"
                                       "turn the output off first")
    try:
        picofox.set_cabled(Transmitting(), False)
    except picofox.PicoFoxError:
        pass
    else:
        raise AssertionError("other errors must not be swallowed")
    print("PASS test_cabled_never_outlives_a_run")


def test_naming_matches_run_py():
    import tempfile
    d = Path(tempfile.mkdtemp())
    assert sweep.compose_label("run", "UV K5", "", sweep.freq_tag(146.0)) == "run_UV_K5_146MHz"
    stem = str(d / "run_x_bench_145.55MHz")
    assert sweep.unique_label(stem) == stem
    Path(stem + ".csv").write_text("")
    assert sweep.unique_label(stem) == stem + "_2"
    print("PASS test_naming_matches_run_py")


def test_twelve_db_point_interpolation():
    p = sweep.twelve_db_point([-110, -115, -120], [20.0, 14.0, 8.0])
    assert abs(p - (-116.667)) < 0.01, p
    assert sweep.twelve_db_point([-110, -115], [20.0, 14.0]) is None
    print("PASS test_twelve_db_point_interpolation")


def test_uv_matches_hp8935_csvs():
    # hp8935 run: -117.87 dBm <-> 0.2857 uV
    assert abs(sweep.dbm_to_uv(-117.87) - 0.2857) < 0.0005
    print("PASS test_uv_matches_hp8935_csvs")


if __name__ == "__main__":
    test_naming_matches_run_py()
    test_cabled_never_outlives_a_run()
    test_twelve_db_point_interpolation()
    test_uv_matches_hp8935_csvs()
    test_finds_the_12db_point()
    print("ALL PASS")
