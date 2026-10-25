"""The desktop transport: real HTTP against the engine on the simulated bench.

Runnable directly: ~/miniconda3/envs/sota-rf/bin/python tests/test_server.py
"""

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foxsinad.app.engine import Engine  # noqa: E402
from foxsinad.app.server import make_server  # noqa: E402


def start():
    eng = Engine(sim=True, sim_speed=200, runs_dir=tempfile.mkdtemp())
    srv = make_server(eng, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return eng, srv, srv.server_address[1]


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, r.read(), r.getheader("Content-Type")


def test_serves_the_ui():
    eng, srv, port = start()
    for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")):
        status, body, ctype = request(port, "GET", path)
        assert status == 200 and ctype.startswith(kind) and body, path
    assert request(port, "GET", "/nothing")[0] == 404
    srv.shutdown(); eng.shutdown()
    print("PASS test_serves_the_ui")


def test_commands_round_trip():
    eng, srv, port = start()
    hdr = {"Content-Type": "application/json"}
    status, body, _ = request(port, "POST", "/cmd", json.dumps({"cmd": "connect"}), hdr)
    assert status == 200 and json.loads(body)["ok"]
    status, body, _ = request(port, "POST", "/cmd", json.dumps({"cmd": "setup", "freq": 150}), hdr)
    assert json.loads(body)["ok"] is False and "freq" in json.loads(body)["error"]
    srv.shutdown(); eng.shutdown()
    print("PASS test_commands_round_trip")


def test_refuses_other_hosts_and_origins():
    eng, srv, port = start()
    hdr = {"Content-Type": "application/json"}
    assert request(port, "GET", "/", headers={"Host": "evil.example:80"})[0] == 403
    assert request(port, "POST", "/cmd", json.dumps({"cmd": "connect"}),
                   {**hdr, "Origin": "http://evil.example"})[0] == 403
    assert request(port, "POST", "/cmd", "cmd=connect",
                   {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert eng.fox is None, "a refused request must not reach the engine"
    srv.shutdown(); eng.shutdown()
    print("PASS test_refuses_other_hosts_and_origins")


def test_events_stream_starts_with_a_snapshot():
    eng, srv, port = start()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    assert r.getheader("Content-Type") == "text/event-stream"
    line = r.fp.readline().decode()
    assert line.startswith("data: ")
    first = json.loads(line[6:])
    assert first["event"] == "snapshot" and "state" in first and "cal" in first
    json.dumps(first)                      # everything in it is plain JSON
    c.close()
    srv.shutdown(); eng.shutdown()
    print("PASS test_events_stream_starts_with_a_snapshot")


if __name__ == "__main__":
    test_serves_the_ui()
    test_commands_round_trip()
    test_refuses_other_hosts_and_origins()
    test_events_stream_starts_with_a_snapshot()
    print("ALL PASS")
