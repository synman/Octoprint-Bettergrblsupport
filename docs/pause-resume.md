# Pause, resume and cancel

How Better Grbl Support stops and restarts the machine when a job is paused, resumed or cancelled, which
settings change it, and what each choice sends. Applies to Grbl 1.1 and FluidNC on OctoPrint 1.11 and 2.0.
Code: `octoprint_bettergrblsupport/_bgs.py` (`pause_strategy`, `parking_available`, `write_pause_resume_scripts`,
`resume_from_park`, `cancel_motion`), the `PARKRESUME` token in `__init__.py` `hook_gcode_sending`.

## What a pause does

The strategy is decided once, when OctoPrint starts pausing (`PAUSING`), and logged at INFO:
`pause: strategy [<name>] (<reason>)` in `octoprint.log`.

| Order | Condition | Strategy | Pause sends | Resume sends |
|---|---|---|---|---|
| 1 | Pre-1.1 Grbl (no feed-hold spindle control, no parking) | `drain` | as `drain`, for a laser `M5` only | as `drain` |
| 2 | Laser mode: Grbl `$32` not 0, FluidNC `Grbl/LaserMode` not 0 | `laser` | `!` | `~` |
| 3 | Setting "Pause with the controller's parking" on **and** the controller can park (below) | `park` | realtime `0x84` | `~` once the controller reports `Door:0` |
| 4 | Setting "Pause without parking" is "Stop at once" | `hold` | `!`, then the spindle-stop override `0x9E` | `~` |
| 5 | otherwise (default) | `drain` | wait for the planner to run dry, `M5`, lift Z 5 mm, restore the distance mode (and the units) | spindle word, `S`, `F`, `G4 P10`, lower Z 5 mm, restore the distance mode, the units and the motion mode |

* **Laser beats parking.** A laser job always stops with `!` alone: the controller switches the beam off while held,
  air assist keeps running, and nothing lifts. A machine in spindle mode (`$32=0`) that happens to carry a diode laser
  gets the spindle behaviour of rows 3 to 5.
* **`park`** is the firmware's own safety-door routine: it decelerates to a stop, pulls the Z axis out with the spindle
  still on, switches the spindle off, retracts to its parking target, and reports `Door:2` until done, then `Door:0`.
  A cycle start is ignored until `Door:0`, so the resume script holds its `~` back (`PARKRESUME`) and polls `?` until
  the controller says so. The resume then restores the spindle, waits for spin-up and plunges back to the exact stop
  point (`Door:3`). Cancelling while it moves waits for it to finish (up to 30 s): a reset during a parking motion is
  an aborted cycle (`ALARM:3`).
* **`drain`** lets the buffered moves finish before anything else happens, so the pause can take as long as the
  controller needs to run through its planner (Grbl holds about 15 blocks plus what is in the serial buffer: seconds
  for short CAM segments, much longer for long slow moves). `hold` pauses at once and keeps the tool where it is.
* The `drain` lift is a fixed 5 mm relative move away from the work (`G91 G0 Z5`); the plunge is the same move back
  (`G91 G0 Z-5`). Away from the work is the Control tab's "up", so with Invert Z on the lift is `Z-5` and the plunge
  `Z5`. With soft limits on and Z within 5 mm of its limit in the lift direction the controller raises a soft-limit
  alarm; the plugin does not check.
* The lift and the plunge are 5 **mm**. In an inch job (`G20`) the scripts switch to `G21` around them and back to
  `G20` after (`G21`, `G91 G0 Z5`, `G90`, `G20`; the resume mirrors it), because the controller reads `Z5` in the
  job's units (127 mm in `G20`). The Z moves also leave the controller in `G0`, so the resume script ends with a bare
  `G1` when the job was in `G1` and a feed rate is known (a `G1` with none is `error:22`), so a job line with no G word
  of its own does not run as a rapid. The feed rate the resume writes back is the F word of the last line the job
  sent, in the job's own units: the `FS:` feed in a status report is always mm/min (unless `$13=1`), so an inch job
  resumed from it would run 25.4 times too fast. The power it writes back is the S word of the last job line likewise.
  Both are the programmed values, before the Control tab's feed and power overrides: the resume lines go through the
  sending hook like every line, which applies an override once. Only lines OctoPrint tags `source:file` count as the
  job's own; a jog, a probe or framing move, or the line an override change sends does not move them. A job paused in
  `G2`/`G3` gets no restore (`G2` without axis words is `error:26`); its next arc line has to repeat the G word.
  The plugin follows the controller's distance mode, units and motion mode from the lines it sends (`$G` replies
  correct it when they arrive): `$J=` jogs do not change the parser's state and are ignored, `G90.1`/`G91.1` are the
  arc-centre mode and are not the distance mode, and text inside comments does not count.

