# foxsinad: working notes for Claude

`README.md` says what foxsinad is and how to run it; `docs/ui-protocol.md`
is the UI ↔ engine contract; `docs/plans/` holds the design notes.

## rfbench (`~/rfbench`) uses this repo as a library

`~/rfbench` is the registry and browser UI over the SINAD desense tests
(design: `~/rfbench/docs/plans/2026-10-28-rfbench-design.md`). It IMPORTS
foxsinad and copies nothing: `rfbench/_libs.py` puts this checkout's root on
`sys.path` from `~/foxsinad` (override with `FOXSINAD_REPO`). What it
calls, so a change here is a change there (run `~/rfbench/run -m pytest`,
about two minutes, and open `python -m rfbench.app` in a browser before
pushing one):

- `foxsinad.picofox`: `PicoFox`, `find_port`, `PicoFoxError`, `set_cabled`;
  the driver's methods `hello`, `status`, `set_freq`, `set_att`, `tone`,
  `carrier`, `off`, `set_cabled`, `close`, and the `events` list and `port`.
- `foxsinad.sim.SimFox` (the same methods, with `speed`, `mode`, `freq`,
  `att`, `tone_hz`, `dev_hz`, `cabled` read directly by rfbench's radio
  model, which is the one truth behind rfbench's simulated bench;
  `SimAnalyzer` is NOT used, because it carries a second model receiver).
- `foxsinad.sweep`: `CalTable` (`dbm`, `att`, `dbm_pts`, `freq_mhz`, `date`,
  `path`) and `twelve_db_fit`; `cal/picofox_r3_attcal.json` is read by
  rfbench's equipment inventory for the PicoFox port's level range.
- `foxsinad.analyzer`: `SinadAnalyzer` (`read(settle_s, avg_s)` → `Reading`
  with `sinad_db`, `std_db`, `n`, `n_discarded`, `peak`, `under_range`),
  `pick_device`, `Reading` (rfbench's simulated sound card builds one).
- `foxsinad.app.engine.Engine`, SUBCLASSED by `rfbench/app/engine.py`: it
  overrides `cmd_connect`, `cmd_disconnect`, `cmd_start` (falls through to
  yours for `mode`), `cmd_stop`, `_rf_off_quietly`, `_open_analyzer`,
  `_poll_once`, `_watchdog`, `shutdown`, `state`, `snapshot`, and relies on
  `handle`, `emit`, `log`, `subscribe`/`unsubscribe`, `_require_idle`,
  `_state_changed`, `_run_mode` and the attributes `fox`, `_fox_raw`,
  `fox_lock`, `cal`, `setup`, `mode`, `worker`, `stop_evt`, `_subs`,
  `_subs_lock`, `_no_ui_since`, `_alive`, `hello`, `_events_seen`,
  `sim_speed`, `watchdog_s`, `MODE_HELP` (adds a `test` entry). `WATCHDOG_S`
  and `CommandError` are imported.
- `foxsinad/app/static/app.js` and `app.css` are served UNCHANGED by
  rfbench's server at `/fox/`; rfbench's page hosts the DOM ids `app.js`
  binds to (`simBadge`, `link`, `linkBanner`, `cabledBanner`, the `.seg`
  buttons, `startBtn`, `stopBtn`, `runState`, `setup`, `modeHelp`,
  `plotTitle`, `legend`, `readout`, `canvasWrap`, `plot`, `tip`, `results`,
  `connectBtn`, `foxStatus`, `foxEvents`, `attSlider`, `attLabel`,
  `toneBtn`, `carrierBtn`, `offBtn`, `idBtn`, `freqBtn`, `manualHint`,
  `log`, `cabledDialog`, `toast`) and uses the CSS tokens `app.css`
  defines. Renaming an id or a token breaks rfbench's page.
- foxsinad's own page is the session wizard (`#tabbar` steps, `.tabpanel`,
  `#fields-<group>`, `#wizNav`, `#padDialog`, the strip's
  `#stripSinad`/`#stripAudio`, `#rfState`);
  rfbench's page has none of those. So every hook added to `app.js` must
  look for its element and do nothing without it (`TABBED`, `if ($(...))`),
  and `buildSetup()` falls back to `#setup`. Check rfbench's page in a
  browser (no console errors) after any `app.js` change.
- The protocol: rfbench's `docs/ui-protocol.md` is this repo's
  `docs/ui-protocol.md` extended; every foxsinad command, event and field is
  kept as it is, by design, so the Android engine is unaffected.

rfbench writes nothing into this checkout: its runs go under
`~/rfbench/runs/`, its results under `~/rfbench/results/`.
