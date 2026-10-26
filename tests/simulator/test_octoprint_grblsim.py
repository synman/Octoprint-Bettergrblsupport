#!/usr/bin/env python3
"""Tests for the ``@SIM ERROR`` fault escape of ``octoprint_grblsim.py`` (the serial wrapper, not the firmware ports).

Run:  cd tests/simulator && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_octoprint_grblsim -v

The plugin imports without OctoPrint, so each test builds a ``SimSerial`` over a real ``GrblVirtual`` or
``FluidNCVirtual`` and reads the reply lines.  The expected bytes are the firmware's own: a controller that rejects
a line answers ``error:<n>`` and no ``ok`` (gnea/grbl ``protocol_execute_line`` -> ``report_status_message``).
"""
import unittest

import octoprint_grblsim as sim_plugin
from fluidnc_virtual import FluidNCVirtual
from grbl_virtual import GrblVirtual

GC = b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n"


def drain(port):
    out = []
    while True:
        line = port.readline()
        if not line:
            return out
        out.append(line)


class ErrorEscape(unittest.TestCase):
    make_sim = staticmethod(GrblVirtual)

    def setUp(self):
        sim_plugin.SimSerial.clear_faults()
        self.addCleanup(sim_plugin.SimSerial.clear_faults)
        self.port = sim_plugin.SimSerial(self.make_sim(), "TEST", 115200, 0.05)
        self.addCleanup(self.port.close)
        drain(self.port)                                    # power-up banner

    def send(self, text):
        self.port.write(text.encode("ascii"))
        return drain(self.port)

    def test_arming_is_answered_with_one_ok(self):
        self.assertEqual(self.send("@SIM ERROR 20 $G\n"), [b"ok\r\n"])

    def test_named_command_errors_once_and_replaces_ok(self):
        self.send("@SIM ERROR 20 $G\n")
        self.assertEqual(self.send("$G\n"), [b"error:20\r\n"])
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])

    def test_other_lines_pass_until_the_named_one_arrives(self):
        self.send("@SIM ERROR 20 $G\n")
        self.assertEqual(self.send("G4 P0\n"), [b"ok\r\n"])
        self.assertEqual(self.send("$G\n"), [b"error:20\r\n"])

    def test_command_match_ignores_case_and_spacing(self):
        self.send("@SIM ERROR 22 g1   x1\n")
        self.assertEqual(self.send("G1 X1\n"), [b"error:22\r\n"])

    def test_bare_code_errors_the_next_line(self):
        self.send("@SIM ERROR 3\n")
        self.assertEqual(self.send("$G\n"), [b"error:3\r\n"])
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])

    def test_count_errors_that_many_lines(self):
        self.send("@SIM ERROR 3 2x\n")
        self.assertEqual(self.send("$G\n"), [b"error:3\r\n"])
        self.assertEqual(self.send("$G\n"), [b"error:3\r\n"])
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])

    def test_the_line_is_rejected_not_run(self):
        self.send("@SIM ERROR 20 G91\n")
        self.assertEqual(self.send("G91\n"), [b"error:20\r\n"])
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])      # still G90: G91 never reached the parser
        self.assertEqual(self.send("G91\n"), [b"ok\r\n"])
        self.assertEqual(self.send("$G\n"), [GC.replace(b"G90", b"G91"), b"ok\r\n"])

    def test_empty_and_realtime_only_lines_do_not_consume_an_error(self):
        self.send("@SIM ERROR 20 $G\n")
        self.port.write(b"\n")
        self.assertEqual(drain(self.port), [b"ok\r\n"])
        self.assertEqual(self.send("$G\n"), [b"error:20\r\n"])

    def test_off_disarms_everything(self):
        self.send("@SIM ERROR 20 $G\n")
        self.send("@SIM ERROR 3 5x\n")
        self.assertEqual(self.send("@SIM ERROR OFF\n"), [b"ok\r\n"])
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])

    def test_bad_usage_is_reported_and_arms_nothing(self):
        for line in ("@SIM ERROR\n", "@SIM ERROR x\n", "@SIM ERROR 0\n", "@SIM ERROR 20 0x\n"):
            reply = self.send(line)
            self.assertTrue(reply[0].startswith(b"[MSG:SIM ERROR usage: @SIM ERROR"), (line, reply))
            self.assertEqual(reply[1], b"ok\r\n")
        self.assertEqual(self.send("$G\n"), [GC, b"ok\r\n"])

    def test_armed_error_belongs_to_the_plugin_and_survives_the_connection(self):
        self.send("@SIM ERROR 20 $G\n")
        self.port.close()
        second = sim_plugin.SimSerial(self.make_sim(), "TEST2", 115200, 0.05)
        self.addCleanup(second.close)
        drain(second)
        second.write(b"$G\n")
        self.assertEqual(drain(second), [b"error:20\r\n"])

    def test_at_command_path_arms_the_live_port(self):
        self.port.inject("@SIM ERROR 20 $G")                # what OctoPrint's atcommand hook delivers
        self.assertEqual(drain(self.port), [])              # nothing was written, so no ok
        self.assertEqual(self.send("$G\n"), [b"error:20\r\n"])

    def test_error_follows_replies_the_firmware_already_owes(self):
        self.send("@SIM ERROR 20 $G\n")
        self.port.write(b"$I\n$G\n")
        got = drain(self.port)
        self.assertEqual(got[-2:], [b"ok\r\n", b"error:20\r\n"])


    def test_a_rejected_line_still_delivers_its_realtime_bytes(self):
        # only the realtime bytes of a rejected line reach the firmware: the `?` is answered, then the error
        self.send("@SIM ERROR 20 $G\n")
        reply = self.send("$G?\n")
        self.assertEqual(len(reply), 2, reply)
        self.assertTrue(reply[0].startswith(b"<") and reply[0].endswith(b">\r\n"), reply)
        self.assertEqual(reply[1], b"error:20\r\n")

    def test_the_armed_command_must_equal_the_line_not_be_part_of_it(self):
        self.send("@SIM ERROR 20 G1\n")
        self.assertEqual(self.send("G1 X1 F100\n"), [b"ok\r\n"])       # contains G1, is not G1
        self.assertEqual(self.send("G1\n"), [b"error:20\r\n"])


