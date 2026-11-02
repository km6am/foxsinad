# UI ↔ engine protocol

The UI (`foxsinad/app/static/`) and the measurement engine
(`foxsinad/app/engine.py`) exchange JSON messages only. This is the
contract: an Android engine (Kotlin) implements the same messages, and the
UI runs unchanged in a WebView.

## Transports

| | Commands (UI → engine) | Events (engine → UI) |
|---|---|---|
| Desktop (`python -m foxsinad.app`) | `POST /cmd`, body = one command, reply = JSON | `GET /events`, server-sent events, one JSON event per `data:` line |
| Android (planned) | `window.FoxsinadBridge.send(json)` returns the reply JSON | the app calls `window.foxsinadEvent(json)` |

The desktop server listens on 127.0.0.1 only, checks the `Host` header,
and only accepts `application/json` POSTs from its own origin.

## Commands

Each command is an object with `cmd` and its arguments. The reply is
`{"ok": true, ...}` or `{"ok": false, "error": "<message for the user>"}`.
Commands that change the PicoFox reply once the change has been made.

| `cmd` | Arguments | Does |
|---|---|---|
| `snapshot` | | replies with `snapshot` (as the event below) |
| `connect` | | opens the PicoFox (or the simulated one); replies `hello` |
| `disconnect` | | RF off, closes the PicoFox. Idle only. |
| `setup` | any of the setup fields | updates the run setup; validates each field |
| `confirm_cabled` | `confirmed`: bool | the operator's confirmation that the output goes only into a load or test equipment; lasts for this engine session. `false` also leaves cabled mode (idle only). |
| `start` | `mode`: `sweep`, `ballpark`, `meter` | starts a mode. Idle only. Sweep and ballpark need a connected PicoFox and a callsign or cabled mode; sweep also needs `pad_db`. |
| `stop` | | ends the running mode; RF off. When idle, turns RF off if it's on. |
| `fox` | `action`, `value`, `dev` | manual control: `att` (value in dB, 0–31.75, rounded to 0.25), `freq` (MHz), `tone` (value = tone Hz, dev = Hz), `carrier`, `off`, `id`. Refused during a sweep; only `att` during ballpark. |

**Setup fields** (`state.setup`): `radio`, `condition`, `session`, `note`
(text); `pad_db` (0–200, or empty), `freq` (144.02–147.98 MHz), `tone`
(100–2400 Hz), `dev` (100–10000 Hz), `repeat` (1–100), `ballpark_att_db`
(0–31.75 dB, rounded to 0.25); `monitor`, `cabled` (bool).
`cabled: true` needs `confirm_cabled` first, and can only change while
idle with RF off. `pad_db` is required for a cabled sweep only.

**Levels.** Every level comes with `level_ref`, saying what it is:
`radio` (cabled: the level at the radio, output minus `pad_db`),
`antenna` (over the air with `pad_db` set: into the transmit antenna) or
`output` (over the air: the PicoFox output). Over the air the path loss
isn't known, so the level at the radio is never claimed.

## Events

Every event has `event` and `t` (Unix time, seconds).

| `event` | Fields | When |
|---|---|---|
| `snapshot` | `state`, `status`, `results`, `cal` | first event on every connection |
| `state` | `state` | mode, connection or setup changed |
| `status` | `status` (or null) | PicoFox state, about once a second |
| `run_started` | `label`, `index`, `of`, `pad_db`, `level_ref` | a sweep begins (`index` of `of` repeats) |
| `point` | `phase` (`coarse`/`fine`), `att`, `gen_dbm`, `level_dbm`, `level_uv`, `sinad_db`, `sinad_sd`, `n`, `n_discarded`, `time`, `clipped` | one sweep reading |
| `run_finished` | `label`, `csv`, `png`, `point_dbm`, `sigma_db`, `fit_points`, `index`, `of`, `radio`, `condition`, `session`, `pad_db`, `level_ref` | a sweep's result (`point_dbm` null if no 12 dB crossing) |
| `reading` | `sinad_db`, `std_db`, `peak_dbfs`, `clipped`, `under_range`, `n_discarded`; ballpark adds `hint`, `att` (steps), `att_db`, `out_dbm`, `level_dbm`, `level_ref` | one 1 s reading in meter or ballpark |
| `fox_event` | `text` (`*ID START`, `*ID DONE`) | a Morse ID started or ended |
| `log` | `level` (`info`, `warn`, `error`), `msg` | anything worth showing |

**`state`**: `mode` (`idle`, `sweep`, `ballpark`, `meter`), `connected`,
`sim`, `hello` (the PicoFox's `*IDN?`), `port`, `setup`,
`cabled_confirmed`, `runs_dir`.

**`status`**: the driver's fields as strings (`mode` OFF/CARRIER/TONE,
`freq` MHz, `att` steps, `tone`, `dev`, `call`, `rev`, `since_id`, `up`,
`cabled`, `id_active`), plus `att_db`, `out_dbm` (from the cal table),
`level_dbm` with `level_ref`, and `next_id_s` (while transmitting, not
cabled).

**`cal`**: `freq_mhz`, `date`, and the cal table as `att` / `dbm` arrays,
so the UI can show levels for an attenuator setting before sending it.

## Safety rules the engine enforces

- Stop, shutdown, and no UI connected for 10 s turn RF off.
- Manual controls are refused during a sweep; only the attenuator during
  ballpark.
- RF never turns on without a callsign unless cabled mode was confirmed.
- One lock serializes every PicoFox command.
