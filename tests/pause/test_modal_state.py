#!/usr/bin/env python3
"""Tests for the plugin's follow-the-controller bookkeeping in ``_bgs``: the modal state tracked from sent lines,
the distance mode an interrupted probe hands back, the frame scan of a stored file.  Pure functions over a
plugin-shaped object (what the controller then does is proven on the simulator rigs, docs/simulator-testing.md).

Needs OctoPrint importable (``_bgs`` imports it): run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_modal_state -v
"""
import importlib
import logging
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_bgs = importlib.import_module("octoprint_bettergrblsupport._bgs")


def plugin(**kw):
    p = types.SimpleNamespace(positioning=0, grblUnits="G21", grblMotionMode="G0", pendingDistanceRestore=None,
                              sent=[], _logger=logging.getLogger("test"))
    p._printer = types.SimpleNamespace(commands=lambda c: p.sent.append(c))
    p.__dict__.update(kw)
    return p


class TrackModalState(unittest.TestCase):
    def track(self, *lines, **kw):
        p = plugin(**kw)
        for line in lines:
            _bgs.track_modal_state(p, line)
        return p

    def test_distance_mode(self):
        self.assertEqual(self.track("G91").positioning, 1)
        self.assertEqual(self.track("G91", "G90").positioning, 0)
        self.assertEqual(self.track("G0 G91 X5").positioning, 1)
        self.assertEqual(self.track("g91").positioning, 1)
        self.assertEqual(self.track("G091").positioning, 1)

    def test_it_reports_whether_the_distance_mode_was_set(self):
        p = plugin()
        self.assertTrue(_bgs.track_modal_state(p, "G91"))
        self.assertFalse(_bgs.track_modal_state(p, "G1 X1"))

    def test_a_jog_does_not_change_the_parser_state(self):
        # Grbl 1.1 `$J=G91 G21 ...` leaves the distance mode, units and motion mode alone
        p = self.track("$J=G91 G20 X2.000000 F500.0", positioning=0, grblUnits="G21", grblMotionMode="G1")
        self.assertEqual((p.positioning, p.grblUnits, p.grblMotionMode), (0, "G21", "G1"))

    def test_a_G1_jog_for_old_grbl_does_change_it(self):
        p = self.track("G1 G91 G21 X2.0 F500.0", grblUnits="G20")
        self.assertEqual((p.positioning, p.grblUnits, p.grblMotionMode), (1, "G21", "G1"))

    def test_arc_centre_mode_is_not_the_distance_mode(self):
        self.assertEqual(self.track("G91.1").positioning, 0)
        self.assertEqual(self.track("G91", "G90.1").positioning, 1)
        self.assertEqual(self.track("G90 G91.1").positioning, 0)

    def test_comments_are_not_words(self):
        self.assertEqual(self.track("G1 X1 (go G91 now)").positioning, 0)
        self.assertEqual(self.track("G1 X1 ; G91").positioning, 0)
        self.assertEqual(self.track("; G20", grblUnits="G21").grblUnits, "G21")

    def test_units(self):
        self.assertEqual(self.track("G20").grblUnits, "G20")
        self.assertEqual(self.track("G20", "G21").grblUnits, "G21")
        self.assertEqual(self.track("G21 G90 G20 X1").grblUnits, "G20")

    def test_motion_mode(self):
        for line, mode in (("G0 X1", "G0"), ("G1 X1 F10", "G1"), ("G01 X1", "G1"), ("G00", "G0"), ("G2 X1 I1", "G2"),
                           ("G3 X1 I1", "G3"), ("G38.2 Z-5 F50", "G38.2"), ("G80", "G80")):
            self.assertEqual(self.track(line).grblMotionMode, mode, line)

    def test_other_g_words_leave_the_motion_mode(self):
        p = self.track("G1 X1", "G4 P1", "G53 G0 Z0", "G54", "G17", "G94", "M3 S100", "G10 L2 P1 X0")
        self.assertEqual(p.grblMotionMode, "G0")      # the G53 G0 line is the last motion word
        self.assertEqual(self.track("G1 X1", "G4 P1", "G54", "G94").grblMotionMode, "G1")


