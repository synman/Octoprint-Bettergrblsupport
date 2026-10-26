# Pause, resume and cancel on a rig

End-to-end scenarios for the pause redesign (`docs/pause-resume.md`): the plugin inside a real OctoPrint, the simulated
Grbl or FluidNC (`tests/simulator`), and a job that cuts below machine Z -5. Standard library only. They need a running
rig that carries the plugin and `octoprint_grblsim.py` (build one as `docs/simulator-testing.md` section 5 says, one per
OctoPrint line) and read two environment variables: `RIG` (the rig directory, with `basedir/` and `apikey`) and
`RIG_PORT`.

| File | What it is |
|---|---|
| `rig_api.py` | REST calls, `serial.log` / `octoprint.log` readers, connect, upload, job control. |
| `scenarios.py` | `scenario` (pause, wait 8 s, resume, run on, cancel), `early_resume` (resume while the controller is still retracting), `cancel_scenario` (cancel during a run, during a pause, during the retract), `reset`. |
| `matrix.sh` | Every scenario below against one rig, one after another (about 25 minutes). |
| `digest.py` | One summary line per evidence file. |

```
export RIG=<rig dir> RIG_PORT=<port>
./matrix.sh grbl   v20 /scratch/pause-evidence      # GRBL_SIM on the rig
./matrix.sh fluidnc v20 /scratch/pause-evidence     # FLUIDNC_SIM on the rig
python3 digest.py /scratch/pause-evidence
python3 scenarios.py reset                          # put the simulator options and pause settings back to defaults
```

Each scenario sets the simulator options and the two pause settings through `POST /api/settings` (no restart), so the
next connection is built to order; see `tests/simulator/README.md` for the options. `GRBL_SIM_TRACE` (set by the
scenario) records the machine's own state and position, because OctoPrint suppresses status requests while a job runs.
Each run prints what was sent and received after the pause (and after the resume), the plugin's `pause: strategy`,
`resume:` and `cancel:` log lines, and the machine trace, with `t=0` at the request.

| Scenario | Simulator | Plugin settings | Expect |
|---|---|---|---|
| `park_on` | parking build and `$22=1` / FluidNC `PWM` spindle, homing XYZ, `parking: enable` | parking on | strategy `park`: `\x84` at once, `Door:2` then `Door:0` at Z -5 with the spindle off; resume sends `~` only then, restores the spindle and plunges back to Z -20 |
| `park_nodetect` | no parking (Grbl without the build / FluidNC with `enable` false) | parking on | strategy `drain`, log says why |
| `park_nohoming` | `$22=0` / FluidNC without a homing cycle | parking on | strategy `drain`, log says why |
| `drain` | stock, spindle mode (`$32=0`) | defaults | planner runs dry, `M5`, `G91 G0 Z5`, `G90`; Z at -15 with the spindle off while paused; resume restores the spindle, waits `G4 P10`, lowers |
| `drain_inch` | stock, spindle mode (`pk_inch.gcode`: `G20`, Z -0.2 in, lines with no G word) | defaults | the scripts switch to `G21` around the 5 mm lift and plunge and back to `G20`; Z while paused is -0.08 (a 5 mm lift from -5.08), not 122; the resume ends `G90`, `G20`, `G1` and the bare lines go out as `G1` |
| `hold` | stock, spindle mode | no parking, "stop at once" | `!` then `0x9E`, Z unchanged, spindle off; resume `~` |
| `laser` | laser mode, parking available | parking on | strategy `laser`: `!` only |
| `park_zabove` | as `park_on`, job at Z -3 | parking on | spindle stops, Z unchanged, resume waits for spin-up, no plunge; log warns `parking did not move Z` |
| `park_spinup` | as `park_on`; FluidNC `spinup_ms` 2000 with an `S10000` job | parking on | FluidNC waits the spin-down and spin-up in place; Grbl waits 4 s |
| `early_resume` | as `park_on` | parking on | the `~` goes out at or after the first `Door:0` |
| `C_*_run`, `C_*_pause_*` | stock / parking | | cancel returns to Operational, no `ALARM` |
| `C_*_retract_park` | as `park_on` | parking on | cancel waits out the retract (up to 30 s), no `ALARM:3` |
| `C_*_restore_park` | as `park_on` | parking on | cancel 1 s into the plunge back (`Door:3`): the plugin holds again when the controller resumes by itself, so the job does not cut on (`Run` under 3 s after the cancel) |

The rig keeps the options it was last given in its `config.yaml`; run `reset` when done.
