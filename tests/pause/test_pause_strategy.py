#!/usr/bin/env python3
"""Decision tests for the pause redesign: ``_bgs.pause_strategy``, ``parking_available`` and the scripts written for
each strategy.  Pure functions over a plugin-shaped object, so the facts they read are set directly; what the
controller then does with the bytes is proven on the simulator rigs (docs/simulator-testing.md), not here.

Needs OctoPrint importable (``_bgs`` imports it): run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_pause_strategy -v
"""
import importlib.util
import logging
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_bgs = importlib.import_module("octoprint_bettergrblsupport._bgs")

GRBL = "[VER:1.1h.20190830:] [OPT:V,15,128]"
GRBL_PARKING = "[VER:1.1h.20190830:] [OPT:VP,15,128]"
GRBL_OLD = "[VER:1.0c.20170101:] [OPT:V,15,128]"
FLUIDNC = "[VER:3.9 FluidNC v3.9.9 (esp32-wifi) :] [OPT:PHS] [CLUSTER:16]"


def fluid_yaml(enable=True, axis=None, z_homing=None, laser=False):
    z = {"steps_per_mm": 80.0}
    if z_homing is not None:
        z["homing"] = {"cycle": z_homing}
    config = {"axes": {"x": {}, "y": {}, "z": z}, "parking": {"enable": enable}}
    if axis:
        config["parking"]["axis"] = axis
    return config


def plugin(version=GRBL, settings=None, fluid_settings=None, yaml=None, parking=False, mode="drain", scripts=None, **kw):
    p = types.SimpleNamespace(
        grblVersion=version,
        grblSettings={k: [str(v), ""] for k, v in (settings or {}).items()},
        fluidSettings=fluid_settings or {}, fluidYaml=yaml,
        pauseParking=parking, pauseMode=mode, pauseHoldActive=False,
        grblPowerLevel=1000.0, grblJobPower=1000.0, powerRate=0.0, positioning=0, grblSpeed=500.0, grblJobFeed=500.0, grblMCode="M3", invertZ=1,
        _logger=logging.getLogger("test"),
        _settings=types.SimpleNamespace(global_get_basefolder=lambda name: scripts or tempfile.gettempdir()),
    )
    p.__dict__.update(kw)
    return p


class ParkingAvailable(unittest.TestCase):
    def test_grbl_needs_p_in_opt_and_homing(self):
        self.assertTrue(_bgs.parking_available(plugin(GRBL_PARKING, {22: 1, 32: 0}))[0])
        ok, why = _bgs.parking_available(plugin(GRBL, {22: 1, 32: 0}))
        self.assertFalse(ok)
        self.assertIn("no parking", why)
        ok, why = _bgs.parking_available(plugin(GRBL_PARKING, {22: 0, 32: 0}))
        self.assertFalse(ok)
        self.assertIn("$22", why)

    def test_grbl_opt_letters_are_read_before_the_first_comma_only(self):
        # a P in the numbers or elsewhere in the text is not the parking letter
        self.assertFalse(_bgs.parking_available(plugin("[VER:1.1h.P:] [OPT:V,15,128]", {22: 1}))[0])

    def test_unknown_firmware_text_is_not_parking(self):
        self.assertFalse(_bgs.parking_available(plugin("unknown", {22: 1}))[0])

    def test_fluidnc_ignores_the_opt_p_it_always_reports(self):
        # Report.cpp: FluidNC prints PH unconditionally, so P says nothing about the parking section
        ok, why = _bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(enable=False, z_homing=1)))
        self.assertFalse(ok)
        self.assertIn("enable", why)

    def test_fluidnc_enabled_with_a_homing_cycle(self):
        self.assertTrue(_bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(z_homing=1)))[0])
        self.assertTrue(_bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(z_homing=0)))[0])    # cycle 0 is in the mask

    def test_fluidnc_enable_as_text(self):
        self.assertTrue(_bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(enable="true", z_homing=1)))[0])
        self.assertFalse(_bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(enable="false", z_homing=1)))[0])

    def test_fluidnc_without_homing_on_the_parking_axis(self):
        ok, why = _bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml()))
        self.assertFalse(ok)
        self.assertIn("homing", why)
        # cycle -1 is "set mpos only": Axis.cpp adds an axis to the homing mask only for cycle >= 0
        self.assertFalse(_bgs.parking_available(plugin(FLUIDNC, yaml=fluid_yaml(z_homing=-1)))[0])

    def test_fluidnc_parking_axis_is_read_from_the_config(self):
        yaml = fluid_yaml(axis="X", z_homing=1)             # parks on X, which has no homing; Z homing is beside the point
        self.assertFalse(_bgs.parking_available(plugin(FLUIDNC, yaml=yaml))[0])
        yaml["axes"]["x"]["homing"] = {"cycle": 2}
        self.assertTrue(_bgs.parking_available(plugin(FLUIDNC, yaml=yaml))[0])

    def test_fluidnc_without_a_fetched_config(self):
        self.assertFalse(_bgs.parking_available(plugin(FLUIDNC, yaml=None))[0])
        self.assertFalse(_bgs.parking_available(plugin(FLUIDNC, yaml=""))[0])


