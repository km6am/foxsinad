"""Engine against the simulated bench: commands in, events out, RF off on stop.

Runnable directly: ~/miniconda3/envs/sota-rf/bin/python tests/test_engine.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad.app.engine import Engine  # noqa: E402

SPEED = 200.0


def drain(q, until, timeout=30):
    seen, end = [], time.time() + timeout
    while time.time() < end:
        try:
            ev = q.get(timeout=0.5)
        except Exception:
            continue
        seen.append(ev)
        if until(ev):
            return seen
    raise AssertionError(f"timed out; last events {[e['event'] for e in seen[-8:]]}")


def back_to_idle():
    """Matches the first idle state after the mode has started."""
    started = []

    def match(e):
        if e["event"] == "state":
            if e["state"]["mode"] != "idle":
                started.append(True)
            elif started:
                return True
        return False
    return match


def make():
    eng = Engine(sim=True, sim_speed=SPEED, runs_dir=tempfile.mkdtemp(),
                 sim_options=dict(pad_db=119.0, twelve_db_dbm=-123.7, seed=1))
    return eng, eng.subscribe()


def test_sweep_finds_the_simulated_point():
    eng, q = make()
    assert q.get()["event"] == "snapshot"
    assert eng.handle(dict(cmd="connect"))["ok"]
    assert eng.handle(dict(cmd="setup", quick=True, radio="SIM", condition="bench"))["ok"]
    assert eng.handle(dict(cmd="start", mode="sweep"))["ok"]
    r = eng.handle(dict(cmd="fox", action="tone", value=1000, dev=3000))
    assert not r["ok"] and "locked" in r["error"], r
    seen = drain(q, back_to_idle())
    fin = [e for e in seen if e["event"] == "run_finished"]
    assert fin and abs(fin[0]["point_dbm"] - (-123.7)) < 1.0, fin
    assert any(e["event"] == "point" for e in seen)
    assert eng._fox_raw.mode == "OFF" and eng._fox_raw.att == 127
    eng.shutdown()
    print(f"PASS test_sweep_finds_the_simulated_point: {fin[0]['point_dbm']} dBm")


def test_cabled_needs_confirmation_and_never_ids():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    r = eng.handle(dict(cmd="setup", cabled=True))
    assert not r["ok"] and "confirm" in r["error"], r
    assert eng.handle(dict(cmd="confirm_cabled"))["ok"]
    assert eng.handle(dict(cmd="setup", cabled=True, call=""))["ok"]
    assert eng.handle(dict(cmd="fox", action="tone", value=1000, dev=3000))["ok"]
    assert eng._fox_raw.cabled and eng._fox_raw.mode == "TONE"
    assert eng.handle(dict(cmd="fox", action="off"))["ok"]
    assert "*ID START" not in eng._fox_raw.events and not eng._fox_raw.cabled
    eng.shutdown()
    print("PASS test_cabled_needs_confirmation_and_never_ids")


def test_no_callsign_no_rf():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", call=""))
    r = eng.handle(dict(cmd="fox", action="carrier"))
    assert not r["ok"] and eng._fox_raw.mode == "OFF", r
    r = eng.handle(dict(cmd="start", mode="ballpark"))
    assert not r["ok"] and "callsign" in r["error"], r
    eng.shutdown()
    print("PASS test_no_callsign_no_rf")


def test_ballpark_stop_turns_rf_off():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    assert eng.handle(dict(cmd="start", mode="ballpark"))["ok"]
    drain(q, lambda e: e["event"] == "reading")
    assert eng._fox_raw.mode == "TONE"
    assert eng.handle(dict(cmd="fox", action="att", value=70))["ok"]
    eng.handle(dict(cmd="stop"))
    end = time.time() + 30
    while eng.mode != "idle" and time.time() < end:
        time.sleep(0.1)
    assert eng.mode == "idle"
    assert eng._fox_raw.mode == "OFF" and eng._fox_raw.att == 127
    eng.shutdown()
    print("PASS test_ballpark_stop_turns_rf_off")


def test_watchdog_turns_rf_off_without_a_ui():
    eng, q = make()
    eng.watchdog_s = 0.5
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="fox", action="carrier"))
    eng.unsubscribe(q)
    end = time.time() + 10
    while eng._fox_raw.mode != "OFF" and time.time() < end:
        time.sleep(0.2)
    assert eng._fox_raw.mode == "OFF"
    eng.shutdown()
    print("PASS test_watchdog_turns_rf_off_without_a_ui")


if __name__ == "__main__":
    test_no_callsign_no_rf()
    test_cabled_needs_confirmation_and_never_ids()
    test_ballpark_stop_turns_rf_off()
    test_watchdog_turns_rf_off_without_a_ui()
    test_sweep_finds_the_simulated_point()
    print("ALL PASS")
