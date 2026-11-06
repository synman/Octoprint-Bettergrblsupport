#!/usr/bin/env python3
"""The hold flow: realtime bytes out of band, acknowledgements on the controller's behalf only while OctoPrint is
blocked and within a byte budget, and the oks OctoPrint never spent swallowed (``_bgs.resume_hold``,
``finish_pause_hold``, ``fake_ack``, ``cancel_motion``; the sending and received hooks).

Needs OctoPrint importable: run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_hold_flow -v
"""
import importlib
import logging
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
bgs = importlib.import_module("octoprint_bettergrblsupport")
_bgs = importlib.import_module("octoprint_bettergrblsupport._bgs")

JOB = {"source:file", "filepos:0", "fileline:1"}
COMMANDS = {"trigger:printer.commands"}


class Port:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)


class Gate:
    """OctoPrint's clear-to-send counter, scripted: blocked() answers from a list, then False."""
    def __init__(self, answers):
        self.answers = list(answers)

    def blocked(self):
        return self.answers.pop(0) if self.answers else False


class Printer:
    def __init__(self, pausing_polls=0, gate=None, with_port=True, paused=False):
        self.sent = []
        self.acks = 0
        self.pausing_polls = pausing_polls
        self.paused = paused
        self._comm = types.SimpleNamespace(_serial=Port() if with_port else None, _log=lambda *a: None,
                                           _clear_to_send=gate)

    def commands(self, c, **k):
        self.sent.append(c)

    def fake_ack(self):
        self.acks += 1

    def is_pausing(self):
        if self.pausing_polls > 0:
            self.pausing_polls -= 1
            return True
        return False

    def is_paused(self):
        return self.paused

    def is_resuming(self):
        return False

    def is_printing(self):
        return True

    def is_operational(self):
        return True

    @property
    def written(self):
        return self._comm._serial.written


def flow_plugin(printer, **kw):
    p = types.SimpleNamespace(_printer=printer, _logger=logging.getLogger("test"), pauseHoldActive=True,
                              pauseStrategy="laser", holdBytes=0, strayOksOwed=0, grblReportedState="Hold:0",
                              cancelling=False)
    p.__dict__.update(kw)
    return p


class ResumeHold(unittest.TestCase):
    def test_laser_and_hold_send_a_bare_realtime_cycle_start(self):
        for strategy in ("laser", "hold"):
            printer = Printer()
            p = flow_plugin(printer, pauseStrategy=strategy)
            with mock.patch.object(_bgs, "schedule_status_refresh"):
                _bgs.resume_hold(p)
            self.assertEqual(printer.written, [b"~"])
            self.assertEqual(printer.sent, [])
            self.assertFalse(p.pauseHoldActive)

    def test_drain_sends_nothing(self):
        printer = Printer()
        _bgs.resume_hold(flow_plugin(printer, pauseStrategy="drain", pauseHoldActive=False))
        self.assertEqual(printer.written, [])

    def test_park_waits_for_door_zero_on_its_own_thread(self):
        with mock.patch.object(_bgs.threading, "Thread") as thread:
            _bgs.resume_hold(flow_plugin(Printer(), pauseStrategy="park"))
        self.assertIs(thread.call_args.kwargs["target"], _bgs.resume_from_park)
        thread.return_value.start.assert_called_once()


