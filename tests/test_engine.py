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
    assert eng.handle(dict(cmd="fox", action="att", value=17.5))["ok"]
    eng.handle(dict(cmd="stop"))
    end = time.time() + 30
    while eng.mode != "idle" and time.time() < end:
        time.sleep(0.1)
    assert eng.mode == "idle"
    assert eng._fox_raw.mode == "OFF" and eng._fox_raw.att == 127
    eng.shutdown()
    print("PASS test_ballpark_stop_turns_rf_off")


def test_level_is_at_the_radio_only_when_cabled():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", pad_db=119))
    eng._poll_once()
    st = eng.last_status
    assert st["level_ref"] == "antenna" and st["level_dbm"] == round(st["out_dbm"] - 119, 2)
    eng.handle(dict(cmd="confirm_cabled"))
    eng.handle(dict(cmd="setup", cabled=True))
    eng._poll_once()
    assert eng.last_status["level_ref"] == "radio"
    eng.handle(dict(cmd="setup", cabled=False, pad_db=""))
    eng._poll_once()
    st = eng.last_status
    assert st["level_ref"] == "output" and st["level_dbm"] == st["out_dbm"]
    r = eng.handle(dict(cmd="setup", cabled=True))
    assert r["ok"]
    r = eng.handle(dict(cmd="start", mode="sweep"))
    assert not r["ok"] and "external attenuation" in r["error"], r
    eng.handle(dict(cmd="setup", ballpark_att_db=16.1))
    assert eng.setup["ballpark_att_db"] == 16.0, "rounded to the attenuator's 0.25 dB steps"
    eng.shutdown()
    print("PASS test_level_is_at_the_radio_only_when_cabled")


def test_sim_follows_the_firmware_timeout_and_id_rules():
    from foxsinad.sim import SimFox
    fox = SimFox(speed=1000)
    fox.set_call("N0CALL")
    fox.set_timeout(30)
    fox.tone(1000, 3000)
    import time as _t
    _t.sleep(0.04)                          # 40 simulated seconds, no command
    fox._tick()
    assert fox.mode == "OFF" and "*TIMEOUT" in fox.events
    assert fox.events.count("*ID DONE") == 1, "the timeout sends the owed ID"
    assert not hasattr(fox, "abort"), "no off without the ID (firmware 0.4.0)"
    print("PASS test_sim_follows_the_firmware_timeout_and_id_rules")


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
    test_level_is_at_the_radio_only_when_cabled()
    test_sim_follows_the_firmware_timeout_and_id_rules()
    test_watchdog_turns_rf_off_without_a_ui()
    test_sweep_finds_the_simulated_point()
    print("ALL PASS")


def test_an_unplugged_picofox_is_dropped_not_polled_forever():
    eng, q = make()
    assert eng.handle(dict(cmd="connect"))["ok"]

    def unplugged():
        raise OSError("USB get_status request failed")
    eng._fox_raw.status = unplugged
    for _ in range(3):
        eng._poll_once()
    assert eng.fox is None and eng.snapshot()["state"]["connected"] is False
    errors = []
    while not q.empty():
        e = q.get()
        if e["event"] == "log" and e["level"] == "error":
            errors.append(e["msg"])
    assert len(errors) == 2 and "lost the PicoFox" in errors[1], errors   # not one per poll
    assert eng.handle(dict(cmd="connect"))["ok"]                          # and it can come back


def test_meter_transmits_the_tone_and_stop_turns_rf_off():
    eng, q = make()
    assert eng.handle(dict(cmd="connect"))["ok"]
    assert eng.handle(dict(cmd="setup", call="KM6AM", freq=145.55, tone=1000, dev=3000))["ok"]
    assert eng.handle(dict(cmd="start", mode="meter"))["ok"]
    seen = drain(q, lambda e: e["event"] == "reading")
    on = [e["status"] for e in seen if e["event"] == "status" and e["status"]]
    assert on and on[-1]["mode"] == "TONE" and abs(float(on[-1]["freq"]) - 145.55) < 1e-6, on[-1:]
    assert eng.handle(dict(cmd="stop"))["ok"]
    drain(q, lambda e: e["event"] == "state" and e["state"]["mode"] == "idle")
    assert eng.last_status["mode"] == "OFF"


def test_the_picofox_follows_the_setup_frequency():
    eng, q = make()
    assert eng.handle(dict(cmd="setup", freq=146.52))["ok"]
    assert eng.handle(dict(cmd="connect"))["ok"]
    assert abs(float(eng.last_status["freq"]) - 146.52) < 1e-6        # tuned at connect
    assert eng.handle(dict(cmd="setup", freq=145.55))["ok"]
    assert abs(float(eng.last_status["freq"]) - 145.55) < 1e-6        # and when it changes
