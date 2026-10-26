# Virtual Grbl and FluidNC for Better Grbl Support

Pure-Python virtual firmware, used only to test the plugin without hardware. Fidelity to the pinned firmware
sources is the goal.

| File | What it is |
|---|---|
| `grbl_virtual.py` | `GrblVirtual`, a port of gnea/grbl 1.1h (pinned `bfb67f0`). `GrblVirtual(parking=True)` is the same build with `PARKING_ENABLE` (safety-door parking motions, `[OPT:VP,...]`); `homing=True` powers it up with `$22=1`. |
| `fluidnc_virtual.py` | `FluidNCVirtual(GrblVirtual)`, a port of FluidNC 3.9 (pinned `9b8236f`). Every emitting method cites `Source: <file>:<function> @ 9b8236f (lines a-b)`. Parking is the runtime `parking:` config section (`Parking.cpp`); `spindle="pwm"` gives a CNC spindle with `spinup_ms`/`spindown_ms`. |
| `fluidnc_web.py` | `FluidNCWebServer`, a port of FluidNC's WebUI `/files` endpoint (`WebUIServer.cpp` @ `9b8236f`): multipart upload into the simulator's LocalFS, file list JSON, delete and rename. It is what Better Grbl Support's HTTP config save talks to. |
| `test_fluidnc_web.py` | Unit tests for `fluidnc_web.py` against a real `FluidNCVirtual`. |
| `test_grbl_virtual.py`, `test_fluidnc_virtual.py` | Byte-exact unit tests named by census id, plus an optional corpus stream. |
| `octoprint_grblsim.py` | Single-file OctoPrint plugin that exposes both ports as serial ports. |
| `test_octoprint_grblsim.py` | Unit tests for the plugin's serial wrapper (the `@SIM ERROR` fault escape, over both ports) and for the environment options the serial factory reads. |

## Run the tests

```
cd tests/simulator
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_grbl_virtual test_fluidnc_virtual test_fluidnc_web
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_octoprint_grblsim     # the serial wrapper, about 6 s
```

Set `GRBL_SIM_CORPUS=<dir of .gcode files>` to also stream a G-code corpus through each port and assert no
`error:` or `ALARM:` replies. Without it those tests are skipped.

## Use the simulator as an OctoPrint serial port

`octoprint_grblsim.py` is a single-file plugin. Copy or symlink it into `<OctoPrint basedir>/plugins/`; a
symlink works because the module puts its own real directory on `sys.path` to find the two firmware ports.
Restart OctoPrint, then pick `GRBL_SIM` or `FLUIDNC_SIM` in the connection panel (any baud rate).

It works on OctoPrint 1.11.8 and 2.0.0rc5 (`__plugin_pythoncompat__ = ">=3.7,<4"`). It has no dependency
outside this directory. It imports without OctoPrint installed, so it can be driven from a plain script.
Both lines were exercised in this repo's work, on throwaway rigs: OctoPrint 2.0.0rc5 and 1.11.8, each with
`GRBL_SIM` and `FLUIDNC_SIM` (connect and `@SIM ALARM 1` through `/api/printer/command`).

Hooks: `octoprint.comm.transport.serial.factory` (returns `None` for any other port name),
`octoprint.comm.transport.serial.additional_port_names`, and `octoprint.comm.protocol.atcommand.sending`
(forwards `@SIM` lines to the live simulated port, see below).

### Settings and environment