class FinishPauseHold(unittest.TestCase):
    def run_hold(self, p):
        with mock.patch.object(_bgs.time, "sleep"):
            _bgs.finish_pause_hold(p)

    def test_one_ack_per_blocked_poll_and_each_is_owed(self):
        printer = Printer(pausing_polls=12, gate=Gate([True, False, True, False, False]))
        p = flow_plugin(printer)
        self.run_hold(p)
        self.assertEqual(printer.acks, 2)
        self.assertEqual(p.strayOksOwed, 2)

    def test_a_poll_that_is_not_blocked_tickles_the_queue_without_acknowledging(self):
        printer = Printer(pausing_polls=4, gate=Gate([False, False]))
        tickles = []
        printer._comm._continue_sending = lambda: tickles.append(1)
        p = flow_plugin(printer)
        self.run_hold(p)
        self.assertEqual((printer.acks, p.strayOksOwed, len(tickles)), (0, 0, 2))

    def test_no_ack_past_the_rx_budget(self):
        printer = Printer(pausing_polls=6, gate=Gate([True, True, True]))
        p = flow_plugin(printer, holdBytes=_bgs.HOLD_RX_BUDGET + 1)
        with self.assertLogs("test", "WARNING"):
            self.run_hold(p)
        self.assertEqual(printer.acks, 0)

    def test_unreadable_gate_falls_back_to_one_ack_per_poll(self):
        printer = Printer(pausing_polls=4, gate=None)
        self.run_hold(flow_plugin(printer))
        self.assertEqual(printer.acks, 2)

    def test_hold_strategy_stops_the_spindle_only_after_the_hold_completes(self):
        printer = Printer(pausing_polls=0)
        states = iter(["Hold:1", "Hold:0"])

        def refresh(plugin, timeout=1.0):
            plugin.grblReportedState = next(states)
            return True

        p = flow_plugin(printer, pauseStrategy="hold", grblReportedState="Run")
        with mock.patch.object(_bgs, "refresh_grbl_state", side_effect=refresh):
            self.run_hold(p)
        self.assertEqual(printer.written, [b"\x9e"])

    def test_laser_never_sends_the_spindle_stop(self):
        printer = Printer(pausing_polls=0)
        self.run_hold(flow_plugin(printer, pauseStrategy="laser"))
        self.assertEqual(printer.written, [])


class Rig:
    """The plugin with a printer that has a serial port, and its hooks."""

    def __init__(self, paused=False, version="[VER:1.1h.20190830:] [OPT:V,15,128]"):
        self.printer = Printer(paused=paused)
        p = self.plugin = bgs.BetterGrblSupportPlugin()
        p._logger = logging.getLogger("test")
        p._printer_profile_manager = types.SimpleNamespace(get_current_or_default=lambda: {"id": "_bgs"})
        p._printer = self.printer
        p._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *a, **k: None)
        p._identifier = "bettergrblsupport"
        p._settings = types.SimpleNamespace(get=lambda key: None, get_boolean=lambda key: False)
        p.grblVersion = version
        p.grblState = "Idle"

    def send(self, line, tags=COMMANDS):
        with mock.patch.object(_bgs, "schedule_status_refresh"):
            return self.plugin.hook_gcode_sending(None, "sending", line, None, None, tags=tags)

    def receive(self, line):
        return self.plugin.hook_gcode_received(None, line)


class SendingHook(unittest.TestCase):
    def test_realtime_names_and_characters_go_out_of_band(self):
        for line, byte in (("FEEDPLUS10", b"\x91"), ("TOGGLESPINDLE", b"\x9e"), ("CANCELJOG", b"\x85"),
                           ("~", b"~"), ("!", b"!")):
            r = Rig()
            self.assertEqual(r.send(line), (None,), line)
            self.assertEqual(r.printer.written, [byte], line)

    def test_realtime_names_send_nothing_on_pre_1_1_grbl(self):
        r = Rig(version="[VER:1.0c.20170101:] [OPT:V,15,128]")
        self.assertEqual(r.send("FEEDPLUS10"), (None,))
        self.assertEqual(r.printer.written, [])

    def test_m400_while_held_is_a_status_line_and_its_ok_is_owed(self):
        r = Rig()
        r.plugin.pauseHoldActive = True
        self.assertEqual(r.send("M400"), ("?",))
        self.assertEqual(r.plugin.strayOksOwed, 1)

    def test_m114_is_a_realtime_status_request(self):
        r = Rig()
        self.assertEqual(r.send("M114"), (None,))
        self.assertEqual(r.printer.written, [b"?"])

    def test_a_cancel_drops_the_pause_and_resume_scripts_by_tag(self):
        r = Rig()
        r.plugin.cancelling = True
        for line in ("G4 P10", "G91 G0 Z5", "M3"):
            self.assertEqual(r.send(line, {"source:script", "script:beforePrintResumed"}), (None,), line)

    def test_lines_sent_into_a_hold_are_counted(self):
        r = Rig()
        r.plugin.pauseHoldActive = True
        r.send("G1 X10", JOB)
        self.assertEqual(r.plugin.holdBytes, len("G1 X10") + 1)


class ReceivedHook(unittest.TestCase):
    def test_owed_oks_are_swallowed_and_the_rest_pass(self):
        r = Rig()
        r.plugin.strayOksOwed = 2
        results = [r.receive("ok") for _ in range(3)]
        self.assertEqual(results[:2], [None, None])
        self.assertIsNot(results[2], None)
        self.assertEqual(r.plugin.strayOksOwed, 0)

    def test_the_banner_is_never_swallowed(self):
        r = Rig()
        r.plugin.strayOksOwed = 1
        self.assertTrue(r.receive("Grbl 1.1f ['$' for help]").startswith("ok "))


