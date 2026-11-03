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
import collections
import contextlib
import dataclasses
import json
import math
import queue
import statistics
import threading
import time
import traceback
from pathlib import Path

from .. import record, sweep
from ..analyzer import AUDIO_TARGET_DBFS, AUDIO_TOL_DB, CLIP_LEVEL, NoAudio, audio_hint, peak_dbfs
from ..ballpark import hint
from ..picofox import PicoFox, PicoFoxError, find_port, set_cabled
from ..sweep import CalTable, Stopped, SweepError, level_reference, read_clean

WATCHDOG_S = 10.0
STATUS_EVERY_S = 1.0
METER_AVG_S = 0.25       # one analyzer window per meter reading: fast enough to adjust by
METER_BOXCAR_S = 1.0     # the SINAD shown is the mean over this long
AUDIO_SET_S = 2.0        # this long with the tone's peak on target sets the audio level
# ... with the tone above the noise. 9 dB (12 dB SINAD less 3), so the volume
# can be set at the sweep's level: there, noise adds to the tone's peak by
# about a dB (estimated, not measured), well inside AUDIO_TOL_DB.
AUDIO_SET_MIN_SINAD_DB = 9.0
LOST_AFTER_POLLS = 3     # failed status polls in a row (while idle) before the PicoFox counts as gone
ID_INTERVAL_S = 540

SETUP_DEFAULTS = dict(
    radio="", condition="", session="", note="", pad_db=None, call="",
    freq=145.55, tone=1000, dev=3000, repeat=1, monitor=False,
    ballpark_att_db=16.0, audio_target_dbfs=AUDIO_TARGET_DBFS, record_att_db=None,
    cabled=False)

NUMERIC = dict(pad_db=(0.0, 200.0), freq=(144.02, 147.98), tone=(100, 2400),
               dev=(100, 10000), repeat=(1, 100), ballpark_att_db=(0.0, 31.75),
               audio_target_dbfs=(-30.0, -3.0), record_att_db=(0.0, 31.75))
INTEGER = {"tone", "dev", "repeat"}
QUARTER_DB = {"ballpark_att_db", "record_att_db"}      # the attenuator's 0.25 dB steps
OPTIONAL = {"pad_db", "record_att_db"}   # may be empty (None): not entered / automatic