| Control | Effect | Default |
|---|---|---|
| env `GRBL_SIM_FLUIDNC_AXES`, else plugin setting `fluidnc_axes` | FluidNC axis letters: a prefix of `XYZABC`, 2 to 6 letters. Anything else makes the connection fail with the `ValueError`. The environment wins when set. | `XYZ` |
| env `GRBL_SIM_FLUIDNC_PROBE`, else plugin setting `fluidnc_probe_pin` | Whether the FluidNC machine has a probe pin. Without one, `G38.x` answers `[MSG:ERR: Probe pin is not configured]`. Env values `0`, `false`, `no`, `off` disable it. | on |
| env `GRBL_SIM_GRBL_PARKING`, else plugin setting `grbl_parking` | Build `GRBL_SIM` with `PARKING_ENABLE`: `$I` reports `[OPT:VP,15,128]` and a safety-door byte (`0x84`) retracts Z as `protocol_exec_rt_suspend` does (needs `$22=1` and laser mode off; pull-out 5 mm at 100 mm/min with the spindle on, spindle off, fast to machine Z -5 at 500 mm/min; resume restores, waits 4 s, plunges). | off |
| env `GRBL_SIM_GRBL_HOMING`, else `grbl_homing` | Power `GRBL_SIM` up with `$22=1`: it starts in Alarm until `$X` or `$H`. | off |
| env `GRBL_SIM_GRBL_LASER`, else `grbl_laser` | Power `GRBL_SIM` up with `$32=1` (laser mode). | off |
| env `GRBL_SIM_FLUIDNC_SPINDLE`, else `fluidnc_spindle` | FluidNC spindle: `laser` (a `laser` section), `none` (`NoSpindle`) or `pwm` (a `PWM` section: CNC spindle, speed map 0..10000, `spinup_ms`, `spindown_ms`). | `laser` |
| env `GRBL_SIM_FLUIDNC_HOMING`, else `fluidnc_homing` | Axes that get a homing cycle in the FluidNC config, for example `XYZ` (the machine then starts in `ALARM:14` until `$X`). Parking needs one on its axis. | none |
| env `GRBL_SIM_FLUIDNC_PARKING`, else `fluidnc_parking` | Set `parking: enable: true` in the FluidNC config (`$/parking/enable`). | off |
| env `GRBL_SIM_FLUIDNC_SPINUP_MS`, else `fluidnc_spinup_ms` | `spinup_ms` and `spindown_ms` of the `PWM` spindle (needs `pwm`), waited on the door path only. | 0 |
| env `GRBL_SIM_TRACE`, else `trace_file` | Append `{"t","state","mpos","spindle"}` as one JSON line every 0.2 s and at every change of state while a port is open: the machine's own view, for tests that cannot poll it (OctoPrint suppresses status requests while a job runs). | off; the file is opened for append when the port opens, so an unwritable path fails the connection |
| env `GRBL_SIM_FLUIDNC_HTTP_PORT` | Start the FluidNC WebUI `/files` server (`fluidnc_web.py`) on `localhost:<port>` inside the OctoPrint process, and boot every `FLUIDNC_SIM` connection with `HTTP/Enable=ON`, `Hostname=localhost`, `HTTP/Port=<port>`. Better Grbl Support then saves a FluidNC config over HTTP instead of the serial line. Give a free port number. Unset or empty: no web server, `HTTP/Enable=OFF`. | unset |