class CoolantWhilePaused(unittest.TestCase):
    def test_m9_while_paused_is_the_realtime_flood_toggle_when_air_is_on(self):
        r = Rig(paused=True)
        r.plugin.coolant = "M8"
        self.assertEqual(r.send("M9"), (None,))
        self.assertEqual(r.printer.written, [b"\xa0"])

    def test_air_comes_back_on_resume_when_no_report_says_it_went_off(self):
        # stock Grbl omits |A: when nothing is on, so the plugin's own record of the toggle has to carry the state
        r = Rig(paused=True)
        r.plugin.coolant = "M8"
        r.plugin.delayM9 = True
        r.send("M9")
        _bgs.process_grbl_status_msg(r.plugin, "<Hold:0|MPos:0.000,0.000,0.000|FS:0,0>")
        r.send("M8")
        self.assertEqual(r.printer.written, [b"\xa0", b"\xa0"])
        self.assertEqual(r.plugin.coolant, "M8")

    def test_m9_while_paused_with_air_already_off_sends_nothing(self):
        r = Rig(paused=True)
        r.plugin.coolant = "M9"
        self.assertEqual(r.send("M9"), (None,))
        self.assertEqual(r.printer.written, [])

    def test_m8_while_printing_stays_a_line(self):
        r = Rig(paused=False)
        self.assertEqual(r.send("M8", JOB), ("M8",))
        self.assertEqual(r.printer.written, [])

    def test_pre_1_1_grbl_keeps_the_line(self):
        r = Rig(paused=True, version="[VER:1.0c.20170101:] [OPT:V,15,128]")
        r.plugin.coolant = "M8"
        self.assertEqual(r.send("M9"), ("M9",))

    def test_the_accessory_field_sets_the_coolant_state(self):
        r = Rig()
        # Grbl prints |A: inside the |Ov: block, and only when something is on: with Ov: it is the word both ways
        _bgs.process_grbl_status_msg(r.plugin, "<Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100|A:SF>")
        self.assertEqual(r.plugin.coolant, "M8")
        _bgs.process_grbl_status_msg(r.plugin, "<Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100>")
        self.assertEqual(r.plugin.coolant, "M9")
        r.plugin.coolant = "M8"
        _bgs.process_grbl_status_msg(r.plugin, "<Run|MPos:0.000,0.000,0.000|FS:100,500>")
        self.assertEqual(r.plugin.coolant, "M8")


