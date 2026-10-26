"""Pause, resume and cancel scenarios on a rig: the plugin driving OctoPrint, the simulator and a real job.

    RIG=<rig dir> RIG_PORT=<port> [EVIDENCE=<dir>] [LABEL=<suffix>] python3 scenarios.py <scenario> <args...>

Each scenario sets the simulator options and the two plugin pause settings through ``POST /api/settings`` (so no server
restart), connects, runs a job that cuts at machine Z -20 (or -3), and prints what crossed the wire, what the plugin
logged and the simulator's own state and position (``GRBL_SIM_TRACE``).  See README.md for the scenario list.
Standard library only; the rig must carry ``octoprint_grblsim.py`` and the plugin (docs/simulator-testing.md section 5).
"""
import json
import os
import sys
import time

from rig_api import (RIG, api, command, connect, format_rows, job, job_state, mark, plugin_command, plugin_log,
                     select, serial_rows, upload, wait_for, wait_job)

sys.stdout.reconfigure(line_buffering=True)
EVIDENCE = os.environ.get("EVIDENCE", os.path.join(os.getcwd(), "evidence"))
LABEL = os.environ.get("LABEL", "x")
os.makedirs(EVIDENCE, exist_ok=True)

SIM_DEFAULTS = {"grbl_parking": False, "grbl_homing": False, "grbl_laser": False, "fluidnc_spindle": "laser",
                "fluidnc_homing": "", "fluidnc_parking": False, "fluidnc_spinup_ms": 0}
PLUGIN_DEFAULTS = {"pauseParking": False, "pauseMode": "drain"}
PLUGIN_LOG = (r"octoprint\.plugins\.bettergrblsupport.*(pause: |resume: |WARNING|ERROR|cancel:)|Traceback")
JOBS = {"pk_cut.gcode": (-20, 1000), "pk_cut_z3.gcode": (-3, 1000), "pk_cut_s10k.gcode": (-20, 10000)}


def write_jobs():
    """Small squares at F500 (the simulator's Grbl caps rapids and feeds at $110=500): each side takes about 0.6 s, so
    the planner's 15 blocks drain in about 9 s."""
    for name, (z, spindle) in JOBS.items():
        lines = ["G21", "G90", "G0 Z%s" % z, "M3 S%s" % spindle]
        for _ in range(120):
            lines += ["G1 X5 Y0 F500", "G1 X5 Y5", "G1 X0 Y5", "G1 X0 Y0"]
        lines += ["M5", "G0 Z0"]
        with open(os.path.join(RIG, name), "w") as file:
            file.write("\n".join(lines) + "\n")
    # an inch job (G20) whose lines carry no G word after the first: the drain lift and plunge must stay 5 mm, and the
    # bare lines must keep running as G1 after the resume
    lines = ["G20", "G90", "G0 Z-0.2", "M3 S1000", "G1 X0.2 Y0 F20"]
    for _ in range(150):
        lines += ["X0.2 Y0.2", "X0 Y0.2", "X0 Y0", "X0.2 Y0"]
    lines += ["M5", "G0 Z0"]
    with open(os.path.join(RIG, "pk_inch.gcode"), "w") as file:
        file.write("\n".join(lines) + "\n")


def set_simulator(**options):
    return api("POST", "/api/settings", {"plugins": {"octoprint_grblsim": options}})


def set_plugin(**options):
    api("POST", "/api/settings", {"plugins": {"bettergrblsupport": options}})
    time.sleep(12)              # on_settings_save pauses status requests for 10 s


def reset():
    """Put the simulator options and the two pause settings back to their defaults (the rig keeps them in config.yaml)."""
    set_simulator(trace_file="", **SIM_DEFAULTS)
    set_plugin(**PLUGIN_DEFAULTS)
    print("reset: simulator options and pause settings back to defaults")


def trace_rows(path, t0, t1):
    rows = []
    if os.path.exists(path):
        for line in open(path):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if t0 <= row["t"] <= t1:
                rows.append(row)
    return rows


