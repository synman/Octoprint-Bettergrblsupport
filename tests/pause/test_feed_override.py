#!/usr/bin/env python3
"""The feed and power overrides, through the real sending hook (``hook_gcode_sending``) and the real API handler
(``on_api_command``): an override change must apply the override to the job's own programmed value exactly once.

A change sends a line of its own, and that line goes back through the sending hook like any other, so anything it is
built from that already carries the override (``grblSpeed`` after a job line, the controller's ``FS:`` feed, which is
always mm/min, ``grblPowerLevel``) is overridden a second time. Lines are fed to the hook with the tags OctoPrint gives
them: ``source:file`` for the job's own, ``trigger:printer.commands`` for what ``printer.commands()`` sends.

Needs OctoPrint importable: run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_feed_override -v
"""
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
bgs = importlib.import_module("octoprint_bettergrblsupport")
_bgs = importlib.import_module("octoprint_bettergrblsupport._bgs")

JOB = {"source:file", "filepos:0", "fileline:1"}
COMMANDS = {"trigger:printer.commands"}
SCRIPT = {"source:script", "script:beforePrintResumed"}


class Rig:
    """The plugin with a printer that records what ``printer.commands()`` is given and a hook to send it through."""

    def __init__(self, version="[VER:1.1h.20190830:] [OPT:V,15,128]"):
        self.sent = []
        self.scripts = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.scripts, "gcode"))
        p = self.plugin = bgs.BetterGrblSupportPlugin()
        p._logger = logging.getLogger("test")
        p._printer_profile_manager = types.SimpleNamespace(get_current_or_default=lambda: {"id": "_bgs"})
        p._printer = types.SimpleNamespace(commands=lambda c, **k: self.sent.append(c), is_printing=lambda: True,
                                           is_operational=lambda: True)
        p._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *a, **k: None)
        p._identifier = "bettergrblsupport"
        p._settings = types.SimpleNamespace(get=lambda key: None, get_boolean=lambda key: False,
                                            global_get_basefolder=lambda name: self.scripts)
        p.grblVersion = version

    def send(self, line, tags=JOB):
        """What the controller receives for ``line``."""
        result = self.plugin.hook_gcode_sending(None, "sending", line, None, None, tags=tags)
        return result[0]

    def script(self, name):
        """The words of the script the plugin wrote for a drain pause or resume."""
        _bgs.write_pause_resume_scripts(self.plugin, "drain")
        with open(os.path.join(self.scripts, "gcode", name)) as file:
            return file.read().split()

    def job(self, *lines):
        return [self.send(line) for line in lines]

    def override(self, command, **data):
        """Make the change through the API, then send what it queued through the hook, as OctoPrint would."""
        self.sent.clear()
        self.plugin.on_api_command(command, data)
        return [self.send(line, COMMANDS) for line in list(self.sent)]


class Base(unittest.TestCase):
    def rig(self):
        r = Rig()
        self.addCleanup(shutil.rmtree, r.scripts, True)
        return r


class FeedOverride(Base):
    def test_inch_job_with_an_fs_report_is_overridden_once(self):
        # an inch job at F20 reports FS:508 (mm/min, whatever G20 says): that is what used to be sent back
        r = self.rig()
        r.job("G20", "G1 X0.2 F20")
        r.plugin.grblSpeed = 508.0
        self.assertEqual(r.override("feedRate", feed_rate=150), ["F30.000"])
        self.assertEqual(r.plugin.grblJobFeed, 20.0)

    def test_mm_job_with_the_override_already_set_is_overridden_once(self):
        # the job line went out as F1500 (grblSpeed = 1500): a second change must not start from that
        r = self.rig()
        r.plugin.feedRate = 1.5
        self.assertEqual(r.job("G21", "G1 X5 F1000"), ["G21", "G1 X5 F1500.000"])
        self.assertEqual(r.plugin.grblSpeed, 1500.0)
        self.assertEqual(r.override("feedRate", feed_rate=150), ["F1500.000"])
        self.assertEqual(r.override("feedRate", feed_rate=120), ["F1200.000"])
        self.assertEqual(r.plugin.grblJobFeed, 1000.0)

    def test_going_back_to_100_percent_sends_the_programmed_feed(self):
        r = self.rig()
        r.plugin.feedRate = 1.5
        r.job("G1 X5 F1000")
        self.assertEqual(r.override("feedRate", feed_rate=100), ["F1000.0"])
        self.assertEqual(r.plugin.feedRate, 0.0)

    def test_no_job_feed_yet_sends_nothing(self):
        r = self.rig()
        self.assertEqual(r.override("feedRate", feed_rate=150), [])

    def test_lines_that_are_not_the_job_do_not_become_the_job_feed(self):
        # framing, a probe, a pre-1.1 jog and the override change itself all send an F word
        r = self.rig()
        r.job("G1 X5 F1000")
        for line in ("G1 F3000", "G0 G91 X-30 F500", "G38.2 Z-20 F100", "G1 G91 X1 F800"):
            r.send(line, COMMANDS)
        r.send("F2000", SCRIPT)
        self.assertEqual(r.plugin.grblJobFeed, 1000.0)
        self.assertEqual(r.override("feedRate", feed_rate=150), ["F1500.000"])

    def test_the_job_feed_is_followed_through_the_hook_from_the_jobs_lines(self):
        r = self.rig()
        r.plugin.feedRate = 1.5
        r.job("G1 X5 F1000")
        self.assertEqual(r.plugin.grblJobFeed, 1000.0)
        r.job("G1 X6 F800")
        self.assertEqual(r.plugin.grblJobFeed, 800.0)

    def test_the_resume_script_restores_the_programmed_feed_which_the_hook_overrides_once(self):
        r = self.rig()
        r.plugin.feedRate = 1.5
        r.job("G21", "G1 X5 F1000")
        r.plugin.grblSpeed = 2250.0                       # an FS: report or an earlier compounded line
        self.assertEqual(r.override("feedRate", feed_rate=150), ["F1500.000"])
        feed = [w for w in r.script("beforePrintResumed") if w.startswith("F")]
        self.assertEqual(feed, ["F1000.0"])
        self.assertEqual(r.send(feed[0], SCRIPT), "F1500.000")


