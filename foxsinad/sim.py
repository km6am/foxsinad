"""A simulated bench: a PicoFox running foxsinad_fw 0.3.0, a step pad, and a
radio feeding the SINAD analyzer. For developing and demonstrating the UI
without hardware, and for tests.

SimFox follows the firmware's rules (docs/API.md in foxsinad-firmware):
RF needs a callsign or cabled mode, cabled mode changes only with RF off,
Morse IDs on schedule and at RF-off, and commands wait out an ID in
progress. SimAnalyzer turns the PicoFox's level into SINAD through a
model FM receiver. `speed` divides every delay, so tests run fast.
"""

import math
import random
import threading
import time

from .analyzer import Reading
from .picofox import PicoFoxError
from .sweep import CalTable

ID_INTERVAL_S = 540.0   # the firmware's 9 minutes
ID_SECONDS = 4.5        # measured for a 5-character callsign


class SimFox:
    """Same methods as foxctl.PicoFox."""

    def __init__(self, port="sim", speed=1.0, id_interval_s=ID_INTERVAL_S, version="0.3.0"):
        self.port = port
        self.speed = speed
        self.id_interval_s = id_interval_s
        self.version = version
        self.events = []
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.mode, self.freq, self.att = "OFF", 146.565, 127
        self.tone_hz, self.dev_hz = 1000, 3000
        self.call, self.cabled = "", False
        self._last_id = self._t0
        self._tx_since_id = False
        self._id_until = None

    # -- time, scaled -----------------------------------------------------
    def _now(self):
        return self._t0 + (time.monotonic() - self._t0) * self.speed

    def _sleep(self, s):
        time.sleep(s / self.speed)

    def id_active(self):
        self._tick()
        return self._id_until is not None

    def _tick(self):
        """Advances the automatic-ID schedule to the present."""
        now = self._now()
        if self._id_until is not None and now >= self._id_until:
            self._id_until = None
            self._last_id = now
            self._tx_since_id = False
            self.events.append("*ID DONE")
        if (self._id_until is None and self.mode != "OFF" and not self.cabled
                and now - self._last_id > self.id_interval_s):
            self._id_until = now + ID_SECONDS
            self.events.append("*ID START")

    def _wait_for_id(self):
        while self.id_active():
            self._sleep(0.05)

    def _send_id_now(self):
        self.events.append("*ID START")
        self._sleep(ID_SECONDS)
        self.events.append("*ID DONE")
        self._last_id = self._now()
        self._tx_since_id = False

    # -- context manager, like the real driver -----------------------------
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- commands -----------------------------------------------------------
    def cmd(self, line):
        raise PicoFoxError(f"{line!r}: raw commands aren't simulated")

    def hello(self):
        return f"FOXSINAD {self.version} REV3 (simulated)"

    def set_call(self, callsign):
        if not callsign or len(callsign) > 11:
            raise PicoFoxError(f"'CALL {callsign}': CALL <callsign> (1-11 chars)")
        self.call = callsign.upper()
        return f"CALL {self.call}"

    def set_cabled(self, on):
        with self._lock:
            if self.version < "0.3.0":
                raise PicoFoxError(f"'CABLED {'ON' if on else 'OFF'}': unknown command CABLED")
            if self.mode != "OFF":
                raise PicoFoxError(f"'CABLED {'ON' if on else 'OFF'}': turn RF off first")
            self.cabled = bool(on)
            return f"CABLED {'ON' if on else 'OFF'}"

    def set_freq(self, mhz):
        with self._lock:
            self._wait_for_id()
            self.freq = float(mhz)
            return f"FREQ {self.freq:.6f}"

    def set_att(self, att):
        if not 0 <= int(att) <= 127:
            raise PicoFoxError(f"ATT {att} outside 0-127")
        with self._lock:
            self._wait_for_id()
            self.att = int(att)
            return f"ATT {self.att}"

    def _rf_on(self, mode, tone_hz=0, dev_hz=0):
        if not self.call and not self.cabled:
            raise PicoFoxError(f"'{mode}': set CALL first, or CABLED ON")
        with self._lock:
            self._wait_for_id()
            if self.mode == "OFF":
                self._last_id = self._now()
            self.mode, self.tone_hz, self.dev_hz = mode, tone_hz, dev_hz
            self._tx_since_id = True

    def tone(self, tone_hz, dev_hz):
        self._rf_on("TONE", int(tone_hz), int(dev_hz))
        return f"TONE {int(tone_hz)} {int(dev_hz)}"

    def carrier(self):
        self._rf_on("CARRIER")
        return "CARRIER"

    def off(self, skip_id=False):
        with self._lock:
            self._wait_for_id()
            if self.mode != "OFF" and self._tx_since_id and not skip_id and not self.cabled:
                self._send_id_now()
            if skip_id or self.cabled:
                self._tx_since_id = False
            self.mode, self.tone_hz, self.dev_hz = "OFF", 0, 0
            return "OFF"

    def abort(self):
        self.off(skip_id=True)
        return "ABORT"

    def send_id(self):
        with self._lock:
            if self.mode == "OFF":
                raise PicoFoxError("'ID': RF is off")
            if not self.call:
                raise PicoFoxError("'ID': set CALL first")
            self._wait_for_id()
            self._send_id_now()
            return "ID"

    def status(self):
        self._tick()
        now = self._now()
        return dict(mode=self.mode, freq=f"{self.freq:.6f}", att=str(self.att),
                    tone=str(self.tone_hz), dev=str(self.dev_hz), call=self.call or "-",
                    rev="3", since_id=str(int(now - self._last_id)),
                    up=str(int(now - self._t0)), cabled="1" if self.cabled else "0")