def show_trace(path, t0, t1, label, origin, every=1.0):
    rows = trace_rows(path, t0, t1)
    shown, last, previous = [], -1e9, None
    for row in rows:
        key = (row["state"], row["spindle"])
        if row["t"] - last >= every or key != previous:
            shown.append("  %+7.1fs state=%-7s Z=%8.3f X=%7.3f spindle=%d" % (
                row["t"] - origin, row["state"], row["mpos"][2], row["mpos"][0], row["spindle"]))
            last, previous = row["t"], key
    print("--- machine trace %s (simulator, t=0 at the request)\n%s" % (label, "\n".join(shown)))
    return rows


def setup(label, port, sim, plugin):
    write_jobs()
    trace = os.path.join(EVIDENCE, "%s_%s.trace.jsonl" % (label, LABEL))
    if os.path.exists(trace):
        os.remove(trace)        # a trace this driver wrote on an earlier run
    set_simulator(trace_file=trace, **dict(SIM_DEFAULTS, **sim))
    set_plugin(**dict(PLUGIN_DEFAULTS, **plugin))
    connect(port)
    if sim.get("grbl_homing") or sim.get("fluidnc_homing"):
        plugin_command("unlock")        # homing enabled: the machine powers up in Alarm
        time.sleep(2)
    return trace


def start_job(name, pre):
    upload(name)
    select(name)
    job("start")
    wait_job("Printing", 30)
    time.sleep(pre)


def wires(since, t0, count=30):
    return format_rows(serial_rows(since)[:count], t0)


def scenario(label, port="GRBL_SIM", sim=None, plugin=None, gfile="pk_cut.gcode", pre=14, after_resume=20):
    """Pause, wait 8 s, resume, run on, cancel."""
    sim, plugin = dict(sim or {}), dict(plugin or {})
    print("=== %s on %s sim=%s plugin=%s job=%s" % (label, port, sim, plugin, gfile))
    trace = setup(label, port, sim, plugin)
    start_job(gfile, pre)
    before_pause = mark()
    t_pause = time.time()
    job("pause", action="pause")
    state = wait_for(lambda: job_state() if job_state().startswith("Paused") else None, 150)
    print("PAUSE: job state %s after %.1fs" % (state, time.time() - t_pause))
    time.sleep(8)
    print("--- serial after the pause request (TX >, RX <), t=0 at the request\n" + wires(before_pause, t_pause))
    for line in plugin_log(before_pause, PLUGIN_LOG):
        print("LOG", line)
    before_resume = mark()
    t_resume = time.time()
    job("pause", action="resume")
    wait_for(lambda: job_state().startswith("Printing") or None, 90)
    print("RESUME: job state %s after %.1fs" % (job_state(), time.time() - t_resume))
    time.sleep(after_resume)
    print("--- serial after the resume request, t=0 at the request\n" + wires(before_resume, t_resume))
    for line in plugin_log(before_resume, PLUGIN_LOG):
        print("LOG", line)
    show_trace(trace, t_pause - 2, time.time(), label, t_pause)
    errors = [x for _, d, x in serial_rows(before_pause) if d == "<" and x.startswith(("ALARM", "error"))]
    print("RESULT %s: alarms/errors on the wire: %s" % (label, errors or "none"))
    job("cancel")
    wait_for(lambda: job_state() == "Operational", 60)
    time.sleep(2)


