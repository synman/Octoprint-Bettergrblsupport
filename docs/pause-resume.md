# Pause, resume and cancel

How Better Grbl Support stops and restarts the machine when a job is paused, resumed or cancelled, which
settings change it, and what each choice sends. Applies to Grbl 1.1 and FluidNC on OctoPrint 1.11 and 2.0.
Code: `octoprint_bettergrblsupport/_bgs.py` (`pause_strategy`, `parking_available`, `write_pause_resume_scripts`,
`resume_hold`, `resume_from_park`, `finish_pause_hold`, `fake_ack`, `cancel_motion`, `send_realtime`), and the sending
and received hooks in `__init__.py` (`REALTIME_ALIASES`, the owed-ok swallow).

## What a pause does

The strategy is decided once, when OctoPrint starts pausing (`PAUSING`), and logged at INFO:
`pause: strategy [<name>] (<reason>)` in `octoprint.log`.

| Order | Condition | Strategy | Pause sends | Resume sends |
|---|---|---|---|---|
| 1 | Pre-1.1 Grbl (no feed-hold spindle control, no parking) | `drain` | as `drain`, for a laser `M5` only | as `drain` |
| 2 | Laser mode: Grbl `$32` not 0, FluidNC `Grbl/LaserMode` not 0 | `laser` | realtime `!` | realtime `~` at `RESUMING` |
| 3 | Setting "Pause with the controller's parking" on **and** the controller can park (below) | `park` | realtime `0x84` | realtime `~` once the controller reports `Door:0` |
| 4 | Setting "Pause without parking" is "Stop at once" | `hold` | realtime `!`, then `0x9E` once it reports `Hold:0` | realtime `~` at `RESUMING` |
| 5 | otherwise (default) | `drain` | wait for the planner to run dry, `M5`, lift Z 5 mm, restore the distance mode (and the units) | spindle word, `S`, `F`, `G4 P10`, lower Z 5 mm, restore the distance mode, the units and the motion mode |

* **Laser beats parking.** A laser job always stops with `!` alone: the controller switches the beam off while held,
  air assist keeps running unless the coolant setting or a subscription switches it off (below), and nothing lifts. A machine in spindle mode (`$32=0`) that happens to carry a diode laser
  gets the spindle behaviour of rows 3 to 5.
* **`park`** is the firmware's own safety-door routine: it decelerates to a stop, pulls the Z axis out with the spindle
  still on, switches the spindle off, retracts to its parking target, and reports `Door:2` until done, then `Door:0`.
  A cycle start is ignored until `Door:0`, so `resume_from_park` holds its `~` back and polls `?` until the controller
  says so. The resume then restores the spindle, waits for spin-up and plunges back to the exact stop
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

## Held controller: realtime only, bounded acknowledgements

For `laser`, `hold` and `park` the controller is held, and it acknowledges no line until the cycle start. Every line
OctoPrint sends meanwhile waits in its RX buffer: a user's event-subscription `M9` on PrintPaused, a plugin's line, the
pause script. OctoPrint sends one line per `ok`, so it waits too. Rules that follow:

* **Nothing the plugin needs is a line while held.** The pause and resume scripts for these strategies are written
  empty. The cycle start is a realtime `~`, sent by `resume_hold` on the `RESUMING` state change. That is the first
  thing OctoPrint emits for a resume, ahead of PrintResumed and its subscriptions. Before, the `~` was a script line,
  and a subscription's `M8` queued ahead of it could deadlock the resume.
* **Realtime commands by name** (`FEEDPLUS10`, `SPINDLEMINUS1`, `TOGGLESPINDLE`, `SAFETYDOOR`, `CANCELJOG`, ...) and a
  bare `!`, `~` or `\x18` go out as bytes through `send_realtime`, never as lines (`REALTIME_ALIASES` in
  `__init__.py`). Nothing is sent for them on pre-1.1 Grbl. The latin-1 serial encoding is no longer needed for them.
  The status request (`M105` and `M114` rewritten to `?`) is a realtime byte too. A status line would leave a newline
  that the controller acknowledges, an `ok` OctoPrint never spent.
