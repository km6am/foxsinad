"""foxsinad bench UI: starts the engine and opens the UI in a browser.

    python -m foxsinad.app            # the real PicoFox and USB audio adapter
    python -m foxsinad.app --sim      # a simulated bench, no hardware needed

Ctrl-C stops any run, turns RF off, and exits.
"""

import argparse
import sys
import threading
import webbrowser

from .engine import Engine
from .server import make_server


def main():
    p = argparse.ArgumentParser(description="foxsinad bench UI")
    p.add_argument("--sim", action="store_true", help="simulated PicoFox and radio")
    p.add_argument("--sim-speed", type=float, default=1.0,
                   help="with --sim: run the simulated bench this many times faster")
    p.add_argument("--http-port", type=int, default=8735, help="local web port (default 8735)")
    p.add_argument("--fox-port", help="PicoFox serial port (default: auto-detect)")
    p.add_argument("--device", help="audio input (default: auto-detect the USB adapter)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = p.parse_args()

    engine = Engine(sim=args.sim, sim_speed=args.sim_speed, fox_port=args.fox_port,
                    audio_device=args.device)
    try:
        server = make_server(engine, args.http_port)
    except OSError as e:
        engine.shutdown()
        sys.exit(f"error: can't listen on 127.0.0.1:{args.http_port} ({e}). "
                 "Is the UI already running? Otherwise pick another --http-port.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"foxsinad UI{' (simulated bench)' if args.sim else ''}: {url}")
    print("Ctrl-C to stop (RF is turned off on the way out).")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        engine.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