class CommandError(ValueError):
    """A command the engine refuses; the message is shown to the user. When
    a setup field is the reason, field names it, so the page can go to it."""

    def __init__(self, msg, field=None):
        super().__init__(msg)
        self.field = field


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
        self._poll_failures = 0

        self.mode = "idle"
        self.worker = None
        self.stop_evt = threading.Event()
        self.results = []             # this engine session's sweep results
        self.records = []             # ... and its records
        self._record_att = queue.Queue()   # record: attenuator changes, applied between windows
        self.audio_set = None         # the tone's peak once the radio's volume was set
        self._start_att = None        # meter: the attenuator setting it was started with

        self._subs = []
        self._subs_lock = threading.Lock()
        self._no_ui_since = time.monotonic()
        self._alive = True
        self._restore_setup()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()

    # -- the Run setup, kept across restarts -----------------------------------
    # All of it (callsign, frequency, radio, ...) but cabled mode, which is
    # confirmed once per session: it means no Morse ID. A simulated bench
    # keeps nothing, so it starts the same every time.
    def _setup_path(self):
        return None if self.sim else self.runs_dir / "setup.json"

    def _restore_setup(self):
        self._saved_setup = dict(self.setup)
        path = self._setup_path()
        if path is None or not path.exists():
            return
        try:
            saved = json.loads(path.read_text())
            saved.pop("cabled", None)
            self.cmd_setup(**{k: v for k, v in saved.items() if k in SETUP_DEFAULTS})
        except Exception as e:      # a bad file costs the saved setup, nothing more
            self.log(f"saved setup not restored: {e}", "warn")

    def _save_setup(self):
        path = self._setup_path()
        if path is None or self.setup == self._saved_setup:
            return
        self._saved_setup = dict(self.setup)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.setup, indent=1))
        except OSError as e:
            self.log(f"setup not saved: {e}", "warn")

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
                    status=self.last_status, results=self.results, records=self.records,
                    session_sweeps=self.session_sweeps(),
                    cal=dict(freq_mhz=self.cal.freq_mhz, date=self.cal.date,
                             att=list(self.cal.att), dbm=list(self.cal.dbm_pts)))

    def session_sweeps(self):
        """Every finished sweep in the session's folder, oldest first, read
        from the CSV headers, so sweeps from before a restart (or from
        another run of the app) count. No session: none, since the runs
        folder itself holds every session-less run ever made."""
        if not self.setup["session"]:
            return []
        folder = self.runs_dir / sweep.slug(self.setup["session"])
        out = [_sweep_row(path, m, self.setup["session"]) for path, m in _finished_runs(folder)
               if m.get("mode") == "foxsinad sweep"]   # not records, nor anything else there
        return sorted(out, key=lambda r: r["date"] or "")

    def _emit_session_sweeps(self):
        self.emit("session_sweeps", session=self.setup["session"], sweeps=self.session_sweeps())

    def state(self):
        return dict(mode=self.mode, connected=self.fox is not None, sim=self.sim,
                    hello=self.hello, port=getattr(self._fox_raw, "port", None),
                    setup=self.setup, cabled_confirmed=self.cabled_confirmed,
                    audio_set=self.audio_set,
                    runs_dir=str(self.runs_dir))

    def _state_changed(self):
        self._save_setup()
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
            field = getattr(e, "field", None)
            return dict(ok=False, error=str(e), **({"field": field} if field else {}))
        except TypeError as e:
            return dict(ok=False, error=f"bad arguments for {name}: {e}")
        except Exception as e:            # never leave the UI without a reply
            self.log(f"{name} failed: {e}", "error")
            return dict(ok=False, error=f"{name} failed: {e}")

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
            raw = self._open_fox()
        self._fox_raw = raw
        self.fox = _Locked(raw, self.fox_lock)
        self.hello = self.fox.hello()
        self._events_seen = len(raw.events)
        self._poll_failures = 0
        self.log(f"PicoFox connected: {self.hello} on {raw.port}")
        # A known state: not transmitting (a crashed or killed app can leave
        # it on), and on the setup's frequency, so the page and the PicoFox agree.
        if self._fox_mode() != "OFF":
            self.log("the PicoFox was transmitting: RF off", "warn")
            self._rf_off_quietly()
        self.fox.set_freq(self.setup["freq"])
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
                raise CommandError(f"unknown setup field {k!r}", k)
            if k in NUMERIC:
                if v in (None, ""):
                    if k not in OPTIONAL:
                        raise CommandError(f"{k} is required", k)
                    v = None
                else:
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        raise CommandError(f"{k}: {v!r} is not a number", k)
                    lo, hi = NUMERIC[k]
                    if k in QUARTER_DB:
                        v = round(v * 4) / 4
                    if not (lo <= v <= hi) or (k in INTEGER and v != int(v)):
                        raise CommandError(f"{k} must be {'a whole number ' if k in INTEGER else ''}"
                                           f"from {lo:g} to {hi:g}", k)
                    v = int(v) if k in INTEGER else v
            elif k in ("monitor", "cabled"):
                v = bool(v)
            else:
                v = str(v).strip()
            new[k] = v
        if "cabled" in fields and fields["cabled"] != self.setup["cabled"]:
            self._require_idle("change cabled mode")
            if self.fox is not None and self._fox_mode() != "OFF":
                raise CommandError("turn RF off before changing cabled mode", "cabled")
        if new["cabled"] and not self.cabled_confirmed:
            raise CommandError("confirm cabled mode first (output into a load or test "
                               "equipment only, never an antenna)")
        retune = (new["freq"] != self.setup["freq"] and self.fox is not None
                  and self.mode != "sweep")        # a sweep holds its frequency
        if new["radio"] != self.setup["radio"]:
            self.audio_set = None             # another radio: its volume isn't set yet
        new_session = new["session"] != self.setup["session"]
        self.setup = new
        if retune:
            self.fox.set_freq(new["freq"])
            self._poll_once()
        self._state_changed()
        if new_session:
            self._emit_session_sweeps()

    def cmd_sessions(self):
        """Every session folder holding foxsinad runs, newest first, for the
        Sessions tab. Other folders in runs/ (other tools' runs) aren't listed."""
        out = []
        current = sweep.slug(self.setup["session"])
        for folder in sorted(self.runs_dir.iterdir()) if self.runs_dir.is_dir() else []:
            if not folder.is_dir() or folder.name.startswith((".", "_")):
                continue
            runs = [m for _, m in _finished_runs(folder)
                    if m.get("mode") in ("foxsinad sweep", "foxsinad record")]
            if not runs:
                continue
            dates = sorted(m["date"] for m in runs if m.get("date"))
            named = [m.get("session") for m in runs if _named(m.get("session"))]
            # the name as typed, if it still leads here; else the folder's own
            name = named[-1] if named and sweep.slug(named[-1]) == folder.name else folder.name
            out.append(dict(
                session=name, folder=folder.name, path=str(folder),
                sweeps=sum(m["mode"] == "foxsinad sweep" for m in runs),
                records=sum(m["mode"] == "foxsinad record" for m in runs),
                first=dates[0] if dates else None, last=dates[-1] if dates else None,
                radios=sorted({_named(m.get("radio")) for m in runs} - {""}),
                current=folder.name == current))
        return dict(sessions=sorted(out, key=lambda s: s["last"] or "", reverse=True))

    def cmd_delete_session(self, folder):
        """Deletes a session's runs: the CSVs and plots foxsinad made, then the
        folder if nothing else is left in it. Can't be undone; the page asks
        first. The session being deleted stops being the current one."""
        self._require_idle("delete a session")
        path = self.runs_dir / str(folder)
        # a plain name of a folder directly in runs/: never "..", nor a link out
        if (not folder or str(folder) != sweep.slug(folder) or str(folder).startswith((".", "_"))
                or not path.is_dir() or path.resolve().parent != self.runs_dir.resolve()):
            raise CommandError(f"no session folder {folder!r}")
        gone = 0
        for csv in sorted(path.glob("*.csv")):
            ours = csv.name.endswith(".partial.csv") or (
                sweep.read_header(csv).get("mode", "").startswith("foxsinad "))
            if not ours:
                continue
            for f in (csv, csv.with_suffix(".png")):
                if f.exists():
                    f.unlink()
                    gone += 1
        left = sorted(f.name for f in path.iterdir())
        if not left:
            path.rmdir()
        self.log(f"deleted session {folder}: {gone} files"
                 + (f"; kept {len(left)} that foxsinad didn't make" if left else ""))
        if sweep.slug(self.setup["session"]) == path.name:
            self.cmd_setup(session="")
        return dict(deleted=gone, kept=left, **self.cmd_sessions())

    def cmd_confirm_cabled(self, confirmed=True):
        """The UI's confirmation that the output goes only into a load or
        test equipment. Lasts for this engine session."""
        if not confirmed and self.setup["cabled"]:
            self._require_idle("leave cabled mode")
            self.setup["cabled"] = False
        self.cabled_confirmed = bool(confirmed)
        self._state_changed()

    def cmd_start(self, mode, att_db=None):
        """att_db (meter only): set the attenuator first, e.g. 0 to set the
        radio's volume on a strong signal. Without it, meter leaves it as set."""
        if mode not in ("sweep", "ballpark", "meter", "record"):
            raise CommandError(f"unknown mode {mode!r}")
        if att_db is not None and mode != "meter":
            raise CommandError(f"att_db is for meter, not {mode}")
        self._start_att = None if att_db is None else round(_number(att_db, 0, 31.75, "att_db") * 4)
        self._require_idle(f"start {mode}")
        if mode in ("sweep", "ballpark", "record") and self.fox is None:
            raise CommandError("connect the PicoFox first")
        if self.fox is not None and not self.setup["call"] and not self.setup["cabled"]:
            raise CommandError("enter a callsign, or use cabled mode", "call")
        if mode == "sweep" and self.setup["cabled"] and self.setup["pad_db"] is None:
            self.log("no external attenuation entered: levels are the PicoFox output, "
                     "not the level at the radio (ballpark helps find it)", "warn")
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
        if self.mode in ("ballpark", "record") and action != "att":
            raise CommandError(f"only the attenuator can change during {self.mode}")
        fox = self.fox
        if action == "att" and self.mode == "record":
            # the record loop sets it between windows, so each row's level is exact
            self._record_att.put(round(_number(value, 0, 31.75, "attenuation (dB)") * 4))
            return
        if action == "att":
            fox.set_att(round(_number(value, 0, 31.75, "attenuation (dB)") * 4))
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
            raise CommandError("enter a callsign, or use cabled mode", "call")

    def _rf_off_quietly(self):
        try:
            self.fox.off()
            self.fox.set_att(127)
            set_cabled(self.fox, False)
        except Exception as e:      # still report, never mask the original problem
            self.log(f"turning RF off failed: {e}", "error")

    def _open_fox(self):
        """The real PicoFox's driver, opened. The phone's engine overrides
        this to use Android's USB link instead of a serial port."""
        return PicoFox(self.fox_port or find_port())

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

    def _level(self, out_dbm):
        """The level for a PicoFox output, and what it means (radio, antenna
        or output; see sweep.level_reference)."""
        pad = self.setup["pad_db"] or 0.0
        ref = level_reference(self.setup["cabled"], self.setup["pad_db"])[0]
        return round(out_dbm - pad, 2), ref

    def _reading_event(self, r, **extra):
        # No SINAD (every window clipped) goes out as null: JSON has no NaN.
        pk, clipped = peak_dbfs(r.peak), r.peak >= CLIP_LEVEL
        self.emit("reading", sinad_db=_round(r.sinad_db, 2), std_db=_round(r.std_db, 3),
                  peak_dbfs=round(pk, 1), clipped=clipped, under_range=r.under_range,
                  audio_hint=audio_hint(pk, self.setup["audio_target_dbfs"], clipped,
                                        r.under_range),
                  n_discarded=r.n_discarded, **extra)

    # -- modes (worker thread) ------------------------------------------------
    def _run_mode(self, mode):
        try:
            if mode in ("sweep", "ballpark", "record") and self._fox_mode() != "OFF":
                self.log("turning RF off first (cabled mode and the callsign are set "
                         "with RF off)")
                self.fox.off()
            getattr(self, f"_mode_{mode}")()
        except Stopped:
            self.log(f"{mode} stopped")
        except (SweepError, PicoFoxError, CommandError, LookupError, NoAudio) as e:
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
        return argparse.Namespace(
            pad_db=s["pad_db"], label="run", session=s["session"], radio=s["radio"],
            condition=s["condition"], note=s["note"], call=s["call"], cabled=s["cabled"],
            freq=s["freq"], tone=s["tone"], dev=s["dev"],
            settle=sweep.SETTLE_S, avg_coarse=sweep.AVG_COARSE_S, avg_fine=sweep.AVG_FINE_S,
            audio_target_dbfs=s["audio_target_dbfs"],
            audio_peak_dbfs=self.audio_set and self.audio_set["peak_dbfs"],
            tail_db=3.0, device=self.audio_device,
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
                    on_start=lambda label: self.emit(
                        "run_started", label=label, index=i, of=n, pad_db=args.pad_db,
                        level_ref=level_reference(args.cabled, args.pad_db)[0]),
                    on_point=lambda row: self.emit("point", **_jsonable(row)),
                    log=self.log, stop=self.stop_evt)
                png = res.csv.with_suffix(".png")
                out = dict(label=res.label, csv=str(res.csv), png=str(png) if png.exists() else None,
                           point_dbm=_round(res.point_dbm, 2), sigma_db=_round(res.sigma_db, 3),
                           fit_points=res.fit_points, index=i, of=n,
                           audio_peak_dbfs=args.audio_peak_dbfs,
                           radio=self.setup["radio"], condition=self.setup["condition"],
                           session=self.setup["session"], pad_db=args.pad_db,
                           level_ref=res.level_ref)
                self.results.append(out)
                self.emit("run_finished", **out)
                self._emit_session_sweeps()

    def _record_level(self):
        """The attenuator setting record starts at, and why: as set in the
        setup, else nearest the last sweep's 12 dB point (as PicoFox output,
        so a changed pad_db doesn't move it), else the ballpark setting."""
        s = self.setup
        if s["record_att_db"] is not None:
            return round(s["record_att_db"] * 4), "as set"
        last = next((r for r in reversed(self.results) if r["point_dbm"] is not None), None)
        if last is not None:
            out = last["point_dbm"] + (last["pad_db"] or 0.0)
            att = min(range(128), key=lambda a: abs(self.cal.dbm(a) - out))
            return att, f"nearest the last sweep's 12 dB point ({last['point_dbm']:.2f} dBm)"
        return round(s["ballpark_att_db"] * 4), "the ballpark attenuation (no sweep yet)"

    def _mode_record(self):
        att, why = self._record_level()
        self.log(f"record: starting at {att * 0.25:.2f} dB (ATT {att}), {why}")
        while not self._record_att.empty():          # stale requests from an earlier run
            self._record_att.get_nowait()
        an, af_input = self._open_analyzer()
        args = self._sweep_args(af_input)
        box = collections.deque(maxlen=round(METER_BOXCAR_S / record.WINDOW_S))

        def on_reading(row, r):
            if math.isnan(r.sinad_db):
                box.clear()
            else:
                box.append(r.sinad_db)
            lvl, ref = self._level(row["gen_dbm"])
            self._reading_event(r, t_s=row["t_s"], att=row["att"], att_db=row["att"] * 0.25,
                                out_dbm=row["gen_dbm"], level_dbm=lvl, level_ref=ref,
                                id_active=bool(row["id_active"]),
                                boxcar_db=_round(statistics.fmean(box), 2) if box else None)

        with an:
            res = record.run(args, fox=self.fox, an=an, att=att, on_reading=on_reading,
                             log=self.log, stop=self.stop_evt, requests=self._record_att)
        out = dict(label=res.label, csv=str(res.csv), summary=res.summary,
                   level_ref=res.level_ref, radio=self.setup["radio"],
                   condition=self.setup["condition"], session=self.setup["session"],
                   pad_db=args.pad_db, why=why)
        self.records.append(out)
        self.emit("record_finished", **out)

    def _mode_ballpark(self):
        s = self.setup
        fox = self.fox
        an, _ = self._open_analyzer()
        with an:
            self._prepare_rf()
            fox.set_freq(s["freq"])
            fox.set_att(round(s["ballpark_att_db"] * 4))
            fox.tone(s["tone"], s["dev"])
            self._poll_once()
            while not self.stop_evt.is_set():
                r = read_clean(fox, an, settle_s=0.0, avg_s=1.0, log=self.log)
                att = _att_steps(fox.status())
                out = self.cal.dbm(att)
                lvl, ref = self._level(out)
                self._reading_event(r, hint=hint(r, stop="press Stop"), att=att,
                                    att_db=att * 0.25, out_dbm=round(out, 2),
                                    level_dbm=lvl, level_ref=ref)
        raise Stopped()

    def _mode_meter(self):
        """SINAD live. With the PicoFox connected it transmits the tone at the
        setup's frequency, at the attenuator as set; Stop turns RF off."""
        s = self.setup
        fox = self.fox
        an, _ = self._open_analyzer()
        with an:
            if fox is None:
                self.log("no PicoFox connected: measuring the radio's audio only")
            else:
                if self._fox_mode() == "OFF":
                    self._prepare_rf()
                fox.set_freq(s["freq"])
                if self._start_att is not None:
                    fox.set_att(self._start_att)
                fox.tone(s["tone"], s["dev"])
                self._poll_once()
            # A reading every window, showing the boxcar mean of the last
            # second's SINAD (in dB); the peak stays the window's own, for
            # setting the volume. A window with no SINAD (clipped) empties it.
            box = collections.deque(maxlen=round(METER_BOXCAR_S / METER_AVG_S))
            on_target = collections.deque(maxlen=round(AUDIO_SET_S / METER_AVG_S))
            while not self.stop_evt.is_set():
                r = an.read(settle_s=0.0, avg_s=METER_AVG_S)
                if fox is not None:
                    self._track_audio(r, on_target)
                if math.isnan(r.sinad_db):
                    box.clear()
                else:
                    box.append(r.sinad_db)
                    r = dataclasses.replace(r, sinad_db=statistics.fmean(box),
                                            std_db=statistics.pstdev(box))
                self._reading_event(r, **(self._meter_level(r) if fox is not None else {}))
        raise Stopped()

    def _meter_level(self, r):
        """With the PicoFox transmitting, a meter reading also says the level
        (from the last status poll) and, as level_hint, what ballpark would
        say about it - so one step sets the volume and the attenuation."""
        out = dict(level_hint=hint(r, stop="press Next"))
        if self.last_status:
            att = _att_steps(self.last_status)
            lvl, ref = self._level(self.cal.dbm(att))
            out.update(att=att, att_db=att * 0.25, out_dbm=round(self.cal.dbm(att), 2),
                       level_dbm=lvl, level_ref=ref)
        return out

    def _track_audio(self, r, on_target):
        """The radio's volume counts as set after AUDIO_SET_S of the tone's
        peak on target at full quieting; the peak goes with the next runs."""
        pk = peak_dbfs(r.peak)
        good = (r.peak < CLIP_LEVEL and not r.under_range and r.sinad_db >= AUDIO_SET_MIN_SINAD_DB
                and abs(pk - self.setup["audio_target_dbfs"]) <= AUDIO_TOL_DB)
        if not good:
            on_target.clear()
            return
        on_target.append(pk)
        if len(on_target) == on_target.maxlen:
            level = round(statistics.median(on_target), 1)
            if self.audio_set is None or abs(level - self.audio_set["peak_dbfs"]) >= 0.5:
                self.audio_set = dict(peak_dbfs=level, t=time.time())
                self._state_changed()

    # -- PicoFox status poll and the UI watchdog ------------------------------
    def _poll_once(self):
        if self.fox is None:
            return
        if not self.fox_lock.acquire(timeout=0.2):
            return                       # a long command (an ID) holds the PicoFox
        lost = False
        try:
            st = self._fox_raw.status()
            events = self._fox_raw.events[self._events_seen:]
            self._events_seen = len(self._fox_raw.events)
            self._poll_failures = 0
        except Exception as e:
            self._poll_failures += 1
            if self._poll_failures == 1:
                self.log(f"PicoFox status failed: {e}", "error")
            lost = self._poll_failures >= LOST_AFTER_POLLS and self.mode == "idle"
            if lost:
                with contextlib.suppress(Exception):
                    self._fox_raw.close()
                self.fox = self._fox_raw = self.hello = self.last_status = None
        finally:
            self.fox_lock.release()
        if self._poll_failures:
            if lost:
                self.log("lost the PicoFox (unplugged?): disconnected. Connect when it's back.",
                         "error")
                self.emit("status", status=None)
                self._state_changed()
            return
        for ev in events:
            self.emit("fox_event", text=ev)
            if ev == "*TIMEOUT":
                self.log("the PicoFox turned RF off: no command within its host timeout "
                         f"({st.get('timeout', '?')} s)", "warn")
        st = dict(st)
        att = _att_steps(st)
        st["att_db"] = att * 0.25
        st["out_dbm"] = round(self.cal.dbm(att), 2)
        st["level_dbm"], st["level_ref"] = self._level(st["out_dbm"])
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


