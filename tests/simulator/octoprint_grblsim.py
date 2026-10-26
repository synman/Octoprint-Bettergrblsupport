# coding=utf-8
"""OctoPrint plugin: virtual Grbl / FluidNC serial ports for testing Better Grbl Support without hardware.

Single-file plugin.  It exposes two serial ports to OctoPrint's connection panel:

* ``GRBL_SIM``    -> ``GrblVirtual``    (gnea/grbl 1.1h port, ``grbl_virtual.py``)
* ``FLUIDNC_SIM`` -> ``FluidNCVirtual`` (FluidNC 3.9 port, ``fluidnc_virtual.py``)

Modelled on OctoPrint's Virtual Printer plugin: the hooks are
``octoprint.comm.transport.serial.factory``, ``octoprint.comm.transport.serial.additional_port_names`` and
``octoprint.comm.protocol.atcommand.sending`` (bridges ``@SIM`` lines OctoPrint consumes itself).
Any other port name makes the factory return ``None`` so OctoPrint falls through to the next factory.

The two firmware ports live next to this file (``tests/simulator``).  The module puts its own real directory
on ``sys.path`` (symlink-safe), so symlinking this one file into ``<OctoPrint basedir>/plugins/`` works.

FluidNC axis letters: environment ``GRBL_SIM_FLUIDNC_AXES`` wins when set, else the plugin setting
``fluidnc_axes``, else ``XYZ``.  The value is a prefix of ``XYZABC`` with 2 to 6 letters.
The FluidNC machine gets a probe pin by default (without one, ``G38.x`` answers "Probe pin is not
configured" and ``@SIM PROBE`` has nothing to act on); environment ``GRBL_SIM_FLUIDNC_PROBE=0`` or the plugin
setting ``fluidnc_probe_pin`` = false removes it.
Parking and pause tests (environment wins over the plugin setting of the same name in brackets):
``GRBL_SIM_GRBL_PARKING=1`` [``grbl_parking``] builds ``GRBL_SIM`` with ``PARKING_ENABLE`` (``[OPT:VP,15,128]``);
``GRBL_SIM_GRBL_HOMING=1`` [``grbl_homing``] powers it up with ``$22=1`` (Alarm until ``$X`` or ``$H``);
``GRBL_SIM_GRBL_LASER=1`` [``grbl_laser``] powers it up with ``$32=1`` (laser mode);
``GRBL_SIM_TRACE=<file>`` [``trace_file``] appends one JSON line every 0.2 s, and at every change of state, while a port is open (``t`` epoch seconds,
``state`` as a status report names it, ``mpos`` machine position, ``spindle`` 0 off / 1 CW / 2 CCW), the machine's own
view for a test that cannot poll it (OctoPrint suppresses status requests while a job runs);
``GRBL_SIM_FLUIDNC_SPINDLE=laser|none|pwm`` [``fluidnc_spindle``] picks the FluidNC spindle (default ``laser``;
``pwm`` is a CNC spindle with ``spinup_ms`` / ``spindown_ms``); ``GRBL_SIM_FLUIDNC_HOMING=XYZ`` [``fluidnc_homing``]
gives those axes a homing cycle; ``GRBL_SIM_FLUIDNC_PARKING=1`` [``fluidnc_parking``] sets ``parking: enable: true``;
``GRBL_SIM_FLUIDNC_SPINUP_MS=<ms>`` [``fluidnc_spinup_ms``] sets the PWM spindle's ``spinup_ms`` and ``spindown_ms``.
FluidNC web server: environment ``GRBL_SIM_FLUIDNC_HTTP_PORT=<port>`` starts the WebUI ``/files`` endpoint
(``fluidnc_web.py``) on ``localhost:<port>`` and boots the FluidNC machine with ``HTTP/Enable=ON``,
``Hostname=localhost`` and ``HTTP/Port=<port>``, so a client that builds ``http://<Hostname>:<HTTP/Port>/files``
from the controller's own settings reaches it, as with a real board.  ``0`` picks a free port.  The server is
started once per OctoPrint process and serves whichever FluidNC machine is connected.

Fault-injection escape (handled in the serial wrapper, never forwarded to the firmware)
----------------------------------------------------------------------------------------
A written line that starts with ``@SIM `` at a line start is consumed by the wrapper.  It is answered with a
single ``ok`` (OctoPrint sent a line, so exactly one acknowledgement comes back, as from real firmware); a
malformed one is answered ``[MSG:SIM ERROR <reason>]`` then ``ok`` and logged as a warning.

OctoPrint never writes a line starting with ``@`` to the port (it handles it as an at-command), so a line typed in
the terminal or sent through ``/api/printer/command`` arrives through the ``atcommand.sending`` hook instead and
goes to the live simulated port of that connection.  Nothing was sent, so that path queues no ``ok``.

* ``@SIM ALARM <n>``       raise ALARM:<n> as the hardware paths do (``inject_alarm``; codes 1 and 2 also
                           reset the machine, as a limit trip does).
* ``@SIM PROBE <A><mm>``   place the virtual probe contact plane for axis ``<A>`` at machine coordinate
                           ``<mm>``, e.g. ``@SIM PROBE Z-5.0``.  Several planes may be set, one per command.
* ``@SIM PROBE OFF``       remove every contact plane and release a forced probe pin.
* ``@SIM LIMIT <letters>`` hold the limit pins of those axes active (``Pn:`` in status reports, hard-limit
                           alarm when enabled), e.g. ``@SIM LIMIT X`` or ``@SIM LIMIT XY`` or ``@SIM LIMIT X Y``.
                           Replaces the previous set.
* ``@SIM LIMIT OFF``       release all limit pins.
* ``@SIM ERROR <n>``       answer the next line written to the port with ``error:<n>`` instead of running it
                           (a controller that rejects a line sends the error and no ``ok``).
* ``@SIM ERROR <n> <k>x``  the same for the next ``k`` lines, e.g. ``@SIM ERROR 20 3x``.
* ``@SIM ERROR <n> <cmd>`` the same for the next line whose text equals ``<cmd>`` (case and spacing ignored),
                           once, e.g. ``@SIM ERROR 20 $G``.  Lines that arrive meanwhile are answered normally.
* ``@SIM ERROR OFF``       disarm every pending error.

Armed errors belong to the plugin, not to one connection, and are consumed by whichever simulated port sees the
matching line next.  The handshake that Better Grbl Support sends at connect cannot be hit by typing, because
the line goes out before the terminal can be used, so arm the error on a live connection, disconnect and
connect again: ``@SIM ERROR 20 $G``, disconnect, connect, and the ``$G`` of that handshake is answered
``error:20``.  Armed errors are cleared by ``@SIM ERROR OFF`` or by restarting OctoPrint.
"""
__plugin_pythoncompat__ = ">=3.7,<4"