def radio_sinad(level_dbm, twelve_db_dbm, ceiling_db=32.0):
    """SINAD of a model FM receiver: about 3 dB of SINAD per dB of signal
    near threshold, limited by the source's own ceiling (noise powers add).
    Same model as tests/test_sweep.py."""
    x = 12.0 + 3.0 * (level_dbm - twelve_db_dbm)
    n_rx = 1 / max(10 ** (x / 10) - 1, 1e-6)
    n_src = 1 / (10 ** (ceiling_db / 10) - 1)
    return 10 * math.log10(1 + 1 / (n_rx + n_src))


class SimAnalyzer:
    """Stands in for SinadAnalyzer: reads the SimFox's state, applies the
    pad and the model radio, and adds reading-to-reading scatter."""

    def __init__(self, fox, pad_db=119.0, twelve_db_dbm=-123.7, cal=None, speed=1.0,
                 scatter_db=0.3, seed=None):
        self.fox = fox
        self.pad_db = pad_db
        self.twelve_db_dbm = twelve_db_dbm
        self.cal = cal or CalTable()
        self.speed = speed
        self.scatter_db = scatter_db
        self.rng = random.Random(seed)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def level_dbm(self):
        return self.cal.dbm(self.fox.att) - self.pad_db

    def read(self, settle_s=0.5, avg_s=2.0):
        time.sleep((settle_s + avg_s) / self.speed)
        fox = self.fox
        if fox.id_active():
            s = 2.0 + self.rng.random() * 4            # Morse tone, not the test tone
        elif fox.mode == "TONE":
            s = radio_sinad(self.level_dbm(), self.twelve_db_dbm)
            # more scatter near threshold, as on the bench
            s += self.rng.gauss(0, self.scatter_db * (1 + 2 * math.exp(-abs(s - 12) / 4)))
        else:
            s = abs(self.rng.gauss(0.4, 0.3))            # carrier or nothing: no tone
        n = max(1, round(avg_s / 0.25))
        return Reading(sinad_db=max(0.0, s), std_db=self.scatter_db, n=n, n_discarded=0,
                       peak=0.25 if fox.mode != "OFF" else 0.08, under_range=False)