class CoolantAcrossAPause(unittest.TestCase):
    def pause(self, strategy, coolant="M8", off=True):
        r = Rig()
        r.plugin.coolant = coolant
        r.plugin.pauseCoolantOff = off
        with mock.patch.object(_bgs, "pause_strategy", return_value=(strategy, "")), \
                mock.patch.object(_bgs.threading, "Thread"), mock.patch.object(_bgs, "write_pause_resume_scripts"), \
                mock.patch.object(_bgs, "schedule_status_refresh"), \
                mock.patch.object(_bgs.Events, "PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN", "uninstall", create=True), \
                mock.patch.object(_bgs.Events, "PLUGIN_PLUGINMANAGER_DISABLE_PLUGIN", "disable", create=True):
            # the plugin manager's events exist only once OctoPrint has registered them
            _bgs.on_event(r.plugin, "PrinterStateChanged", {"state_id": "PAUSING"})
        return r

    def test_pause_switches_air_off_and_records_it(self):
        r = self.pause("laser")
        self.assertEqual(r.printer.written, [b"!", b"\xa0"])
        self.assertEqual((r.plugin.coolantAtPause, r.plugin.coolant), ("M8", "M9"))

    def test_pause_with_the_setting_off_only_records(self):
        r = self.pause("laser", off=False)
        self.assertEqual(r.printer.written, [b"!"])
        self.assertEqual(r.plugin.coolantAtPause, "M8")

    def test_park_records_but_never_toggles(self):
        r = self.pause("park")
        self.assertEqual(r.printer.written, [b"\x84"])

    def test_resume_restores_air_before_the_cycle_start(self):
        r = Rig()
        r.plugin.pauseStrategy, r.plugin.coolantAtPause, r.plugin.coolant = "laser", "M8", "M9"
        with mock.patch.object(_bgs, "schedule_status_refresh"):
            _bgs.resume_hold(r.plugin)
        self.assertEqual(r.printer.written, [b"\xa0", b"~"])
        self.assertEqual(r.plugin.coolant, "M8")

    def test_resume_sends_only_the_cycle_start_when_air_was_off_or_is_already_on(self):
        for at_pause, now in (("M9", "M9"), ("M8", "M8")):
            r = Rig()
            r.plugin.pauseStrategy, r.plugin.coolantAtPause, r.plugin.coolant = "laser", at_pause, now
            with mock.patch.object(_bgs, "schedule_status_refresh"):
                _bgs.resume_hold(r.plugin)
            self.assertEqual(r.printer.written, [b"~"])

    def test_a_user_m9_while_paused_runs_the_shell_override_even_with_delay_m9(self):
        r = Rig(paused=True)
        r.plugin.coolant, r.plugin.overrideM9, r.plugin.m9Command, r.plugin.delayM9 = "M8", True, "x", True
        with mock.patch.object(_bgs.subprocess, "Popen") as popen:
            self.assertEqual(r.send("M9"), (None,))
        popen.assert_called_once_with("x", shell=True)
        self.assertEqual(r.printer.written, [b"\xa0"])
        self.assertFalse(r.plugin.M9DelayActive)

    def test_the_redundant_m8_after_printing_stays_a_line(self):
        r = Rig(paused=False)
        r.plugin.coolant = "M8"
        self.assertEqual(r.send("M8"), ("M8",))
        self.assertEqual(r.printer.written, [])

    def test_a_cancel_drops_coolant_on_and_passes_coolant_off(self):
        r = Rig()
        r.plugin.cancelMotionActive, r.plugin.overrideM8, r.plugin.m8Command = True, True, "x"
        with mock.patch.object(_bgs.subprocess, "Popen") as popen:
            self.assertEqual(r.send("M8"), (None,))
            popen.assert_not_called()
        self.assertEqual(r.send("M9"), ("M9",))
        self.assertEqual(r.send("M3 S0"), ("M3 S0",))


class LaserOffAfterReset(unittest.TestCase):
    def test_the_banner_sends_the_laser_off_lines_once(self):
        r = Rig()
        _bgs.arm_laser_off_after_reset(r.plugin)
        r.receive("Grbl 1.1f ['$' for help]")
        r.receive("Grbl 1.1f ['$' for help]")
        self.assertEqual(r.printer.sent, [["M3 S0", "M5"]])

    def test_an_unarmed_banner_sends_nothing(self):
        r = Rig()
        r.receive("Grbl 1.1f ['$' for help]")
        self.assertEqual(r.printer.sent, [])

    def test_a_laser_cancel_without_a_banner_still_switches_the_laser_off(self):
        printer = Printer()
        p = flow_plugin(printer, grblState="Hold:0", positioning=0, grblCmdQueue=[], laserOffAfterReset=False)
        with mock.patch.object(_bgs, "refresh_grbl_state", return_value=False), \
                mock.patch.object(_bgs, "is_laser_mode", return_value=True), \
                mock.patch.object(_bgs, "set_grbl_state"), mock.patch.object(_bgs.time, "sleep"):
            _bgs.cancel_motion(p)
        self.assertIn(["M3 S0", "M5"], printer.sent)
        self.assertFalse(p.laserOffAfterReset)


class CancelMotion(unittest.TestCase):
    def test_the_reset_ends_the_hold_and_the_debt(self):
        printer = Printer()
        p = flow_plugin(printer, strayOksOwed=3, grblState="Hold:0", positioning=0, grblCmdQueue=[], coolantAtPause="M8",
                        laserOffAfterReset=False)
        with mock.patch.object(_bgs, "refresh_grbl_state", return_value=False), \
                mock.patch.object(_bgs, "is_laser_mode", return_value=False), \
                mock.patch.object(_bgs, "set_grbl_state"), mock.patch.object(_bgs.time, "sleep"):
            _bgs.cancel_motion(p)
        self.assertEqual((p.pauseHoldActive, p.strayOksOwed, p.coolantAtPause), (False, 0, "M9"))
        self.assertIn(b"\x18", printer.written)


if __name__ == "__main__":
    unittest.main()