import json
import logging
import os
import queue
import re
import sys
import threading
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import octoprint.plugin

    _SettingsPlugin = octoprint.plugin.SettingsPlugin
except ImportError:  # standalone use (smoke tests, scripts) without OctoPrint installed
    class _SettingsPlugin(object):
        _settings = None

try:
    from serial import SerialException as _SerialError
except ImportError:
    _SerialError = IOError

GRBL_PORT = "GRBL_SIM"
FLUIDNC_PORT = "FLUIDNC_SIM"
ENV_FLUIDNC_AXES = "GRBL_SIM_FLUIDNC_AXES"
ENV_FLUIDNC_PROBE = "GRBL_SIM_FLUIDNC_PROBE"
ENV_FLUIDNC_HTTP_PORT = "GRBL_SIM_FLUIDNC_HTTP_PORT"
ENV_GRBL_PARKING = "GRBL_SIM_GRBL_PARKING"
ENV_GRBL_HOMING = "GRBL_SIM_GRBL_HOMING"
ENV_GRBL_LASER = "GRBL_SIM_GRBL_LASER"
ENV_TRACE = "GRBL_SIM_TRACE"
TRACE_INTERVAL = 0.2
ENV_FLUIDNC_SPINDLE = "GRBL_SIM_FLUIDNC_SPINDLE"
ENV_FLUIDNC_HOMING = "GRBL_SIM_FLUIDNC_HOMING"
ENV_FLUIDNC_PARKING = "GRBL_SIM_FLUIDNC_PARKING"
ENV_FLUIDNC_SPINUP_MS = "GRBL_SIM_FLUIDNC_SPINUP_MS"
DEFAULT_FLUIDNC_AXES = "XYZ"
SIM_PREFIX = b"@SIM "
# Bytes the firmware's RX ISR picks off the stream; they never enter the line buffer, so they do not move
# the "at a line start" state the @SIM detector tracks (a bare "?" status poll must not hide the next line).
_REALTIME_BYTES = b"?!~\x18" + bytes(bytearray(range(0x80, 0x100)))
TICK_INTERVAL = 0.01
_SIM_ERROR_COUNT = re.compile(r"^(\d+)X$")