class JobFeed(unittest.TestCase):
    """The feed rate the pause resume writes back: the F word of the lines the job sent, in the job's units."""

    def track(self, *lines, **kw):
        p = plugin(**kw)
        for line in lines:
            _bgs.track_modal_state(p, line)
        return p

    def test_the_f_word_of_a_sent_line_is_the_job_feed(self):
        self.assertEqual(self.track("G1 X1 F20").grblJobFeed, 20.0)
        self.assertEqual(self.track("G1 X1 F20", "G1 X2").grblJobFeed, 20.0)
        self.assertEqual(self.track("G1 X1 F20", "G1 X2 F12.5").grblJobFeed, 12.5)
        self.assertEqual(self.track("F 7.5").grblJobFeed, 7.5)
        self.assertEqual(self.track("g1 x1 f300").grblJobFeed, 300.0)

    def test_it_keeps_the_job_units_whatever_the_status_report_says(self):
        p = self.track("G20", "G1 X0.2 F20", grblSpeed=508.0)
        self.assertEqual((p.grblJobFeed, p.grblSpeed), (20.0, 508.0))

    def test_comments_and_jogs_are_not_the_job_feed(self):
        self.assertEqual(self.track("G1 X1 F20", "G1 X2 (F99)").grblJobFeed, 20.0)
        self.assertEqual(self.track("G1 X1 F20", "G1 X2 ; F99").grblJobFeed, 20.0)
        self.assertEqual(self.track("G1 X1 F20", "$J=G91 G21 X1 F500").grblJobFeed, 20.0)

    def test_a_line_without_a_feed_word_leaves_it(self):
        self.assertEqual(self.track("G1 X1 F20", "M3 S1000", "G4 P1", "G0 X5").grblJobFeed, 20.0)
        self.assertNotIn("grblJobFeed", vars(self.track("G0 X5")))


class JobPower(unittest.TestCase):
    """The S word of the job's lines, before the power override, and what is not the job's."""

    def track(self, *lines, **kw):
        p = plugin(**kw)
        for line in lines:
            _bgs.track_modal_state(p, line)
        return p

    def test_the_s_word_of_a_sent_line_is_the_job_power(self):
        self.assertEqual(self.track("M3 S500").grblJobPower, 500.0)
        self.assertEqual(self.track("M3 S500", "G1 X1 S250.5").grblJobPower, 250.5)
        self.assertEqual(self.track("m3s500").grblJobPower, 500.0)
        self.assertEqual(self.track("M3 S 40").grblJobPower, 40.0)

    def test_comments_jogs_and_words_that_merely_contain_an_s_are_not_the_job_power(self):
        self.assertEqual(self.track("M3 S500", "G1 X1 (S99)").grblJobPower, 500.0)
        self.assertEqual(self.track("M3 S500", "G1 X1 ; S99").grblJobPower, 500.0)
        self.assertEqual(self.track("M3 S500", "$J=G91 X1 S99").grblJobPower, 500.0)
        self.assertEqual(self.track("M3 S500", "SPINDLEPLUS10").grblJobPower, 500.0)
        self.assertNotIn("grblJobPower", vars(self.track("G0 X5", "SPINDLEPLUS10")))

    def test_a_line_that_is_not_the_job_changes_the_modal_state_but_not_the_feed_or_power(self):
        p = plugin()
        _bgs.track_modal_state(p, "M3 S500 G1 F1000")
        self.assertTrue(_bgs.track_modal_state(p, "G91 G20 G1 F3000 S100", job=False))
        self.assertEqual((p.grblJobFeed, p.grblJobPower), (1000.0, 500.0))
        self.assertEqual((p.positioning, p.grblUnits), (1, "G20"))


