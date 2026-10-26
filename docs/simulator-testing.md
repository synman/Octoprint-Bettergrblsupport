# Simulator testing runbook

Maintainer runbook for the virtual Grbl and FluidNC used to test Better Grbl Support without hardware.
Scope: `tests/simulator/` and the throwaway OctoPrint rigs built on it. Owner: this repo's maintainers.
The short file index is `../tests/simulator/README.md`; this document is the procedure and the reasoning.

Conventions: `<repo>` is a checkout of this repository, `<scratch>` is any scratch directory outside it,
`<venv20>` / `<venv111>` are Python virtual environments. Shell blocks work in bash or zsh. Nothing here
needs real hardware, your own OctoPrint, or the network (except the one-time `pip install` and `git clone`
steps, marked as such).

## 1. What the ports are, and are not

| Module | Models | Pin |
|---|---|---|
| `grbl_virtual.py` `GrblVirtual` | gnea/grbl 1.1h, stock 3-axis `DEFAULTS_GENERIC` build; `GrblVirtual(parking=True)` is the same build with `PARKING_ENABLE` | `bfb67f0` (`SRC`), `VERSION` 1.1h, `BUILD` 20190830 |
| `fluidnc_virtual.py` `FluidNCVirtual(GrblVirtual)` | bdring/FluidNC 3.9, "Default (Test Drive no I/O)" machine with a laser spindle (`spindle="none"` or `"pwm"` for a CNC one); no probe pin unless the constructor adds one (`probe_pin=True`) | `9b8236f` (`SRC`), declared as tag `v3.9.9`, `esp32-wifi` |
| `octoprint_grblsim.py` | Single-file OctoPrint plugin that serves both as serial ports `GRBL_SIM` and `FLUIDNC_SIM` | n/a |

Pure Python, standard library only, no OctoPrint import in the ports. The goal is fidelity of everything the
plugin can see on the wire: byte-exact replies, error and alarm numbering, status-report cadence, settings
and `$I` text. The goal is not a physics or timing model.

**Deviations from the firmware (read before trusting a timing or motion result).** The authoritative lists
are the module docstrings at the top of each file; this is their summary.

- Motion is a constant-speed linear model at the planner's nominal speed with one constant-deceleration
  ramp for hold, jog cancel and probe cancel. No junction planning. An arc is one planner block of true arc
  length; soft limits check only its end point.
- `$H` completes instantly (state never reads `Home`). The 500 ms `delay_ms` after `ALARM:` is omitted.
- The probe is a virtual contact plane (`probe_trigger`), not a pin on a moving machine.
- Settings, coordinate data and startup lines persist across soft reset inside one instance (an EEPROM
  stand-in) but not across instances.
- Hard-limit, door and control pins are reachable only through `limit_pins` and `inject_alarm`. The safety-door
  event is the realtime byte `0x84` (it works with no door pin on both firmwares), never a pin.