def early_resume(label, port="GRBL_SIM", sim=None, plugin=None, gfile="pk_cut.gcode", pre=14, gap=1.0):
    """Resume while the controller is still retracting: the cycle start must wait for Door:0."""
    sim, plugin, gap = dict(sim or {}), dict(plugin or {}), float(gap)
    print("=== %s early resume on %s sim=%s plugin=%s" % (label, port, sim, plugin))
    trace = setup(label, port, sim, plugin)
    start_job(gfile, pre)
    since = mark()
    t_pause = time.time()
    job("pause", action="pause")
    wait_for(lambda: job_state() if job_state().startswith("Paused") else None, 150)
    time.sleep(gap)
    t_resume = time.time()
    job("pause", action="resume")
    print("resume requested %.2fs after the pause request" % (t_resume - t_pause))
    wait_for(lambda: job_state().startswith("Printing") or None, 90)
    time.sleep(20)
    rows = serial_rows(since)
    print(format_rows(rows[:14], t_pause))
    for line in plugin_log(since, PLUGIN_LOG):
        print("LOG", line)
    trace_rows_ = show_trace(trace, t_pause - 1, time.time(), label, t_pause)
    door0 = next((r["t"] for r in trace_rows_ if r["state"] == "Door:0"), None)
    tilde = next((t for t, d, x in rows if d == ">" and x.startswith("~")), None)
    ok = door0 is not None and tilde is not None and tilde >= door0 - 0.25
    print("RESULT %s: resume requested at +%.2fs, first Door:0 at +%s, '~' sent at +%s -> %s" % (
        label, t_resume - t_pause, "%.2fs" % (door0 - t_pause) if door0 else "never",
        "%.2fs" % (tilde - t_pause) if tilde else "never", "PASS (~ not before Door:0)" if ok else "FAIL"))
    job("cancel")
    wait_for(lambda: job_state() == "Operational", 60)
    time.sleep(2)


def cancel_scenario(label, when, port="GRBL_SIM", sim=None, plugin=None, gfile="pk_cut.gcode", pre=14):
    """``when``: run | pause | retract (cancel 1.5 s after the pause request, while the controller is still parking) |
    restore (resume from a park, cancel 1 s into the plunge back, ``Door:3``: the buffered job must not run on)."""
    sim, plugin = dict(sim or {}), dict(plugin or {})
    print("=== %s cancel@%s on %s sim=%s plugin=%s" % (label, when, port, sim, plugin))
    trace = setup(label, port, sim, plugin)
    start_job(gfile, pre)
    if when in ("pause", "retract", "restore"):
        job("pause", action="pause")
        if when == "restore":
            wait_for(lambda: job_state() if job_state().startswith("Paused") else None, 150)
            time.sleep(8)
            t_resume = time.time()
            job("pause", action="resume")
            wait_for(lambda: any(r["state"] == "Door:3" and r["t"] >= t_resume for r in trace_rows(trace, t_resume, 1e12)) or None, 60)
            time.sleep(1)
        elif when == "retract":
            time.sleep(1.5)
        else:
            wait_for(lambda: job_state() if job_state().startswith("Paused") else None, 150)
            time.sleep(8)
    since = mark()
    t_cancel = time.time()
    job("cancel")
    state = wait_for(lambda: job_state() if job_state() == "Operational" else None, 90)
    print("CANCEL (%s): job state %s after %.1fs" % (when, state or "HUNG in " + job_state(), time.time() - t_cancel))
    time.sleep(24)              # cancel_motion keeps working after OctoPrint says Operational (up to 30 s while parking)
    print(wires(since, t_cancel, 16))
    for line in plugin_log(since, PLUGIN_LOG):
        print("LOG", line)
    show_trace(trace, t_cancel - 2, time.time(), label, t_cancel)
    alarms = [x for _, d, x in serial_rows(since) if d == "<" and x.startswith("ALARM")]
    verdict = ""
    if when == "restore":
        after = trace_rows(trace, t_cancel, 1e12)
        runs = [r["t"] for r in after if r["state"] == "Run"]
        cut = (max(runs) - min(runs)) if runs else 0.0
        verdict = ", Run for %.1fs after the cancel -> %s" % (cut, "PASS (held again)" if cut < 3.0 else "FAIL (job kept cutting)")
    print("RESULT %s cancel@%s: job %s, alarms %s%s" % (label, when, state, alarms or "none", verdict))
    if not state:
        command("M999")
        time.sleep(3)


if __name__ == "__main__":
    args = sys.argv[1:]
    function = globals()[args[0]]
    keywords, positional = {}, []
    for arg in args[1:]:
        if "=" in arg and not arg.startswith("{"):
            key, value = arg.split("=", 1)
            keywords[key] = json.loads(value) if value[:1] in "{[0123456789tfn-\"" else value
        else:
            positional.append(arg)
    function(*positional, **keywords)