class InterruptedProbe(unittest.TestCase):
    def setUp(self):
        _bgs.zProbe = _bgs.xyProbe = None
        _bgs.zProbeAborted = False

    tearDown = setUp

    def probe(self, prior, units="G21"):
        return types.SimpleNamespace(_priorPositioning=prior, _priorUnits=units, teardown=lambda: None)

    def test_an_error_or_cancel_hands_the_mode_back_at_once(self):
        for prior, mode in ((0, "G90"), (1, "G91")):
            p = plugin()
            _bgs.xyProbe = self.probe(prior)
            _bgs.grbl_alarm_or_error_occurred(p)
            self.assertEqual(p.sent, [[mode, "G21", "G0"]])
            self.assertIsNone(_bgs.xyProbe)

    def test_an_interrupted_probe_does_not_leave_the_parser_in_the_probe_motion_mode(self):
        # a probe that fails or is cancelled stops in G38.2; the job's next line with no G word must not probe
        for locked in (False, True):
            p = plugin()
            _bgs.xyProbe = self.probe(0)
            _bgs.grbl_alarm_or_error_occurred(p, locked=locked)
            if locked:
                _bgs.send_pending_distance_restore(p)
            self.assertEqual(p.sent, [["G90", "G21", "G0"]], locked)
            self.assertEqual(p.sent[0][-1], "G0")

    def test_an_alarm_holds_the_restore_for_the_unlock(self):
        # the controller rejects G-code in alarm: the restore goes out right behind the next $X
        p = plugin()
        _bgs.xyProbe = self.probe(0)
        _bgs.grbl_alarm_or_error_occurred(p, locked=True)
        self.assertEqual((p.sent, p.pendingDistanceRestore), ([], ["G90", "G21", "G0"]))
        _bgs.send_pending_distance_restore(p)
        self.assertEqual((p.sent, p.pendingDistanceRestore), ([["G90", "G21", "G0"]], None))
        _bgs.send_pending_distance_restore(p)
        self.assertEqual(p.sent, [["G90", "G21", "G0"]])

    def test_the_xy_probe_mode_wins_over_the_z_probe_mode(self):
        # in the XYZ probe the Z probe is torn down first; the X/Y probe holds the mode from before both
        p = plugin()
        _bgs.xyProbe, _bgs.zProbe = self.probe(1, "G20"), self.probe(0)
        _bgs.grbl_alarm_or_error_occurred(p)
        self.assertEqual(p.sent, [["G91", "G20", "G0"]])

    def test_an_interrupted_probe_hands_the_units_back_not_the_millimetres_it_forced(self):
        # every probe move is G21: an inch job's parser would otherwise stay in millimetres after the unlock
        for locked in (False, True):
            p = plugin()
            _bgs.zProbe = self.probe(0, "G20")
            _bgs.grbl_alarm_or_error_occurred(p, locked=locked)
            if locked:
                self.assertEqual(p.sent, [])
                _bgs.send_pending_distance_restore(p)
            self.assertEqual(p.sent, [["G90", "G20", "G0"]], locked)

    def test_the_restore_lines_are_the_prior_mode_then_units_then_the_motion_mode(self):
        self.assertEqual(_bgs.probe_restore_commands(1, "G20"), ["G91", "G20", "G0"])
        self.assertEqual(_bgs.probe_restore_commands(0, "G21"), ["G90", "G21", "G0"])

    def test_nothing_to_restore_when_no_probe_runs(self):
        p = plugin()
        _bgs.grbl_alarm_or_error_occurred(p)
        _bgs.grbl_alarm_or_error_occurred(p, locked=True)
        self.assertEqual((p.sent, p.pendingDistanceRestore, _bgs.zProbeAborted), ([], None, False))

    def test_a_torn_down_z_probe_is_flagged_so_the_xyz_chain_stops(self):
        _bgs.zProbe = self.probe(0)
        _bgs.grbl_alarm_or_error_occurred(plugin())
        self.assertTrue(_bgs.zProbeAborted)

    def test_a_probe_that_found_nothing_is_torn_down_like_an_alarm(self):
        p = plugin(zProbeOffset=0, invertZ=1)
        _bgs.zProbe = types.SimpleNamespace(_priorPositioning=1, _priorUnits="G21", _sessionId="s", teardown=lambda: None)
        _bgs.simple_zprobe_hook(p, 0, 0.0)
        self.assertEqual((p.sent, _bgs.zProbe, _bgs.zProbeAborted), ([["G91", "G21", "G0"]], None, True))


class Reconnect(unittest.TestCase):
    def test_a_restore_owed_by_the_previous_connection_is_dropped_on_connect(self):
        # an alarmed probe, a disconnect with no $X, a reconnect: the stale G91 must not go out behind the first $X
        from octoprint.events import Events
        p = plugin(pendingDistanceRestore=["G91", "G21", "G0"], connectionState=None, whenConnected=0, userCommandSinceConnect=True,
                   autoSleepTimer=0, is_operational=False, fluidConfig="x", grblMCode="M3")
        p._printer_profile_manager = types.SimpleNamespace(get_current_or_default=lambda: {"id": "_bgs"})
        p._settings = types.SimpleNamespace(set_boolean=lambda *a: None)
        real = _bgs.schedule_status_refresh
        _bgs.schedule_status_refresh = lambda *a, **k: None
        # the plugin manager registers these events at runtime; a bare import of octoprint.events does not have them
        added = [n for n in ("PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN", "PLUGIN_PLUGINMANAGER_DISABLE_PLUGIN") if not hasattr(Events, n)]
        for name in added:
            setattr(Events, name, "plugin_pluginmanager_" + name.lower())
        try:
            _bgs.on_event(p, Events.CONNECTED, {})
        finally:
            _bgs.schedule_status_refresh = real
            for name in added:
                delattr(Events, name)
        self.assertIsNone(p.pendingDistanceRestore)
        _bgs.send_pending_distance_restore(p)
        self.assertEqual(p.sent, [["$I", "$G", "$#"]])


