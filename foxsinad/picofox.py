"""Serial driver for the foxsinad_fw PicoFox firmware (see firmware/PROTOCOL.md).

Replies are synchronous: OK means the RF core applied the change. A reply
can be delayed ~10 s by an in-progress morse ID, hence the long timeout.
Async "*" event lines (boot banner, *ID START/DONE) are collected into
.events, never returned as replies.

CLI:
    python3 -m foxsinad.picofox --call KM6AM --freq 145.55 --att 64 \
        --tone 1000 3000          # start transmitting and exit
    python3 -m foxsinad.picofox --off       # stop (keys ID first)
    python3 -m foxsinad.picofox --status
"""

import glob
import time

import serial


class PicoFoxError(RuntimeError):
    """ERR reply from the firmware."""


class PicoFox:
    def __init__(self, port=None, timeout_s=20.0):
        if port is None:
            candidates = sorted(glob.glob("/dev/cu.usbmodem*"))
            if not candidates:
                raise PicoFoxError("no /dev/cu.usbmodem* device found")
            port = candidates[0]
        self.port = port
        self.timeout_s = timeout_s
        self.events = []
        self._ser = serial.Serial(port, 115200, timeout=0.5)
        time.sleep(0.3)
        self._ser.reset_input_buffer()

    def close(self):
        self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def cmd(self, line):
        """Sends one command, returns the OK reply text (sans 'OK ')."""
        self._ser.write((line + "\r\n").encode())
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            raw = self._ser.readline().decode(errors="replace").strip()
            if not raw:
                continue
            if raw.startswith("*"):
                self.events.append(raw)
                continue
            if raw.startswith("OK"):
                return raw[2:].strip()
            if raw.startswith("ERR"):
                raise PicoFoxError(f"{line!r}: {raw[3:].strip()}")
        raise PicoFoxError(f"{line!r}: timeout after {self.timeout_s} s")

    # -- commands ---------------------------------------------------------
    def hello(self):
        return self.cmd("HELLO")

    def set_call(self, callsign):
        return self.cmd(f"CALL {callsign}")

    def set_freq(self, mhz):
        return self.cmd(f"FREQ {mhz:.6f}")

    def set_att(self, att):
        """Attenuator 0-127 in 0.25 dB steps; RF blanks a few ms if on."""
        return self.cmd(f"ATT {int(att)}")

    def tone(self, tone_hz, dev_hz):
        """FM tone on: tone 100-2400 Hz, peak deviation 100-10000 Hz."""
        return self.cmd(f"TONE {int(tone_hz)} {int(dev_hz)}")

    def carrier(self):
        return self.cmd("CARRIER ON")

    def off(self):
        """RF off; keys the morse ID first if due, so may take ~10 s."""
        return self.cmd("OFF")

    def send_id(self):
        return self.cmd("ID")

    def status(self):
        """Returns the STATUS fields as a dict of strings."""
        reply = self.cmd("STATUS")  # "STATUS mode=... freq=... ..."
        return dict(kv.split("=", 1) for kv in reply.split()[1:])


def main():
    import argparse

    p = argparse.ArgumentParser(description="PicoFox test-source control")
    p.add_argument("--port", help="serial port (default: first /dev/cu.usbmodem*)")
    p.add_argument("--call", help="callsign (required before RF on)")
    p.add_argument("--freq", type=float, help="carrier MHz (144-148)")
    p.add_argument("--att", type=int, help="attenuator 0-127 (0.25 dB steps)")
    p.add_argument("--tone", nargs=2, type=int, metavar=("HZ", "DEV_HZ"),
                   help="FM tone: audio Hz and peak deviation Hz")
    p.add_argument("--carrier", action="store_true", help="unmodulated carrier on")
    p.add_argument("--off", action="store_true", help="RF off (keys ID first)")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    with PicoFox(args.port) as fox:
        if args.call:
            print("CALL", fox.cmd(f"CALL {args.call}"))
        if args.freq is not None:
            print("FREQ", fox.set_freq(args.freq))
        if args.att is not None:
            print("ATT", fox.set_att(args.att))
        if args.tone:
            print("TONE", fox.tone(*args.tone))
        if args.carrier:
            print("CARRIER", fox.carrier())
        if args.off:
            print("OFF", fox.off())
        if args.status or not any([args.call, args.freq is not None,
                                   args.att is not None, args.tone,
                                   args.carrier, args.off]):
            for k, v in fox.status().items():
                print(f"{k}={v}")
        for ev in fox.events:
            print(ev)


if __name__ == "__main__":
    main()
