"""The measurement engine behind the UI.

It owns the PicoFox and the audio input, runs one mode at a time on a
worker thread, takes commands as dicts and publishes events as dicts. The
message set is specified in docs/ui-protocol.md; a later Android engine
implements the same messages, so the UI doesn't change.

Safety rules (docs/plans/2026-10-25-ui-design.md):
- stop, shutdown, and losing every UI connection for WATCHDOG_S all turn
  RF off;
- manual PicoFox controls are refused during a sweep (only ATT is allowed
  during ballpark);
- RF never turns on without a callsign unless cabled mode was confirmed;
- every PicoFox command goes through one lock.
"""

import argparse
import math
import queue
import threading
import time
import traceback
from pathlib import Path

from .. import sweep
from ..analyzer import CLIP_LEVEL, peak_dbfs
from ..ballpark import hint
from ..picofox import PicoFox, PicoFoxError, find_port, set_cabled
from ..sweep import CalTable, Stopped, SweepError, read_clean

WATCHDOG_S = 10.0
STATUS_EVERY_S = 1.0
ID_INTERVAL_S = 540

SETUP_DEFAULTS = dict(
    radio="", condition="", session="", note="", pad_db=None, call="",
    freq=145.55, tone=1000, dev=3000, repeat=1, quick=False, monitor=False,
    ballpark_att=64, cabled=False)

NUMERIC = dict(pad_db=(0.0, 200.0), freq=(144.02, 147.98), tone=(100, 2400),
               dev=(100, 10000), repeat=(1, 100), ballpark_att=(0, 127))
INTEGER = {"tone", "dev", "repeat", "ballpark_att"}


class CommandError(ValueError):
    """A command the engine refuses; the message is shown to the user."""