class NextJob(Base):
    def test_a_jobs_feed_and_power_are_forgotten_when_it_ends(self):
        # job A (mm, S12000 F3000) ends; job B is inch and has set no S or F yet: neither override nor resume may use A's
        from octoprint.events import Events
        for name in ("PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN", "PLUGIN_PLUGINMANAGER_DISABLE_PLUGIN"):
            if not hasattr(Events, name):               # registered by the plugin manager at runtime
                setattr(Events, name, name.lower())
                self.addCleanup(delattr, Events, name)
        r = self.rig()
        r.plugin._settings.set_boolean = lambda *a, **k: None
        r.job("G21", "M3 S12000", "G1 X5 F3000")
        self.assertEqual((r.plugin.grblJobFeed, r.plugin.grblJobPower), (3000.0, 12000.0))
        _bgs.on_event(r.plugin, Events.PRINT_DONE, {})
        self.assertEqual((r.plugin.grblJobFeed, r.plugin.grblJobPower), (0.0, 0.0))
        r.job("G20")
        self.assertEqual(r.override("feedRate", feed_rate=110), [])
        self.assertEqual(r.override("powerRate", power_rate=110), [])
        self.assertNotIn("F3000.0", r.script("beforePrintResumed"))


class PowerOverride(Base):
    def test_a_power_change_is_applied_to_the_programmed_power_once(self):
        r = self.rig()
        r.plugin.powerRate = 1.5
        self.assertEqual(r.job("M3 S500"), ["M3 S750.000"])
        self.assertEqual(r.plugin.grblPowerLevel, 750.0)       # what went out, as the display shows it
        self.assertEqual(r.override("powerRate", power_rate=150), ["S750.000"])
        self.assertEqual(r.override("powerRate", power_rate=120), ["S600.000"])
        self.assertEqual(r.plugin.grblJobPower, 500.0)

    def test_going_back_to_100_percent_sends_the_programmed_power(self):
        r = self.rig()
        r.plugin.powerRate = 1.5
        r.job("M3 S500")
        self.assertEqual(r.override("powerRate", power_rate=100), ["S500.0"])

    def test_the_status_report_power_is_not_the_job_power(self):
        r = self.rig()
        r.job("M3 S500")
        r.plugin.grblPowerLevel = 750.0                    # FS: or $G after an override
        self.assertEqual(r.override("powerRate", power_rate=150), ["S750.000"])

    def test_lines_that_are_not_the_job_do_not_become_the_job_power(self):
        r = self.rig()
        r.job("M3 S500")
        r.send("M3 S100", COMMANDS)
        r.send("S900", SCRIPT)
        self.assertEqual(r.plugin.grblJobPower, 500.0)

    def test_the_resume_script_restores_the_programmed_power(self):
        r = self.rig()
        r.plugin.powerRate = 1.5
        r.job("M3 S500")
        power = [w for w in r.script("beforePrintResumed") if w.startswith("S")]
        self.assertEqual(power, ["S500.0"])
        self.assertEqual(r.send(power[0], SCRIPT), "S750.000")

    def test_a_word_inside_another_word_is_not_a_power_or_feed_word(self):
        r = self.rig()
        r.job("M3 S500", "G1 X1 F200")
        r.send("SPINDLEPLUS10", COMMANDS)
        r.send("SPINDLEPLUS10", JOB)
        self.assertEqual((r.plugin.grblJobPower, r.plugin.grblJobFeed), (500.0, 200.0))


if __name__ == "__main__":
    unittest.main()