class XyzProbeChain(unittest.TestCase):
    """defer_do_xyz_probe waits for the Z probe to leave scope, then starts the X/Y probe: only if the Z probe finished."""

    def run_chain(self, how_it_ends, units="G21"):
        import threading
        _bgs.zProbe = _bgs.xyProbe = None
        started = []
        p = plugin(grblUnits=units)

        def fake_z_probe(_plugin, session):
            _bgs.zProbe = types.SimpleNamespace(_priorPositioning=0, _priorUnits="G21", teardown=lambda: None)
            threading.Timer(0.2, how_it_ends, args=(p,)).start()

        real_z, real_xy = _bgs.do_simple_zprobe, _bgs.do_xy_probe
        _bgs.do_simple_zprobe = fake_z_probe
        _bgs.do_xy_probe = lambda *a: started.append(a)
        try:
            _bgs.defer_do_xyz_probe(p, "s")
        finally:
            _bgs.do_simple_zprobe, _bgs.do_xy_probe = real_z, real_xy
            _bgs.zProbe = _bgs.xyProbe = None
        return started, p

    def test_a_z_probe_that_alarms_does_not_start_the_xy_probe(self):
        started, p = self.run_chain(lambda p: _bgs.grbl_alarm_or_error_occurred(p, locked=True))
        self.assertEqual(started, [])
        self.assertEqual(p.pendingDistanceRestore, ["G90", "G21", "G0"])

    def test_a_z_probe_that_finishes_starts_the_xy_probe(self):
        def finishes(p):
            _bgs.zProbe = None      # what defer_simple_z_probe does when the probe is done
        started, _ = self.run_chain(finishes)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][1:], ("XY", "s", 0, "G21"))


    def test_the_xy_probe_is_started_with_the_units_from_before_the_z_probe(self):
        def finishes(p):
            _bgs.zProbe = None
        started, _ = self.run_chain(finishes, units="G20")
        self.assertEqual(started[0][1:], ("XY", "s", 0, "G20"))