class _Locked:
    """Every method call on the wrapped driver holds the shared lock."""

    def __init__(self, obj, lock):
        self._obj, self._lock = obj, lock

    def __getattr__(self, name):
        attr = getattr(self._obj, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return call


class Engine:
    def __init__(self, sim=False, sim_speed=1.0, fox_port=None, audio_device=None,
                 runs_dir=None, watchdog_s=WATCHDOG_S, sim_options=None):
        self.sim = sim
        self.sim_speed = sim_speed
        self.sim_options = sim_options or {}
        self.fox_port = fox_port
        self.audio_device = audio_device
        self.runs_dir = Path(runs_dir) if runs_dir else (
            sweep.RUNS / "_sim" if sim else sweep.RUNS)
        self.watchdog_s = watchdog_s
        self.cal = CalTable()

        self.setup = dict(SETUP_DEFAULTS)
        if sim:
            self.setup.update(pad_db=119.0, call="N0CALL", radio="SIM", condition="bench")
        self.cabled_confirmed = False

        self._fox_raw = None
        self.fox = None
        self.fox_lock = threading.RLock()
        self.hello = None
        self.last_status = None
        self._events_seen = 0

        self.mode = "idle"
        self.worker = None
        self.stop_evt = threading.Event()
        self.results = []             # this engine session's sweep results

        self._subs = []
        self._subs_lock = threading.Lock()
        self._no_ui_since = time.monotonic()
        self._alive = True
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    # -- events -------------------------------------------------------------
    def subscribe(self):
        q = queue.Queue()
        with self._subs_lock:
            self._subs.append(q)
        q.put(self.snapshot())
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)
            if not self._subs:
                self._no_ui_since = time.monotonic()

    def emit(self, event, **data):
        msg = dict(event=event, t=time.time(), **data)
        with self._subs_lock:
            for q in self._subs:
                q.put(msg)
        return msg

    def log(self, msg, level="info"):
        self.emit("log", level=level, msg=str(msg).strip("\n"))

    def snapshot(self):
        return dict(event="snapshot", t=time.time(), state=self.state(),
                    status=self.last_status, results=self.results,
                    cal=dict(freq_mhz=self.cal.freq_mhz, date=self.cal.date,
                             att=list(self.cal.att), dbm=list(self.cal.dbm_pts)))

    def state(self):
        return dict(mode=self.mode, connected=self.fox is not None, sim=self.sim,
                    hello=self.hello, port=getattr(self._fox_raw, "port", None),
                    setup=self.setup, cabled_confirmed=self.cabled_confirmed,
                    runs_dir=str(self.runs_dir))

    def _state_changed(self):
        self.emit("state", state=self.state())

    # -- commands -----------------------------------------------------------
    def handle(self, cmd):
        """Runs one command dict; returns a reply dict. Never raises."""
        name = cmd.get("cmd")
        fn = getattr(self, f"cmd_{name}", None) if isinstance(name, str) else None
        if fn is None:
            return dict(ok=False, error=f"unknown command {name!r}")
        try:
            out = fn(**{k: v for k, v in cmd.items() if k != "cmd"})
            return dict(ok=True, **(out or {}))
        except (CommandError, PicoFoxError, SweepError, LookupError) as e:
            self.log(e, "error")
            return dict(ok=False, error=str(e))
        except TypeError as e:
            return dict(ok=False, error=f"bad arguments for {name}: {e}")

    def cmd_snapshot(self):
        return dict(snapshot=self.snapshot())

    def cmd_connect(self):
        if self.fox is not None:
            return dict(hello=self.hello)
        if self.sim:
            from ..sim import SimFox
            raw = SimFox(speed=self.sim_speed,
                         **{k: v for k, v in self.sim_options.items() if k in ("id_interval_s", "version")})
        else:
            raw = PicoFox(self.fox_port or find_port())
        self._fox_raw = raw
        self.fox = _Locked(raw, self.fox_lock)
        self.hello = self.fox.hello()
        self._events_seen = len(raw.events)
        self.log(f"PicoFox connected: {self.hello} on {raw.port}")
        self._state_changed()
        self._poll_once()
        return dict(hello=self.hello)

    def cmd_disconnect(self):
        self._require_idle("disconnect")
        if self.fox is not None:
            self._rf_off_quietly()
            with self.fox_lock:
                self._fox_raw.close()
        self.fox = self._fox_raw = self.hello = self.last_status = None
        self.emit("status", status=None)
        self._state_changed()

    def cmd_setup(self, **fields):
        new = dict(self.setup)
        for k, v in fields.items():
            if k not in SETUP_DEFAULTS:
                raise CommandError(f"unknown setup field {k!r}")
            if k in NUMERIC:
                if v in (None, ""):
                    if k != "pad_db":
                        raise CommandError(f"{k} is required")
                    v = None
                else:
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        raise CommandError(f"{k}: {v!r} is not a number")
                    lo, hi = NUMERIC[k]
                    if not (lo <= v <= hi) or (k in INTEGER and v != int(v)):
                        raise CommandError(f"{k} must be {'a whole number ' if k in INTEGER else ''}"
                                           f"from {lo:g} to {hi:g}")
                    v = int(v) if k in INTEGER else v
            elif k in ("quick", "monitor", "cabled"):
                v = bool(v)
            else:
                v = str(v).strip()
            new[k] = v
        if "cabled" in fields and fields["cabled"] != self.setup["cabled"]:
            self._require_idle("change cabled mode")
            if self.fox is not None and self._fox_mode() != "OFF":
                raise CommandError("turn RF off before changing cabled mode")
        if new["cabled"] and not self.cabled_confirmed:
            raise CommandError("confirm cabled mode first (output into a load or test "
                               "equipment only, never an antenna)")
        self.setup = new
        self._state_changed()

    def cmd_confirm_cabled(self, confirmed=True):
        """The UI's confirmation that the output goes only into a load or
        test equipment. Lasts for this engine session."""
        self.cabled_confirmed = bool(confirmed)
        if not confirmed and self.setup["cabled"]:
            self._require_idle("leave cabled mode")
            self.setup["cabled"] = False
        self._state_changed()

    def cmd_start(self, mode):
        if mode not in ("sweep", "ballpark", "meter"):
            raise CommandError(f"unknown mode {mode!r}")
        self._require_idle(f"start {mode}")
        if mode in ("sweep", "ballpark"):
            if self.fox is None:
                raise CommandError("connect the PicoFox first")
            if not self.setup["call"] and not self.setup["cabled"]:
                raise CommandError("enter a callsign, or use cabled mode")
        if mode == "sweep" and self.setup["pad_db"] is None:
            raise CommandError("enter the total external attenuation (pad) first; "
                               "Ballpark helps find it")
        self.stop_evt.clear()
        self.mode = mode
        self.worker = threading.Thread(target=self._run_mode, args=(mode,), daemon=True)
        self.worker.start()
        self._state_changed()

    def cmd_stop(self):
        if self.mode == "idle":
            if self.fox is not None and self._fox_mode() != "OFF":
                self._rf_off_quietly()
            return
        self.stop_evt.set()
        self.log(f"stopping {self.mode} ...")

    def cmd_fox(self, action, value=None, dev=None):
        if self.fox is None:
            raise CommandError("connect the PicoFox first")
        if self.mode == "sweep":
            raise CommandError("manual controls are locked during a sweep; press Stop first")
        if self.mode == "ballpark" and action != "att":
            raise CommandError("only the attenuator can change during ballpark")
        fox = self.fox
        if action == "att":
            fox.set_att(_whole(value, 0, 127, "ATT"))
        elif action == "freq":
            fox.set_freq(_number(value, 144.02, 147.98, "frequency"))
            self.setup["freq"] = float(value)
            self._state_changed()
        elif action in ("tone", "carrier"):
            if self._fox_mode() == "OFF":
                self._prepare_rf()
            if action == "tone":
                fox.tone(_whole(value, 100, 2400, "tone"), _whole(dev, 100, 10000, "deviation"))
            else:
                fox.carrier()
        elif action == "off":
            self.log("RF off" + ("" if self.setup["cabled"] else " (Morse ID first if due)"))
            fox.off()
            set_cabled(fox, False)
        elif action == "abort":
            fox.abort()
            set_cabled(fox, False)
        elif action == "id":
            fox.send_id()
        else:
            raise CommandError(f"unknown PicoFox action {action!r}")
        self._poll_once()

    # -- helpers --------------------------------------------------------------
    def _require_idle(self, what):
        if self.mode != "idle":
            raise CommandError(f"can't {what} while {self.mode} is running; press Stop first")

    def _fox_mode(self):
        st = self.fox.status()
        return st.get("mode", "OFF")

    def _prepare_rf(self):
        """Callsign and cabled mode, set before RF turns on from off."""
        set_cabled(self.fox, self.setup["cabled"])
        if self.setup["call"]:
            self.fox.set_call(self.setup["call"])
        elif not self.setup["cabled"]:
            raise CommandError("enter a callsign, or use cabled mode")

    def _rf_off_quietly(self):
        try:
            self.fox.off()
            self.fox.set_att(127)
            set_cabled(self.fox, False)
        except Exception as e:      # still report, never mask the original problem
            self.log(f"turning RF off failed: {e}", "error")

    def _open_analyzer(self):
        if self.sim:
            from ..sim import SimAnalyzer
            opts = {k: v for k, v in self.sim_options.items()
                    if k in ("pad_db", "twelve_db_dbm", "seed")}
            return SimAnalyzer(self._fox_raw, cal=self.cal, speed=self.sim_speed, **opts), \
                "(simulated)"
        from ..analyzer import SinadAnalyzer, pick_device, pick_monitor
        dev = pick_device(self.audio_device)
        mon = pick_monitor("default", dev) if self.setup["monitor"] else None
        return SinadAnalyzer(dev, monitor=mon), f"audio device {dev}"

    def _reading_event(self, r, **extra):
        self.emit("reading", sinad_db=round(r.sinad_db, 2), std_db=round(r.std_db, 3),
                  peak_dbfs=round(peak_dbfs(r.peak), 1), clipped=r.peak >= CLIP_LEVEL,
                  under_range=r.under_range, n_discarded=r.n_discarded, **extra)

    # -- modes (worker thread) ------------------------------------------------
    def _run_mode(self, mode):
        try:
            if mode in ("sweep", "ballpark") and self._fox_mode() != "OFF":
                self.log("turning RF off first (cabled mode and the callsign are set "
                         "with RF off)")
                self.fox.off()
            getattr(self, f"_mode_{mode}")()
        except Stopped:
            self.log(f"{mode} stopped")
        except (SweepError, PicoFoxError, CommandError, LookupError) as e:
            self.log(e, "error")
        except Exception as e:
            self.log(f"{mode} failed: {e}\n{traceback.format_exc()}", "error")
        finally:
            if self.fox is not None:
                self._rf_off_quietly()
            self.mode = "idle"
            self._state_changed()
            self._poll_once()

    def _sweep_args(self, af_input):
        s = self.setup
        quick = s["quick"]
        return argparse.Namespace(
            pad_db=s["pad_db"], label="run", session=s["session"], radio=s["radio"],
            condition=s["condition"], note=s["note"], call=s["call"], cabled=s["cabled"],
            freq=s["freq"], tone=s["tone"], dev=s["dev"],
            settle=0.5 if quick else 1.0, avg_coarse=0.75 if quick else 1.0,
            avg_fine=1.5 if quick else 3.0, tail_db=3.0, device=self.audio_device,
            port=None, cal=str(self.cal.path), monitor=False, live=False,
            runs_dir=self.runs_dir, af_input=af_input)

    def _mode_sweep(self):
        n = self.setup["repeat"]
        an, af_input = self._open_analyzer()
        with an:
            for i in range(1, n + 1):
                if self.stop_evt.is_set():
                    raise Stopped()
                args = self._sweep_args(af_input)
                res = sweep.run(
                    args, fox=self.fox, an=an,
                    on_start=lambda label: self.emit("run_started", label=label, index=i, of=n,
                                                     pad_db=args.pad_db),
                    on_point=lambda row: self.emit("point", **_jsonable(row)),
                    log=self.log, stop=self.stop_evt)
                out = dict(label=res.label, csv=str(res.csv), png=str(res.csv.with_suffix(".png")),
                           point_dbm=_round(res.point_dbm, 2), sigma_db=_round(res.sigma_db, 3),
                           fit_points=res.fit_points, index=i, of=n,
                           radio=self.setup["radio"], condition=self.setup["condition"],
                           session=self.setup["session"], pad_db=args.pad_db)
                self.results.append(out)
                self.emit("run_finished", **out)

    def _mode_ballpark(self):
        s = self.setup
        fox = self.fox
        an, _ = self._open_analyzer()
        with an:
            self._prepare_rf()
            fox.set_freq(s["freq"])
            fox.set_att(s["ballpark_att"])
            fox.tone(s["tone"], s["dev"])
            self._poll_once()
            while not self.stop_evt.is_set():
                r = read_clean(fox, an, settle_s=0.0, avg_s=1.0, log=self.log)
                att = int(fox.status()["att"])
                out = self.cal.dbm(att)
                lvl = None if s["pad_db"] is None else round(out - s["pad_db"], 2)
                self._reading_event(r, hint=hint(r), att=att, out_dbm=round(out, 2),
                                    level_dbm=lvl)
        raise Stopped()

    def _mode_meter(self):
        an, _ = self._open_analyzer()
        with an:
            while not self.stop_evt.is_set():
                r = an.read(settle_s=0.0, avg_s=1.0)
                self._reading_event(r)
        raise Stopped()

    # -- PicoFox status poll and the UI watchdog ------------------------------
    def _poll_once(self):
        if self.fox is None:
            return
        if not self.fox_lock.acquire(timeout=0.2):
            return                       # a long command (an ID) holds the PicoFox
        try:
            st = self._fox_raw.status()
            events = self._fox_raw.events[self._events_seen:]
            self._events_seen = len(self._fox_raw.events)
        except Exception as e:
            self.log(f"PicoFox status failed: {e}", "error")
            return
        finally:
            self.fox_lock.release()
        for ev in events:
            self.emit("fox_event", text=ev)
        st = dict(st)
        att = int(st.get("att", 127))
        st["out_dbm"] = round(self.cal.dbm(att), 2)
        if self.setup["pad_db"] is not None:
            st["level_dbm"] = round(st["out_dbm"] - self.setup["pad_db"], 2)
        ids = [e for e in self._fox_raw.events if e.startswith("*ID")]
        st["id_active"] = bool(ids) and ids[-1].startswith("*ID START")
        if st.get("mode") != "OFF" and st.get("cabled") != "1":
            st["next_id_s"] = max(0, ID_INTERVAL_S - int(st.get("since_id", 0)))
        self.last_status = st
        self.emit("status", status=st)

    def _poll_loop(self):
        while self._alive:
            time.sleep(STATUS_EVERY_S)
            try:
                self._poll_once()
                self._watchdog()
            except Exception as e:
                self.log(f"background check failed: {e}", "error")

    def _watchdog(self):
        with self._subs_lock:
            if self._subs:
                return
            idle_for = time.monotonic() - self._no_ui_since
        if idle_for < self.watchdog_s:
            return
        if self.mode != "idle" and not self.stop_evt.is_set():
            self.stop_evt.set()
            self.log(f"no UI connected for {self.watchdog_s:g} s: stopping {self.mode}", "warn")
        elif self.mode == "idle" and self.last_status and self.last_status.get("mode") != "OFF":
            self.log(f"no UI connected for {self.watchdog_s:g} s: RF off", "warn")
            self._rf_off_quietly()
            self._poll_once()

    def shutdown(self):
        """Stops any mode and leaves RF off. Called on server exit."""
        self.stop_evt.set()
        if self.worker is not None:
            self.worker.join(timeout=30)
        if self.fox is not None:
            self._rf_off_quietly()
            with self.fox_lock:
                self._fox_raw.close()
        self._alive = False


def _number(v, lo, hi, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CommandError(f"{what}: {v!r} is not a number")
    if not lo <= x <= hi:
        raise CommandError(f"{what} must be from {lo:g} to {hi:g}")
    return x


def _whole(v, lo, hi, what):
    x = _number(v, lo, hi, what)
    if x != int(x):
        raise CommandError(f"{what} must be a whole number")
    return int(x)


def _round(v, n):
    return None if v is None else round(v, n)


def _jsonable(row):
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else
                bool(v) if isinstance(v, bool) else v) for k, v in row.items()}