class ErrorEscapeFluidNC(ErrorEscape):
    make_sim = staticmethod(FluidNCVirtual)


class MachineOptions(unittest.TestCase):
    """Environment variables that shape the machine the serial factory builds (parking and pause tests)."""

    ENV = ("GRBL_SIM_GRBL_PARKING", "GRBL_SIM_GRBL_HOMING", "GRBL_SIM_GRBL_LASER", "GRBL_SIM_TRACE",
           "GRBL_SIM_FLUIDNC_SPINDLE", "GRBL_SIM_FLUIDNC_HOMING",
           "GRBL_SIM_FLUIDNC_PARKING", "GRBL_SIM_FLUIDNC_SPINUP_MS")

    def setUp(self):
        import os
        self.environ = os.environ
        saved = {k: os.environ.pop(k, None) for k in self.ENV}

        def restore():
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        self.addCleanup(restore)
        sim_plugin.__plugin_load__()
        self.plugin = sim_plugin.__plugin_implementation__

    def open(self, name):
        port = sim_plugin.__plugin_hooks__["octoprint.comm.transport.serial.factory"](None, name, 115200, 0.05)
        self.addCleanup(port.close)
        drain(port)
        return port

    def ask(self, port, text):
        port.write(text.encode("ascii"))
        return drain(port)

    def test_defaults_build_the_stock_machines(self):
        self.assertEqual(self.ask(self.open("GRBL_SIM"), "$I\n")[1], b"[OPT:V,15,128]\r\n")
        port = self.open("FLUIDNC_SIM")
        self.assertEqual(self.ask(port, "$/parking/enable\n"), [b"$/parking/enable=false\r\n", b"ok\r\n"])
        self.assertEqual(self.ask(port, "$Grbl/LaserMode\n")[0], b"$Grbl/LaserMode=1\r\n")

    def test_grbl_parking_and_homing(self):
        self.environ["GRBL_SIM_GRBL_PARKING"] = "1"
        self.environ["GRBL_SIM_GRBL_HOMING"] = "1"
        port = self.open("GRBL_SIM")
        self.assertEqual(self.ask(port, "$I\n")[1], b"[OPT:VP,15,128]\r\n")
        self.assertIn(b"$22=1\r\n", self.ask(port, "$$\n"))
        self.assertTrue(self.ask(port, "G0X1\n")[0].startswith(b"error:9"))    # homing lock until $X

    def test_false_looking_values_switch_a_flag_off(self):
        # a flag is off for 0 / false / no / off, in any case and with spaces; anything else non-empty is on
        for value in ("0", "false", "No", " OFF "):
            for name in ("GRBL_SIM_GRBL_PARKING", "GRBL_SIM_GRBL_HOMING", "GRBL_SIM_GRBL_LASER"):
                self.environ[name] = value
            port = self.open("GRBL_SIM")
            self.assertEqual(self.ask(port, "$I\n")[1], b"[OPT:V,15,128]\r\n", value)
            settings = self.ask(port, "$$\n")
            self.assertIn(b"$22=0\r\n", settings, value)
            self.assertIn(b"$32=0\r\n", settings, value)
            self.assertEqual(self.ask(port, "G0X1\n"), [b"ok\r\n"], value)     # no homing lock
            port.close()
        for value in ("1", "true", "yes", "on"):
            self.environ["GRBL_SIM_GRBL_LASER"] = value
            self.assertIn(b"$32=1\r\n", self.ask(self.open("GRBL_SIM"), "$$\n"), value)

    def test_grbl_laser_mode(self):
        self.environ["GRBL_SIM_GRBL_LASER"] = "1"
        self.assertIn(b"$32=1\r\n", self.ask(self.open("GRBL_SIM"), "$$\n"))

    def test_trace_file_gets_the_machine_state(self):
        import json
        import os
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            self.environ["GRBL_SIM_TRACE"] = path
            port = self.open("GRBL_SIM")
            self.ask(port, "G21G90\nG1X5F600\n")
            time.sleep(0.7)
            port.close()
            rows = [json.loads(line) for line in open(path)]
        self.assertTrue(len(rows) >= 2, rows)
        self.assertEqual(sorted(rows[0]), ["mpos", "spindle", "state", "t"])
        self.assertTrue(any(r["state"] == "Run" for r in rows), rows)
        self.assertEqual(len(rows[0]["mpos"]), 3)

    def test_trace_writes_every_change_of_state_whatever_the_interval(self):
        import json
        import os
        import tempfile
        import time
        saved = sim_plugin.TRACE_INTERVAL
        self.addCleanup(setattr, sim_plugin, "TRACE_INTERVAL", saved)
        sim_plugin.TRACE_INTERVAL = 60.0        # after the first row only a change of state can write another
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            self.environ["GRBL_SIM_TRACE"] = path
            port = self.open("GRBL_SIM")
            self.ask(port, "G21G90\nG1X0.8F1000\n")
            time.sleep(0.6)
            port.close()
            states = [json.loads(line)["state"] for line in open(path)]
        self.assertEqual(states[0], "Idle")
        self.assertIn("Run", states)
        self.assertEqual(states[-1], "Idle")

    def test_fluidnc_cnc_spindle_homing_and_parking(self):
        self.environ["GRBL_SIM_FLUIDNC_SPINDLE"] = "pwm"
        self.environ["GRBL_SIM_FLUIDNC_HOMING"] = "XYZ"
        self.environ["GRBL_SIM_FLUIDNC_PARKING"] = "1"
        self.environ["GRBL_SIM_FLUIDNC_SPINUP_MS"] = "1500"
        port = self.open("FLUIDNC_SIM")
        self.assertEqual(self.ask(port, "$/parking/enable\n"), [b"$/parking/enable=true\r\n", b"ok\r\n"])
        self.assertEqual(self.ask(port, "$/PWM/spinup_ms\n"), [b"$/PWM/spinup_ms=1500\r\n", b"ok\r\n"])
        self.assertEqual(self.ask(port, "$/PWM/spindown_ms\n"), [b"$/PWM/spindown_ms=1500\r\n", b"ok\r\n"])
        self.assertEqual(self.ask(port, "$Grbl/LaserMode\n")[0], b"$Grbl/LaserMode=0\r\n")
        self.assertEqual(self.ask(port, "$Grbl/HomingCycleEnable\n")[0], b"$Grbl/HomingCycleEnable=1\r\n")


if __name__ == "__main__":
    unittest.main()