class Strategy(unittest.TestCase):
    def test_default_is_drain_even_when_parking_is_available(self):
        p = plugin(GRBL_PARKING, {22: 1, 32: 0})
        self.assertEqual(_bgs.pause_strategy(p)[0], "drain")

    def test_parking_on_and_available_parks(self):
        self.assertEqual(_bgs.pause_strategy(plugin(GRBL_PARKING, {22: 1, 32: 0}, parking=True))[0], "park")
        self.assertEqual(_bgs.pause_strategy(plugin(FLUIDNC, yaml=fluid_yaml(z_homing=1), fluid_settings={"Grbl/LaserMode": "0"},
                                                    parking=True))[0], "park")

    def test_parking_on_but_unavailable_falls_back_to_the_mode_and_says_why(self):
        strategy, why = _bgs.pause_strategy(plugin(GRBL, {22: 1, 32: 0}, parking=True))
        self.assertEqual(strategy, "drain")
        self.assertIn("unavailable", why)
        self.assertEqual(_bgs.pause_strategy(plugin(GRBL, {22: 1, 32: 0}, parking=True, mode="hold"))[0], "hold")

    def test_no_parking_mode_hold(self):
        self.assertEqual(_bgs.pause_strategy(plugin(GRBL, {32: 0}, mode="hold"))[0], "hold")

    def test_laser_mode_is_an_instant_hold_whatever_else_is_set(self):
        for mode in ("drain", "hold"):
            self.assertEqual(_bgs.pause_strategy(plugin(GRBL_PARKING, {22: 1, 32: 1}, parking=True, mode=mode))[0], "laser")
        fluid = plugin(FLUIDNC, yaml=fluid_yaml(z_homing=1), fluid_settings={"Grbl/LaserMode": "1"}, parking=True)
        self.assertEqual(_bgs.pause_strategy(fluid)[0], "laser")

    def test_spindle_mode_zero_is_not_a_laser(self):
        self.assertEqual(_bgs.pause_strategy(plugin(GRBL, {32: 0}))[0], "drain")

    def test_pre_1_1_grbl_always_drains(self):
        p = plugin(GRBL_OLD, {32: 0}, parking=True, mode="hold")
        self.assertEqual(_bgs.pause_strategy(p)[0], "drain")
        self.assertEqual(_bgs.pause_strategy(plugin(GRBL_OLD, {32: 1}))[0], "drain")