_log = logging.getLogger("octoprint.plugins.grblsim")


class SimSerial(object):
    """The serial-object contract OctoPrint needs, over a firmware port.

    ``write(bytes) -> int``; ``readline() -> bytes`` (``b""`` on timeout); ``close()`` stops the tick thread;
    ``timeout``, ``write_timeout``, ``port``, ``baudrate`` attributes; optional ``cancel_read`` /
    ``cancel_write``.  A daemon thread calls ``port.tick()`` on the wall clock; every access to the firmware
    port happens under one lock."""

    _faults = []                        # armed ``@SIM ERROR`` entries, shared by every port of this process
    _faults_lock = threading.Lock()

    @classmethod
    def clear_faults(cls):
        with cls._faults_lock:
            del cls._faults[:]

    def __init__(self, sim, port_name, baudrate, read_timeout, trace_file=None):
        self._sim = sim
        self._trace = open(trace_file, "a") if trace_file else None
        self._trace_next = 0.0
        self._trace_state = None
        self._port = port_name
        self._baudrate = baudrate
        self._timeout = read_timeout
        self.write_timeout = 10.0
        self._lock = threading.RLock()
        self._out = queue.Queue()
        self._closed = threading.Event()
        self._fault = None
        self._at_line_start = True
        self._capture = None            # partial "@SIM " line carried across writes
        with self._lock:
            self._collect()             # power-up banner
        self._thread = threading.Thread(target=self._run, name="grblsim-tick-" + port_name)
        self._thread.daemon = True
        self._thread.start()

    # ------------------------------------------------------------------ serial attributes
    @property
    def port(self):
        return self._port

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self._baudrate = value

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout = value

    @property
    def is_open(self):
        return not self._closed.is_set()

    @property
    def in_waiting(self):
        return self._out.qsize()

    @property
    def sim(self):
        """The firmware port object (tests may poke it directly; take no lock, so only when idle)."""
        return self._sim

    # ------------------------------------------------------------------ internals
    def _collect(self):
        """Move the firmware's pending reply lines into the read queue.  Caller holds the lock."""
        for line in self._sim.drain():
            self._out.put(line)

    def _run(self):
        while not self._closed.wait(TICK_INTERVAL):
            try:
                with self._lock:
                    self._sim.tick()
                    self._collect()
                    if self._trace is not None:
                        self._write_trace()
            except Exception as exc:
                _log.exception("virtual firmware tick failed; the simulated port is dead")
                self._fault = exc
                self._out.put(None)
                return

    def _write_trace(self):
        """One line of the machine's own state, at most every TRACE_INTERVAL.  Caller holds the lock."""
        now = time.time()
        sim = self._sim
        state = sim._state_text()
        if now < self._trace_next and state == self._trace_state:
            return              # a change of state is always written, so a short state cannot fall between samples
        self._trace_next = now + TRACE_INTERVAL
        self._trace_state = state
        self._trace.write(json.dumps({"t": round(now, 3), "state": state, "mpos": [round(v, 3) for v in sim.mpos()],
                                      "spindle": sim._spindle_get_state()}) + "\n")
        self._trace.flush()

    def _check(self):
        if self._fault is not None:
            raise _SerialError("simulator faulted: %r" % (self._fault,))

    # ------------------------------------------------------------------ serial I/O
    def write(self, data):
        self._check()
        if self._closed.is_set():
            raise _SerialError("write on closed simulated port")
        data = bytes(data)
        with self._lock:
            for piece in data.splitlines(True):
                if self._capture is not None:
                    self._capture += piece
                    self._finish_capture()
                elif self._at_line_start and piece.startswith(SIM_PREFIX):
                    self._capture = piece
                    self._finish_capture()
                else:
                    content = piece.translate(None, _REALTIME_BYTES)
                    code = self._take_error(content) if content.endswith((b"\n", b"\r")) else None
                    if code is not None:
                        # the line is rejected, not run: only its realtime bytes reach the firmware
                        self._sim.feed(bytes(bytearray(b for b in bytearray(piece) if b in bytearray(_REALTIME_BYTES))))
                        self._collect()     # replies the firmware already owes come before the error
                        self._out.put(("error:%d\r\n" % code).encode("ascii"))
                        self._at_line_start = True
                        continue
                    self._sim.feed(piece)
                    if content:
                        self._at_line_start = content.endswith((b"\n", b"\r"))
            self._collect()
        return len(data)

    def _take_error(self, content):
        """The error code of the armed ``@SIM ERROR`` that claims this complete line, else None."""
        text = " ".join(content.decode("ascii", "replace").split()).upper()
        if not text:
            return None
        with self._faults_lock:
            for fault in self._faults:
                if fault["command"] is None or fault["command"] == text:
                    fault["remaining"] -= 1
                    if fault["remaining"] <= 0:
                        self._faults.remove(fault)
                    return fault["code"]
        return None

    def inject(self, text):
        """Apply one ``@SIM ...`` line that did not come through ``write`` (OctoPrint's at-command hook).

        OctoPrint consumes the line itself and sends nothing to the port, so no ``ok`` is queued: it is not
        waiting for one.  Replies the verb produces (``ALARM:n``, a malformed-line message) are queued."""
        self._check()
        if self._closed.is_set():
            raise _SerialError("inject on closed simulated port")
        with self._lock:
            self._handle_sim(text.strip())
            self._collect()

    def _finish_capture(self):
        if not self._capture.endswith((b"\n", b"\r")):
            return                      # keep collecting until the line ends
        line, self._capture = self._capture, None
        self._at_line_start = True
        self._handle_sim(line.decode("ascii", "replace").strip())
        self._out.put(b"ok\r\n")

    def readline(self):
        self._check()
        if self._closed.is_set():
            return b""
        try:
            if self._timeout is None:
                while True:
                    try:
                        item = self._out.get(timeout=0.25)
                        break
                    except queue.Empty:
                        if self._closed.is_set():
                            return b""
            elif self._timeout <= 0:
                item = self._out.get_nowait()
            else:
                item = self._out.get(timeout=self._timeout)
        except queue.Empty:
            return b""
        if item is None:                # cancel_read wake-up, or the tick thread died
            self._check()
            return b""
        return item

    def cancel_read(self):
        self._out.put(None)

    def cancel_write(self):
        pass

    def flush(self):
        pass

    def close(self):
        self._closed.set()
        if self._thread is not threading.current_thread():
            self._thread.join(2.0)
        if self._trace is not None:
            self._trace.close()
            self._trace = None
        self._out.put(None)

    # ------------------------------------------------------------------ fault injection
    def _handle_sim(self, text):
        try:
            self._apply_sim(text.split()[1:])
        except ValueError as exc:
            _log.warning("bad %r: %s", text, exc)
            self._out.put(("[MSG:SIM ERROR %s]\r\n" % exc).encode("ascii", "replace"))

    def _apply_sim(self, words):
        """Caller holds the lock (called from write)."""
        sim = self._sim
        if not words:
            raise ValueError("empty @SIM command")
        verb, args = words[0].upper(), words[1:]
        if verb == "ALARM":
            if len(args) != 1 or not args[0].isdigit():
                raise ValueError("usage: @SIM ALARM <n>")
            sim.inject_alarm(int(args[0]))
        elif verb == "PROBE":
            if len(args) != 1:
                raise ValueError("usage: @SIM PROBE <axis><mm> | OFF")
            arg = args[0].upper()
            if arg == "OFF":
                sim.probe_trigger = None
                sim.probe_tripped = False
            else:
                if arg[0] not in sim.axis_letters:
                    raise ValueError("probe axis %r not in %s" % (arg[0], sim.axis_letters))
                try:
                    mm = float(arg[1:])
                except ValueError:
                    raise ValueError("usage: @SIM PROBE <axis><mm> | OFF")
                planes = dict(sim.probe_trigger or {})
                planes[arg[0]] = mm
                sim.probe_trigger = planes
        elif verb == "LIMIT":
            letters = "".join(args).upper()
            if not letters:
                raise ValueError("usage: @SIM LIMIT <axis letters> | OFF")
            mask = 0
            if letters != "OFF":
                for c in letters:
                    if c not in sim.axis_letters:
                        raise ValueError("limit axis %r not in %s" % (c, sim.axis_letters))
                    mask |= 1 << sim.axis_letters.index(c)
            sim.limit_pins = mask
        elif verb == "ERROR":
            self._arm_error(args)
        else:
            raise ValueError("unknown @SIM verb %r" % verb)
        sim._pump()
        self._collect()


    def _arm_error(self, args):
        usage = "usage: @SIM ERROR <n> [<k>x | <command>] | OFF"
        if len(args) == 1 and args[0].upper() == "OFF":
            self.clear_faults()
            return
        if not args or not args[0].isdigit() or int(args[0]) < 1:
            raise ValueError(usage)
        fault = {"code": int(args[0]), "command": None, "remaining": 1}
        rest = " ".join(args[1:]).upper()
        if rest:
            counted = _SIM_ERROR_COUNT.match(rest)
            if counted:
                fault["remaining"] = int(counted.group(1))
                if fault["remaining"] < 1:
                    raise ValueError(usage)
            else:
                fault["command"] = rest
        with self._faults_lock:
            self._faults.append(fault)


