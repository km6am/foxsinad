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
    assert eng.handle(dict(cmd="setup", radio="SIM", condition="bench"))["ok"]
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
    eng._poll_once()                  # cabled, attenuation not entered: still the output
    assert eng.last_status["level_ref"] == "output"
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


def test_a_refusal_names_the_field_that_caused_it():
    eng, q = make()
    r = eng.handle(dict(cmd="setup", tone=99999))
    assert not r["ok"] and r["field"] == "tone", r
    assert eng.handle(dict(cmd="connect"))["ok"]
    assert eng.handle(dict(cmd="setup", call=""))["ok"]
    r = eng.handle(dict(cmd="start", mode="meter"))
    assert not r["ok"] and r["field"] == "call", r


def test_a_cabled_sweep_without_attenuation_runs_and_reports_picofox_output():
    eng, q = make()
    eng.handle(dict(cmd="confirm_cabled"))
    assert eng.handle(dict(cmd="setup", cabled=True, pad_db=""))["ok"]
    assert eng.handle(dict(cmd="connect"))["ok"]
    assert eng.handle(dict(cmd="start", mode="sweep"))["ok"]
    seen = drain(q, lambda e: e["event"] == "run_started")
    assert seen[-1]["level_ref"] == "output" and seen[-1]["pad_db"] is None
    assert eng.handle(dict(cmd="stop"))["ok"]
    drain(q, lambda e: e["event"] == "state" and e["state"]["mode"] == "idle")


def test_the_setup_is_remembered_but_not_cabled_mode():
    runs = tempfile.mkdtemp()
    eng = Engine(runs_dir=runs)                       # a real bench, though nothing is opened
    eng.handle(dict(cmd="confirm_cabled"))
    assert eng.handle(dict(cmd="setup", call="KM6AM", freq=146.52, radio="TD-H3", cabled=True))["ok"]
    eng.shutdown()
    again = Engine(runs_dir=runs)
    s = again.setup
    assert (s["call"], s["freq"], s["radio"], s["cabled"]) == ("KM6AM", 146.52, "TD-H3", False), s
    again.shutdown()
    assert not (Path(make()[0].runs_dir) / "setup.json").exists()   # the simulator keeps nothing


def test_a_saved_setup_from_an_older_version_still_loads():
    import json
    runs = tempfile.mkdtemp()
    (Path(runs) / "setup.json").write_text(json.dumps(dict(call="KM6AM", quick=True)))
    eng = Engine(runs_dir=runs)
    assert eng.setup["call"] == "KM6AM" and "quick" not in eng.setup, eng.setup
    eng.shutdown()


def test_a_clipped_reading_is_valid_json():
    # JSON has no NaN: a reading with no SINAD must go out as null, or the
    # page can't parse it and shows nothing at all.
    import json
    from foxsinad.analyzer import Reading
    eng, q = make()
    eng._reading_event(Reading(float("nan"), float("nan"), 0, 8, 1.0, False))
    while True:
        e = q.get(timeout=1)
        if e["event"] == "reading":
            break
    text = json.dumps(e, allow_nan=False)
    assert e["sinad_db"] is None and e["clipped"] is True, text


def test_audio_hint_bands():
    from foxsinad.analyzer import audio_hint
    assert audio_hint(-10.0) == "OK" and audio_hint(-7.5) == "OK" and audio_hint(-12.9) == "OK"
    assert audio_hint(-5.0) == "volume down" and audio_hint(-20.0) == "volume up"
    assert audio_hint(-0.5, clipped=True).startswith("CLIPPING")
    assert audio_hint(-60.0, under_range=True) == "NO AUDIO"
    assert audio_hint(-16.0, target=-16.0) == "OK"


def test_the_audio_step_sets_full_level_and_records_the_volume():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", call="N0CALL", radio="SIM"))
    eng.handle(dict(cmd="fox", action="att", value=20))
    assert not eng.handle(dict(cmd="start", mode="sweep", att_db=0))["ok"]
    assert eng.handle(dict(cmd="start", mode="meter", att_db=0))["ok"]
    seen = drain(q, lambda e: e["event"] == "state" and e["state"]["audio_set"])
    assert eng._fox_raw.att == 0
    readings = [e for e in seen if e["event"] == "reading"]
    assert readings and readings[-1]["audio_hint"] == "OK", readings[-1:]
    assert abs(eng.audio_set["peak_dbfs"] - (-12.0)) < 0.5, eng.audio_set
    eng.handle(dict(cmd="stop"))
    drain(q, lambda e: e["event"] == "state" and e["state"]["mode"] == "idle")
    eng.handle(dict(cmd="setup", radio="ANOTHER"))
    assert eng.audio_set is None, "another radio's volume isn't set"
    eng.shutdown()