class ProbeUnits(unittest.TestCase):
    """Each probe records the units (and distance mode) it found, before any of its G21 / G91 moves are sent, and an
    interrupted one hands them back: the Z, the multipoint Z and the X/Y probe alike."""

    SETTINGS = {"frame_origin": "grblTopLeft", "frame_width": 10, "frame_length": 10, "xyProbeTravel": 30}

    def setUp(self):
        _bgs.zProbe = _bgs.xyProbe = None
        _bgs.zProbeAborted = False
        self.real = (_bgs.get_axes_limits, _bgs.get_axes_max_rates)
        _bgs.get_axes_limits = lambda p: (200, 200, 100)
        _bgs.get_axes_max_rates = lambda p: (500, 500, 100)

    def tearDown(self):
        _bgs.get_axes_limits, _bgs.get_axes_max_rates = self.real
        _bgs.zProbe = _bgs.xyProbe = None

    def plugin(self, units, positioning):
        return plugin(grblUnits=units, positioning=positioning, zProbeTravel=5, invertZ=1, invertX=1, invertY=1,
                      framingPercentOfMaxSpeed=50, _identifier="x", grblVersion="[VER:1.1h.20190830:]", sent=[],
                      _plugin_manager=types.SimpleNamespace(send_plugin_message=lambda *a, **k: None),
                      _settings=types.SimpleNamespace(get=lambda key: self.SETTINGS[key[0]]))

    def test_the_single_z_probe_records_them(self):
        _bgs.do_simple_zprobe(self.plugin("G20", 1), "s")
        self.assertEqual((_bgs.zProbe._priorUnits, _bgs.zProbe._priorPositioning), ("G20", 1))

    def test_the_multipoint_z_probe_records_them_and_hands_them_back_when_it_fails(self):
        p = self.plugin("G20", 1)
        _bgs.do_multipoint_zprobe(p, "s")
        self.assertEqual((_bgs.zProbe._priorUnits, _bgs.zProbe._priorPositioning), ("G20", 1))
        p.grblUnits, p.positioning = "G21", 1                     # the probe's own G91 G21 line went out
        _bgs.multipoint_zprobe_hook(p, 0, 0.0)
        self.assertEqual((p.sent, _bgs.zProbe, _bgs.zProbeAborted), ([["G91", "G20", "G0"]], None, True))

    def test_the_multipoint_z_probe_hands_them_back_when_it_finishes(self):
        p = self.plugin("G20", 0)
        p.grblCoordinateSystem, p.offsets, p.grblCmdQueue, p.notifications = "G54", {"G54": {}}, [], []
        p.zProbeOffset = 0
        p._settings = types.SimpleNamespace(get=lambda key: "AVG" if key == ["zprobeCalc"] else self.SETTINGS[key[0]])
        _bgs.do_multipoint_zprobe(p, "s")
        _bgs.zProbe._locations, _bgs.zProbe._step = [{"location": "here"}], 0
        _bgs.zProbe._results.append({"position": -1.0, "location": "here"})
        _bgs.do_multipoint_zprobe(p, "s")                          # step 1 is past the last location: done
        self.assertEqual(p.grblCmdQueue[1:], ["G90", "G20", "G0"])
        self.assertIsNone(_bgs.zProbe)

    def test_the_single_z_probe_hands_them_back_when_it_finishes(self):
        p = self.plugin("G20", 0)
        p.grblCmdQueue, p.offsets, p.grblCoordinateSystem = [], {"G54": {}}, "G54"
        p.zProbeEndPos = 5
        p._printer.commands = lambda c: p.grblCmdQueue.clear() if c == "?" else p.sent.append(c)
        _bgs.do_simple_zprobe(p, "s")
        _bgs.defer_simple_z_probe(p, -1.0)
        self.assertEqual(p.sent[-1][-2:], ["G90", "G20"])
        self.assertEqual(p.sent[-1][:2], ["G91", "G21"])           # the retract itself still runs in millimetres

    def test_the_xy_probe_hands_them_back_when_it_finishes(self):
        for axes, results in (("X", [1.0]), ("XY", [1.0, 2.0])):
            with self.subTest(axes=axes):
                p = self.plugin("G20", 1)
                p.notifications = []
                _bgs.do_xy_probe(p, axes, "s")
                _bgs.xyProbe._results.extend(results)
                _bgs.do_xy_probe(p, axes, "s")
                self.assertEqual(p.sent[-1][-2:], ["G91", "G20"])
                self.assertIsNone(_bgs.xyProbe)

    def test_the_xy_probe_records_them(self):
        p = self.plugin("G20", 1)
        _bgs.do_xy_probe(p, "X", "s")
        self.assertEqual((_bgs.xyProbe._priorUnits, _bgs.xyProbe._priorPositioning), ("G20", 1))


class FrameScan(unittest.TestCase):
    def scan(self, data):
        meta = {}
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            fm = types.SimpleNamespace(
                path_on_disk=lambda storage, name: path,
                set_additional_metadata=lambda storage, name, key, value, overwrite=False: meta.__setitem__(key, value),
                remove_additional_metadata=lambda storage, name, key: None)
            p = plugin(_file_manager=fm)
            _bgs.defer_generate_metadata_for_file(p, "x.gcode", False)
            return meta
        finally:
            os.remove(path)

    def test_arc_centre_mode_does_not_make_the_scan_relative(self):
        meta = self.scan(b"G21 G90 G91.1\nM3 S1000\nG0 X-10 Y0\nG1 X-10 Y-10 F500\nG1 X-30 Y-10\nG1 X-50 Y-10\nG1 X0 Y0\nM5\n")
        self.assertEqual((meta["bgs_width"], meta["bgs_length"], meta["bgs_origin"]), (50, 10, "grblTopRight"))

    def test_relative_mode_still_accumulates(self):
        meta = self.scan(b"G21 G91\nG1 X10 Y5 F100\nG1 X-30 Y-8\n")
        # visited points: x 10 then -20, y 5 then -3
        self.assertEqual((meta["bgs_width"], meta["bgs_length"]), (30, 8))

    def test_a_latin1_comment_does_not_cost_the_scan(self):
        meta = self.scan(b"; caf\xe9 job\nG21 G90\nM3 S1000\nG0 X-10 Y-10\nG1 X-50 Y-20 F500\nM5\n")
        self.assertEqual((meta["bgs_width"], meta["bgs_length"], meta["bgs_origin"]), (40, 10, "grblTopRight"))


if __name__ == "__main__":
    unittest.main()