* **Acknowledging on the controller's behalf** (`finish_pause_hold`) happens only while OctoPrint is pausing and its
  send loop is actually waiting for an `ok`. Each one lets exactly one line out, because OctoPrint's counter clamps at
  1. It stops after 100 bytes have been sent into the hold (gnea Grbl's RX buffer is 128 bytes) with a warning.
  OctoPrint then reaches Paused on its own timeout.
* **Owed oks.** Each acknowledgement given on the controller's behalf, and the `?` line `M400` becomes while held, is
  answered by the controller later. The received hook swallows that many `ok`s (`strayOksOwed`). A reset or a new
  connection clears the count.
* **Coolant (air assist) across a pause: the pause does not change the job's coolant unless asked to.** At
  `PAUSING` the plugin records the coolant state (`coolantAtPause`).
  * With the setting "A pause switches coolant (air assist) off" (`pauseCoolantOff`, default off), a `laser` or
    `hold` pause switches it off with the realtime flood toggle `0xA0`. A `drain` pause's script carries an `M9` line
    instead. A `park` leaves it to the firmware, which switches coolant off and back on itself.
  * An `M8` or `M9` sent while OctoPrint is pausing, paused or resuming is applied at once through the same path:
    the toggle when it changes the state, the `M8`/`M9` shell command when configured, and the tracked state. That
    covers an event subscription's `M9` on PrintPaused. It applies on Grbl 1.1-class firmware. Pre-1.1 keeps the lines.
  * At `RESUMING`, before the `~`, the plugin switches coolant back on with the toggle if it was on at the pause and
    is off now. A subscription's `M8` on PrintResumed arrives after OctoPrint is already printing, so it goes out as a
    line. It then matches the parser's state, and Grbl does not stall the planner for an unchanged coolant word.
  * The report's `|A:` field corrects the tracked state. It is trusted both ways only in a report that carries the
    `|Ov:` block, because Grbl prints `|A:` inside that block only when something is on.
  * Mist (`M7`) is never toggled.
* User lines queued during a hold still run, after the resume. A synchronising line (`M3`/`M5`/`M7`/`G4`) then waits
  for the planner to run dry, and OctoPrint stays in "Resuming" until its `ok`.

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

`!`, wait until the machine reports it has stopped (up to 10 s, 30 s while a parking motion is still running), then a
soft reset. While it waits the plugin treats the machine as held (`M400` becomes a `?`), and an `M7` or `M8` line is
dropped: a cancel never switches coolant on, even with a subscription's `M8` still waiting in OctoPrint's queue. The reset ends the hold and
clears the owed oks. Lines from the pause or resume scripts still queued are dropped by their OctoPrint tags.

In laser mode the plugin sends `M3 S0` then `M5` the moment the controller's banner arrives after the reset (or after 2 s
without one). Some firmware (the
LaserUpgrades "EleksMocker" Grbl 1.1f fork) relights the laser coming out of a reset from a hold
(`[MSG:Restoring spindle]`) while its parser reports `M5 S0`. There, `M5` or `S0` alone change no modal state and do not
clear it, but `M3 S0` does. On other firmware the two lines change nothing. They are never sent for a spindle, which
`M3` would switch on. The wait list includes `Door:2` and `Door:3`.
The controller ignores `!` in a door state and, when the restore after a park finishes, resumes the buffered job by
itself. So when the wait has seen `Door:2` or `Door:3` and then sees `Run` or `Hold:1`, the plugin sends `!` again
before the reset; without it a cancel during the plunge back keeps cutting until the planner is dry.

## Settings

Settings, Better Grbl Support:

* **Pause with the controller's parking (safety door)**, `pauseParking`, default off.
* **Pause without parking**, `pauseMode`: `drain` (default, "Finish moves, lift Z") or `hold` ("Stop at once, no lift").
* **A pause switches coolant (air assist) off**, `pauseCoolantOff`, default off. The resume puts it back.

Both are read when the settings are saved and at startup; a pause in progress is not affected.

## Tests

* Decision logic and the scripts written for each strategy: `tests/pause/test_pause_strategy.py` (needs OctoPrint
  importable; run it with a rig's interpreter, see its docstring). The modal state the scripts restore from is followed
  by `_bgs.track_modal_state`, tested in `tests/pause/test_modal_state.py`. The position estimate kept from sent lines
  (a `$J=` jog is relative or absolute by its own `G91`/`G90`) is `tests/pause/test_jog_estimate.py`. The feed and power overrides, which the resume lines pass through, are
  `tests/pause/test_feed_override.py` (the real sending hook and API handler).
* The hold flow (realtime cycle start, bounded acknowledgements, owed oks, realtime aliases, coolant while paused,
  cancel ending the hold): `tests/pause/test_hold_flow.py`. What it relies on in the firmware (a held planner
  acknowledges nothing, a status line's newline is acknowledged after the resume, `0xA0` in a hold): `HoldFlow` in
  `tests/simulator/test_grbl_virtual.py`.
* The firmware behaviour the strategies rely on: the parking suites in `tests/simulator/test_grbl_virtual.py` and
  `test_fluidnc_virtual.py` (`Parking`), documented in `simulator-testing.md`.
* End to end on a rig, the plugin driving OctoPrint, the simulator and a job: `tests/pause/rig/` (README there lists the
  scenarios and what each must show). They set the simulator options for the connection (`plugins.octoprint_grblsim.*`,
  `simulator-testing.md` section 3) and the two plugin settings, cut below machine Z -5, pause, resume, cancel, and read
  `serial.log`, `octoprint.log` and, with `GRBL_SIM_TRACE`, the machine's own position and state.