The plugin settings live under `plugins.octoprint_grblsim.<name>` (the plugin's identifier is its module name), so a
rig can reconfigure the next connection without a restart: `POST /api/settings
{"plugins": {"octoprint_grblsim": {"grbl_parking": true}}}`. Each connection builds a new machine, so a `$22=1` or a
`parking` edit typed into the terminal is gone after a reconnect; the options above are the way to keep them.

`GRBL_SIM` is always the stock three-axis XYZ build (with or without parking).

The web server is one per OctoPrint process and serves whichever `FLUIDNC_SIM` connection is current; its LocalFS
is that connection's, so it starts from the default `config.yaml` on every connect. Check it with
`curl http://localhost:<port>/files` (file list JSON).

### Serial object contract

`SimSerial` provides `write(bytes) -> int`, `readline() -> bytes` (`b""` on timeout), `close()` (stops the
tick thread), `cancel_read()`, `cancel_write()`, `flush()`, and the attributes `port`, `baudrate`, `timeout`,
`write_timeout`, `is_open`, `in_waiting`. A daemon thread calls the firmware port's `tick()` every 10 ms on
the wall clock; every access to the firmware port is under one lock. A failure in the tick thread is logged,
and later `write`/`readline` calls raise `SerialException`. The firmware object is available as `.sim`.

## Fault injection: `@SIM` lines

A written line that starts with `@SIM ` at a line start is consumed by the serial wrapper and never reaches
the firmware. A realtime byte such as `?` sent on its own does not hide a following `@SIM` line. Each handled
line is answered with one `ok`, as OctoPrint expects one acknowledgement per line it sent. A malformed line is
answered `[MSG:SIM ERROR <reason>]` then `ok`, and logged as a warning.

Through OctoPrint: OctoPrint handles a line starting with `@` as an at-command and never writes it to the
port, so the plugin's `octoprint.comm.protocol.atcommand.sending` hook hands `@SIM ...` to the live simulated
port of that connection. Type it in the terminal or `POST /api/printer/command {"command": "@SIM ALARM 1"}`.
Nothing was sent to the port, so this path queues no `ok`; any reply the verb produces (`ALARM:1`, or a
`[MSG:SIM ERROR ...]` line) is delivered as usual. OctoPrint does not run at-command hooks while it is
streaming a print, so inject faults before the job starts or from a script that calls `SimSerial.write`.

| Line | Effect |
|---|---|
| `@SIM ALARM <n>` | Raise `ALARM:<n>` as the hardware paths do (`inject_alarm`). Codes 1 and 2 are critical events. `GRBL_SIM`, as gnea/grbl: the firmware ignores everything, status polls included, until a soft reset (`0x18`), after which the machine is locked (`$X` unlocks). `FLUIDNC_SIM`, as FluidNC: the Critical state keeps answering (`?`, `$G`, `$X` as a no-op, `$H`, settings), gcode is refused with `error:9`, and only a soft reset or a homing cycle leaves it. The reset lands in Idle, or in `ALARM:14` after alarm 1 (hard limit), which marks the axes unhomed. |
| `@SIM PROBE <axis><mm>` | Put the virtual probe contact plane for that axis at that machine coordinate, for example `@SIM PROBE Z-5.0`. The axis must be one of the machine's letters. One plane per command; send several for several axes. |
| `@SIM PROBE OFF` | Remove every contact plane and release a forced probe pin, so the next `G38.2` ends in `ALARM:5`. |
| `@SIM LIMIT <letters>` | Hold those axes' limit pins active, for example `@SIM LIMIT X`, `@SIM LIMIT XY`, `@SIM LIMIT X Y`. They show as `Pn:` in status reports and trip a hard-limit alarm when hard limits are enabled. Replaces the previous set. |
| `@SIM LIMIT OFF` | Release all limit pins. |
| `@SIM ERROR <n>` | Answer the next line written to the port with `error:<n>` instead of running it, and send no `ok`, as a controller does when it rejects a line. |
| `@SIM ERROR <n> <k>x` | The same for the next `k` lines, for example `@SIM ERROR 20 3x`. |
| `@SIM ERROR <n> <command>` | The same, once, for the next line whose text equals `<command>` (case and spacing ignored), for example `@SIM ERROR 20 $G` or `@SIM ERROR 22 G1 X1`. Lines that arrive meanwhile are answered normally. Empty lines never count. |
| `@SIM ERROR OFF` | Disarm every pending error. |

**Armed errors belong to the plugin, not to one connection**, and are consumed by whichever simulated port sees the
matching line next. That is what makes the connect handshake reachable: the plugin sends `$I`, `$G`, `$#` before any
terminal line can be typed. Recipe: connect, `POST /api/printer/command {"command": "@SIM ERROR 20 $G"}`,
disconnect, connect again; the `$G` of the second handshake is answered `error:20` (`Recv: error:20` in
`serial.log`). Armed errors are cleared by `@SIM ERROR OFF` or an OctoPrint restart.

## Smoke without OctoPrint

```python
import sys
sys.path.insert(0, "tests/simulator")
import octoprint_grblsim as m
m.__plugin_load__()
factory = m.__plugin_hooks__["octoprint.comm.transport.serial.factory"]
port = factory(None, "GRBL_SIM", 115200, 0.2)       # or "FLUIDNC_SIM"
port.write(b"?\n")
line = port.readline()                               # b"" on timeout
port.close()
```