def _att_steps(status):
    """The attenuator setting in 0.25 dB steps; boards without the attenuator
    (the r2) report '-', which counts as the maximum."""
    att = str(status.get("att", "127"))
    return int(att) if att.isdigit() else 127


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
    """For JSON, which has no NaN: a missing or non-finite value is null."""
    return None if v is None or not math.isfinite(v) else round(v, n)


def _finished_runs(folder):
    """(path, header) for each finished CSV in a folder, by name. Partial
    CSVs (a run still going, or cut off) have no header yet: skipped."""
    for path in sorted(folder.glob("*.csv")) if folder.is_dir() else []:
        if path.name.endswith(".partial.csv"):
            continue
        try:
            yield path, sweep.read_header(path)
        except OSError:
            continue


def _sweep_row(path, m, session):
    """One finished sweep, from its CSV header, as the Finish table shows it."""
    png = path.with_suffix(".png")
    return dict(
        label=m.get("label", path.stem), csv=str(path),
        png=str(png) if png.exists() else None, date=m.get("date"),
        point_dbm=_num(m.get("sinad12_dbm")), sigma_db=_num(m.get("sinad12_sigma_db")),
        fit_points=None if _num(m.get("sinad12_fit_points")) is None
        else int(_num(m.get("sinad12_fit_points"))),
        audio_peak_dbfs=_num(m.get("audio_peak_dbfs")),
        radio=_named(m.get("radio")), condition=_named(m.get("condition")),
        session=session, pad_db=-(_num(m.get("offset_db")) or 0.0),
        level_ref=next((k for k, v in sweep.LEVEL_REF_HEADER.items()
                        if v == m.get("level_ref")), "output"))


def _num(v):
    """A header value as a number; missing, blank or None is None."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _named(v):
    """A header name; "(none)" (written for an empty field) is empty."""
    return "" if v in (None, "(none)") else v


def _jsonable(row):
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else
                bool(v) if isinstance(v, bool) else v) for k, v in row.items()}