class GrblSimPlugin(_SettingsPlugin):
    """Serial factory + port-name hooks for the two simulated controllers."""

    def get_settings_defaults(self):
        return {"fluidnc_axes": DEFAULT_FLUIDNC_AXES, "fluidnc_probe_pin": True, "grbl_parking": False,
                "grbl_homing": False, "grbl_laser": False, "trace_file": "",
                "fluidnc_spindle": "laser", "fluidnc_homing": "", "fluidnc_parking": False,
                "fluidnc_spinup_ms": 0}

    def _option(self, env, setting, default=None):
        """The environment variable when set and not empty, else the plugin setting, else ``default``."""
        value = os.environ.get(env)
        if value is not None and value.strip():
            return value.strip()
        if self._settings is not None:
            value = self._settings.get([setting])
            if value not in (None, ""):
                return value
        return default

    def _flag(self, env, setting):
        value = self._option(env, setting, False)
        return value if isinstance(value, bool) else str(value).strip().lower() not in ("0", "false", "no", "off")

    def _fluidnc_axes(self):
        axes = os.environ.get(ENV_FLUIDNC_AXES)
        if not axes and self._settings is not None:
            axes = self._settings.get(["fluidnc_axes"])
        return (axes or DEFAULT_FLUIDNC_AXES).strip().upper()

    def _fluidnc_probe_pin(self):
        env = os.environ.get(ENV_FLUIDNC_PROBE)
        if env is not None and env.strip():
            return env.strip().lower() not in ("0", "false", "no", "off")
        if self._settings is not None:
            return bool(self._settings.get(["fluidnc_probe_pin"]))
        return True

    _web = None

    def _fluidnc_web(self):
        """The FluidNC WebUI server, started on first use when GRBL_SIM_FLUIDNC_HTTP_PORT is set."""
        env = os.environ.get(ENV_FLUIDNC_HTTP_PORT, "").strip()
        if not env:
            return None
        if GrblSimPlugin._web is None:
            from fluidnc_web import FluidNCWebServer
            GrblSimPlugin._web = FluidNCWebServer("localhost", int(env))
            _log.info("FluidNC sim web server on localhost:%d", GrblSimPlugin._web.port)
        return GrblSimPlugin._web

    def serial_factory(self, comm_instance, port, baudrate, read_timeout, *args, **kwargs):
        if port == GRBL_PORT:
            from grbl_virtual import BIT_LASER_MODE, GrblVirtual
            sim = GrblVirtual(parking=self._flag(ENV_GRBL_PARKING, "grbl_parking"),
                              homing=self._flag(ENV_GRBL_HOMING, "grbl_homing"))
            if self._flag(ENV_GRBL_LASER, "grbl_laser"):
                sim.settings.flags |= BIT_LASER_MODE
        elif port == FLUIDNC_PORT:
            from fluidnc_virtual import FluidNCVirtual
            web = self._fluidnc_web()
            sim = FluidNCVirtual(axes=self._fluidnc_axes(), probe_pin=self._fluidnc_probe_pin(),
                                 http_enable=web is not None,
                                 spindle=str(self._option(ENV_FLUIDNC_SPINDLE, "fluidnc_spindle", "laser")).lower(),
                                 homing=str(self._option(ENV_FLUIDNC_HOMING, "fluidnc_homing", "")).upper() or None)
            if self._flag(ENV_FLUIDNC_PARKING, "fluidnc_parking"):
                sim._cfg["parking"]["enable"] = True
            spinup = int(self._option(ENV_FLUIDNC_SPINUP_MS, "fluidnc_spinup_ms", 0))
            if spinup and "PWM" in sim._cfg:
                sim._cfg["PWM"]["spinup_ms"] = sim._cfg["PWM"]["spindown_ms"] = spinup
            if web is not None:
                for key, value in (("Hostname", "localhost"), ("HTTP/Port", str(web.port))):
                    status = sim._set_setting(sim._find_setting(key, False), value)
                    if status:
                        _log.warning("FluidNC sim: could not set %s=%s (error %d)", key, value, status)
                web.sim = sim
        else:
            return None
        return SimSerial(sim, port, baudrate, float(read_timeout), self._option(ENV_TRACE, "trace_file"))

    def additional_port_names(self, *args, **kwargs):
        return [GRBL_PORT, FLUIDNC_PORT]

    def atcommand_sending(self, comm_instance, phase, command, parameters, tags=None, *args, **kwargs):
        """``@SIM <verb> ...`` typed in OctoPrint reaches the live simulated port of that connection.

        OctoPrint strips the ``@``, so ``command`` is ``SIM``.  Any other at-command, and a connection that is
        not a simulated port, is left alone.  OctoPrint does not run at-command hooks while it is streaming
        a print."""
        if command != "SIM":
            return
        serial = getattr(comm_instance, "_serial", None)
        if isinstance(serial, SimSerial):
            serial.inject("@SIM " + (parameters or ""))


def __plugin_load__():
    plugin = GrblSimPlugin()

    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.transport.serial.factory": plugin.serial_factory,
        "octoprint.comm.transport.serial.additional_port_names": plugin.additional_port_names,
        "octoprint.comm.protocol.atcommand.sending": plugin.atcommand_sending,
    }


__plugin_name__ = "Grbl/FluidNC Simulator"
__plugin_description__ = "Virtual Grbl 1.1h and FluidNC 3.9 serial ports for testing Better Grbl Support"
__plugin_license__ = "AGPLv3"