## When the controller can park

Read from what the plugin fetched at connect, with no query at pause time:

| Firmware | Parking is available when |
|---|---|
| Grbl | `[OPT:` letters (before the first comma) contain `P`, **and** `$22` (homing) is not 0. Laser mode is excluded by row 2 above. |
| FluidNC | `$CD` shows `parking: enable: true`, **and** the parking axis (`parking: axis`, default Z) has a `homing:` section with `cycle` 0 or more. Laser spindles are excluded by row 2. The `P` in FluidNC's `[OPT:PHS]` is always present and is never used. |

If the setting is on but the controller cannot park, the pause falls back to the "without parking" choice and the log
line says why (`parking is enabled but unavailable: ...`). A `$22` or `$/parking/enable` changed in the terminal is not
seen until the next connect (or, for FluidNC, a config save that re-reads `$CD`).

What the plugin cannot know:

* The parking target, rates and pull-out distance. Grbl's are compile-time constants; FluidNC's are
  `parking:` keys that the plugin does not read. If Z is already above the target the firmware only stops the
  spindle. The plugin logs `resume: parking moved Z by ...` or a warning `parking did not move Z`.
* Spin-up. Grbl waits a fixed 4 s after restoring the spindle. FluidNC waits `spinup_ms` of the spindle section, which
  defaults to 0 and exists only for spindle types with a mechanical ramp (not the laser), so a FluidNC resume may plunge
  before the spindle is at speed unless `spinup_ms` is set in the machine config.
* A real door switch. With one wired and open the controller stays in `Door:1`; the resume waits for it (up to 60 s,
  then sends `~` anyway with a warning, which the controller drops).

## Cancel

Unchanged by the redesign: `!`, wait until the machine reports it has stopped (up to 10 s, 30 s while a parking motion
is still running), then a soft reset. The wait list includes `Door:2` and `Door:3`.
The controller ignores `!` in a door state and, when the restore after a park finishes, resumes the buffered job by
itself. So when the wait has seen `Door:2` or `Door:3` and then sees `Run` or `Hold:1`, the plugin sends `!` again
before the reset; without it a cancel during the plunge back keeps cutting until the planner is dry.

## Settings

Settings, Better Grbl Support:

* **Pause with the controller's parking (safety door)**, `pauseParking`, default off.
* **Pause without parking**, `pauseMode`: `drain` (default, "Finish moves, lift Z") or `hold` ("Stop at once, no lift").

Both are read when the settings are saved and at startup; a pause in progress is not affected.

## Tests

* Decision logic and the scripts written for each strategy: `tests/pause/test_pause_strategy.py` (needs OctoPrint
  importable; run it with a rig's interpreter, see its docstring). The modal state the scripts restore from is followed
  by `_bgs.track_modal_state`, tested in `tests/pause/test_modal_state.py`. The position estimate kept from sent lines
  (a `$J=` jog is relative or absolute by its own `G91`/`G90`) is `tests/pause/test_jog_estimate.py`. The feed and power overrides, which the resume lines pass through, are
  `tests/pause/test_feed_override.py` (the real sending hook and API handler).
* The firmware behaviour the strategies rely on: the parking suites in `tests/simulator/test_grbl_virtual.py` and
  `test_fluidnc_virtual.py` (`Parking`), documented in `simulator-testing.md`.
* End to end on a rig, the plugin driving OctoPrint, the simulator and a job: `tests/pause/rig/` (README there lists the
  scenarios and what each must show). They set the simulator options for the connection (`plugins.octoprint_grblsim.*`,
  `simulator-testing.md` section 3) and the two plugin settings, cut below machine Z -5, pause, resume, cancel, and read
  `serial.log`, `octoprint.log` and, with `GRBL_SIM_TRACE`, the machine's own position and state.