def test_record_summary_leaves_out_ids_and_clipped_windows():
    from foxsinad import record
    nan = float("nan")
    rows = ([dict(att=68, level_dbm=-125.0, sinad_db=s, id_active=0) for s in (10, 12, 14, 16)]
            + [dict(att=68, level_dbm=-125.0, sinad_db=3.0, id_active=1),
               dict(att=68, level_dbm=-125.0, sinad_db=nan, id_active=0),
               dict(att=72, level_dbm=-126.0, sinad_db=8.0, id_active=0)])
    a, b = record.summarize(rows)
    assert (a["att"], a["n"], a["median_db"], a["below_pct"]) == (68, 4, 13.0, 25.0), a
    assert (b["att"], b["n"], b["below_pct"]) == (72, 1, 100.0), b


def test_record_follows_the_last_sweep_and_changes_level_between_windows():
    import csv
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", call="N0CALL", radio="SIM", condition="bench", pad_db=119.0))
    assert eng._record_level()[0] == 64                       # no sweep yet: ballpark 16 dB
    eng.results.append(dict(point_dbm=-123.7, pad_db=119.0))  # as a sweep leaves it
    att0, why = eng._record_level()
    assert "12 dB point" in why and abs(eng.cal.dbm(att0) - 119.0 - (-123.7)) < 0.3, (att0, why)
    assert eng.handle(dict(cmd="start", mode="record"))["ok"]
    assert not eng.handle(dict(cmd="fox", action="tone", value=1000, dev=3000))["ok"]
    seen = drain(q, lambda e: e["event"] == "reading" and e["att"] == att0)
    assert eng.handle(dict(cmd="fox", action="att", value=(att0 + 8) / 4))["ok"]
    seen = drain(q, lambda e: e["event"] == "reading" and e["att"] == att0 + 8)
    assert seen[-1]["boxcar_db"] is not None and "t_s" in seen[-1]
    eng.handle(dict(cmd="stop"))
    seen = drain(q, lambda e: e["event"] == "record_finished")
    fin = seen[-1]
    assert [g["att"] for g in fin["summary"]] == [att0, att0 + 8], fin["summary"]
    path = Path(fin["csv"])
    assert path.name.startswith("record_SIM_bench") and not list(path.parent.glob("*.partial.csv"))
    text = path.read_text()
    assert "# summary_1: ATT" in text and "# mode: foxsinad record" in text
    rows = list(csv.DictReader(l for l in text.splitlines() if not l.startswith("#")))
    assert {int(r["att"]) for r in rows} == {att0, att0 + 8}
    drain(q, lambda e: e["event"] == "state" and e["state"]["mode"] == "idle")
    assert eng._fox_raw.mode == "OFF" and eng._fox_raw.att == 127
    eng.shutdown()


def test_record_flags_morse_id_windows_and_leaves_them_out():
    eng = Engine(sim=True, sim_speed=SPEED, runs_dir=tempfile.mkdtemp(),
                 sim_options=dict(pad_db=119.0, twelve_db_dbm=-123.7, seed=1, id_interval_s=20))
    q = eng.subscribe()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", call="N0CALL", radio="SIM"))
    assert eng.handle(dict(cmd="start", mode="record"))["ok"]
    drain(q, lambda e: e["event"] == "reading" and e.get("id_active"))
    drain(q, lambda e: e["event"] == "reading" and not e.get("id_active"))
    eng.handle(dict(cmd="stop"))
    fin = drain(q, lambda e: e["event"] == "record_finished")[-1]
    text = Path(fin["csv"]).read_text()
    windows = int(text.split("# windows: ")[1].split()[0])
    ids = int(text.split("# id_windows: ")[1].split()[0])
    assert ids > 0 and sum(g["n"] for g in fin["summary"]) <= windows - ids, (windows, ids)
    eng.shutdown()


def test_meter_with_the_picofox_reports_the_level_and_a_level_hint():
    eng, q = make()
    eng.handle(dict(cmd="connect"))
    eng.handle(dict(cmd="setup", call="N0CALL", pad_db=119.0))
    assert eng.handle(dict(cmd="start", mode="meter", att_db=16))["ok"]
    seen = drain(q, lambda e: e["event"] == "reading" and e.get("att") == 64)
    r = seen[-1]
    assert r["level_hint"] and r["level_ref"] == "antenna" and r["att_db"] == 16.0, r
    assert abs(r["level_dbm"] - (eng.cal.dbm(64) - 119.0)) < 0.01
    eng.handle(dict(cmd="stop"))
    drain(q, lambda e: e["event"] == "state" and e["state"]["mode"] == "idle")
    eng.shutdown()