- Parking (`GrblVirtual(parking=True)`, FluidNC's `parking:` section): the retract and restore motions are system
  motions at constant speed with no ramp, held outside the planner (`sys_block`), so the interrupted block resumes
  from its own progress; positions advance by step deltas, so a parking move is not undone by the next block. A
  second door event while a parking motion runs (the firmware re-retracts) is not modelled. FluidNC: M56 override
  control is not modelled (it behaves as M56 P1); `spinup_ms`/`spindown_ms` are waited on the door path only, not
  for M3/M5 in a program, and only a `PWM` spindle has them (the `laser` section here also prints them, which
  `Laser::group` at the pinned SHA does not); the coolant `delay_ms` is not modelled.
- `GrblVirtual` accepts only the stock axes `"XYZ"`, so A/B words are `error:20` as on the real build.
- FluidNC: the git tag is not resolvable from the pinned tree, so `v3.9.9`, `grbl_version` 3.9 and
  `esp32-wifi` are declared choices, giving the banner `Grbl 3.9 [FluidNC v3.9.9 (esp32-wifi) '$' for help]`.
- FluidNC: axes are a prefix of `XYZABC`, 2 to 6 letters (`GRBL_SIM_FLUIDNC_AXES`, default `XYZ`). A directly
  constructed `FluidNCVirtual()` has **no** probe pin (`probe_pin=False`, `G38.x` answers
  `[MSG:ERR: Probe pin is not configured]`); the OctoPrint plugin passes `probe_pin=True` by default and
  `GRBL_SIM_FLUIDNC_PROBE=0` removes it again.
- FluidNC: `$CD` replays only the config sections the port models, and `$S` lists only the settings the
  plugin reads plus neighbours. The order of WebUI settings against `make_settings` ones is an assumption.
- Critical alarms (`ALARM:1` and `ALARM:2`, from a hard or soft limit): `GrblVirtual` then ignores all input,
  status polls included, until a soft reset, as gnea/grbl does. `FluidNCVirtual` keeps answering instead: it
  reports `Alarm`, answers `?` and `$G`, answers G-code with `error:9`, and answers `$X` with `ok` and no effect, until
  a soft reset or a completed `$H`. A soft-limit move is rejected without being queued, after any motion already
  queued has run out, so nothing is left for a reset or `$H` to finish; a reset also discards a line still waiting
  on the planner (no reply, no completed `$H`).
- Laser mode (`$32=1`): the spindle power in the `FS:` field of a status report follows the block being executed
  (`M3 S100` then `G1` reads `FS:<feed>,100` while the move runs, and `0` when idle, as on the firmware).
- FluidNC not modelled: boot log, WiFi lines in `$I`, `#` parameters, expressions, O-word flow control, `M6`,
  `G59.1-.3`, `G38.6-9`, the `[G54:...]` auto-report after a coordinate write, SD card and file jobs, and the
  ALARM:6 reset-during-homing path. The inherited gnea parser answers those as gnea does.

**What this means for results.** A green run says the plugin and the port agree on bytes, and that the
plugin survives that conversation inside a real OctoPrint. It does not say a real controller agrees. Do not
use the simulator to tune feed rates, estimate job time, or judge motion quality.

Test names carry ids (`F01`, `S04`, `P02`, `T05`, `D5`) that point into a firmware census (every string the
plugin sends, every pattern it parses, with firmware source refs). **That census is not committed to this
repo**; the ids survive only in test names and docstrings. To rebuild one, enumerate the plugin's sends
(`octoprint_bettergrblsupport/__init__.py`, `_bgs.py`, `static/js/bettergrblsupport_control.js`) and parses,
then map each to a firmware source line the way the `Source:` docstrings do.

**Writing a test.** The ports have no OctoPrint dependency: construct `GrblVirtual()` or `FluidNCVirtual(axes="XY")`,
`feed(bytes)` what the plugin would send, `tick(dt)` to advance the machine, and `drain()` the reply lines. The
hooks `probe_trigger`, `limit_pins` and `inject_alarm(code)` stand in for hardware. See the "Public surface" line of
the `grbl_virtual.py` docstring and the `send`, `out` and `tick_until` helpers at the top of
`test_grbl_virtual.py`; name new tests with the census id they cover. A delay or a parking motion that ends inside one
large `tick()` starts its successor on the next one, so tests that cross several of them use `advance(g, seconds)`
(small ticks).

## 2. Run the unit suites

From the repo root, no corpus (about a tenth of a second):

```
cd <repo>/tests/simulator
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_grbl_virtual test_fluidnc_virtual test_fluidnc_web
```

Python 3.12.1 was used; the plugin declares `>=3.7,<4` and no lower floor has been tested for the ports.
Expected tail: `Ran 418 tests in 1.3s` then `OK (skipped=2)` (400 of them are the two firmware ports; the 18 in
`test_fluidnc_web` start a local HTTP server on an ephemeral port). The two skips are the corpus tests.

With a corpus, any directory tree of `.gcode` files (about a minute for 66 files and 1.68 million lines):

```
GRBL_SIM_CORPUS=<dir of .gcode files> PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest test_grbl_virtual test_fluidnc_virtual -v
```

Expected: `Ran 400 tests`, `OK` with no skips, and each suite prints a summary line, for the corpus used here:

```
corpus summary: 66 files, 1680096 lines, 0 error/ALARM replies, 29 '%' lines held back (plugin T05)
fluidnc corpus summary: 66 files, 1680096 lines, 0 error/ALARM replies, 29 '%' lines held back (plugin T05)
```

`'%' lines held back` are lines the plugin deliberately never sends (the plugin's T05 behaviour), so the
simulator is not asked to answer them. The corpus is not in the repo; any real laser or CNC output works,
and the only pass condition is zero `error:` or `ALARM:` replies. Run one suite or test by name, for
example `python3 -m unittest test_fluidnc_virtual.Banner.test_F01_P01_banner_bytes` or
`python3 -m unittest test_grbl_virtual -k probe`. PyYAML is needed by one FluidNC test and that test skips
without it. The test count (400 at the time of writing) changes as tests are added; the contract is `OK`.

The plugin's serial wrapper (`octoprint_grblsim.py`, the `@SIM ERROR` verb) has its own suite, which needs no corpus:

```
cd <repo>/tests/simulator
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_octoprint_grblsim
```

Expected tail: `Ran 37 tests in 10.8s` then `OK`.

The plugin's own decision and bookkeeping tests need OctoPrint importable, so run them with a rig's interpreter:

```
cd <repo>/tests/pause
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_pause_strategy test_modal_state test_viewer_cache test_jog_estimate test_feed_override
```

`test_pause_strategy` is `_bgs.pause_strategy`, `parking_available` and the scripts written per strategy (the units and
motion-mode restore included); `test_modal_state` is the modal state followed from sent lines, the distance mode an
interrupted probe hands back (distance mode, the units it forced to `G21`, then `G0` so the parser does not stay in
`G38.2`) and the single, multipoint and X/Y probes record and restore, the job feed rate and power, the
XYZ probe chain and the frame scan of a stored file; `test_feed_override` sends job lines, the Control tab's feed and
power override changes and the pause resume lines through the real `hook_gcode_sending` and `on_api_command` (with the
tags OctoPrint gives each kind of line) and pins that an override is applied once; `test_viewer_cache` is the byte cap on a normalised viewer copy;
`test_jog_estimate` is the position estimate the plugin keeps from sent lines, `$J=` jogs included. Expected tail:
`Ran 96 tests` then `OK` (both OctoPrint lines). `docs/pause-resume.md`
says what the pause ones pin.

Clean up: with `PYTHONDONTWRITEBYTECODE=1` nothing is written. If a run without it left
`tests/simulator/__pycache__`, remove that one directory by its literal path.

## 3. Smoke the plugin without OctoPrint

From the repo root. This drives `SimSerial` directly, so `@SIM` lines go straight to it; section 7 covers sending them through OctoPrint.

```python
import sys
sys.path.insert(0, "tests/simulator")
import octoprint_grblsim as m
m.__plugin_load__()
factory = m.__plugin_hooks__["octoprint.comm.transport.serial.factory"]

def drain(port):
    out = []
    while True:
        line = port.readline()          # b"" on timeout
        if not line:
            return out
        out.append(line)

for name in ("GRBL_SIM", "FLUIDNC_SIM"):
    port = factory(None, name, 115200, 0.2)     # (comm, port, baudrate, read_timeout)
    print(name, "boot:", drain(port))
    port.write(b"?\n")
    print("  ?\\n          ->", drain(port))
    port.write(b"@SIM ALARM 1\n")
    print("  @SIM ALARM 1 ->", drain(port))
    port.close()
print("other port ->", factory(None, "COM1", 115200, 0.2))
```

Expected output:

```
GRBL_SIM boot: [b'\r\n', b"Grbl 1.1h ['$' for help]\r\n"]
  ?\n          -> [b'<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n', b'ok\r\n']
  @SIM ALARM 1 -> [b'ALARM:1\r\n', b'[MSG:Reset to continue]\r\n', b'ok\r\n']
FLUIDNC_SIM boot: [b'\r\n', b"Grbl 3.9 [FluidNC v3.9.9 (esp32-wifi) '$' for help]\r\n"]
  ?\n          -> [b'<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n', b'ok\r\n']
  @SIM ALARM 1 -> [b'[MSG:INFO: ALARM: Hard Limit]\r\n', b'ALARM:1\r\n', b'[MSG:ERR: Reset to continue]\r\n', b'ok\r\n']
other port -> None
```

Environment controls (read when the connection is made; the environment wins over the plugin setting, which is stored
under `plugins.octoprint_grblsim.<name>`, so a rig can change the next connection with one `POST /api/settings`, no restart):

| Variable | Plugin setting | Effect |
|---|---|---|
| `GRBL_SIM_FLUIDNC_AXES` | `fluidnc_axes` | FluidNC axis letters. Anything but a 2 to 6 letter prefix of `XYZABC` makes the factory raise `ValueError: axes must be a prefix of 'XYZABC' with 2..6 letters, got 'XQ'`. |
| `GRBL_SIM_FLUIDNC_PROBE` | `fluidnc_probe_pin` | `0`, `false`, `no`, `off` removes the probe pin. |
| `GRBL_SIM_GRBL_PARKING` | `grbl_parking` | Build `GRBL_SIM` with `PARKING_ENABLE`: `$I` reports `[OPT:VP,15,128]`, and `0x84` retracts Z (needs `$22=1` and laser mode off). |
| `GRBL_SIM_GRBL_HOMING` | `grbl_homing` | Power `GRBL_SIM` up with `$22=1` (Alarm until `$X`). |
| `GRBL_SIM_GRBL_LASER` | `grbl_laser` | Power `GRBL_SIM` up with `$32=1`. |
| `GRBL_SIM_FLUIDNC_SPINDLE` | `fluidnc_spindle` | `laser` (default), `none` or `pwm` (a CNC spindle with `spinup_ms` and `spindown_ms`). |
| `GRBL_SIM_FLUIDNC_HOMING` | `fluidnc_homing` | Axes with a homing cycle, e.g. `XYZ` (the machine then starts in `ALARM:14`). Parking needs one on its axis. |
| `GRBL_SIM_FLUIDNC_PARKING` | `fluidnc_parking` | `parking: enable: true` in the FluidNC config. |
| `GRBL_SIM_FLUIDNC_SPINUP_MS` | `fluidnc_spinup_ms` | The `PWM` spindle's `spinup_ms` and `spindown_ms`. |
| `GRBL_SIM_TRACE` | `trace_file` | Append one JSON line `{t, state, mpos, spindle}` every 0.2 s and at every change of state while a port is open: the machine's own view, for tests that cannot poll it (OctoPrint suppresses status requests while a job runs). |
| `GRBL_SIM_FLUIDNC_HTTP_PORT` | none | Starts the FluidNC WebUI `/files` server (`fluidnc_web.py`) on `localhost:<port>` and boots `FLUIDNC_SIM` with `HTTP/Enable=ON`, `Hostname=localhost`, `HTTP/Port=<port>`, so the plugin's HTTP config save (`upload_fluid_config`) has a real endpoint. Unset: no server, `HTTP/Enable=OFF`. Read when the first `FLUIDNC_SIM` connection is made. |

With the port set, `curl http://localhost:<port>/files` returns the LocalFS list, for example
`{"files": [{"name": "config.yaml", ...}], "path": "", "total": "192.00 KB", "used": "3.27 KB", "occupation": 1, "status": "Ok"}`.
The server models upload, list, delete and rename only (docstring of `fluidnc_web.py`, which also lists what a
real board does on a failed upload: the truncated or mismatched file is left in place, it is not removed).

## 4. Python environments for the rigs

Two throwaway environments, one per OctoPrint line. This is the one step that needs the network (PyPI) and was
**not re-run while writing this runbook**; the existing environments it was verified in report
`OctoPrint==2.0.0rc5` and `OctoPrint==1.11.8` (each with `PyYAML==6.0.3` and `tornado==6.5.10`, Python 3.12.1)
from `pip freeze`.

```
python3.12 -m venv <scratch>/venv20  && <scratch>/venv20/bin/python  -m pip install "OctoPrint==2.0.0rc5"
python3.12 -m venv <scratch>/venv111 && <scratch>/venv111/bin/python -m pip install "OctoPrint==1.11.8"
```

`rigtool.py` below needs `tornado` and `yaml`; both arrive as OctoPrint dependencies, so always run it with the
rig's own `<venv>/bin/python`. Never install into a system Python or your real OctoPrint's environment.

Keep `<scratch>` out of `/tmp` for any rig that must outlive a single test run, for example `~/octoprint-rigs`.
macOS deletes files under `/tmp` that have gone unchanged for about three days, even while a server is still
using them. A rig in `/tmp` keeps running on the deleted files until its next request needs one. On 2026-10-11
that cost a rig its venv, `config.yaml`, `users.yaml`, printer profiles and uploads.

## 5. Stand up a rig

A rig is an OctoPrint server on `127.0.0.1` with its own `--basedir`, the plugin package and
`octoprint_grblsim.py` symlinked into `<basedir>/plugins/` (not pip-installed, so edits are live), and a seed
`config.yaml`. One rig per OctoPrint line; give each its own port.

Save the next three blocks as `setup.sh`, `ctl.sh` and, into the rig directory after `setup.sh` has created it, `rigtool.py`.

**`setup.sh`** (once per rig; export `REPO`, `VENV` and `RIG` first, as in "Start and confirm" below, then `bash setup.sh`; it refuses to run if one is unset):

```bash
# Needs REPO, VENV and RIG exported first (see "Start and confirm"); creates the rig under $RIG.
: "${REPO:?set REPO}" "${VENV:?set VENV}" "${RIG:?set RIG}"
mkdir -p "$RIG/basedir/plugins"
ln -s "$REPO/octoprint_bettergrblsupport"          "$RIG/basedir/plugins/bettergrblsupport"   # link name = plugin identifier (pyproject entry point)
ln -s "$REPO/tests/simulator/octoprint_grblsim.py" "$RIG/basedir/plugins/octoprint_grblsim.py"
"$VENV/bin/python" -c "import secrets; print(secrets.token_hex(16))" > "$RIG/apikey"
cat > "$RIG/basedir/config.yaml" <<YAML
api:
  key: $(cat "$RIG/apikey")
accessControl: {enabled: false}
server:
  firstRun: false
  onlineCheck: {enabled: false}
  pluginBlocklist: {enabled: false}
  pluginBlacklist: {enabled: false}   # the 1.11 spelling
  seenWizards: {corewizard: 9999}
printerConnection: {autoconnect: false}
serial: {log: true}                 # 1.11 serial.log switch
plugins:
  _disabled: [announcements, softwareupdate, tracking, errortracking, discovery, health_check, firmware_check, achievements, file_check, printer_safety_check, gcodeviewer, mqtt, mqttchambertemperature]
  serial_connector: {log: true}        # the 2.0 switch
  pluginmanager: {repository: '', notices: ''}   # otherwise it fetches plugins.octoprint.org at startup
YAML
cat > "$RIG/basedir/logging.yaml" <<YAML
version: 1
loggers:
  octoprint.plugins.bettergrblsupport: {level: DEBUG}
  octoprint.plugins.octoprint_grblsim: {level: DEBUG}
YAML
```

What the seed does and why: a known API key (`apikey` file); `accessControl` off so the passive API-key login
used for the websocket collector works on both lines; wizards marked seen; serial logging on (the 2.0 key is
`plugins.serial_connector.log`, the 1.11 key is `serial.log`; without it `serial.log` stays empty and every
check below fails); network-touching bundled plugins disabled; the plugin manager's repository and notice URLs
emptied so the server makes no outbound request (the price is a few `ERROR` lines and `Traceback`s from the
plugin manager about an invalid empty URL on every start, which are expected noise); debug logging for the two
plugins, which is where `is_grbl_fluidnc result=[...]` and `alarm received: ...` come from. Better Grbl Support
creates and selects its own printer profile `_bgs` on startup; nothing to seed.

**Using the rig in a browser (OctoPrint 2.0).** With `accessControl: {enabled: false}` the 2.0 web UI reloads
forever: its calls to `/api/settings/reauthReq`, `/api/printer/error` and `/api/printer/command/custom` return
`403` with no logged-in user (observed on 2.0.0rc5). The headless checks below are unaffected. For browser
use, stop the server, add a local admin, and replace the `accessControl` line with localhost autologin:

```bash
"$VENV/bin/python" -c "import secrets; print(secrets.token_hex(12))" > "$RIG/simpw"; chmod 600 "$RIG/simpw"
"$VENV/bin/octoprint" --basedir "$RIG/basedir" user add sim --password "$(cat "$RIG/simpw")" --admin
```

`user add` rewrites `config.yaml` in block style, so then set:

```yaml
accessControl:
  autologinLocal: true
  autologinAs: sim
  localNetworks:
  - 127.0.0.0/8
  - ::1/128
```

The server log then shows `Logging in user sim from 127.0.0.1 via autologin`. The plugin symlink must be named
`bettergrblsupport` (as in `setup.sh` above): a folder plugin's directory name is its identifier, and under any
other name the plugin's view models find no `settings.plugins.bettergrblsupport` and throw on page load.

**`ctl.sh`** (source it in every shell that talks to the rig, after setting `VENV`, `RIG`, `RIG_PORT`):

```bash
# usage: rig_start [AXES]  /  rig_stop      (needs VENV RIG RIG_PORT from the setup block)
rig_start() {
  PYTHONDONTWRITEBYTECODE=1 GRBL_SIM_FLUIDNC_AXES="${1:-XYZ}" nohup "$VENV/bin/octoprint" serve \
    --basedir "$RIG/basedir" --host 127.0.0.1 --port "$RIG_PORT" >> "$RIG/server.out" 2>&1 &
  echo $! > "$RIG/server.pid"
  for i in $(seq 1 90); do
    curl -s -o /dev/null -H "X-Api-Key: $(cat "$RIG/apikey")" "127.0.0.1:$RIG_PORT/api/version" && { echo "up after ${i}s, pid $(cat "$RIG/server.pid")"; return 0; }
    sleep 1
  done
  echo "server did not come up; see $RIG/server.out" >&2; return 1
}
rig_stop() {   # kills only the PID rig_start recorded
  kill "$(cat "$RIG/server.pid")" && while kill -0 "$(cat "$RIG/server.pid")" 2>/dev/null; do sleep 1; done; echo stopped
}
ws_stop() {    # stops the `rt ws` collector; it also ends by itself whenever the server restarts
  kill "$(cat "$RIG/ws.pid")" 2>/dev/null && echo "collector stopped" || echo "collector already exited"
}
rt() { "$VENV/bin/python" "$RIG/rigtool.py" "$@"; }   # the check driver, run with the rig's own python
```

`rig_start` takes the FluidNC axis letters (default `XYZ`) because the axes are read from the server's
environment; change axes by `rig_stop` then `rig_start XY`. `rig_stop` and `ws_stop` signal only the PID they recorded.

**`rigtool.py`** (save as `$RIG/rigtool.py`; `rt <command>` runs it):

```python
#!/usr/bin/env python3
"""Drive a throwaway OctoPrint rig (1.11 or 2.0) running Better Grbl Support and octoprint_grblsim.

Environment:  RIG=<rig dir holding basedir/ and apikey>   RIG_PORT=<http port>
Run it with the rig's own python so `tornado` and `yaml` import:  <venv>/bin/python rigtool.py <command> ...
Commands:  ws <out.jsonl> | connect <PORT_NAME> | status <ws.jsonl> | jog <ws.jsonl> <axes>
           filters | alarm <ws.jsonl> <axis> | atsim | plugin enable|disable | disconnect | snap | stream <file.gcode> <ws.jsonl> [max_seconds]
Every check prints one PASS/FAIL line; exit status is 1 on FAIL."""
import asyncio, json, os, re, sys, time, urllib.error, urllib.request, uuid

RIG = os.environ["RIG"]
PORT = os.environ.get("RIG_PORT", "5091")
BASE = "http://127.0.0.1:" + PORT
KEY = open(os.path.join(RIG, "apikey")).read().strip()
PLUG = "bettergrblsupport"
SERLOG = os.path.join(RIG, "basedir", "logs", "serial.log")
OCLOG = os.path.join(RIG, "basedir", "logs", "octoprint.log")
LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ - (Send: |>>> |Recv: |<<< )(.*)$")  # 1.11 / 2.0 prefixes


def api(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"X-Api-Key": KEY, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode()
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:300]}
    return json.loads(t) if t.strip().startswith(("{", "[")) else t


def size(p):
    return os.path.getsize(p) if os.path.exists(p) else 0


def tail(p, off):
    with open(p, "rb") as f:
        f.seek(off if size(p) >= off else 0)
        return f.read().decode("utf-8", "replace")


def serial_lines(off=0):
    """[(direction, text)] from serial.log; direction '>' sent by OctoPrint, '<' received."""
    return [(">" if m.group(1) in ("Send: ", ">>> ") else "<", m.group(2))
            for m in map(LINE.match, tail(SERLOG, off).splitlines()) if m]


def verdict(name, ok, note):
    print("%s | %s | %s" % (name, "PASS" if ok else "FAIL", note), flush=True)
    if not ok:
        verdict.failed = True
verdict.failed = False


def ws_states(wsfile):
    out = []
    for ln in open(wsfile):
        try:
            item = json.loads(ln)["msg"]
        except ValueError:
            continue
        for m in (item if isinstance(item, list) else [item]):
            m = json.loads(m) if isinstance(m, str) else m
            p = m.get("plugin") if isinstance(m, dict) else None
            if p and p.get("plugin") == PLUG and p["data"].get("type") == "grbl_state" and "x" in p["data"]:
                out.append(p["data"])
    return out


def wait_idle(wsfile, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = ws_states(wsfile)
        if s and s[-1]["state"] == "Idle":
            return s[-1]
        time.sleep(0.5)
    return (ws_states(wsfile) or [None])[-1]


def cmd_ws(out):
    """Collect SockJS push messages (passive API-key login) into a JSONL file until killed."""
    import tornado.ioloop, tornado.websocket, random, string
    req = urllib.request.Request(BASE + "/api/login", data=b'{"passive":true}', headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    login = json.load(urllib.request.urlopen(req))
    auth = login["name"] + ":" + login["session"]
    url = "ws://127.0.0.1:%s/sockjs/000/%s/websocket" % (PORT, "".join(random.choices(string.ascii_lowercase, k=8)))
    async def main():
        ws = await tornado.websocket.websocket_connect(url)
        await ws.write_message(json.dumps([json.dumps({"auth": auth})]))
        with open(out, "a") as f:
            while True:
                m = await ws.read_message()
                if m is None:
                    return
                if m.startswith("a"):
                    for item in json.loads(m[1:]):
                        f.write(json.dumps({"t": round(time.time(), 3), "msg": item}) + "\n"); f.flush()
    tornado.ioloop.IOLoop.current().run_sync(main)


def cmd_connect(port):
    oc0, s0 = size(OCLOG), size(SERLOG)
    api("POST", "/api/connection", {"command": "connect", "port": port, "baudrate": 115200, "printerProfile": "_bgs"})
    t0 = time.time(); st = None
    while time.time() - t0 < 60:
        st = api("GET", "/api/connection")["current"]["state"]
        if st.startswith("Operational"):
            break
        time.sleep(1)
    time.sleep(12)  # the plugin's hello sequence ($$, $I, $G ...)
    ver = api("GET", "/api/settings")["plugins"].get(PLUG, {}).get("grblVersion")
    det = [l.split(" - ", 2)[-1] for l in tail(OCLOG, oc0).splitlines() if "is_grbl_fluidnc result" in l or "is_grbl_one_dot_one result" in l]
    is_fl = bool(ver) and " FLUIDNC " in ver.upper()
    want_fl = port == "FLUIDNC_SIM"
    verdict("connect", st.startswith("Operational") and bool(ver) and is_fl == want_fl,
            "state=%s grblVersion=%r fluidnc_detected=%s expected=%s detection=%s" % (st, ver, is_fl, want_fl, det[-2:]))


def cmd_status(wsfile):
    s = ws_states(wsfile)
    d = s[-1] if s else {}
    verdict("status", bool(s) and d.get("state") in ("Idle", "Alarm") and isinstance(d.get("x"), float) and d.get("mode") in ("MPos", "WPos"),
            "grbl_state msgs=%d last=%s" % (len(s), json.dumps(d)))


def cmd_jog(wsfile, axes):
    s0 = size(SERLOG)
    before = wait_idle(wsfile)
    moves = [("east", "X", 5.0), ("north", "Y", 5.0)] + ([("up", "Z", 1.0)] if "Z" in axes else [])
    for direction, axis, dist in moves:
        wait_idle(wsfile)
        api("POST", "/api/plugin/" + PLUG, {"command": "move", "sessionId": "rig", "direction": direction, "distance": dist, "axis": axis})
        time.sleep(6)
    after = wait_idle(wsfile)
    sent = [t for d, t in serial_lines(s0) if d == ">" and ("$J=" in t or "G1 G91" in t)]
    form = "$J=" if any("$J=" in t for t in sent) else ("G1 G91" if sent else "none")
    ok = bool(before and after and abs(after["x"] - before["x"]) >= 4.9 and abs(after["y"] - before["y"]) >= 4.9 and len(sent) == len(moves))
    verdict("jog", ok, "form=%s sent=%s before=(%s,%s,%s) after=(%s,%s,%s)" % (
        form, sent, *[before[k] for k in "xyz"], *[after[k] for k in "xyz"]) if before and after else "no grbl_state")


def cmd_filters():
    s = api("GET", "/api/settings")
    tf = s.get("terminalFilters") or []
    act = s["plugins"].get(PLUG, {}).get("activeFilters")
    rs = [(f["name"], re.compile(f["regex"])) for f in tf]
    lines = ["%s%s" % (">>> " if d == ">" else "<<< ", t) for d, t in serial_lines()]
    counts = {n: sum(1 for l in lines if r.search(l)) for n, r in rs}
    verdict("filters", len(tf) == 4 and all(counts.values()),
            "filters=%d match-counts=%s activeFilters=%s (offline regex match against serial.log; a filter only hides lines once selected in the Terminal tab)" % (len(tf), json.dumps(counts), act))


def cmd_alarm(wsfile, axis):
    s1, o1 = size(SERLOG), size(OCLOG)
    api("POST", "/api/printer/command", {"commands": ["G91", "G38.2 %s F100" % axis]})
    t0 = time.time()
    while time.time() - t0 < 60 and not any(t.startswith("ALARM:") for d, t in serial_lines(s1) if d == "<"):
        time.sleep(1)
    time.sleep(4)
    got = [t for d, t in serial_lines(s1) if d == "<" and t.startswith("ALARM:")]
    warn = [l.split(" - ", 2)[-1] for l in tail(OCLOG, o1).splitlines() if "alarm received" in l]
    tb = [l for l in tail(OCLOG, o1).splitlines() if "Traceback" in l]
    st = (ws_states(wsfile) or [{}])[-1].get("state")
    api("POST", "/api/plugin/" + PLUG, {"command": "unlock"}); time.sleep(3)
    api("POST", "/api/printer/command", {"commands": ["G90"]})
    verdict("alarm", bool(got) and bool(warn) and not tb and st == "Alarm",
            "serial=%s plugin-log=%s tracebacks=%d last grbl_state=%s" % (got, warn[:1], len(tb), st))


def cmd_atsim():
    """'@SIM ALARM 1' typed into OctoPrint must reach the simulator through its at-command hook."""
    s0, o0 = size(SERLOG), size(OCLOG)
    r = api("POST", "/api/printer/command", {"command": "@SIM ALARM 1"})
    time.sleep(3)
    L = serial_lines(s0)
    got = [t for d, t in L if d == "<" and t.startswith("ALARM:")]
    leaked = [t for d, t in L if d == ">" and "@SIM" in t]
    warn = [l.split(" - ", 2)[-1] for l in tail(OCLOG, o0).splitlines() if "alarm received" in l]
    tb = [l for l in tail(OCLOG, o0).splitlines() if "Traceback" in l]
    verdict("atsim", got == ["ALARM:1"] and not leaked and bool(warn) and not tb,
            "post=%r serial=%s @SIM-written-to-port=%d plugin-log=%s tracebacks=%d" % (r, got, len(leaked), warn[:1], len(tb)))


def cmd_plugin(action):
    r = api("POST", "/api/plugin/pluginmanager", {"command": action, "plugin": PLUG})
    p = r.get("plugin", {})
    print("%s -> result=%s enabled=%s pending_disable=%s pending_enable=%s needs_restart=%s" % (
        action, r.get("result"), p.get("enabled"), p.get("pending_disable"), p.get("pending_enable"), r.get("needs_restart")))


def cmd_disconnect():
    api("POST", "/api/connection", {"command": "disconnect"}); time.sleep(2)
    print("connection:", api("GET", "/api/connection")["current"]["state"])


def cmd_snap():
    import yaml
    cfg = yaml.safe_load(open(os.path.join(RIG, "basedir", "config.yaml")))
    names = [f["name"] for f in api("GET", "/api/settings").get("terminalFilters", [])]
    print("config.yaml terminalFilters: %s" % ("null/absent" if not cfg.get("terminalFilters") else "%d entries" % len(cfg["terminalFilters"])))
    print("API terminalFilters: %d entries, first=%r" % (len(names), names[:1]))


def cmd_stream(path, wsfile, max_seconds="1800"):
    """Print a file and wait at most max_seconds (default 1800). The simulator runs at the planner's nominal
    speed, so a long CNC file takes about as long as the real job: pass a larger cap for those."""
    cap = float(max_seconds)
    name = os.path.basename(path)
    wait_idle(wsfile, 20)
    s0 = size(SERLOG)
    b = uuid.uuid4().hex
    body = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n' % (b, name)).encode() \
        + open(path, "rb").read() + ("\r\n--%s--\r\n" % b).encode()
    print("upload:", "ok" if "done" in str(api("POST", "/api/files/local", raw=body, ctype="multipart/form-data; boundary=" + b)) else "FAILED")
    api("POST", "/api/files/local/" + name.replace(" ", "%20"), {"command": "select", "print": True})
    t0 = time.time(); seen = False; j = {}
    while time.time() - t0 < cap:
        j = api("GET", "/api/job"); st = j["state"]
        seen = seen or st.startswith(("Printing", "Starting"))
        if seen and not st.startswith(("Printing", "Starting", "Finishing")):
            break
        time.sleep(2)
    time.sleep(2)
    L = serial_lines(s0)
    bad = [t for d, t in L if d == "<" and (t.startswith("error:") or t.startswith("ALARM"))]
    sent = [t for d, t in L if d == ">" and t != "?"]
    done = (j["progress"]["completion"] or 0) >= 100    # a job the plugin aborts also ends Operational, with no 100 percent
    capped = j["state"].startswith(("Printing", "Starting"))
    verdict("stream", seen and j["state"].startswith("Operational") and done and not bad,
            "%s state=%s progress=%s elapsed=%.0fs cap=%.0fs%s sent=%d error/ALARM=%d %s" % (
                name, j["state"], j["progress"]["completion"], time.time() - t0, cap,
                " CAP REACHED, job left running" if capped else "", len(sent), len(bad), bad[:3]))


if __name__ == "__main__":
    c, a = sys.argv[1], sys.argv[2:]
    {"ws": cmd_ws, "connect": cmd_connect, "status": cmd_status, "jog": cmd_jog, "filters": cmd_filters, "alarm": cmd_alarm,
     "atsim": cmd_atsim, "plugin": cmd_plugin, "disconnect": cmd_disconnect, "snap": cmd_snap, "stream": cmd_stream}[c](*a)
    sys.exit(1 if verdict.failed else 0)
```

Start and confirm:

```
export VENV=<scratch>/venv20 RIG=<scratch>/rig20 RIG_PORT=5091 REPO=<repo>
bash setup.sh && cp rigtool.py "$RIG/" && source ctl.sh && rig_start XYZ
```

Expected: `up after 3s, pid <n>`. The server log (`$RIG/server.out`) warns that the global API key is deprecated
and will stop working in OctoPrint 2.1.0 (observed on 2.0.0rc5); a rig that must outlive 2.1 needs a user API
key instead. Do not point `RIG_PORT` at an OctoPrint you care about, and never connect these rigs to real hardware.

For the 1.11 rig repeat with `VENV=<scratch>/venv111 RIG=<scratch>/rig111 RIG_PORT=5092`, a different port and
directory. Differences you will see between the lines:

| | OctoPrint 2.0.0rc5 | OctoPrint 1.11.8 |
|---|---|---|
| `serial.log` line prefix | `>>> ` / `<<< ` | `Send: ` / `Recv: ` |
| serial logging switch | `plugins.serial_connector.log` | `serial.log` |
| default terminal filters with the plugin disabled | 5 entries, first `Suppress temperature messages` | same (5 entries, same first name) |

`rigtool.py` accepts both prefixes. The filters shipped by the plugin accept both too.

## 6. The seven checks, plus streaming

Run in this order for each firmware. `<ws>` is a websocket capture file; the plugin's `grbl_state` messages
exist only on the websocket, so start the collector first and leave it running.

```
export PYTHONDONTWRITEBYTECODE=1
(rt ws $RIG/ws_grbl.jsonl > $RIG/ws.err 2>&1 & echo $! > $RIG/ws.pid); sleep 2
rt connect GRBL_SIM
rt status  $RIG/ws_grbl.jsonl
rt jog     $RIG/ws_grbl.jsonl XYZ
rt filters
rt stream  <a .gcode file> $RIG/ws_grbl.jsonl [max_seconds]   # optional; cap defaults to 1800 s, see below
rt alarm   $RIG/ws_grbl.jsonl Z-2
rt atsim
```

`atsim` raises a critical alarm (`ALARM:1`), after which the simulated Grbl ignores all input, so run it after the
stream and `alarm` checks. Then check 7, which restarts the server (`rt disconnect` first drops the alarmed connection):

```
rt disconnect;                       rt snap      # before
rt plugin disable; rig_stop; rig_start; rt snap   # plugin off
rt plugin enable;  rig_stop; rig_start; rt snap   # plugin on again
ws_stop                              # the restarts already ended the collector: prints "collector already exited"
```

For FluidNC replace `GRBL_SIM` with `FLUIDNC_SIM`, restart the server with the axes you want
(`rig_stop; rig_start XY`), start a fresh collector file, and pass the axes to `jog` (`XY`) and an axis the
machine has to `alarm` (`X-2` for XY). The six checks and `stream` each print one `name | PASS|FAIL | evidence` line and exit 1 on FAIL;
`plugin`, `snap` and `disconnect` just print state.

| # | Check | What it does | Pass condition | Evidence |
|---|---|---|---|---|
| 1 | `connect` | `POST /api/connection` (port, profile `_bgs`), waits for `Operational`, waits 12 s for the plugin's hello sequence | state is `Operational`, the plugin setting `grblVersion` is set, and the plugin classes the firmware as FluidNC exactly when the port is `FLUIDNC_SIM` | `octoprint.log` `is_grbl_fluidnc result=[True/False]`; `grblVersion` is `[VER:1.1h.20190830:] [OPT:V,15,128]` for Grbl and `[VER:3.9 FluidNC v3.9.9 (esp32-wifi) :] [OPT:PHS] [CLUSTER:16]` for FluidNC |
| 2 | `status` | reads the plugin's `grbl_state` websocket messages | at least one, state `Idle` or `Alarm`, `x` a float, mode `MPos` or `WPos` | the collector file |
| 3 | `jog` | plugin command `move` east, north (and up if Z) | the machine moved at least 4.9 mm in X and Y, and one jog command per move reached the port | `serial.log`: Grbl jogs as `$J=G91 G21 X5.000000 F500.0`, FluidNC as `G1 G91 G21 X5.000000 F1000.0` (the plugin does not call FluidNC "Grbl 1.1", so it never uses `$J=` there) |
| 4 | `filters` | reads the saved `terminalFilters` and matches them offline against `serial.log` | 4 filters, each matching at least one line | per-filter counts. `activeFilters` is `[]` by default: the filters hide nothing until selected in the Terminal tab, so this check proves the regexes fit the real traffic, not that the UI hides it |
| 5 | `alarm` | `G91` then `G38.2 <axis> F100` with nothing to touch, then `unlock` and `G90` | `ALARM:5` on the wire, the plugin logs `alarm received: 5: Probe fail...`, no `Traceback`, last `grbl_state` is `Alarm` | `serial.log`, `octoprint.log` |
| 6 | `atsim` | `POST /api/printer/command {"command": "@SIM ALARM 1"}`, waits 3 s | exactly `ALARM:1` on the wire, no `@SIM` line written to the port, the plugin logs `alarm received: 1: Hard limit triggered...`, no `Traceback` | `serial.log`, `octoprint.log` (section 7). Leaves the machine in the critical alarm state: disconnect afterwards |
| 7 | `plugin disable/enable` + restart | disables the plugin, restarts, re-enables, restarts | `config.yaml terminalFilters` is `4 entries`, then `null/absent` with the API serving OctoPrint's 5 defaults, then `4 entries` again | `rt snap` output |

Streaming (`rt stream <file> <ws> [max_seconds]`): uploads, selects and prints a file and passes when the job returns to
`Operational` with `progress.completion` at 100 and no `error:` or `ALARM` reply. The completion test matters: a
job the plugin aborts also ends in `Operational`, with completion `None`, and would otherwise pass. Stream before the
`atsim` check; the `alarm` check issues `unlock`, so streaming after it also works.

**The wait is capped: `max_seconds`, default 1800.** The simulator moves at the planner's nominal speed, so a long CNC
file takes about as long as the real job and can outlast the default. When the cap is reached `stream` prints `FAIL`
with `CAP REACHED, job left running` and the progress so far. The job is not cancelled, so a later check would run
against a machine that is still printing. Either wait for it and re-run with a larger cap, for example
`rt stream big.gcode $RIG/ws_grbl.jsonl 3600`, or cancel it (`POST /api/job {"command": "cancel"}`), then `rig_stop`
and `rig_start` before re-running: re-running at once in the same server session printed `upload: FAILED` in the check
below (cause not diagnosed), and after the restart the same file streamed. Size the cap from the table below.

Measured wall times (the two ports differ for the same file because their nominal speeds differ):

| File | `GRBL_SIM` | `FLUIDNC_SIM` |
|---|---|---|
| laser calibration, 43 source lines, 34 sent (2.0.0rc5 and 1.11.8) | 124 s | 78 s (XY and XYZ) |
| small move test, 6 sent | 10 s | 10 s (XYZ) |
| CNC `spoilboard-surface.gcode` (1.11.8) | 2547.6 s, so it exceeds the default cap: with the default the check printed `FAIL` at 1803 s and 93.3 percent | 1277 s (XYZ), inside the cap |

The 2547.6 s is OctoPrint's own `Finished in 2547.637 s.` line in `octoprint.log`, after the job had run past the cap.

Expected output of a clean run on OctoPrint 2.0.0rc5, `GRBL_SIM` (jog and filter numbers vary with timing):

```
connect | PASS | state=Operational grblVersion='[VER:1.1h.20190830:] [OPT:V,15,128]' fluidnc_detected=False expected=False detection=[...]
status | PASS | grbl_state msgs=2 last={"type": "grbl_state", "mode": "MPos", "state": "Idle", "x": 0.0, ...}
jog | PASS | form=$J= sent=['$J=G91 G21 X5.000000 F500.0', '$J=G91 G21 Y5.000000 F500.0', '$J=G91 G21 Z1.000000 F500.0'] before=(0.0,0.0,0.0) after=(5.0,5.0,1.0)
filters | PASS | filters=4 match-counts={...} activeFilters=[] (...)
stream | PASS | calibration.gcode state=Operational progress=100.0 elapsed=124s cap=1800s sent=34 error/ALARM=0 []
alarm | PASS | serial=['ALARM:5'] plugin-log=['WARNING - alarm received: 5: Probe fail. ...'] tracebacks=0 last grbl_state=Alarm
atsim | PASS | post='' serial=['ALARM:1'] @SIM-written-to-port=0 plugin-log=['WARNING - alarm received: 1: Hard limit triggered. ...'] tracebacks=0
```

The same sequence also passed on OctoPrint 1.11.8 for `GRBL_SIM` and on 2.0.0rc5 for `FLUIDNC_SIM` with axes `XYZ`
(connect, status, jog, filters, alarm, atsim; see the verification record for what was re-run and what was not).

**Things that look like failures and are not simulator faults.**

- 2-axis FluidNC (`rig_start XY`): the plugin raises `IndexError: list index out of range` in
  `hook_gcode_received` (`octoprint_bettergrblsupport/__init__.py:1313`, `offsetvalues[2]`) when it parses the
  `$#` reply, because it assumes three values per offset. Connection still reaches `Operational`. This is a
  plugin defect on machines without Z, recorded here so nobody "fixes" the simulator. Seen on 2.0.0rc5.
- Plugin manager `ERROR` lines and `Traceback`s about an invalid empty URL: caused by the seed (section 5).
- `@SIM` typed into OctoPrint is not written to the port and gets no `ok`; the simulator's hook delivers it (section 7).

If a check fails: read `$RIG/basedir/logs/serial.log` first (what actually crossed the wire), then
`octoprint.log`. `serial.log is currently not enabled` means the seed's serial logging key did not take
effect for that OctoPrint line. A connection that never reaches `Operational` with an `IndexError`-free log is
usually a stale server from an earlier run on the same port: `rig_stop` and start again.

Teardown: `ws_stop`, `rig_stop`, then the rig directory is yours to delete (a scratch
directory you created; delete it by its literal path, never through an unset variable).

## 7. `@SIM` fault escapes

`@SIM ALARM <n>`, `@SIM PROBE <axis><mm>`, `@SIM PROBE OFF`, `@SIM LIMIT <letters>`, `@SIM LIMIT OFF` and
`@SIM ERROR <n> [<k>x | <command>] | OFF` are
consumed by `SimSerial.write` and never reach the firmware port; the full verb table and semantics are in
`../tests/simulator/README.md`. They are parsed in `SimSerial._handle_sim` and `_apply_sim` in
`octoprint_grblsim.py`; a new verb goes there, with a test and a row in the README table. Each handled line is answered with one `ok`; a malformed one with
`[MSG:SIM ERROR <reason>]` then `ok`.

**Through OctoPrint they arrive by a hook.** OctoPrint treats any line starting with `@` as an at-command: its send
loop handles it and continues without writing it to the port (`serial_comm.py` in `plugins/serial_connector` on 2.0
and `util/comm.py` on 1.11, the `command.startswith("@")` branch of the sending loop). The plugin therefore registers
`octoprint.comm.protocol.atcommand.sending` (`GrblSimPlugin.atcommand_sending`, which calls `SimSerial.inject`);
for the at-command `SIM` it applies `@SIM <parameters>` to the live simulated port of that connection. The hook
signature is the same on both lines. Send it from the terminal or with
`POST /api/printer/command {"command": "@SIM ALARM 1"}`. Nothing was sent to the port, so this path queues no `ok`;
the replies the verb produces (`ALARM:1`, or `[MSG:SIM ERROR <reason>]`) arrive as usual. OctoPrint does not run
at-command hooks while it is streaming a print, so inject faults before the job starts. Check 6 (`atsim`)
proves the path on every run; the direct `SimSerial.write` path (section 3) is unchanged.

**`@SIM ERROR` and the connect handshake.** The verb makes the next matching line, or the next `k` lines, come back
as `error:<n>` with no `ok` (the line is rejected, never run). The armed entries are kept on `SimSerial` itself, not
on one connection, so they carry over a reconnect; that is the only way to reach the handshake, because the plugin
sends `$I`, `$G` and `$#` the moment it connects. Recipe, from any rig shell (`rt` and the rig variables from
section 5):

```bash
rt connect GRBL_SIM                                   # a first connection, so the verb has a live port to arrive on
curl -s -X POST -H "X-Api-Key: $(cat "$RIG/apikey")" -H "Content-Type: application/json" \
  -d '{"command": "@SIM ERROR 20 $G"}' "127.0.0.1:$RIG_PORT/api/printer/command"
rt disconnect                                         # the armed error stays
rt connect GRBL_SIM                                   # this handshake's $G is answered error:20
grep -n "error:20" -B3 "$RIG/basedir/logs/serial.log" | tail -4
```

Run on a 1.11.8 rig, the last command printed `Recv: [VER:1.1h.20190830:]`, `Recv: [OPT:V,15,128]`, `Recv: ok` (the end
of the `$I` reply) and then `Recv: error:20`, the slot where the `$G` reply belongs. A line typed by the user in the
first seconds (`G1 X1` answers `error:22` without help) needs no arming. `@SIM ERROR OFF` or an OctoPrint
restart clears armed entries.

## 8. Update the ports when gnea/grbl or FluidNC change

The pins are the `SRC` (and `VERSION`, `BUILD`, `GIT_INFO`, `GRBL_VERSION`) constants at the top of each module;
every emitting method and handler carries `Source: <file>:<function> @ <sha> (lines a-b)`.

1. Get both firmware trees at the new commits (network; not re-run here). The checker wants the gnea
   `grbl/` directory and FluidNC's `FluidNC/src/` directory:
   ```
   git clone https://github.com/gnea/grbl.git       <scratch>/grbl    && git -C <scratch>/grbl    checkout <sha>
   git clone https://github.com/bdring/FluidNC.git  <scratch>/FluidNC && git -C <scratch>/FluidNC checkout <sha>
   ```
2. Run the citation checker against the **old** pins first (see below) to prove the tool and your checkout agree,
   then diff old and new trees for the cited files.
3. For every cited function whose body changed, update the port, re-read the cited lines, change the
   `(lines a-b)` and the sha in the docstring, and bump `SRC`. Ranges that moved without a behaviour change
   still need the line numbers updated; the checker reports them.
4. Re-run the checker, both unit suites with a corpus (section 2), and the rig checks (section 6) on both
   OctoPrint lines. Rename or add tests by census id only if a row's behaviour changed.
5. Update the pins, counts and dates in sections 1 and 2 and the verification record at the end.

**`check_sources.py`** (save anywhere; run with plain `python3`):

```python
#!/usr/bin/env python3
"""Check every 'Source: <file>:<name> @ <sha> (lines a-b[, c-d])' citation against a firmware checkout.

usage: check_sources.py <grbl_virtual.py | fluidnc_virtual.py> <firmware source dir>
  gnea/grbl dir: <clone>/grbl         (holds protocol.c, gcode.c, ...)
  FluidNC dir:   <clone>/FluidNC/src  (holds Report.cpp, Configuration/, ...)
A citation FAILS when its sha differs from the module's SRC pin, its file is missing, its first range runs
past end of file, or its identifier appears nowhere in the file.  Of the passing ones, 'start' means the
identifier is on the range's first lines (a function-level citation); 'inner' means the range sits inside
the construct (a deliberate sub-range).  Names that are not one identifier (a/b, *x, spaces) are 'skipped'.
Exit 0 only when nothing failed."""
import os, re, sys
mod, root = sys.argv[1], sys.argv[2]
text = open(mod).read()
pin = re.search(r'^SRC = "([0-9a-f]+)"', text, re.M).group(1)
pat = re.compile(r"Source: ([\w./]+?):([^ @]+) @ ([0-9a-f]{7}) \(lines (\d+)-(\d+)")
n = {"start": 0, "inner": 0, "skipped": 0, "failed": 0}
seen = 0
for m in pat.finditer(text):
    seen += 1
    path, name, sha, a, b = m.groups()
    a, b = int(a), int(b)
    full = os.path.join(root, path)
    why = []
    if sha != pin:
        why.append("sha %s != pin %s" % (sha, pin))
    lines = open(full, errors="replace").read().splitlines() if os.path.isfile(full) else None
    if lines is None:
        why.append("no such file")
    elif b > len(lines):
        why.append("end line %d beyond EOF (%d)" % (b, len(lines)))
    if not why:
        inner = re.search(r"\((\w+)\)", name)
        ident = inner.group(1) if inner else re.split(r"::|:", name)[-1]
        if re.search(r"[*/ ]", name):
            n["skipped"] += 1; print("skip   %s:%s" % (path, name)); continue
        if not any(ident in l for l in lines):
            why.append("%r appears nowhere in the file" % ident)
        elif any(ident in l for l in lines[max(a - 2, 0):a + 1]):
            n["start"] += 1; continue
        else:
            n["inner"] += 1; continue
    n["failed"] += 1
    print("FAIL   %s:%s lines %d-%d: %s" % (path, name, a, b, "; ".join(why)))
if seen == 0:
    sys.exit("no citations found in %s: the pattern no longer matches the Source: format" % mod)
print("%s: pin %s, %d citations, %s" % (os.path.basename(mod), pin, seen, ", ".join("%d %s" % (v, k) for k, v in n.items())))
sys.exit(1 if n["failed"] else 0)
```

```
cd <repo>/tests/simulator
PYTHONDONTWRITEBYTECODE=1 python3 check_sources.py grbl_virtual.py    <scratch>/grbl/grbl
PYTHONDONTWRITEBYTECODE=1 python3 check_sources.py fluidnc_virtual.py <scratch>/FluidNC/FluidNC/src
```

(use the path where you saved the script). Expected at the current pins:

```
grbl_virtual.py: pin bfb67f0, 83 citations, 71 start, 12 inner, 0 skipped, 0 failed
skip   Configuration/Generator.cpp:Generator::enter/leave
skip   Settings.cpp:*Setting::getStringValue
fluidnc_virtual.py: pin 9b8236f, 72 citations, 68 start, 2 inner, 2 skipped, 0 failed
```

The checker proves each cited file exists, each range fits the file, the identifier is in the file, and the
sha equals the pin. It does not prove the ported behaviour is right: that is what the byte-exact tests are for.
Pointing it at the wrong tree is the negative control: `grbl_virtual.py` against the FluidNC tree prints
`grbl_virtual.py: pin bfb67f0, 83 citations, 0 start, 1 inner, 0 skipped, 82 failed` (one citation still resolves there),
and `fluidnc_virtual.py` against the gnea tree fails all 72. Finding no citations at all is an error, so a changed
`Source:` format cannot pass silently.

## 9. When to update this document

- A pin changes (`SRC` in either module): sections 1, 2 (counts) and 8 (expected checker output).
- A `@SIM` verb is added, changed or removed, or the at-command hook changes: sections 3 and 7, the
  README table, and `test_octoprint_grblsim.py`.
- A parking or door behaviour of a port changes, or a simulator option is added: sections 1 and 3, the README table,
  `docs/pause-resume.md` and `tests/pause/rig/` (the end-to-end pause scenarios use those options).
- A new check is added to `rigtool.py`, or the plugin changes what it sends or parses on connect, status, jog,
  alarm or filters: section 6.
- A new OctoPrint major or minor is used for rigs (venv pins, serial log keys, log prefixes, API-key rules): sections 4, 5, 6.
- The plugin defect in section 6 is fixed, or a new known false failure is found: that list.
- The census is committed to the repo: section 1's note about test ids.

Staleness test: re-run section 2, the section 3 smoke, the section 8 checker and one rig sequence and compare to
the expected output printed here; any difference means this document is behind.

## 10. Pause, parking and cancel on a rig

`tests/pause/rig/` drives the pause redesign (`pause-resume.md`) end to end: the plugin inside OctoPrint, the simulator
set up through the options in section 3, and a job that cuts at machine Z -20. Its README lists the scenarios and what each
must show; `matrix.sh` runs them against one rig. What a clean run measures (seconds from the first `Door:2`, both
OctoPrint lines, linear motion model so only the ratios mean anything):

| Scenario | Result |
|---|---|
| Grbl parking | `0x84` is the first byte after the pause request; Z -20 to -5 with the spindle off after the 5 mm pull-out; `Door:0` at 5.0 s; `~` goes out at `Door:0`; restore 8.2 s (fast return 1.2 s, spindle wait 4.0 s, plunge 3.0 s) back to Z -20 |
| FluidNC parking, `spinup_ms` 0 | `Door:0` at 2.3 s; restore 1.9 s, no spindle wait |
| FluidNC parking, `spinup_ms` 2000, `S10000` | spin-down wait before the fast retract (`Door:0` at 4.3 s), spin-up wait before the plunge (restore 3.9 s) |
| Z already above the target (-3) | spindle off at 0.3 s, Z unchanged, restore is the spindle wait only (4.0 s on Grbl); the plugin logs `parking did not move Z` |
| Parking on, controller cannot park | `drain`, the log line names the reason (Grbl without the `P` build, `$22=0`; FluidNC `enable` false, no homing cycle) |
| Resume asked 1.5 s into the retract | the `~` goes out at the first `Door:0` (Grbl 5.1 s, FluidNC 2.4 s), not at the request |
| `drain`, spindle mode | planner runs dry (9.6 s for this job), spindle off and Z -20 to -15 within 1 s, held there while paused; resume restores the spindle, waits `G4 P10`, lowers to -20 |
| `hold` / `laser` | `!` at once (and `0x9E` for `hold`), Z unchanged, spindle off; resume `~` |
| Cancel during a run, a pause, or the retract | Operational, no `ALARM`; during the retract the plugin waits for `Door:0` before the reset |

The first scenario run exposed a defect in the pause script this work started from, the `drain` choice: its trailing `!`
reached the controller a few milliseconds after the `G91 G0 Z5` lift, so the hold stopped the lift at its first step and the
tool stayed at Z -20 for the whole pause; the lift only ran after the resume. The script no longer holds.

## Verification record

Last run: 2026-10-05 against the plugin at `615a2539a6b5` on `devel` (read from `.git`; the working tree has the untracked
`tests/` and `docs/` described here), both firmware pins as above, macOS, Python 3.12.1.

- Unit suites: no corpus `Ran 372 tests in 0.247s` / `OK (skipped=2)`; with the 66-file corpus
  `Ran 372 tests in 63.818s` / `OK`, both summary lines as quoted in section 2.
- Smoke (section 3) and the environment controls, and the citation checker: last run before the 362-to-372 test additions;
  neither depends on them. Output as printed (checker positive on both modules; negative control on both wrong trees:
  82 of 83 and 72 of 72 failed).
- Rig reruns on the printed `setup.sh`, `ctl.sh` and `rigtool.py` (before the `max_seconds` argument and `ws_stop` were added):
  - OctoPrint 2.0.0rc5: `GRBL_SIM` checks 1 to 7 PASS with streams `calibration.gcode` 124 s and `testmove.gcode` 10 s;
    `FLUIDNC_SIM` XYZ checks 1 to 7 PASS with streams 78 s and 10 s; `FLUIDNC_SIM` XY checks 1 to 7 PASS (jog X and Y only)
    with the 78 s stream. Check 7 read `4 entries`, `null/absent` (API serving 5 defaults), `4 entries` in each.
  - OctoPrint 1.11.8: `GRBL_SIM` checks 1 to 6 PASS except the capped stream, 124 s calibration stream PASS, check 7 PASS in a separate run; the
    `spoilboard-surface.gcode` stream printed `FAIL` at the 1800 s cap (state `Printing`, 93.3 percent, no error or ALARM
    reply) and the job later finished in 2547.6 s (the `alarm` and `atsim` checks of that run therefore ran while it was still printing). `FLUIDNC_SIM` XYZ checks 1 to 7 PASS including the CNC stream
    (`spoilboard-surface.gcode`, 1277 s) and the 78 s calibration; `FLUIDNC_SIM` XY checks 1 to 7 PASS with the 78 s stream.
  - Both lines: `kill $(cat $RIG/ws.pid)` printed `No such process` at the end of check 7 because the server restarts
    end the collector; replaced by `ws_stop`.
- Rig re-run of the changed blocks (2.0.0rc5, `GRBL_SIM`, `setup.sh`/`ctl.sh`/`rigtool.py` extracted from this file):
  `rt stream <file> <ws> 20` printed `FAIL ... cap=20s CAP REACHED, job left running` with exit status 1; `ws_stop` printed
  `collector already exited` after the check-7 restarts. After `rig_stop`/`rig_start`, `rt stream <calibration.gcode> <ws> 300`
  printed `PASS ... elapsed=125s cap=300s sent=34 error/ALARM=0`, then `ws_stop` printed `collector stopped`. Re-running
  the stream right after cancelling the capped job, without a restart, printed `upload: FAILED` (the reason for that is
  not diagnosed; the `rig_stop`/`rig_start` advice in section 6 follows from it).
- Red-then-green in scratch (earlier work): the stream verdict (a job ending `Operational` with completion `None` printed
  PASS before the completion test and FAIL after, a completed one PASS in both) and `atsim` (FAIL with the hook registration
  removed from a scratch copy of `octoprint_grblsim.py`, PASS with it).
- Not re-run: `pip install` and `git clone` steps.
- Pause redesign (this change; the plugin tree and simulator ports as in the working tree, macOS, Python 3.12.1): unit suites
  `Ran 418 tests` / `OK (skipped=2)`, with the 66-file corpus `Ran 400 tests` / `OK` and the two summary lines
  unchanged; wrapper `Ran 32 tests` / `OK`; `tests/pause/test_pause_strategy.py` `Ran 23 tests` / `OK` on 2.0.0rc5 and
  1.11.8. Rig scenarios (`tests/pause/rig`, section 10) 28 per OctoPrint line, all as listed above, on 2.0.0rc5 and 1.11.8;
  settings dialog (checkbox, select, Save, reload) PASS on both. Mutation checks (one behaviour broken in a scratch copy,
  the named tests go red): 20 simulator and wrapper mutations and 14 plugin-decision mutations, all caught, plus one
  equivalent mutant (the firmware repeats the below-target comparison, so only breaking both copies is caught; see the test
  docstring).