class Scripts(unittest.TestCase):
    def written(self, strategy, **kw):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "gcode"))
            p = plugin(GRBL, {32: kw.pop("laser", 0)}, scripts=d, **kw)
            _bgs.write_pause_resume_scripts(p, strategy)
            def read(name):
                with open(os.path.join(d, "gcode", name)) as file:
                    return file.read().split()
            return read("afterPrintPaused"), read("beforePrintResumed")

    def test_hold_stops_the_spindle_with_the_override_and_resumes_with_a_cycle_start(self):
        self.assertEqual(self.written("hold"), (["TOGGLESPINDLE"], ["~"]))

    def test_laser_sends_nothing_but_the_cycle_start(self):
        self.assertEqual(self.written("laser", laser=1), ([], ["~"]))

    def test_park_resume_is_the_gated_cycle_start(self):
        self.assertEqual(self.written("park"), ([], ["PARKRESUME"]))

    def test_drain_lifts_z_then_puts_the_distance_mode_back_and_does_not_hold(self):
        # a `!` behind the lift stops it a few steps in (measured on the simulator rigs: the lift then ran after the resume)
        pause, resume = self.written("drain")
        self.assertEqual(pause, ["M5", "G91", "G0", "Z5", "G90"])
        self.assertNotIn("!", pause)
        self.assertNotIn("~", resume)

    def test_drain_resume_restores_spindle_speed_feed_waits_lowers_z_then_the_distance_mode(self):
        self.assertEqual(self.written("drain")[1], ["M3", "S1000.0", "F500.0", "G4", "P10", "G91", "G0", "Z-5", "G90"])

    def test_drain_resume_ends_in_the_distance_mode_the_job_was_in(self):
        pause, resume = self.written("drain", positioning=1)
        self.assertEqual((pause[-1], resume[-1]), ("G91", "G91"))
        pause, resume = self.written("drain", positioning=0)
        self.assertEqual((pause[-1], resume[-1]), ("G90", "G90"))

    def test_drain_in_an_inch_job_runs_the_lift_and_the_plunge_in_millimetres(self):
        # Z5 in G20 is 127 mm; the units are switched to G21 around the 5 mm moves and put back after them
        pause, resume = self.written("drain", grblUnits="G20")
        self.assertEqual(pause, ["M5", "G21", "G91", "G0", "Z5", "G90", "G20"])
        self.assertEqual(resume, ["M3", "S1000.0", "F500.0", "G4", "P10", "G21", "G91", "G0", "Z-5", "G90", "G20"])

    def test_drain_in_a_metric_job_writes_no_unit_words(self):
        for units in ("G21", None):
            kw = {} if units is None else {"grblUnits": units}
            pause, resume = self.written("drain", **kw)
            self.assertNotIn("G20", pause + resume)
            self.assertNotIn("G21", pause + resume)

    def test_drain_resume_puts_a_linear_motion_mode_back_after_the_plunge(self):
        # the Z moves leave Grbl in G0; a job line with no G word of its own would then run as a rapid
        pause, resume = self.written("drain", grblMotionMode="G1")
        self.assertEqual(resume, ["M3", "S1000.0", "F500.0", "G4", "P10", "G91", "G0", "Z-5", "G90", "G1"])
        self.assertEqual(pause, ["M5", "G91", "G0", "Z5", "G90"])

    def test_drain_resume_motion_mode_comes_after_the_units_and_distance_restore(self):
        _, resume = self.written("drain", grblMotionMode="G1", grblUnits="G20", positioning=1)
        self.assertEqual(resume[-3:], ["G91", "G20", "G1"])

    def test_drain_resume_writes_no_bare_g1_without_a_known_feed_rate(self):
        # a G1 with no feed rate set is error:22 on the controller
        _, resume = self.written("drain", grblMotionMode="G1", grblJobFeed=0.0)
        self.assertEqual(resume[-1], "G90")

    def test_drain_resume_motion_mode_does_not_depend_on_the_status_report_feed(self):
        # grblSpeed is the controller's FS: report, which says nothing about whether the job set a feed rate
        _, resume = self.written("drain", grblMotionMode="G1", grblSpeed=0.0)
        self.assertEqual(resume[-1], "G1")

    def test_drain_resume_does_not_write_a_bare_arc_word(self):
        # G2/G3 without axis words is error:26, which would stop the job on resume
        for mode in ("G0", "G2", "G3", "G38.2", "G80"):
            _, resume = self.written("drain", grblMotionMode=mode)
            self.assertEqual(resume[-1], "G90", mode)

    def test_drain_resume_feed_is_the_jobs_own_not_the_status_report_feed(self):
        # an inch job at F20 reports FS:508 (mm/min, whatever G20/G21 says): writing F508 back would be 25x too fast
        for units in ("G20", "G21"):
            _, resume = self.written("drain", grblUnits=units, grblJobFeed=20.0, grblSpeed=508.0)
            self.assertIn("F20.0", resume, units)
            self.assertNotIn("F508.0", resume, units)

    def test_drain_resume_power_is_the_jobs_own_not_the_overridden_one(self):
        # grblPowerLevel carries the power override and the resume line goes through the sending hook, which applies it again
        _, resume = self.written("drain", grblPowerLevel=1500.0, grblJobPower=1000.0)
        self.assertIn("S1000.0", resume)
        self.assertNotIn("S1500.0", resume)

    def test_drain_resume_power_falls_back_to_the_last_known_when_the_job_set_none(self):
        # a job with a bare M3 runs at whatever S was set before it; writing S0 back would stop the spindle on resume
        _, resume = self.written("drain", grblPowerLevel=700.0, grblJobPower=0.0)
        self.assertIn("S700.0", resume)

    def test_drain_resume_power_fallback_takes_the_power_override_back_out(self):
        # a start-script M3 S1000 at 150% went out as S1500; the resume line passes the hook again, so it must be S1000
        _, resume = self.written("drain", grblPowerLevel=1500.0, grblJobPower=0.0, powerRate=1.5)
        self.assertIn("S1000.0", resume)

    def test_drain_with_invert_z_lifts_and_plunges_the_other_way(self):
        # the jog's "up" is distance * invertZ, so with Invert Z on away from the work is -Z; +Z would drive the tool in
        pause, resume = self.written("drain", invertZ=-1)
        self.assertEqual(pause, ["M5", "G91", "G0", "Z-5", "G90"])
        self.assertEqual(resume, ["M3", "S1000.0", "F500.0", "G4", "P10", "G91", "G0", "Z5", "G90"])

    def test_drain_with_invert_z_in_an_inch_job_keeps_the_millimetre_switch(self):
        pause, resume = self.written("drain", invertZ=-1, grblUnits="G20")
        self.assertEqual(pause, ["M5", "G21", "G91", "G0", "Z-5", "G90", "G20"])
        self.assertEqual(resume[5:11], ["G21", "G91", "G0", "Z5", "G90", "G20"])

    def test_drain_for_a_laser_never_writes_units_or_motion_mode(self):
        pause, resume = self.written("drain", laser=1, grblUnits="G20", grblMotionMode="G1")
        self.assertEqual(pause, ["M5"])
        self.assertEqual(resume, ["M3", "S1000.0", "F500.0", "G90"])

    def test_drain_for_a_laser_stops_it_without_lifting(self):
        pause, resume = self.written("drain", laser=1)
        self.assertEqual(pause, ["M5"])
        self.assertEqual(resume, ["M3", "S1000.0", "F500.0", "G90"])


if __name__ == "__main__":
    unittest.main()
