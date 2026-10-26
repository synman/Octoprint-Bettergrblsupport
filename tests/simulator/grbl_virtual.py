#!/usr/bin/env python3
"""Pure-Python "virtual firmware" port of gnea/grbl 1.1h (commit bfb67f0).

Target configuration: DEFAULTS_GENERIC, the stock config.h (N_AXIS 3, variable spindle, no M7, no
parking, no safety-door pin, HOMING_INIT_LOCK).  ``GrblVirtual(parking=True)`` is the same build with
``PARKING_ENABLE`` uncommented (``[OPT:VP,...]``, the parking motions of ``protocol_exec_rt_suspend`` with the
config.h constants ``PARKING_AXIS`` Z, ``PARKING_TARGET`` -5.0, ``PARKING_RATE`` 500, ``PARKING_PULLOUT_RATE`` 100,
``PARKING_PULLOUT_INCREMENT`` 5.0; not modelled: a second door event while a parking motion runs).  No OctoPrint import; stdlib only.  Fidelity to the C
source is the goal; every firmware rule carries a ``Source: <file>:<function> @ bfb67f0 (lines a-b)``
docstring line.

Public surface: ``feed(bytes)``, ``drain() -> [bytes]``, ``tick(dt=None)``, ``inject_alarm(code)`` and the
test hooks ``probe_trigger`` / ``probe_trigger_z`` / ``probe_tripped`` / ``limit_pins`` / ``instant``.

Control flow mirrors the C program: ``_main_gen`` is main(), ``_protocol_main_loop`` is protocol.c's loop,
and everything that blocks in C (buffer sync, dwell, probe cycle, suspend, full planner, critical alarm)
is a generator that yields until ``tick`` / ``feed`` give it something to do.  Floats are emulated as
IEEE-754 binary32 (avr-gcc double == float) at the points where they reach reports or limit checks.

Deliberate deviations (no hardware): (1) motion is a constant-speed linear model at the planner's
nominal speed, with one constant-deceleration ramp for hold / jog cancel / probe cancel and no
junction planning; (2) an arc is one planner block of true arc length (no segmentation, soft limits
only check its end point); (3) ``$H`` completes instantly (state never reads Home); (4) the 500 ms
``delay_ms`` after ``ALARM:`` is omitted; (5) the probe is a virtual contact plane (see
``probe_trigger``); (6) settings / coordinate data / startup lines persist across soft reset inside one
instance (EEPROM stand-in) but not across instances; (7) ``isequal_position_vector`` compares values,
not bit patterns (-0.0 == 0.0); (8) hard-limit / door / control pins are only reachable through
``limit_pins`` / ``inject_alarm``; (9) the stock build has 3 axes (STOCK_N_AXIS), so A/B words are error:20 as on the real build.

The axis count is per instance (``self.n_axis`` / ``self.axis_letters``) so subclasses can model
firmware with a different axis set; ``GrblVirtual`` itself only accepts the stock ``"XYZ"``.
"""
import math
import re
import struct
import time as _time

SRC = "bfb67f0"
VERSION = "1.1h"
BUILD = "20190830"

STOCK_N_AXIS = 3     # nuts_bolts.h:N_AXIS of the stock build; the live axis count is per instance (self.n_axis)
RX_BUFFER_SIZE = 128
BLOCK_BUFFER_SIZE = 16
LINE_BUFFER_SIZE = 80
MM_PER_INCH = 25.40
INCH_PER_MM = 0.0393701

_F32 = struct.Struct("<f")
_pack, _unpack = _F32.pack, _F32.unpack


def f32(x):
    """Round a Python float to IEEE binary32 (AVR float arithmetic)."""
    try:
        return _unpack(_pack(x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def c_round(x):
    """C round() / lround(): half away from zero.

    Non-finite input saturates like AVR libgcc's float-to-long conversion (+-inf to the long limits, NaN to 0)
    instead of raising."""
    if not math.isfinite(x):
        return 0 if x != x else (0x7FFFFFFF if x > 0 else -0x80000000)
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def c_fix_u32(x):
    """C float -> unsigned 32-bit (AVR libgcc ``__fixunssfsi``, the ESP32 ``utrunc.s``): truncates toward zero;
    negative and NaN give 0; overflow, +inf included, saturates to all bits set.  A narrower C target (uint8_t,
    uint16_t) keeps the low bits, so callers mask the result with ``& 0xFF`` / ``& 0xFFFF``."""
    if x != x or x < 0:
        return 0
    return 0xFFFFFFFF if x >= 4294967296.0 else int(x)


def c_fix_i32(x):
    """C float -> signed 32-bit (AVR libgcc ``__fixsfsi``): truncates toward zero; NaN gives 0; overflow, +-inf
    included, saturates to the int32 limits (the same limits ``c_round`` uses)."""
    if x != x:
        return 0
    if x >= 2147483648.0:
        return 0x7FFFFFFF
    return -0x80000000 if x <= -2147483648.0 else int(x)


# --- report.h status codes -------------------------------------------------------------------
STATUS_OK = 0
STATUS_EXPECTED_COMMAND_LETTER = 1
STATUS_BAD_NUMBER_FORMAT = 2
STATUS_INVALID_STATEMENT = 3
STATUS_NEGATIVE_VALUE = 4
STATUS_SETTING_DISABLED = 5
STATUS_SETTING_STEP_PULSE_MIN = 6
STATUS_SETTING_READ_FAIL = 7
STATUS_IDLE_ERROR = 8
STATUS_SYSTEM_GC_LOCK = 9
STATUS_SOFT_LIMIT_ERROR = 10
STATUS_OVERFLOW = 11
STATUS_CHECK_DOOR = 13
STATUS_TRAVEL_EXCEEDED = 15
STATUS_INVALID_JOG_COMMAND = 16
STATUS_GCODE_UNSUPPORTED_COMMAND = 20
STATUS_GCODE_MODAL_GROUP_VIOLATION = 21
STATUS_GCODE_UNDEFINED_FEED_RATE = 22
STATUS_GCODE_COMMAND_VALUE_NOT_INTEGER = 23
STATUS_GCODE_AXIS_COMMAND_CONFLICT = 24
STATUS_GCODE_WORD_REPEATED = 25
STATUS_GCODE_NO_AXIS_WORDS = 26
STATUS_GCODE_INVALID_LINE_NUMBER = 27
STATUS_GCODE_VALUE_WORD_MISSING = 28
STATUS_GCODE_UNSUPPORTED_COORD_SYS = 29
STATUS_GCODE_G53_INVALID_MOTION_MODE = 30
STATUS_GCODE_AXIS_WORDS_EXIST = 31
STATUS_GCODE_NO_AXIS_WORDS_IN_PLANE = 32
STATUS_GCODE_INVALID_TARGET = 33
STATUS_GCODE_ARC_RADIUS_ERROR = 34
STATUS_GCODE_NO_OFFSETS_IN_PLANE = 35
STATUS_GCODE_UNUSED_WORDS = 36
STATUS_GCODE_G43_DYNAMIC_AXIS_ERROR = 37
STATUS_GCODE_MAX_VALUE_EXCEEDED = 38

# report.h feedback messages
MESSAGE_CRITICAL_EVENT, MESSAGE_ALARM_LOCK, MESSAGE_ALARM_UNLOCK, MESSAGE_ENABLED, MESSAGE_DISABLED = 1, 2, 3, 4, 5
MESSAGE_SAFETY_DOOR_AJAR, MESSAGE_CHECK_LIMITS, MESSAGE_PROGRAM_END, MESSAGE_RESTORE_DEFAULTS = 6, 7, 8, 9
MESSAGE_SPINDLE_RESTORE, MESSAGE_SLEEP_MODE = 10, 11
_MESSAGES = {
    1: "Reset to continue", 2: "'$H'|'$X' to unlock", 3: "Caution: Unlocked", 4: "Enabled", 5: "Disabled",
    6: "Check Door", 7: "Check Limits", 8: "Pgm End", 9: "Restoring defaults", 10: "Restoring spindle",
    11: "Sleeping",
}

# system.h
EXEC_STATUS_REPORT, EXEC_CYCLE_START, EXEC_CYCLE_STOP, EXEC_FEED_HOLD = 1, 2, 4, 8
EXEC_RESET, EXEC_SAFETY_DOOR, EXEC_MOTION_CANCEL, EXEC_SLEEP = 16, 32, 64, 128
EXEC_ALARM_HARD_LIMIT, EXEC_ALARM_SOFT_LIMIT, EXEC_ALARM_ABORT_CYCLE = 1, 2, 3
EXEC_ALARM_PROBE_FAIL_INITIAL, EXEC_ALARM_PROBE_FAIL_CONTACT, EXEC_ALARM_HOMING_FAIL_RESET = 4, 5, 6
EXEC_FEED_OVR_RESET, EXEC_FEED_OVR_COARSE_PLUS, EXEC_FEED_OVR_COARSE_MINUS = 1, 2, 4
EXEC_FEED_OVR_FINE_PLUS, EXEC_FEED_OVR_FINE_MINUS, EXEC_RAPID_OVR_RESET = 8, 16, 32
EXEC_RAPID_OVR_MEDIUM, EXEC_RAPID_OVR_LOW = 64, 128
EXEC_SPINDLE_OVR_RESET, EXEC_SPINDLE_OVR_COARSE_PLUS, EXEC_SPINDLE_OVR_COARSE_MINUS = 1, 2, 4
EXEC_SPINDLE_OVR_FINE_PLUS, EXEC_SPINDLE_OVR_FINE_MINUS, EXEC_SPINDLE_OVR_STOP = 8, 16, 32
EXEC_COOLANT_FLOOD_OVR_TOGGLE = 64
STATE_IDLE, STATE_ALARM, STATE_CHECK_MODE, STATE_HOMING = 0, 1, 2, 4
STATE_CYCLE, STATE_HOLD, STATE_JOG, STATE_SAFETY_DOOR, STATE_SLEEP = 8, 16, 32, 64, 128
SUSPEND_HOLD_COMPLETE, SUSPEND_RESTART_RETRACT, SUSPEND_RETRACT_COMPLETE, SUSPEND_INITIATE_RESTORE = 1, 2, 4, 8
SUSPEND_RESTORE_COMPLETE, SUSPEND_SAFETY_DOOR_AJAR, SUSPEND_MOTION_CANCEL, SUSPEND_JOG_CANCEL = 16, 32, 64, 128
STEP_CONTROL_EXECUTE_HOLD, STEP_CONTROL_EXECUTE_SYS_MOTION, STEP_CONTROL_UPDATE_SPINDLE_PWM = 2, 4, 8
SPINDLE_STOP_OVR_ENABLED, SPINDLE_STOP_OVR_INITIATE, SPINDLE_STOP_OVR_RESTORE, SPINDLE_STOP_OVR_RESTORE_CYCLE = 1, 2, 4, 8

# planner.h condition flags
PL_COND_RAPID, PL_COND_SYSTEM, PL_COND_NO_FEED_OVR, PL_COND_INVERSE_TIME = 1, 2, 4, 8
PL_COND_SPINDLE_CW, PL_COND_SPINDLE_CCW, PL_COND_COOLANT_FLOOD = 16, 32, 64
PL_COND_SPINDLE_MASK = PL_COND_SPINDLE_CW | PL_COND_SPINDLE_CCW
PL_COND_ACCESSORY_MASK = PL_COND_SPINDLE_MASK | PL_COND_COOLANT_FLOOD

# config.h parking constants (PARKING_ENABLE builds)
PARKING_AXIS = 2
PARKING_TARGET = -5.0
PARKING_RATE = 500.0
PARKING_PULLOUT_RATE = 100.0
PARKING_PULLOUT_INCREMENT = 5.0
SAFETY_DOOR_SPINDLE_DELAY = 4.0
SAFETY_DOOR_COOLANT_DELAY = 1.0

# gcode.h
(MG_G0, MG_G1, MG_G2, MG_G3, MG_G4, MG_G5, MG_G6, MG_G7, MG_G8, MG_G12, MG_G13, MG_M4, MG_M7, MG_M8) = range(14)
MOTION_SEEK, MOTION_LINEAR, MOTION_CW_ARC, MOTION_CCW_ARC, MOTION_NONE = 0, 1, 2, 3, 80
MOTION_PROBE_TOWARD, MOTION_PROBE_TOWARD_NO_ERROR, MOTION_PROBE_AWAY, MOTION_PROBE_AWAY_NO_ERROR = 140, 141, 142, 143
(W_F, W_I, W_J, W_K, W_L, W_N, W_P, W_R, W_S, W_T, W_X, W_Y, W_Z, W_A, W_B, W_C) = range(16)
NM_DWELL, NM_SET_COORD_DATA, NM_GO_HOME_0, NM_SET_HOME_0, NM_GO_HOME_1, NM_SET_HOME_1 = 4, 10, 28, 38, 30, 40
NM_ABS_OVERRIDE, NM_SET_COORD_OFFSET, NM_RESET_COORD_OFFSET = 53, 92, 102
PROGRAM_FLOW_PAUSED = 3
GC_PARSER_JOG, GC_PARSER_ARC_CW, GC_PARSER_PROBE_AWAY, GC_PARSER_PROBE_NO_ERROR = 1, 4, 8, 16
GC_PARSER_LASER_FORCE_SYNC, GC_PARSER_LASER_DISABLE, GC_PARSER_LASER_ISMOTION = 32, 64, 128
GC_UPDATE_POS_TARGET, GC_UPDATE_POS_SYSTEM, GC_UPDATE_POS_NONE = 0, 1, 2
SETTING_INDEX_G28, SETTING_INDEX_G30, SETTING_INDEX_NCOORD = 6, 7, 7

# settings.h flags
BIT_REPORT_INCHES, BIT_LASER_MODE, BIT_INVERT_ST_ENABLE, BIT_HARD_LIMIT = 1, 2, 4, 8
BIT_HOMING_ENABLE, BIT_SOFT_LIMIT, BIT_INVERT_LIMIT_PINS, BIT_INVERT_PROBE_PIN = 16, 32, 64, 128

# serial buffer / line assembly
_LF_PAREN, _LF_SEMI, _LF_OVERFLOW = 2, 4, 1
_EOL = re.compile(rb"[\r\n]")
_SPECIAL = re.compile(rb"[()/;]")
_REALTIME = re.compile(rb"[\x18?!~\x80-\xff]")
_DEL = bytes(range(0, 33))
_UPPER = bytes.maketrans(bytes(range(97, 123)), bytes(range(65, 91)))
_NUM = re.compile(r"([+-]?)(\d*)(?:(\.)(\d*))?")


def _fdiv(a, b):
    """IEEE float division (no ZeroDivisionError)."""
    if b == 0:
        return math.nan if a == 0 or a != a else math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _word_parts(value):
    """gcode.c:132-133 ``int_value = trunc(value); mantissa = round(100*(value - int_value))``.

    A word value that overflowed binary32 to infinity saturates both conversions the way AVR libgcc's
    float-to-unsigned conversion does (all bits set) instead of raising."""
    if math.isinf(value) and value > 0:
        return 0xFF, 0xFFFF
    iv = (int(value) & 0xFF) if value >= 0 else 0
    return iv, c_round(f32(100 * f32(value - iv))) & 0xFFFF


def _print_float(n, dp):
    """Source: print.c:printFloat @ bfb67f0 (lines 133-168)."""
    out = ""
    if n < 0:
        out, n = "-", -n
    d = dp
    while d >= 2:
        n = f32(n * 100)
        d -= 2
    if d:
        n = f32(n * 10)
    n = f32(n + 0.5)
    a = 0 if n != n else (int(n) if n < 4294967296.0 else 0xFFFFFFFF)
    s = str(a)
    if dp:
        s = s.rjust(dp + 1, "0")
        s = s[:-dp] + "." + s[-dp:]
    return out + s


_FLOAT_CACHE = {}


def read_float(line, i):
    """Source: nuts_bolts.c:read_float @ bfb67f0 (lines 35-108).

    Returns ``(ok, value, next_index)``.  Eight significant digits, binary32 scaling by 0.01 / 0.1 / 10."""
    m = _NUM.match(line, i)
    ip, fp = m.group(2), m.group(4) or ""
    if not (ip or fp):
        return False, 0.0, i
    tok = m.group(0)
    v = _FLOAT_CACHE.get(tok)
    if v is None:
        digits = ip + fp
        intval = int(digits[:8])
        if len(ip) >= 8:
            exp = len(ip) - 8
        else:
            exp = -min(len(fp), 8 - len(ip))
        fval = f32(float(intval))
        if fval != 0:
            while exp <= -2:
                fval = f32(fval * f32(0.01))
                exp += 2
            if exp < 0:
                fval = f32(fval * f32(0.1))
            elif exp > 0:
                while True:
                    fval = f32(fval * 10.0)
                    exp -= 1
                    if exp <= 0:
                        break
        v = -fval if m.group(1) == "-" else fval
        if len(_FLOAT_CACHE) > 300000:
            _FLOAT_CACHE.clear()
        _FLOAT_CACHE[tok] = v
    return True, v, m.end()


class _Settings:
    """Source: settings.c:defaults @ bfb67f0 (lines 26-61), defaults.h DEFAULTS_GENERIC (lines 31-65)."""

    def __init__(self, n_axis=STOCK_N_AXIS):
        self.n_axis = n_axis
        self.restore_defaults()

    def restore_defaults(self):
        n = self.n_axis
        self.steps_per_mm = [250.0] * n
        self.max_rate = [500.0] * n
        self.acceleration = [f32(10.0 * 60 * 60)] * n
        self.max_travel = [-200.0] * n
        self.pulse_microseconds = 10
        self.step_invert_mask = 0
        self.dir_invert_mask = 0
        self.stepper_idle_lock_time = 25
        self.status_report_mask = 1
        self.junction_deviation = f32(0.01)
        self.arc_tolerance = f32(0.002)
        self.rpm_max = 1000.0
        self.rpm_min = 0.0
        self.flags = 0
        self.homing_dir_mask = 0
        self.homing_feed_rate = 25.0
        self.homing_seek_rate = 500.0
        self.homing_debounce_delay = 250
        self.homing_pulloff = 1.0


class _Sys:
    """Source: system.h:system_t @ bfb67f0 (lines 126-143)."""

    def __init__(self):
        self.state = STATE_IDLE
        self.abort = False
        self.suspend = 0
        self.soft_limit = False
        self.step_control = 0
        self.probe_succeeded = False
        self.f_override = 100
        self.r_override = 100
        self.spindle_speed_ovr = 100
        self.spindle_stop_ovr = 0
        self.spindle_speed = 0.0
        self.report_ovr_counter = 0
        self.report_wco_counter = 0


class _Modal:
    __slots__ = ("motion", "feed_rate", "units", "distance", "plane_select", "tool_length", "coord_select",
                 "program_flow", "coolant", "spindle")

    def __init__(self):
        self.motion = self.feed_rate = self.units = self.distance = self.plane_select = 0
        self.tool_length = self.coord_select = self.program_flow = self.coolant = self.spindle = 0

    def copy(self):
        m = _Modal.__new__(_Modal)
        m.motion, m.feed_rate, m.units, m.distance = self.motion, self.feed_rate, self.units, self.distance
        m.plane_select, m.tool_length, m.coord_select = self.plane_select, self.tool_length, self.coord_select
        m.program_flow, m.coolant, m.spindle = self.program_flow, self.coolant, self.spindle
        return m


class _GC:
    def __init__(self, n_axis=STOCK_N_AXIS):
        self.modal = _Modal()
        self.spindle_speed = 0.0
        self.feed_rate = 0.0
        self.tool = 0
        self.line_number = 0
        self.position = [0.0] * n_axis
        self.coord_system = [0.0] * n_axis
        self.coord_offset = [0.0] * n_axis
        self.tool_length_offset = 0.0


class _Block:
    """gcode.h:parser_block_t."""
    __slots__ = ("nmc", "modal", "f", "ijk", "l", "n", "p", "r", "s", "t", "xyz")

    def __init__(self, n_axis=STOCK_N_AXIS):
        self.nmc = 0
        self.modal = None
        self.f = self.p = self.r = self.s = 0.0
        self.l = self.n = self.t = 0
        self.ijk = [0.0] * max(3, n_axis)
        self.xyz = [0.0] * n_axis


class _PL:
    """planner.h:plan_line_data_t."""
    __slots__ = ("condition", "feed_rate", "spindle_speed")

    def __init__(self):
        self.condition = 0
        self.feed_rate = self.spindle_speed = 0.0


class _Park:
    """Locals of protocol_exec_rt_suspend that outlive one pass of its loop (restore_target, parking_target,
    retract_waypoint and the system-motion plan_line_data_t)."""

    def __init__(self):
        self.restore = None
        self.target = None
        self.waypoint = None
        self.pl = _PL()
        self.pl.condition = PL_COND_SYSTEM | PL_COND_NO_FEED_OVR


class _Blk:
    """planner.h:plan_block_t plus execution progress for the linear motion model."""
    __slots__ = ("start", "end", "steps", "mm", "accel", "rapid_rate", "programmed_rate", "condition",
                 "spindle_speed", "p", "trip", "pwm_adj", "inv_rate", "loaded", "applied")


class _GCX:
    """Per-line parse results handed from the check phase to the execute phase of gc_execute_line."""
    __slots__ = ("flags", "axis_command", "axis_words", "command_words", "coord_select", "block_coord_system",
                 "axis_0", "axis_1", "axis_linear")


class GrblVirtual:
    """A virtual gnea/grbl 1.1h controller.  See the module docstring for scope and deviations."""

    _m4_supported = True            # gnea: M4 is always parsed (variable spindle build)
    _coolant_m_words = (8, 9)       # gnea: M7 is not compiled in (ENABLE_M7 undefined) -> error:20

    def _coolant_for_m(self, iv):
        """Source: gcode.c:gc_execute_line @ bfb67f0 (lines 263-283): M8 floods, M9 stops."""
        return PL_COND_COOLANT_FLOOD if iv == 8 else 0

    # ------------------------------------------------------------------ construction / public API
    def __init__(self, axes="XYZ", clock=None, instant=False, parking=False, homing=False):
        """Power up: ``main.c:main`` @ bfb67f0 (lines 39-109).  Prints the banner.  ``parking`` builds with
        ``PARKING_ENABLE`` (config.h:567); ``homing`` powers up with ``$22=1`` (and so in Alarm, HOMING_INIT_LOCK)."""
        self.parking_build = bool(parking)
        self.sys_block = None           # the parking system motion, outside the planner (motion_control.c:321-345)
        self._init_axes(axes)
        self._clock = clock or _time.monotonic
        self.instant = bool(instant)
        self.time = 0.0
        self._last_clock = self._clock()
        self._susp_accum = 0.0
        self.settings = self._make_settings()
        if homing:
            self.settings.flags |= BIT_HOMING_ENABLE
        self.eeprom_coord = [[0.0] * self.n_axis for _ in range(SETTING_INDEX_NCOORD + 1)]
        self.eeprom_startup = ["", ""]
        self.eeprom_build_info = ""
        self.probe_trigger = None       # {'X'|'Y'|'Z': machine-coordinate of the virtual contact plane}
        self.probe_tripped = False      # force the probe pin active (ALARM:4 on a G38.2 start)
        self.limit_pins = 0             # bitmask X=1 Y=2 Z=4 reported as Pn: letters
        self.sys_position = [0] * self.n_axis
        self.sys_probe_position = [0] * self.n_axis
        self.sys_probe_state = 0
        self.pl_pos = [0] * self.n_axis
        self.blocks = []
        self._out = bytearray()
        self._rx = bytearray()
        self._lbuf = bytearray()
        self._lflags = 0
        self.rt_state = self.rt_alarm = self.rt_motion_ovr = self.rt_acc_ovr = 0
        self.critical = False
        self.decel = None
        self.cur_speed = 0.0
        self.pwm_on = False
        self.spindle_ccw = False
        self.coolant_on = False
        self.pwm_gradient = 0.0
        self.sys = _Sys()
        self.gc = _GC(self.n_axis)
        self.sys.state = STATE_ALARM if self.settings.flags & BIT_HOMING_ENABLE else STATE_IDLE
        self._main = self._main_gen()
        self._pumping = False
        self._pump()

    def _make_settings(self):
        """Settings store for this build (subclasses supply their own defaults).

        Source: settings.c:settings_init @ bfb67f0 (lines 311-325)."""
        return _Settings(self.n_axis)

    def _init_axes(self, axes):
        """Axis set of this build.  Stock gnea/grbl: N_AXIS 3 (``nuts_bolts.h:31``), letters X Y Z.

        Source: nuts_bolts.h:N_AXIS @ bfb67f0 (lines 31-36)."""
        if axes != "XYZ":
            raise ValueError("gnea/grbl 1.1h is built with N_AXIS=3 (config default); axes must be 'XYZ'")
        self.axis_letters = axes
        self.n_axis = len(axes)

    @property
    def probe_trigger_z(self):
        return None if not self.probe_trigger else self.probe_trigger.get("Z")

    @probe_trigger_z.setter
    def probe_trigger_z(self, value):
        d = dict(self.probe_trigger or {})
        if value is None:
            d.pop("Z", None)
        else:
            d["Z"] = float(value)
        self.probe_trigger = d or None

    def feed(self, data):
        """Serial RX: realtime bytes are picked off exactly as the RX ISR does, everything else enters the
        128-byte RX ring and is consumed by the main loop.

        Source: serial.c:ISR(SERIAL_RX) @ bfb67f0 (lines 143-198)."""
        data = bytes(data)
        if not _REALTIME.search(data):
            self._rx_put(data)
            self._pump()
            return
        buf = bytearray()
        for c in data:
            if c in (0x18, 0x3F, 0x21, 0x7E) or c > 0x7F:
                self._rx_put(bytes(buf))
                buf.clear()
                self._pump()
                self._isr_realtime(c)
                self._pump()
            else:
                buf.append(c)
        self._rx_put(bytes(buf))
        self._pump()

    def drain(self):
        """Return and clear the reply lines, each ending ``\\r\\n`` (the banner is ``\\r\\n`` then its text).

        Source: serial.c:serial_write @ bfb67f0 (lines 86-103) (TX ring stand-in; bytes split at CRLF)."""
        parts = bytes(self._out).split(b"\r\n")
        self._out = bytearray(parts[-1])
        return [p + b"\r\n" for p in parts[:-1]]

    def tick(self, dt=None):
        """Advance virtual time by ``dt`` seconds (default: injected-clock delta) and run the motion model.

        Source: stepper.c:st_prep_buffer @ bfb67f0 (lines 668-1082) (behavioural stand-in, see module docstring)."""
        if dt is None:
            now = self._clock()
            dt, self._last_clock = max(0.0, now - self._last_clock), now
        self.time += dt
        if self.sys.suspend:
            self._susp_accum += dt
        self._run_motion(dt)
        self._pump()

    def inject_alarm(self, code):
        """Raise an alarm as the hardware paths do (limit ISR / probe / homing), printing report_alarm_message.

        Source: system.c:system_set_exec_alarm @ bfb67f0 (lines 370-375); limits.c:ISR(LIMIT_INT_vect) (hard
        limit: mc_reset then alarm 1); limits.c:limits_soft_check (lines 411-430)."""
        if code in (EXEC_ALARM_HARD_LIMIT, EXEC_ALARM_SOFT_LIMIT):
            self._mc_reset()
        self.rt_alarm = code
        self._pump()

    def mpos(self):
        """Test helper: machine position in mm as the status report computes it."""
        return [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]

    # ------------------------------------------------------------------ serial / pump
    def _rx_put(self, buf):
        """128-byte RX ring: bytes arriving while it is full are dropped.

        Source: serial.c:ISR(SERIAL_RX) @ bfb67f0 (lines 187-197); serial_get_rx_buffer_available (lines 37-42)."""
        while buf:
            room = RX_BUFFER_SIZE - len(self._rx)
            if room <= 0:
                self._pump()
                room = RX_BUFFER_SIZE - len(self._rx)
                if room <= 0:
                    return  # ring full: the ISR drops the byte
            self._rx += buf[:room]
            buf = buf[room:]

    def _pump(self):
        """Run the main-loop generator until it needs input or time."""
        if self._pumping:
            return
        self._pumping = True
        try:
            next(self._main)
        finally:
            self._pumping = False

    def _isr_realtime(self, c):
        """Source: serial.c:ISR(SERIAL_RX) @ bfb67f0 (lines 143-198)."""
        s = self.sys
        if c == 0x18:
            self._mc_reset()
        elif c == 0x3F:
            self.rt_state |= EXEC_STATUS_REPORT
        elif c == 0x7E:
            self.rt_state |= EXEC_CYCLE_START
        elif c == 0x21:
            self.rt_state |= EXEC_FEED_HOLD
        elif c == 0x84:
            self.rt_state |= EXEC_SAFETY_DOOR
        elif c == 0x85:
            if s.state & STATE_JOG:
                self.rt_state |= EXEC_MOTION_CANCEL
        elif 0x90 <= c <= 0x94:
            self.rt_motion_ovr |= 1 << (c - 0x90)
        elif 0x95 <= c <= 0x97:
            self.rt_motion_ovr |= 1 << (5 + c - 0x95)
        elif 0x99 <= c <= 0x9E:
            self.rt_acc_ovr |= 1 << (c - 0x99)
        elif c == 0xA0:
            self.rt_acc_ovr |= EXEC_COOLANT_FLOOD_OVR_TOGGLE
        # 0xA1 (mist toggle) needs ENABLE_M7; any other extended byte is discarded.

    # ------------------------------------------------------------------ report.c
    def _w(self, text):
        """Queue reply bytes.  Source: serial.c:serial_write @ bfb67f0 (lines 86-103)."""
        self._out.extend(text.encode("latin-1"))

    def report_status_message(self, code):
        """Source: report.c:report_status_message @ bfb67f0 (lines 112-122)."""
        self._w("ok\r\n" if code == STATUS_OK else "error:%d\r\n" % code)

    def report_alarm_message(self, code):
        """Source: report.c:report_alarm_message @ bfb67f0 (lines 125-131); delay_ms(500) omitted."""
        self._w("ALARM:%d\r\n" % code)

    def report_feedback_message(self, code):
        """Source: report.c:report_feedback_message @ bfb67f0 (lines 138-166)."""
        self._w("[MSG:%s]\r\n" % _MESSAGES[code])

    def report_init_message(self):
        """Source: report.c:report_init_message @ bfb67f0 (lines 170-173)."""
        self._w("\r\nGrbl %s ['$' for help]\r\n" % VERSION)

    def report_grbl_help(self):
        """Source: report.c:report_grbl_help @ bfb67f0 (lines 176-178)."""
        self._w("[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]\r\n")

    def _coord(self, n):
        """Source: print.c:printFloat_CoordValue @ bfb67f0 (lines 175-181)."""
        if self.settings.flags & BIT_REPORT_INCHES:
            return _print_float(f32(n * INCH_PER_MM), 4)
        return _print_float(n, 3)

    def _rate(self, n):
        """Source: print.c:printFloat_RateValue @ bfb67f0 (lines 183-189)."""
        if self.settings.flags & BIT_REPORT_INCHES:
            return _print_float(f32(n * INCH_PER_MM), 1)
        return _print_float(n, 0)

    def _axis_values(self, v):
        """Source: report.c:report_util_axis_values @ bfb67f0 (lines 33-45)."""
        return ",".join(self._coord(x) for x in v)

    def report_grbl_settings(self):
        """Source: report.c:report_grbl_settings @ bfb67f0 (lines 183-225)."""
        st = self.settings
        f = st.flags
        b = lambda m: 1 if f & m else 0
        rows = [(0, st.pulse_microseconds), (1, st.stepper_idle_lock_time), (2, st.step_invert_mask),
                (3, st.dir_invert_mask), (4, b(BIT_INVERT_ST_ENABLE)), (5, b(BIT_INVERT_LIMIT_PINS)),
                (6, b(BIT_INVERT_PROBE_PIN)), (10, st.status_report_mask)]
        for n, v in rows:
            self._w("$%d=%d\r\n" % (n, v))
        self._w("$11=%s\r\n$12=%s\r\n$13=%d\r\n" % (_print_float(st.junction_deviation, 3),
                                                   _print_float(st.arc_tolerance, 3), b(BIT_REPORT_INCHES)))
        self._w("$20=%d\r\n$21=%d\r\n$22=%d\r\n$23=%d\r\n" % (b(BIT_SOFT_LIMIT), b(BIT_HARD_LIMIT),
                                                              b(BIT_HOMING_ENABLE), st.homing_dir_mask))
        self._w("$24=%s\r\n$25=%s\r\n$26=%d\r\n$27=%s\r\n" % (
            _print_float(st.homing_feed_rate, 3), _print_float(st.homing_seek_rate, 3),
            st.homing_debounce_delay, _print_float(st.homing_pulloff, 3)))
        self._w("$30=%s\r\n$31=%s\r\n$32=%d\r\n" % (_print_float(st.rpm_max, 0), _print_float(st.rpm_min, 0),
                                                    b(BIT_LASER_MODE)))
        cols = (st.steps_per_mm, st.max_rate, [f32(a / (60 * 60)) for a in st.acceleration],
                [-x for x in st.max_travel])
        for set_idx, col in enumerate(cols):
            for idx in range(self.n_axis):
                self._w("$%d=%s\r\n" % (100 + set_idx * 10 + idx, _print_float(col[idx], 3)))

    def report_probe_parameters(self):
        """Source: report.c:report_probe_parameters @ bfb67f0 (lines 231-241)."""
        pos = [self._mpos_axis(self.sys_probe_position, i) for i in range(self.n_axis)]
        self._w("[PRB:%s:%d]\r\n" % (self._axis_values(pos), 1 if self.sys.probe_succeeded else 0))

    def report_ngc_parameters(self):
        """Source: report.c:report_ngc_parameters @ bfb67f0 (lines 245-271)."""
        for sel in range(SETTING_INDEX_NCOORD + 1):
            name = {6: "28", 7: "30"}.get(sel, str(sel + 54))
            self._w("[G%s:%s]\r\n" % (name, self._axis_values(self.eeprom_coord[sel])))
        self._w("[G92:%s]\r\n" % self._axis_values(self.gc.coord_offset))
        self._w("[TLO:%s]\r\n" % self._coord(self.gc.tool_length_offset))
        self.report_probe_parameters()

    def report_gcode_modes(self):
        """Source: report.c:report_gcode_modes @ bfb67f0 (lines 275-349)."""
        m = self.gc.modal
        g = "38.%d" % (m.motion - 138) if m.motion >= MOTION_PROBE_TOWARD else str(m.motion)
        parts = ["G" + g, "G%d" % (m.coord_select + 54), "G%d" % (m.plane_select + 17), "G%d" % (21 - m.units),
                 "G%d" % (m.distance + 90), "G%d" % (94 - m.feed_rate)]
        if m.program_flow in (PROGRAM_FLOW_PAUSED,):
            parts.append("M0")
        elif m.program_flow in (2, 30):
            parts.append("M%d" % m.program_flow)
        parts.append("M3" if m.spindle == PL_COND_SPINDLE_CW else "M4" if m.spindle == PL_COND_SPINDLE_CCW else "M5")
        parts.append("M8" if m.coolant else "M9")
        parts.append("T%d" % self.gc.tool)
        parts.append("F" + self._rate(self.gc.feed_rate))
        parts.append("S" + _print_float(self.gc.spindle_speed, 0))
        self._w("[GC:%s]\r\n" % " ".join(parts))

    def report_startup_line(self, n, line):
        """Source: report.c:report_startup_line @ bfb67f0 (lines 352-359)."""
        self._w("$N%d=%s\r\n" % (n, line))

    def report_execute_startup_message(self, line, status):
        """Source: report.c:report_execute_startup_message @ bfb67f0 (lines 361-367)."""
        self._w(">%s:" % line)
        self.report_status_message(status)

    def report_build_info(self, line):
        """Source: report.c:report_build_info @ bfb67f0 (lines 370-449): OPT is ``V`` for this config.h, ``VP``
        with PARKING_ENABLE."""
        self._w("[VER:%s.%s:%s]\r\n" % (VERSION, BUILD, line))
        self._w("[OPT:V%s,%d,%d]\r\n" % ("P" if self.parking_build else "", BLOCK_BUFFER_SIZE - 1, RX_BUFFER_SIZE))

    def _state_text(self):
        """Machine-state field of the status report.

        Source: report.c:report_realtime_status @ bfb67f0 (lines 478-506)."""
        s = self.sys
        st = s.state
        if st == STATE_IDLE:
            return "Idle"
        if st == STATE_CYCLE:
            return "Run"
        if st == STATE_HOLD and not (s.suspend & SUSPEND_JOG_CANCEL):
            return "Hold:0" if s.suspend & SUSPEND_HOLD_COMPLETE else "Hold:1"
        if st in (STATE_HOLD, STATE_JOG):
            return "Jog"
        if st == STATE_HOMING:
            return "Home"
        if st == STATE_ALARM:
            return "Alarm"
        if st == STATE_CHECK_MODE:
            return "Check"
        if st == STATE_SAFETY_DOOR:
            if s.suspend & SUSPEND_INITIATE_RESTORE:
                return "Door:3"
            if s.suspend & SUSPEND_RETRACT_COMPLETE:
                return "Door:1" if s.suspend & SUSPEND_SAFETY_DOOR_AJAR else "Door:0"
            return "Door:2"
        return "Sleep"

    def report_realtime_status(self):
        """Source: report.c:report_realtime_status @ bfb67f0 (lines 466-654)."""
        s, st, gcs = self.sys, self.settings, self.gc
        pos = [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]
        out = "<" + self._state_text()
        mpos_mode = bool(st.status_report_mask & 1)
        wco = [0.0] * self.n_axis
        if not mpos_mode or s.report_wco_counter == 0:
            for i in range(self.n_axis):
                wco[i] = f32(gcs.coord_system[i] + gcs.coord_offset[i])
                if i == 2:
                    wco[i] = f32(wco[i] + gcs.tool_length_offset)
                if not mpos_mode:
                    pos[i] = f32(pos[i] - wco[i])
        out += ("|MPos:" if mpos_mode else "|WPos:") + self._axis_values(pos)
        if st.status_report_mask & 2:
            out += "|Bf:%d,%d" % (BLOCK_BUFFER_SIZE - 1 - len(self.blocks), RX_BUFFER_SIZE - len(self._rx))
        busy = s.state & (STATE_HOMING | STATE_CYCLE | STATE_HOLD | STATE_JOG | STATE_SAFETY_DOOR)
        rate = self.cur_speed if busy else 0.0
        out += "|FS:%s,%s" % (self._rate(rate), _print_float(s.spindle_speed, 0))
        probe = self._probe_get_state(self._probe_contact_now(), False)
        if probe or self.limit_pins:
            out += "|Pn:" + ("P" if probe else "") + "".join(c for i, c in enumerate(self.axis_letters)
                                                            if self.limit_pins & (1 << i))
        if s.report_wco_counter > 0:
            s.report_wco_counter -= 1
        else:
            s.report_wco_counter = 29 if busy else 9
            if s.report_ovr_counter == 0:
                s.report_ovr_counter = 1
            out += "|WCO:" + self._axis_values(wco)
        if s.report_ovr_counter > 0:
            s.report_ovr_counter -= 1
        else:
            s.report_ovr_counter = 19 if busy else 9
            out += "|Ov:%d,%d,%d" % (s.f_override, s.r_override, s.spindle_speed_ovr)
            sp, cl = self._spindle_get_state(), self.coolant_on
            if sp or cl:
                out += "|A:" + (("S" if sp == 1 else "C") if sp else "") + ("F" if cl else "")
        self._w(out + ">\r\n")

    # ------------------------------------------------------------------ main.c / protocol.c
    def _main_gen(self):
        """Source: main.c:main @ bfb67f0 (lines 39-109): the re-initialisation loop."""
        while True:
            prior = self.sys.state
            self.sys = s = _Sys()
            s.state = prior
            self.rt_state = self.rt_alarm = self.rt_motion_ovr = self.rt_acc_ovr = 0
            self.sys_probe_position = [0] * self.n_axis
            self.sys_probe_state = 0
            self.critical = False
            self.decel = None
            self.sys_block = None
            self.cur_speed = 0.0
            self._rx.clear()
            self._lbuf.clear()
            self._lflags = 0
            self._gc_init()
            self._spindle_init()
            self.coolant_on = False
            self.blocks = []
            self._plan_sync_position()
            self._gc_sync_position()
            self.report_init_message()
            yield from self._protocol_main_loop()

    def _protocol_main_loop(self):
        """Source: protocol.c:protocol_main_loop @ bfb67f0 (lines 38-164)."""
        s = self.sys
        if self.settings.flags & BIT_HARD_LIMIT and self.limit_pins:
            s.state = STATE_ALARM
            self.report_feedback_message(MESSAGE_CHECK_LIMITS)
        if s.state & (STATE_ALARM | STATE_SLEEP):
            self.report_feedback_message(MESSAGE_ALARM_LOCK)
            s.state = STATE_ALARM
        else:
            s.state = STATE_IDLE
            yield from self._system_execute_startup()
        rx = self._rx
        while True:
            while rx:
                m = _EOL.search(rx)
                if m is None:
                    self._assemble(bytes(rx))
                    rx.clear()
                    break
                seg = bytes(rx[:m.start()])
                del rx[:m.end()]
                self._assemble(seg)
                self._rt_system()
                if self.critical or s.suspend:
                    yield from self._rt_block()
                if s.abort:
                    return
                flags, line = self._lflags, self._lbuf.decode("latin-1")
                self._lbuf.clear()
                self._lflags = 0
                if flags & _LF_OVERFLOW:
                    status = STATUS_OVERFLOW
                elif not line:
                    status = STATUS_OK
                elif line[0] == "$":
                    status = yield from self._system_execute_line(line)
                elif s.state & (STATE_ALARM | STATE_JOG):
                    status = STATUS_SYSTEM_GC_LOCK
                else:
                    status = self._gc_check(line)
                    if status is None:
                        status = yield from self._gc_run()
                self.report_status_message(status)
            self._auto_cycle_start()
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort:
                return
            yield

    def _assemble(self, seg):
        """Line assembly: comment strip, whitespace removal, upper-casing, 80-char overflow.

        Source: protocol.c:protocol_main_loop @ bfb67f0 (lines 96-146)."""
        if not self._lflags and not self._lbuf and not _SPECIAL.search(seg):
            clean = seg.translate(_UPPER, _DEL)
            if len(clean) > LINE_BUFFER_SIZE - 1:
                self._lflags |= _LF_OVERFLOW
                clean = clean[:LINE_BUFFER_SIZE - 1]
            self._lbuf = bytearray(clean)
            return
        lb, fl = self._lbuf, self._lflags
        for c in seg:
            if fl:
                if c == 41 and fl & _LF_PAREN:
                    fl &= ~_LF_PAREN
            elif c <= 32 or c == 47:
                pass
            elif c == 40:
                fl |= _LF_PAREN
            elif c == 59:
                fl |= _LF_SEMI
            elif len(lb) >= LINE_BUFFER_SIZE - 1:
                fl |= _LF_OVERFLOW
            elif 97 <= c <= 122:
                lb.append(c - 32)
            else:
                lb.append(c)
        self._lflags = fl

    def _auto_cycle_start(self):
        """Source: protocol.c:protocol_auto_cycle_start @ bfb67f0 (lines 186-191)."""
        if self.blocks:
            self.rt_state |= EXEC_CYCLE_START

    def _rt_block(self):
        """Blocking tails of protocol_execute_realtime: critical-event loop and the suspend loop.

        Source: protocol.c:protocol_execute_realtime @ bfb67f0 (lines 205-209)."""
        while self.critical:
            yield
            self._rt_system()
        if self.sys.suspend:
            yield from self._rt_suspend()

    def _buffer_sync(self):
        """Source: protocol.c:protocol_buffer_synchronize @ bfb67f0 (lines 169-177)."""
        s = self.sys
        self._auto_cycle_start()
        while True:
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort:
                return
            if not (self.blocks or s.state == STATE_CYCLE):
                return
            yield

    def _rt_system(self):
        """Source: protocol.c:protocol_exec_rt_system @ bfb67f0 (lines 215-501)."""
        self._rt_system_once()
        if self.rt_state & EXEC_CYCLE_STOP and not self.sys.abort:
            self._rt_system_once()

    def _rt_system_once(self):
        """One pass of protocol_exec_rt_system.  Source: protocol.c:protocol_exec_rt_system @ bfb67f0 (lines 215-501)."""
        s = self.sys
        if self.critical:
            if not self.rt_state & EXEC_RESET:
                return
            self.critical = False       # reset arrived: leave the critical-event loop
            self.rt_alarm = 0
        else:
            ra = self.rt_alarm
            if ra:
                s.state = STATE_ALARM
                self.report_alarm_message(ra)
                if ra in (EXEC_ALARM_HARD_LIMIT, EXEC_ALARM_SOFT_LIMIT):
                    self.report_feedback_message(MESSAGE_CRITICAL_EVENT)
                    self.rt_state &= ~EXEC_RESET
                    self.critical = True
                    return
                self.rt_alarm = 0
        rt = self.rt_state
        if rt:
            if rt & EXEC_RESET:
                s.abort = True
                return
            if rt & EXEC_STATUS_REPORT:
                self.report_realtime_status()
                self.rt_state &= ~EXEC_STATUS_REPORT
            if rt & (EXEC_MOTION_CANCEL | EXEC_FEED_HOLD | EXEC_SAFETY_DOOR | EXEC_SLEEP):
                if not s.state & (STATE_ALARM | STATE_CHECK_MODE):
                    if s.state & (STATE_CYCLE | STATE_JOG):
                        if not s.suspend & (SUSPEND_MOTION_CANCEL | SUSPEND_JOG_CANCEL):
                            self._begin_decel()
                            s.step_control = STEP_CONTROL_EXECUTE_HOLD
                            if s.state == STATE_JOG and not rt & EXEC_SLEEP:
                                s.suspend |= SUSPEND_JOG_CANCEL
                    if s.state == STATE_IDLE:
                        s.suspend = SUSPEND_HOLD_COMPLETE
                    if rt & EXEC_MOTION_CANCEL and not s.state & STATE_JOG:
                        s.suspend |= SUSPEND_MOTION_CANCEL
                    if rt & EXEC_FEED_HOLD and not s.state & (STATE_SAFETY_DOOR | STATE_JOG | STATE_SLEEP):
                        s.state = STATE_HOLD
                    if rt & EXEC_SAFETY_DOOR:
                        self.report_feedback_message(MESSAGE_SAFETY_DOOR_AJAR)
                        if not s.suspend & SUSPEND_JOG_CANCEL:
                            if s.state == STATE_SAFETY_DOOR and s.suspend & SUSPEND_INITIATE_RESTORE:
                                s.suspend &= ~(SUSPEND_RETRACT_COMPLETE | SUSPEND_INITIATE_RESTORE
                                               | SUSPEND_RESTORE_COMPLETE)
                                s.suspend |= SUSPEND_RESTART_RETRACT
                            if s.state != STATE_SLEEP:
                                s.state = STATE_SAFETY_DOOR
                        s.suspend |= SUSPEND_SAFETY_DOOR_AJAR
                if rt & EXEC_SLEEP:
                    if s.state == STATE_ALARM:
                        s.suspend |= SUSPEND_RETRACT_COMPLETE | SUSPEND_HOLD_COMPLETE
                    s.state = STATE_SLEEP
                self.rt_state &= ~(EXEC_MOTION_CANCEL | EXEC_FEED_HOLD | EXEC_SAFETY_DOOR | EXEC_SLEEP)
            if rt & EXEC_CYCLE_START:
                if not rt & (EXEC_FEED_HOLD | EXEC_MOTION_CANCEL | EXEC_SAFETY_DOOR):
                    if s.state == STATE_SAFETY_DOOR and not s.suspend & SUSPEND_SAFETY_DOOR_AJAR:
                        if s.suspend & SUSPEND_RESTORE_COMPLETE:
                            s.state = STATE_IDLE
                        elif s.suspend & SUSPEND_RETRACT_COMPLETE:
                            s.suspend |= SUSPEND_INITIATE_RESTORE
                    if s.state == STATE_IDLE or (s.state & STATE_HOLD and s.suspend & SUSPEND_HOLD_COMPLETE):
                        if s.state == STATE_HOLD and s.spindle_stop_ovr:
                            s.spindle_stop_ovr |= SPINDLE_STOP_OVR_RESTORE_CYCLE
                        else:
                            s.step_control = 0
                            if self.blocks and not s.suspend & SUSPEND_MOTION_CANCEL:
                                s.suspend = 0
                                s.state = STATE_CYCLE
                            else:
                                s.suspend = 0
                                s.state = STATE_IDLE
                self.rt_state &= ~EXEC_CYCLE_START
            if rt & EXEC_CYCLE_STOP:
                if s.state & (STATE_HOLD | STATE_SAFETY_DOOR | STATE_SLEEP) and not s.soft_limit \
                        and not s.suspend & SUSPEND_JOG_CANCEL:
                    if s.step_control & STEP_CONTROL_EXECUTE_HOLD:
                        s.suspend |= SUSPEND_HOLD_COMPLETE
                    s.step_control &= ~(STEP_CONTROL_EXECUTE_HOLD | STEP_CONTROL_EXECUTE_SYS_MOTION)
                else:
                    if s.suspend & SUSPEND_JOG_CANCEL:
                        s.step_control = 0
                        self.blocks = []
                        self._gc_sync_position()
                        self._plan_sync_position()
                    if s.suspend & SUSPEND_SAFETY_DOOR_AJAR:
                        s.suspend &= ~SUSPEND_JOG_CANCEL
                        s.suspend |= SUSPEND_HOLD_COMPLETE
                        s.state = STATE_SAFETY_DOOR
                    else:
                        s.suspend = 0
                        s.state = STATE_IDLE
                self.rt_state &= ~EXEC_CYCLE_STOP
        ro = self.rt_motion_ovr
        if ro:
            self.rt_motion_ovr = 0
            f = s.f_override
            if ro & EXEC_FEED_OVR_RESET:
                f = 100
            if ro & EXEC_FEED_OVR_COARSE_PLUS:
                f += 10
            if ro & EXEC_FEED_OVR_COARSE_MINUS:
                f -= 10
            if ro & EXEC_FEED_OVR_FINE_PLUS:
                f += 1
            if ro & EXEC_FEED_OVR_FINE_MINUS:
                f -= 1
            f = max(min(f, 200), 10)
            r = s.r_override
            if ro & EXEC_RAPID_OVR_RESET:
                r = 100
            if ro & EXEC_RAPID_OVR_MEDIUM:
                r = 50
            if ro & EXEC_RAPID_OVR_LOW:
                r = 25
            if f != s.f_override or r != s.r_override:
                s.f_override, s.r_override = f, r
                s.report_ovr_counter = 0
        ra = self.rt_acc_ovr
        if ra:
            self.rt_acc_ovr = 0
            sp = s.spindle_speed_ovr
            if ra & EXEC_SPINDLE_OVR_RESET:
                sp = 100
            if ra & EXEC_SPINDLE_OVR_COARSE_PLUS:
                sp += 10
            if ra & EXEC_SPINDLE_OVR_COARSE_MINUS:
                sp -= 10
            if ra & EXEC_SPINDLE_OVR_FINE_PLUS:
                sp += 1
            if ra & EXEC_SPINDLE_OVR_FINE_MINUS:
                sp -= 1
            sp = max(min(sp, 200), 10)
            if sp != s.spindle_speed_ovr:
                s.spindle_speed_ovr = sp
                if s.state == STATE_IDLE:
                    self._spindle_set_state(self.gc.modal.spindle, self.gc.spindle_speed)
                else:
                    s.step_control |= STEP_CONTROL_UPDATE_SPINDLE_PWM
                s.report_ovr_counter = 0
            if ra & EXEC_SPINDLE_OVR_STOP and s.state == STATE_HOLD:
                if not s.spindle_stop_ovr:
                    s.spindle_stop_ovr = SPINDLE_STOP_OVR_INITIATE
                elif s.spindle_stop_ovr & SPINDLE_STOP_OVR_ENABLED:
                    s.spindle_stop_ovr |= SPINDLE_STOP_OVR_RESTORE
            if ra & EXEC_COOLANT_FLOOD_OVR_TOGGLE:
                if s.state == STATE_IDLE or s.state & (STATE_CYCLE | STATE_HOLD | STATE_JOG):
                    cs = 0 if self.gc.modal.coolant & PL_COND_COOLANT_FLOOD else PL_COND_COOLANT_FLOOD
                    self._coolant_set_state(cs)
                    self.gc.modal.coolant = cs

    # ------------------------------------------------------------------ parking (door / sleep suspend)
    def _park_params(self):
        """``(axis, target_mpos, rate, pullout_distance, pullout_rate)``, or None when the build has no parking code.

        Source: config.h:PARKING_AXIS / PARKING_TARGET / PARKING_RATE / PARKING_PULLOUT_RATE /
        PARKING_PULLOUT_INCREMENT @ bfb67f0 (lines 567-571); compile-time constants, the host cannot read them."""
        if not self.parking_build:
            return None
        return (PARKING_AXIS, PARKING_TARGET, PARKING_RATE, PARKING_PULLOUT_INCREMENT, PARKING_PULLOUT_RATE)

    def _park_can_park(self):
        """Parking needs homing enabled (``$22``) and laser mode off.

        Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (lines 581-590 and 649-654)."""
        return bool(self.settings.flags & BIT_HOMING_ENABLE) and not self.settings.flags & BIT_LASER_MODE

    def _door_spin_down(self):
        """Source: spindle_control.c:spindle_set_state(SPINDLE_DISABLE) @ bfb67f0 (lines 226-271)."""
        self._spindle_set_state(0, 0.0)
        return
        yield

    def _door_spindle_delay(self, rpm):
        """Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (line 678): SAFETY_DOOR_SPINDLE_DELAY (config.h:179)."""
        yield from self._delay_sec(SAFETY_DOOR_SPINDLE_DELAY, True)

    def _door_coolant_delay(self):
        """Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (line 687): SAFETY_DOOR_COOLANT_DELAY (config.h:180)."""
        yield from self._delay_sec(SAFETY_DOOR_COOLANT_DELAY, True)

    def _mc_parking_motion(self, target, pl):
        """Plan and run one system motion, independent of the main planner.

        Source: motion_control.c:mc_parking_motion @ bfb67f0 (lines 321-345); the motion itself is the linear
        model of ``_run_motion`` (no ramp), held in ``sys_block`` instead of the planner ring."""
        s = self.sys
        if s.abort:
            return
        if self._plan_buffer_line(target, pl):
            s.step_control |= STEP_CONTROL_EXECUTE_SYS_MOTION
            if self.instant:
                s.step_control &= ~STEP_CONTROL_EXECUTE_SYS_MOTION
                return
            while s.step_control & STEP_CONTROL_EXECUTE_SYS_MOTION:
                self._rt_system()
                if s.abort:
                    return
                if s.step_control & STEP_CONTROL_EXECUTE_SYS_MOTION:
                    yield
            self.sys_block = None
        else:
            s.step_control &= ~STEP_CONTROL_EXECUTE_SYS_MOTION
            self._rt_system()

    def _door_retract(self, park, rest_cond, rest_speed):
        """Handles retraction motions and de-energizing.

        Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (lines 555-626)."""
        s = self.sys
        prm = self._park_params()
        if prm is None:
            yield from self._door_spin_down()
            self._coolant_set_state(0)
            return
        axis, target, rate, pullout, pullout_rate = prm
        if park.waypoint is None:
            park.waypoint = pullout
        park.target = [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]
        if not s.suspend & SUSPEND_RESTART_RETRACT:
            park.restore = list(park.target)
            park.waypoint = min(park.waypoint + park.restore[axis], target)
        pl = park.pl
        if self._park_can_park() and park.target[axis] < target:
            if park.target[axis] < park.waypoint:
                park.target[axis] = park.waypoint
                pl.feed_rate = pullout_rate
                pl.condition |= rest_cond & PL_COND_ACCESSORY_MASK
                pl.spindle_speed = rest_speed
                yield from self._mc_parking_motion(park.target, pl)
            pl.condition = PL_COND_SYSTEM | PL_COND_NO_FEED_OVR
            pl.spindle_speed = 0.0
            yield from self._door_spin_down()
            self._coolant_set_state(0)
            if park.target[axis] < target:
                park.target[axis] = target
                pl.feed_rate = rate
                yield from self._mc_parking_motion(park.target, pl)
        else:
            yield from self._door_spin_down()
            self._coolant_set_state(0)

    def _door_restore(self, park, laser, rest_cond, rest_speed):
        """Handles parking restore and safety door resume.

        Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (lines 649-715)."""
        s, gcs = self.sys, self.gc
        prm = self._park_params()
        can = prm is not None and self._park_can_park()
        pl = park.pl
        if can:
            axis, target, rate, pullout, pullout_rate = prm
            if park.target[axis] <= target:
                park.target[axis] = park.waypoint
                pl.feed_rate = rate
                yield from self._mc_parking_motion(park.target, pl)
        if gcs.modal.spindle != 0 and not s.suspend & SUSPEND_RESTART_RETRACT:
            if laser:
                s.step_control |= STEP_CONTROL_UPDATE_SPINDLE_PWM
            else:
                self._spindle_set_state(rest_cond & PL_COND_SPINDLE_MASK, rest_speed)
                yield from self._door_spindle_delay(rest_speed)
        if gcs.modal.coolant != 0 and not s.suspend & SUSPEND_RESTART_RETRACT:
            self._coolant_set_state(rest_cond & PL_COND_COOLANT_FLOOD)
            yield from self._door_coolant_delay()
        if can and not s.suspend & SUSPEND_RESTART_RETRACT:
            pl.feed_rate = pullout_rate
            pl.condition |= rest_cond & PL_COND_ACCESSORY_MASK
            pl.spindle_speed = rest_speed
            yield from self._mc_parking_motion(park.restore, pl)
        if not s.suspend & SUSPEND_RESTART_RETRACT:
            s.suspend |= SUSPEND_RESTORE_COMPLETE
            self.rt_state |= EXEC_CYCLE_START

    def _rt_suspend(self):
        """Source: protocol.c:protocol_exec_rt_suspend @ bfb67f0 (lines 509-765); the parking branches only run
        in a PARKING_ENABLE build (``parking_build``)."""
        s, gcs = self.sys, self.gc
        laser = bool(self.settings.flags & BIT_LASER_MODE)
        park = _Park()
        if self.blocks:
            blk = self.blocks[0]
            rest_cond = (blk.condition & PL_COND_SPINDLE_MASK) | (PL_COND_COOLANT_FLOOD if self.coolant_on else 0)
            rest_speed = blk.spindle_speed
        else:
            rest_cond = gcs.modal.spindle | gcs.modal.coolant
            rest_speed = gcs.spindle_speed
        if laser:
            self.rt_acc_ovr |= EXEC_SPINDLE_OVR_STOP   # DISABLE_LASER_DURING_HOLD
        while s.suspend:
            if s.abort:
                return
            key = (s.suspend, s.state, s.step_control, s.spindle_stop_ovr)
            if s.suspend & SUSPEND_HOLD_COMPLETE:
                if s.state & (STATE_SAFETY_DOOR | STATE_SLEEP):
                    if not s.suspend & SUSPEND_RETRACT_COMPLETE:
                        s.spindle_stop_ovr = 0
                        yield from self._door_retract(park, rest_cond, rest_speed)
                        s.suspend &= ~SUSPEND_RESTART_RETRACT
                        s.suspend |= SUSPEND_RETRACT_COMPLETE
                    else:
                        if s.state == STATE_SLEEP:
                            self.report_feedback_message(MESSAGE_SLEEP_MODE)
                            self._spindle_set_state(0, 0.0)
                            self._coolant_set_state(0)
                            while not s.abort:
                                self._rt_system()
                                yield
                            return
                        if s.state == STATE_SAFETY_DOOR:
                            s.suspend &= ~SUSPEND_SAFETY_DOOR_AJAR   # no door pin: never ajar
                        if s.suspend & SUSPEND_INITIATE_RESTORE:
                            yield from self._door_restore(park, laser, rest_cond, rest_speed)
                elif s.spindle_stop_ovr:
                    if s.spindle_stop_ovr & SPINDLE_STOP_OVR_INITIATE:
                        if gcs.modal.spindle != 0:
                            self._spindle_set_state(0, 0.0)
                            s.spindle_stop_ovr = SPINDLE_STOP_OVR_ENABLED
                        else:
                            s.spindle_stop_ovr = 0
                    elif s.spindle_stop_ovr & (SPINDLE_STOP_OVR_RESTORE | SPINDLE_STOP_OVR_RESTORE_CYCLE):
                        if gcs.modal.spindle != 0:
                            self.report_feedback_message(MESSAGE_SPINDLE_RESTORE)
                            if laser:
                                s.step_control |= STEP_CONTROL_UPDATE_SPINDLE_PWM
                            else:
                                self._spindle_set_state(rest_cond & PL_COND_SPINDLE_MASK, rest_speed)
                        if s.spindle_stop_ovr & SPINDLE_STOP_OVR_RESTORE_CYCLE:
                            self.rt_state |= EXEC_CYCLE_START
                        s.spindle_stop_ovr = 0
                elif s.step_control & STEP_CONTROL_UPDATE_SPINDLE_PWM:
                    self._spindle_set_state(rest_cond & PL_COND_SPINDLE_MASK, rest_speed)
                    s.step_control &= ~STEP_CONTROL_UPDATE_SPINDLE_PWM
            self._rt_system()
            if self.critical:
                yield from self._rt_block()
            if s.suspend and key == (s.suspend, s.state, s.step_control, s.spindle_stop_ovr):
                yield

    def _etime(self):
        """Virtual time with suspended intervals removed (delay_sec does not count time spent in the suspend loop).

        Source: nuts_bolts.c:delay_sec @ bfb67f0 (lines 112-126)."""
        return self.time - self._susp_accum

    def _delay_sec(self, seconds, suspend_mode=False):
        """Non-blocking delay in DWELL_TIME_STEP (50 ms) slices; time spent suspended does not count.

        Source: nuts_bolts.c:delay_sec @ bfb67f0 (lines 112-126)."""
        s = self.sys
        ticks = f32(20 * seconds)                       # 1000/DWELL_TIME_STEP is the int 20; * float is binary32
        n = c_fix_u32(math.ceil(ticks) if math.isfinite(ticks) else ticks) & 0xFFFF     # uint16_t i = ceil(...)
        base = None
        while n > 0:
            n -= 1
            if s.abort:
                return
            self._rt_system()
            if suspend_mode:
                if s.suspend & SUSPEND_RESTART_RETRACT:
                    return
            elif self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort or self.instant:
                continue
            now = (lambda: self.time) if suspend_mode else self._etime
            if base is None:
                base = now()
            base += 0.05           # slices accumulate so one large tick() can finish several of them
            while now() < base - 1e-12:
                yield
                self._rt_system()
                if not suspend_mode and (self.critical or s.suspend):
                    yield from self._rt_block()
                if s.abort:
                    return

    # ------------------------------------------------------------------ mc_* / planner / motion
    def _mc_reset(self):
        """Source: motion_control.c:mc_reset @ bfb67f0 (lines 366-388)."""
        s = self.sys
        if not self.rt_state & EXEC_RESET:
            self.rt_state |= EXEC_RESET
            self._spindle_stop()
            self.coolant_on = False
            if s.state & (STATE_CYCLE | STATE_HOMING | STATE_JOG) or \
                    s.step_control & (STEP_CONTROL_EXECUTE_HOLD | STEP_CONTROL_EXECUTE_SYS_MOTION):
                if s.state == STATE_HOMING:
                    if not self.rt_alarm:
                        self.rt_alarm = EXEC_ALARM_HOMING_FAIL_RESET
                else:
                    self.rt_alarm = EXEC_ALARM_ABORT_CYCLE

    def _mpos_axis(self, steps, i):
        """Source: system.c:system_convert_axis_steps_to_mpos @ bfb67f0 (lines 292-307)."""
        return f32(_fdiv(steps[i], self.settings.steps_per_mm[i]))

    def _gc_sync_position(self):
        """Source: gcode.c:gc_sync_position @ bfb67f0 (lines 55-58)."""
        self.gc.position = [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]

    def _plan_sync_position(self):
        """Source: planner.c:plan_sync_position @ bfb67f0 (lines 476-494)."""
        self.pl_pos = list(self.sys_position)

    def _check_travel_limits(self, target):
        """Source: system.c:system_check_travel_limits @ bfb67f0 (lines 334-352)."""
        mt = self.settings.max_travel
        return any(target[i] > 0 or target[i] < mt[i] for i in range(self.n_axis))

    def _limits_soft_check(self, target):
        """Source: limits.c:limits_soft_check @ bfb67f0 (lines 411-430)."""
        s = self.sys
        if self._check_travel_limits(target):
            s.soft_limit = True
            if s.state == STATE_CYCLE:
                self.rt_state |= EXEC_FEED_HOLD
                while True:
                    self._rt_system()
                    if self.critical or s.suspend:
                        yield from self._rt_block()
                    if s.abort:
                        return
                    if s.state == STATE_IDLE:
                        break
                    yield
            self._mc_reset()
            self.rt_alarm = EXEC_ALARM_SOFT_LIMIT
            self._rt_system()
            if self.critical:
                yield from self._rt_block()

    def _mc_line(self, target, pl, trip=None, mm=None):
        """Source: motion_control.c:mc_line @ bfb67f0 (lines 32-77)."""
        s = self.sys
        if self.settings.flags & BIT_SOFT_LIMIT and s.state != STATE_JOG:
            yield from self._limits_soft_check(target)
        if s.state == STATE_CHECK_MODE:
            return
        while True:
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort:
                return
            if len(self.blocks) >= BLOCK_BUFFER_SIZE - 1:
                self._auto_cycle_start()
                yield
            else:
                break
        if not self._plan_buffer_line(target, pl, trip, mm) and self.settings.flags & BIT_LASER_MODE \
                and pl.condition & PL_COND_SPINDLE_CW:
            yield from self._spindle_sync(PL_COND_SPINDLE_CW, pl.spindle_speed)

    def _plan_buffer_line(self, target, pl, trip=None, mm=None):
        """Returns False for PLAN_EMPTY_BLOCK.

        Source: planner.c:plan_buffer_line @ bfb67f0 (lines 315-472)."""
        st = self.settings
        spm = st.steps_per_mm
        start = list(self.sys_position if pl.condition & PL_COND_SYSTEM else self.pl_pos)
        end = [c_round(f32(target[i] * spm[i])) for i in range(self.n_axis)]
        steps = [abs(end[i] - start[i]) for i in range(self.n_axis)]
        if max(steps) == 0 and mm is None:
            return False
        d = [f32(_fdiv(end[i] - start[i], spm[i])) for i in range(self.n_axis)]
        mag = math.sqrt(sum(x * x for x in d if x != 0.0))
        unit = [abs(x / mag) if mag else 0.0 for x in d]
        b = _Blk()
        b.start, b.end, b.steps = start, end, steps
        b.mm = mm if mm is not None else f32(mag)
        b.accel = min((abs(st.acceleration[i] / unit[i]) for i in range(self.n_axis) if unit[i]), default=1.0e38)
        b.rapid_rate = min((abs(st.max_rate[i] / unit[i]) for i in range(self.n_axis) if unit[i]), default=1.0e38)
        b.condition = pl.condition
        if pl.condition & PL_COND_RAPID:
            b.programmed_rate = b.rapid_rate
        else:
            b.programmed_rate = pl.feed_rate
            if pl.condition & PL_COND_INVERSE_TIME:
                b.programmed_rate = f32(b.programmed_rate * b.mm)
        b.spindle_speed = pl.spindle_speed
        b.p, b.trip = 0.0, trip
        b.loaded = False
        b.applied = [0] * self.n_axis
        b.pwm_adj = bool(st.flags & BIT_LASER_MODE and pl.condition & PL_COND_SPINDLE_CCW)
        b.inv_rate = 1.0 / b.programmed_rate if b.programmed_rate else 0.0
        if not pl.condition & PL_COND_SYSTEM:
            self.pl_pos = list(end)
        if self.instant:
            self._finish_instant(b)
        elif pl.condition & PL_COND_SYSTEM:
            self.sys_block = b          # a system motion bypasses the planner ring (motion_control.c:321-345)
        else:
            self.blocks.append(b)
        return True

    def _finish_instant(self, b):
        """``instant=True``: the block completes the moment it is planned (probe trips on the way).

        Source: stepper.c:ISR(TIMER1_COMPA_vect) @ bfb67f0 (lines 300-510) (completion stand-in)."""
        end = b.end
        if b.trip is not None and self.sys_probe_state == 1:
            end = [b.start[i] + int((b.end[i] - b.start[i]) * b.trip) for i in range(self.n_axis)]
            self.sys_probe_state = 0
            self.sys_probe_position = list(end)
        self.sys_position = list(end)
        if b.pwm_adj:
            self.pwm_on = False

    def _nominal_speed(self, b):
        """Source: planner.c:plan_compute_profile_nominal_speed @ bfb67f0 (lines 259-269)."""
        s = self.sys
        v = b.programmed_rate
        if b.condition & PL_COND_RAPID:
            v *= 0.01 * s.r_override
        else:
            if not b.condition & PL_COND_NO_FEED_OVR:
                v *= 0.01 * s.f_override
            v = min(v, b.rapid_rate)
        return v if v > 1.0 else 1.0

    def _begin_decel(self):
        """Start the hold / cancel deceleration ramp.

        Source: stepper.c:st_update_plan_block_parameters @ bfb67f0 (lines 598-605); linear-model stand-in."""
        if self.instant or not self.blocks or self.cur_speed <= 0.0:
            self.decel = None
            self.rt_state |= EXEC_CYCLE_STOP
            return
        b = self.blocks[0]
        v = self.cur_speed
        t = 60.0 * v / b.accel
        remaining = b.mm * (1.0 - b.p)
        if v * (t / 60.0) / 2.0 > remaining:
            t = 60.0 * 2.0 * remaining / v
        if t <= 0.0:
            self.decel = None
            self.rt_state |= EXEC_CYCLE_STOP
        else:
            self.decel = [v, t, 0.0]

    def _advance_block(self, b, dist_mm):
        """Move ``dist_mm`` along the block; sys_position follows in whole steps.

        Source: stepper.c:ISR(TIMER1_COMPA_vect) @ bfb67f0 (lines 300-510) (Bresenham stand-in)."""
        b.p = min(1.0, b.p + dist_mm / b.mm)
        for i in range(self.n_axis):
            done = b.steps[i] if b.p >= 1.0 else int(b.p * b.steps[i] + 1e-9)
            if done != b.applied[i]:      # step deltas, not an absolute position: a parking motion may have moved the
                self.sys_position[i] += (1 if b.end[i] >= b.start[i] else -1) * (done - b.applied[i])   # axes meanwhile
                b.applied[i] = done

    def _finish_block(self, b):
        """Block complete: discard it, cycle-stop when the planner empties, laser PWM off for M4 rate-adjusted blocks.

        Source: planner.c:plan_discard_current_block @ bfb67f0 (lines 215-223); stepper.c lines 393-399."""
        b.p = 1.0
        self._advance_block(b, 0.0)
        self.blocks.pop(0)
        if b.pwm_adj:
            self.pwm_on = False          # stepper.c: spindle_set_speed(SPINDLE_PWM_OFF_VALUE) at motion end
        if not self.blocks:
            self.cur_speed = 0.0
            self.rt_state |= EXEC_CYCLE_STOP

    def _sys_motion_active(self):
        return self.sys_block is not None and bool(self.sys.step_control & STEP_CONTROL_EXECUTE_SYS_MOTION)

    def _run_motion(self, dt):
        """Linear constant-speed motion over ``dt`` seconds: the planner's blocks, or a parking system motion.

        Source: stepper.c:st_prep_buffer @ bfb67f0 (lines 668-1082) (behavioural stand-in, see module docstring)."""
        left = dt
        while left > 1e-12:
            if self._sys_motion_active():
                left = self._run_sys_motion(left)
                continue
            left = self._run_planner_motion(left)
            if not self._sys_motion_active():     # a hold that just completed may have started a parking motion
                break

    def _run_sys_motion(self, left):
        """The parking motion runs whatever the state, with the planner untouched; ends with a cycle stop.

        Source: stepper.c:st_prep_buffer @ bfb67f0 (lines 668-1082), STEP_CONTROL_EXECUTE_SYS_MOTION."""
        b = self.sys_block
        v = self._nominal_speed(b)
        need = b.mm * (1.0 - b.p) / v * 60.0
        if need <= left + 1e-12:
            left -= need
            b.p = 1.0
            self._advance_block(b, 0.0)
            self.sys_block = None
            self.rt_state |= EXEC_CYCLE_STOP
            self._pump()
            return left
        self._advance_block(b, v / 60.0 * left)
        return 0.0

    def _run_planner_motion(self, left):
        s = self.sys
        while left > 1e-12 and self.blocks and not self._sys_motion_active() and (s.state & (STATE_CYCLE | STATE_JOG)
                                                or s.step_control & STEP_CONTROL_EXECUTE_HOLD):
            b = self.blocks[0]
            if self.decel is not None:
                v0, t, e = self.decel
                step = min(left, t - e)
                e1 = e + step
                self._advance_block(b, v0 / 60.0 * (step - (e1 * e1 - e * e) / (2.0 * t)))
                self.cur_speed = v0 * (1.0 - e1 / t)
                left -= step
                self.decel[2] = e1
                if e1 >= t - 1e-12:
                    self.decel, self.cur_speed = None, 0.0
                    self.rt_state |= EXEC_CYCLE_STOP
                    if b.p >= 1.0:
                        self._finish_block(b)
                    self._pump()
                continue
            v = self._nominal_speed(b)
            self.cur_speed = v
            if not b.loaded:
                b.loaded = True
                s.step_control |= STEP_CONTROL_UPDATE_SPINDLE_PWM   # stepper.c:845: force update whenever a block loads
            if b.pwm_adj or s.step_control & STEP_CONTROL_UPDATE_SPINDLE_PWM:
                if b.condition & PL_COND_SPINDLE_MASK:
                    rpm = f32(b.spindle_speed * (v * b.inv_rate)) if b.pwm_adj else b.spindle_speed
                    self._spindle_set_speed(self._spindle_compute_pwm(rpm))
                else:
                    s.spindle_speed = 0.0
                    self._spindle_set_speed(0)
                s.step_control &= ~STEP_CONTROL_UPDATE_SPINDLE_PWM
            limit = b.trip if b.trip is not None and b.p < b.trip else 1.0
            need = b.mm * (limit - b.p) / v * 60.0
            if need <= left + 1e-12:
                left -= need
                b.p = limit
                if limit < 1.0:
                    b.trip = None
                    self._advance_block(b, 0.0)
                    self._probe_state_monitor()
                    self._pump()
                else:
                    self._finish_block(b)
                    self._pump()        # the main loop refills the planner while a long tick() is still running
            else:
                self._advance_block(b, v / 60.0 * left)
                left = 0.0
        return left

    def _mc_arc(self, target, pl, position, offset, radius, axis_0, axis_1, axis_linear, clockwise):
        """One planner block of true arc length.

        Source: motion_control.c:mc_arc @ bfb67f0 (lines 87-191) (segmentation not modelled)."""
        cx, cy = position[axis_0] + offset[axis_0], position[axis_1] + offset[axis_1]
        r0, r1 = -offset[axis_0], -offset[axis_1]
        rt0, rt1 = target[axis_0] - cx, target[axis_1] - cy
        ang = math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1)
        if clockwise:
            if ang >= -5e-7:
                ang -= 2 * math.pi
        elif ang <= 5e-7:
            ang += 2 * math.pi
        lin = target[axis_linear] - position[axis_linear] if axis_linear < self.n_axis else 0.0
        length = math.hypot(abs(ang) * radius, lin)
        yield from self._mc_line(target, pl, mm=f32(length) if length > 0 else None)

    def _mc_dwell(self, seconds):
        """Source: motion_control.c:mc_dwell @ bfb67f0 (lines 195-200)."""
        if self.sys.state == STATE_CHECK_MODE:
            return
        yield from self._buffer_sync()
        yield from self._delay_sec(seconds)

    def _mc_homing_cycle(self):
        """Instant homing: positions per limits_go_home.

        Source: motion_control.c:mc_homing_cycle @ bfb67f0 (lines 206-251); limits.c:limits_go_home (lines 371-399)."""
        s, st = self.sys, self.settings
        for i in range(self.n_axis):
            if st.homing_dir_mask & (1 << i):
                self.sys_position[i] = c_round(f32((st.max_travel[i] + st.homing_pulloff) * st.steps_per_mm[i]))
            else:
                self.sys_position[i] = c_round(f32(-st.homing_pulloff * st.steps_per_mm[i]))
        self._rt_system()
        if self.critical or s.suspend:
            yield from self._rt_block()
        if s.abort:
            return
        self._gc_sync_position()
        self._plan_sync_position()

    # ---- probe
    def _probe_contact_now(self):
        """Virtual probe pin: forced, or sitting on a contact plane.

        Source: probe.c:probe_get_state @ bfb67f0 (lines 53-66) (the pin level is simulated)."""
        if self.probe_tripped:
            return True
        if self.probe_trigger:
            sp = self.settings.steps_per_mm
            for ax, v in self.probe_trigger.items():
                i = self.axis_letters.index(ax)
                if abs(self.sys_position[i] - v * sp[i]) < 0.5:
                    return True
        return False

    def _probe_get_state(self, contact, away):
        """Source: probe.c:probe_get_state / probe_configure_invert_mask @ bfb67f0 (lines 44-66)."""
        invert = (not self.settings.flags & BIT_INVERT_PROBE_PIN) != bool(away)
        return (not contact) != invert

    def _probe_trip_fraction(self, start_mm, target):
        """Earliest fraction along start->target at which the contact plane is reached (None if never).

        Source: probe.c:probe_state_monitor @ bfb67f0 (lines 53-66 region) (contact detection is simulated)."""
        best = None
        for ax, v in (self.probe_trigger or {}).items():
            i = self.axis_letters.index(ax)
            delta = target[i] - start_mm[i]
            if delta == 0:
                continue
            t = (v - start_mm[i]) / delta
            if 0.0 <= t <= 1.0 and (best is None or t < best):
                best = t
        return best

    def _probe_state_monitor(self):
        """Source: probe.c:probe_state_monitor @ bfb67f0 (lines 53-66 region): record position, cancel motion."""
        self.sys_probe_state = 0
        self.sys_probe_position = list(self.sys_position)
        self.rt_state |= EXEC_MOTION_CANCEL

    def _mc_probe_cycle(self, target, pl, flags):
        """Source: motion_control.c:mc_probe_cycle @ bfb67f0 (lines 256-318)."""
        s = self.sys
        if s.state == STATE_CHECK_MODE:
            return GC_UPDATE_POS_TARGET
        yield from self._buffer_sync()
        if s.abort:
            return GC_UPDATE_POS_NONE
        away = bool(flags & GC_PARSER_PROBE_AWAY)
        no_error = bool(flags & GC_PARSER_PROBE_NO_ERROR)
        s.probe_succeeded = False
        start_mm = [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]
        trip = self._probe_trip_fraction(start_mm, target)
        letters = self.axis_letters
        at_plane = any(abs(start_mm[i] - self.probe_trigger[letters[i]]) <= 0.5 / self.settings.steps_per_mm[i]
                       for i in range(self.n_axis) if letters[i] in (self.probe_trigger or {}))
        contact0 = self.probe_tripped or at_plane or (away and trip is not None)
        if self._probe_get_state(contact0, away):
            self.rt_alarm = EXEC_ALARM_PROBE_FAIL_INITIAL
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            return GC_UPDATE_POS_NONE
        if away and trip == 0.0:
            trip = 1e-9      # leaving the plane trips on the first step
        self.sys_probe_state = 1
        yield from self._mc_line(target, pl, trip=trip)
        if s.abort:
            return GC_UPDATE_POS_NONE
        self.rt_state |= EXEC_CYCLE_START
        while True:
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort:
                return GC_UPDATE_POS_NONE
            if s.state == STATE_IDLE:
                break
            yield
        if self.sys_probe_state == 1:
            if no_error:
                self.sys_probe_position = list(self.sys_position)
            else:
                self.rt_alarm = EXEC_ALARM_PROBE_FAIL_CONTACT
        else:
            s.probe_succeeded = True
        self.sys_probe_state = 0
        self._rt_system()
        self.blocks = []
        self.cur_speed = 0.0
        self._plan_sync_position()
        self.report_probe_parameters()
        return GC_UPDATE_POS_SYSTEM if s.probe_succeeded else GC_UPDATE_POS_TARGET

    def _jog_execute(self, pl, blk):
        """Source: jog.c:jog_execute @ bfb67f0 (lines 25-50)."""
        s = self.sys
        pl.feed_rate = blk.f
        pl.condition |= PL_COND_NO_FEED_OVR
        if self.settings.flags & BIT_SOFT_LIMIT and self._check_travel_limits(blk.xyz):
            return STATUS_TRAVEL_EXCEEDED
        yield from self._mc_line(blk.xyz, pl)
        if s.state == STATE_IDLE and self.blocks:
            s.state = STATE_JOG
        return STATUS_OK

    # ------------------------------------------------------------------ spindle / coolant
    def _spindle_init(self):
        """Source: spindle_control.c:spindle_init @ bfb67f0 (lines 30-54)."""
        st = self.settings
        self.pwm_gradient = 254.0 / (st.rpm_max - st.rpm_min) if st.rpm_max != st.rpm_min else math.inf
        self._spindle_stop()

    def _spindle_stop(self):
        """Source: spindle_control.c:spindle_stop @ bfb67f0 (lines 98-116)."""
        self.pwm_on = False

    def _spindle_set_speed(self, pwm):
        """Source: spindle_control.c:spindle_set_speed @ bfb67f0 (lines 122-146)."""
        self.pwm_on = pwm != 0

    def _spindle_get_state(self):
        """0 off, 1 CW, 2 CCW.  Source: spindle_control.c:spindle_get_state @ bfb67f0 (lines 57-92)."""
        if self.pwm_on:
            return 2 if self.spindle_ccw else 1
        return 0

    def _spindle_compute_pwm(self, rpm):
        """Source: spindle_control.c:spindle_compute_pwm_value @ bfb67f0 (lines 192-222)."""
        s, st = self.sys, self.settings
        rpm = f32(rpm * f32(0.010 * s.spindle_speed_ovr))
        if st.rpm_min >= st.rpm_max or rpm >= st.rpm_max:
            s.spindle_speed = st.rpm_max
            return 255
        if rpm <= st.rpm_min:
            if rpm == 0.0:
                s.spindle_speed = 0.0
                return 0
            s.spindle_speed = st.rpm_min
            return 1
        s.spindle_speed = rpm
        return int(math.floor(f32(f32(rpm - st.rpm_min) * self.pwm_gradient))) + 1

    def _spindle_set_state(self, state, rpm):
        """Source: spindle_control.c:spindle_set_state @ bfb67f0 (lines 226-271)."""
        s = self.sys
        if s.abort:
            return
        if state == 0:
            s.spindle_speed = 0.0
            self._spindle_stop()
        else:
            self.spindle_ccw = state != PL_COND_SPINDLE_CW
            if self.settings.flags & BIT_LASER_MODE and state == PL_COND_SPINDLE_CCW:
                rpm = 0.0
            self._spindle_set_speed(self._spindle_compute_pwm(rpm))
        s.report_ovr_counter = 0

    def _spindle_sync(self, state, rpm):
        """Source: spindle_control.c:spindle_sync @ bfb67f0 (lines 277-285)."""
        if self.sys.state == STATE_CHECK_MODE:
            return
        yield from self._buffer_sync()
        self._spindle_set_state(state, rpm)

    def _coolant_set_state(self, mode):
        """Source: coolant_control.c:coolant_set_state @ bfb67f0 (lines 81-116)."""
        if self.sys.abort:
            return
        self.coolant_on = bool(mode & PL_COND_COOLANT_FLOOD)
        self.sys.report_ovr_counter = 0

    def _coolant_sync(self, mode):
        """Source: coolant_control.c:coolant_sync @ bfb67f0 (lines 121-126)."""
        if self.sys.state == STATE_CHECK_MODE:
            return
        yield from self._buffer_sync()
        self._coolant_set_state(mode)

    def _flag_wco_change(self):
        """Source: system.c:system_flag_wco_change @ bfb67f0 (lines 280-286)."""
        yield from self._buffer_sync()
        self.sys.report_wco_counter = 0

    # ------------------------------------------------------------------ settings.c
    def _read_coord(self, idx):
        """Source: settings.c:settings_read_coord_data @ bfb67f0 (lines 163-173)."""
        return list(self.eeprom_coord[idx])

    def _write_coord(self, idx, data):
        """Source: settings.c:settings_write_coord_data @ bfb67f0 (lines 84-91)."""
        yield from self._buffer_sync()
        self.eeprom_coord[idx] = list(data)

    def _gc_init(self):
        """Source: gcode.c:gc_init @ bfb67f0 (lines 42-50)."""
        self.gc = _GC(self.n_axis)
        self.gc.coord_system = self._read_coord(0)

    def _settings_restore(self, flag):
        """Source: settings.c:settings_restore @ bfb67f0 (lines 104-132)."""
        if flag & 1:
            self.settings.restore_defaults()
        if flag & 2:
            self.eeprom_coord = [[0.0] * self.n_axis for _ in range(SETTING_INDEX_NCOORD + 1)]
        if flag & 4:
            self.eeprom_startup = ["", ""]
        if flag & 8:
            self.eeprom_build_info = ""

    def _store_global_setting(self, parameter, value):
        """Source: settings.c:settings_store_global_setting @ bfb67f0 (lines 193-303)."""
        st = self.settings
        if value < 0.0:
            return STATUS_NEGATIVE_VALUE
        if parameter >= 100:
            parameter -= 100
            set_idx = 0
            while set_idx < 4:
                if parameter < self.n_axis:
                    if set_idx == 0:
                        st.steps_per_mm[parameter] = value
                    elif set_idx == 1:
                        st.max_rate[parameter] = value
                    elif set_idx == 2:
                        st.acceleration[parameter] = f32(value * 60 * 60)
                    else:
                        st.max_travel[parameter] = -value
                    return STATUS_OK
                set_idx += 1
                if parameter < 10 or set_idx == 4:
                    return STATUS_INVALID_STATEMENT
                parameter -= 10
            return STATUS_OK
        iv = c_fix_u32(value) & 0xFF                    # uint8_t int_value = trunc(value)
        flag_map = {4: BIT_INVERT_ST_ENABLE, 5: BIT_INVERT_LIMIT_PINS, 6: BIT_INVERT_PROBE_PIN, 13: BIT_REPORT_INCHES,
                    21: BIT_HARD_LIMIT, 32: BIT_LASER_MODE}
        if parameter == 0:
            if iv < 3:
                return STATUS_SETTING_STEP_PULSE_MIN
            st.pulse_microseconds = iv
        elif parameter == 1:
            st.stepper_idle_lock_time = iv
        elif parameter == 2:
            st.step_invert_mask = iv
        elif parameter == 3:
            st.dir_invert_mask = iv
        elif parameter in flag_map:
            if iv:
                st.flags |= flag_map[parameter]
            else:
                st.flags &= ~flag_map[parameter]
            if parameter == 13:
                yield from self._flag_wco_change()
        elif parameter == 10:
            st.status_report_mask = iv
        elif parameter == 11:
            st.junction_deviation = value
        elif parameter == 12:
            st.arc_tolerance = value
        elif parameter == 20:
            if iv:
                if not st.flags & BIT_HOMING_ENABLE:
                    return STATUS_SOFT_LIMIT_ERROR
                st.flags |= BIT_SOFT_LIMIT
            else:
                st.flags &= ~BIT_SOFT_LIMIT
        elif parameter == 22:
            if iv:
                st.flags |= BIT_HOMING_ENABLE
            else:
                st.flags &= ~(BIT_HOMING_ENABLE | BIT_SOFT_LIMIT)
        elif parameter == 23:
            st.homing_dir_mask = iv
        elif parameter == 24:
            st.homing_feed_rate = value
        elif parameter == 25:
            st.homing_seek_rate = value
        elif parameter == 26:
            st.homing_debounce_delay = iv
        elif parameter == 27:
            st.homing_pulloff = value
        elif parameter in (30, 31):
            if parameter == 30:
                st.rpm_max = value
            else:
                st.rpm_min = value
            self._spindle_init()
        else:
            return STATUS_INVALID_STATEMENT
        return STATUS_OK

    # ------------------------------------------------------------------ system.c
    def _system_execute_startup(self):
        """Source: system.c:system_execute_startup @ bfb67f0 (lines 98-112)."""
        for n in range(2):
            line = self.eeprom_startup[n]
            if line:
                status = self._gc_check(line)
                if status is None:
                    status = yield from self._gc_run()
                self.report_execute_startup_message(line, status)

    def _system_execute_line(self, line):
        """Source: system.c:system_execute_line @ bfb67f0 (lines 123-276)."""
        s, st = self.sys, self.settings
        L = line + "\0\0\0\0\0\0\0"
        cc = 1
        helper = False
        ch = L[1]
        if ch == "\0":
            self.report_grbl_help()
            return STATUS_OK
        if ch == "J":
            if s.state != STATE_IDLE and s.state != STATE_JOG:
                return STATUS_IDLE_ERROR
            if L[2] != "=":
                return STATUS_INVALID_STATEMENT
            status = self._gc_check(line)
            if status is None:
                status = yield from self._gc_run()
            return status
        if ch in "$GCX":
            if L[2] != "\0":
                return STATUS_INVALID_STATEMENT
            if ch == "$":
                if s.state & (STATE_CYCLE | STATE_HOLD):
                    return STATUS_IDLE_ERROR
                self.report_grbl_settings()
            elif ch == "G":
                self.report_gcode_modes()
            elif ch == "C":
                if s.state == STATE_CHECK_MODE:
                    self._mc_reset()
                    self.report_feedback_message(MESSAGE_DISABLED)
                else:
                    if s.state:
                        return STATUS_IDLE_ERROR
                    s.state = STATE_CHECK_MODE
                    self.report_feedback_message(MESSAGE_ENABLED)
            elif s.state == STATE_ALARM:
                self.report_feedback_message(MESSAGE_ALARM_UNLOCK)
                s.state = STATE_IDLE
            return STATUS_OK
        if not (s.state == STATE_IDLE or s.state == STATE_ALARM):
            return STATUS_IDLE_ERROR
        if ch == "#":
            if L[2] != "\0":
                return STATUS_INVALID_STATEMENT
            self.report_ngc_parameters()
        elif ch == "H":
            if not st.flags & BIT_HOMING_ENABLE:
                return STATUS_SETTING_DISABLED
            s.state = STATE_HOMING
            if L[2] != "\0":
                return STATUS_INVALID_STATEMENT   # HOMING_SINGLE_AXIS_COMMANDS undefined
            yield from self._mc_homing_cycle()
            if not s.abort:
                s.state = STATE_IDLE
                yield from self._system_execute_startup()
        elif ch == "S":
            if L[2:5] != "LP\0":
                return STATUS_INVALID_STATEMENT
            self.rt_state |= EXEC_SLEEP
        elif ch == "I":
            if L[2] == "\0":
                self.report_build_info(self.eeprom_build_info)
            else:
                if L[2] != "=":
                    return STATUS_INVALID_STATEMENT
                self.eeprom_build_info = line[3:]
        elif ch == "R":
            if L[2:4] != "ST" or L[4] != "=" or L[6] != "\0":
                return STATUS_INVALID_STATEMENT
            flag = {"$": 1, "#": 2, "*": 0xFF}.get(L[5])
            if flag is None:
                return STATUS_INVALID_STATEMENT
            self._settings_restore(flag)
            self.report_feedback_message(MESSAGE_RESTORE_DEFAULTS)
            self._mc_reset()
        else:
            if ch == "N":
                cc = 2
                if L[2] == "\0":
                    for n in range(2):
                        self.report_startup_line(n, self.eeprom_startup[n])
                    return STATUS_OK
                if s.state != STATE_IDLE:
                    return STATUS_IDLE_ERROR
                helper = True
            ok, parameter, cc = read_float(L, cc)
            if not ok:
                return STATUS_BAD_NUMBER_FORMAT
            if L[cc] != "=":
                return STATUS_INVALID_STATEMENT
            cc += 1
            if helper:
                body = line[cc:]
                status = self._gc_check(body)
                if status is None:
                    status = yield from self._gc_run()
                if status:
                    return status
                yield from self._buffer_sync()
                n = c_fix_u32(parameter) & 0xFF         # helper_var = trunc(parameter), a uint8_t
                if n < 2:
                    self.eeprom_startup[n] = body
            else:
                ok, value, cc = read_float(L, cc)
                if not ok:
                    return STATUS_BAD_NUMBER_FORMAT
                if L[cc] != "\0" or parameter > 255:
                    return STATUS_INVALID_STATEMENT
                return (yield from self._store_global_setting(c_fix_u32(parameter) & 0xFF, value))
        return STATUS_OK

    # ------------------------------------------------------------------ gcode.c
    def _gc_check(self, line):
        """Steps 1-3 of gc_execute_line (parse and error-check).  Returns a status or None to execute.

        Source: gcode.c:gc_execute_line @ bfb67f0 (lines 66-503)."""
        gcs = self.gc
        st = self.settings
        blk = _Block(self.n_axis)
        blk.modal = m = gcs.modal.copy()
        x = _GCX()
        axis_command = axis_words = ijk_words = command_words = value_words = flags = 0
        jog = line[:1] == "$"
        if jog:
            flags |= GC_PARSER_JOG
            m.motion = MOTION_LINEAR
            m.feed_rate = 0
        i = 3 if jog else 0
        n = len(line)
        xyz, ijk = blk.xyz, blk.ijk
        while i < n:
            letter = line[i]
            if letter < "A" or letter > "Z":
                return STATUS_EXPECTED_COMMAND_LETTER
            ok, value, i = read_float(line, i + 1)
            if not ok:
                return STATUS_BAD_NUMBER_FORMAT
            iv, mantissa = _word_parts(value)
            if letter == "G":
                if iv in (10, 28, 30, 92):
                    if mantissa == 0:
                        if axis_command:
                            return STATUS_GCODE_AXIS_COMMAND_CONFLICT
                        axis_command = 1
                    wb = MG_G0
                    blk.nmc = iv
                    if iv != 10:
                        if mantissa not in (0, 10):
                            return STATUS_GCODE_UNSUPPORTED_COMMAND
                        blk.nmc += mantissa
                        mantissa = 0
                elif iv in (4, 53):
                    wb = MG_G0
                    blk.nmc = iv
                elif iv in (0, 1, 2, 3, 38, 80):
                    if iv != 80:
                        if axis_command:
                            return STATUS_GCODE_AXIS_COMMAND_CONFLICT
                        axis_command = 2
                    wb = MG_G1
                    m.motion = iv
                    if iv == 38:
                        if mantissa not in (20, 30, 40, 50):
                            return STATUS_GCODE_UNSUPPORTED_COMMAND
                        m.motion += mantissa // 10 + 100
                        mantissa = 0
                elif iv in (17, 18, 19):
                    wb = MG_G2
                    m.plane_select = iv - 17
                elif iv in (90, 91):
                    if mantissa == 0:
                        wb = MG_G3
                        m.distance = iv - 90
                    else:
                        wb = MG_G4
                        if mantissa != 10 or iv == 90:
                            return STATUS_GCODE_UNSUPPORTED_COMMAND
                        mantissa = 0
                elif iv in (93, 94):
                    wb = MG_G5
                    m.feed_rate = 94 - iv
                elif iv in (20, 21):
                    wb = MG_G6
                    m.units = 21 - iv
                elif iv == 40:
                    wb = MG_G7
                elif iv in (43, 49):
                    wb = MG_G8
                    if axis_command:
                        return STATUS_GCODE_AXIS_COMMAND_CONFLICT
                    axis_command = 3
                    if iv == 49:
                        m.tool_length = 0
                    elif mantissa == 10:
                        m.tool_length = 1
                    else:
                        return STATUS_GCODE_UNSUPPORTED_COMMAND
                    mantissa = 0
                elif 54 <= iv <= 59:
                    wb = MG_G12
                    m.coord_select = iv - 54
                elif iv == 61:
                    wb = MG_G13
                    if mantissa != 0:
                        return STATUS_GCODE_UNSUPPORTED_COMMAND
                else:
                    return STATUS_GCODE_UNSUPPORTED_COMMAND
                if mantissa > 0:
                    return STATUS_GCODE_COMMAND_VALUE_NOT_INTEGER
                if command_words & (1 << wb):
                    return STATUS_GCODE_MODAL_GROUP_VIOLATION
                command_words |= 1 << wb
            elif letter == "M":
                if mantissa > 0:
                    return STATUS_GCODE_COMMAND_VALUE_NOT_INTEGER
                if iv in (0, 1, 2, 30):
                    wb = MG_M4
                    if iv == 0:
                        m.program_flow = PROGRAM_FLOW_PAUSED
                    elif iv != 1:
                        m.program_flow = iv
                elif iv in (3, 4, 5):
                    wb = MG_M7
                    if iv == 4 and not self._m4_supported:
                        return STATUS_GCODE_UNSUPPORTED_COMMAND
                    m.spindle = {3: PL_COND_SPINDLE_CW, 4: PL_COND_SPINDLE_CCW, 5: 0}[iv]
                elif iv in self._coolant_m_words:
                    wb = MG_M8
                    m.coolant = self._coolant_for_m(iv)
                else:
                    return STATUS_GCODE_UNSUPPORTED_COMMAND
                if command_words & (1 << wb):
                    return STATUS_GCODE_MODAL_GROUP_VIOLATION
                command_words |= 1 << wb
            else:
                if letter == "F":
                    wb = W_F
                    blk.f = value
                elif letter in "IJK":
                    wb = W_I + "IJK".index(letter)
                    ijk["IJK".index(letter)] = value
                    ijk_words |= 1 << "IJK".index(letter)
                elif letter == "L":
                    wb = W_L
                    blk.l = iv
                elif letter == "N":
                    wb = W_N
                    blk.n = c_fix_i32(value)            # int32_t n = trunc(value)
                elif letter == "P":
                    wb = W_P
                    blk.p = value
                elif letter == "R":
                    wb = W_R
                    blk.r = value
                elif letter == "S":
                    wb = W_S
                    blk.s = value
                elif letter == "T":
                    wb = W_T
                    if value > 255:
                        return STATUS_GCODE_MAX_VALUE_EXCEEDED
                    blk.t = iv
                elif letter in self.axis_letters:
                    ax = self.axis_letters.index(letter)
                    wb = W_X + "XYZABC".index(letter)
                    xyz[ax] = value
                    axis_words |= 1 << ax
                else:
                    return STATUS_GCODE_UNSUPPORTED_COMMAND
                if value_words & (1 << wb):
                    return STATUS_GCODE_WORD_REPEATED
                if wb in (W_F, W_N, W_P, W_T, W_S) and value < 0.0:
                    return STATUS_NEGATIVE_VALUE
                value_words |= 1 << wb
        # ---- step 3
        if axis_words and not axis_command:
            axis_command = 2
        if value_words & (1 << W_N) and blk.n > 10000000:
            return STATUS_GCODE_INVALID_LINE_NUMBER
        if flags & GC_PARSER_JOG:
            if not value_words & (1 << W_F):
                return STATUS_GCODE_UNDEFINED_FEED_RATE
            if m.units == 1:
                blk.f = f32(blk.f * MM_PER_INCH)
        elif m.feed_rate == 1:
            if axis_command == 2 and m.motion != MOTION_NONE and m.motion != MOTION_SEEK:
                if not value_words & (1 << W_F):
                    return STATUS_GCODE_UNDEFINED_FEED_RATE
        else:
            if gcs.modal.feed_rate == 0:
                if value_words & (1 << W_F):
                    if m.units == 1:
                        blk.f = f32(blk.f * MM_PER_INCH)
                else:
                    blk.f = gcs.feed_rate
        if not value_words & (1 << W_S):
            blk.s = gcs.spindle_speed
        if blk.nmc == NM_DWELL:
            if not value_words & (1 << W_P):
                return STATUS_GCODE_VALUE_WORD_MISSING
            value_words &= ~(1 << W_P)
        if m.plane_select == 0:
            a0, a1, al = 0, 1, 2
        elif m.plane_select == 1:
            a0, a1, al = 2, 0, 1
        else:
            a0, a1, al = 1, 2, 0
        if m.units == 1:
            for idx in range(self.n_axis):
                if axis_words & (1 << idx):
                    xyz[idx] = f32(xyz[idx] * MM_PER_INCH)
        if axis_command == 3 and m.tool_length == 1 and axis_words ^ (1 << 2):
            return STATUS_GCODE_G43_DYNAMIC_AXIS_ERROR
        block_cs = list(gcs.coord_system)
        if command_words & (1 << MG_G12) and gcs.modal.coord_select != m.coord_select:
            block_cs = self._read_coord(m.coord_select)
        coord_select = 0
        pos = gcs.position
        nmc = blk.nmc
        if nmc == NM_SET_COORD_DATA:
            if not axis_words:
                return STATUS_GCODE_NO_AXIS_WORDS
            if not value_words & ((1 << W_P) | (1 << W_L)):   # sic: bit_isfalse fails only when neither word is present
                return STATUS_GCODE_VALUE_WORD_MISSING
            coord_select = c_fix_u32(blk.p) & 0xFF      # uint8_t coord_select = trunc(p)
            if coord_select > 6:
                return STATUS_GCODE_UNSUPPORTED_COORD_SYS
            if blk.l != 20:
                if blk.l == 2:
                    if value_words & (1 << W_R):
                        return STATUS_GCODE_UNSUPPORTED_COMMAND
                else:
                    return STATUS_GCODE_UNSUPPORTED_COMMAND
            value_words &= ~((1 << W_L) | (1 << W_P))
            if coord_select > 0:
                coord_select -= 1
            else:
                coord_select = m.coord_select
            blk.ijk = ijk = self._read_coord(coord_select)
            for idx in range(self.n_axis):
                if axis_words & (1 << idx):
                    if blk.l == 20:
                        v = f32(f32(pos[idx] - gcs.coord_offset[idx]) - xyz[idx])
                        if idx == 2:
                            v = f32(v - gcs.tool_length_offset)
                        ijk[idx] = v
                    else:
                        ijk[idx] = xyz[idx]
        elif nmc == NM_SET_COORD_OFFSET:
            if not axis_words:
                return STATUS_GCODE_NO_AXIS_WORDS
            for idx in range(self.n_axis):
                if axis_words & (1 << idx):
                    v = f32(f32(pos[idx] - block_cs[idx]) - xyz[idx])
                    if idx == 2:
                        v = f32(v - gcs.tool_length_offset)
                    xyz[idx] = v
                else:
                    xyz[idx] = gcs.coord_offset[idx]
        else:
            if axis_command != 3 and axis_words:
                for idx in range(self.n_axis):
                    if not axis_words & (1 << idx):
                        xyz[idx] = pos[idx]
                    elif nmc != NM_ABS_OVERRIDE:
                        if m.distance == 0:
                            xyz[idx] = f32(xyz[idx] + f32(block_cs[idx] + gcs.coord_offset[idx]))
                            if idx == 2:
                                xyz[idx] = f32(xyz[idx] + gcs.tool_length_offset)
                        else:
                            xyz[idx] = f32(xyz[idx] + pos[idx])
            if nmc in (NM_GO_HOME_0, NM_GO_HOME_1):
                blk.ijk = ijk = self._read_coord(SETTING_INDEX_G28 if nmc == NM_GO_HOME_0 else SETTING_INDEX_G30)
                if axis_words:
                    for idx in range(self.n_axis):
                        if not axis_words & (1 << idx):
                            ijk[idx] = pos[idx]
                else:
                    axis_command = 0
            elif nmc == NM_ABS_OVERRIDE:
                if not (m.motion == MOTION_SEEK or m.motion == MOTION_LINEAR):
                    return STATUS_GCODE_G53_INVALID_MOTION_MODE
        if m.motion == MOTION_NONE:
            if axis_words:
                return STATUS_GCODE_AXIS_WORDS_EXIST
        elif axis_command == 2:
            if m.motion == MOTION_SEEK:
                if not axis_words:
                    axis_command = 0
            else:
                if blk.f == 0.0:
                    return STATUS_GCODE_UNDEFINED_FEED_RATE
                if m.motion == MOTION_LINEAR:
                    if not axis_words:
                        axis_command = 0
                elif m.motion in (MOTION_CW_ARC, MOTION_CCW_ARC):
                    if m.motion == MOTION_CW_ARC:
                        flags |= GC_PARSER_ARC_CW
                    if not axis_words:
                        return STATUS_GCODE_NO_AXIS_WORDS
                    if a0 >= self.n_axis or a1 >= self.n_axis or not axis_words & ((1 << a0) | (1 << a1)):
                        return STATUS_GCODE_NO_AXIS_WORDS_IN_PLANE   # plane axis absent from this build
                    dx = f32(xyz[a0] - pos[a0])
                    dy = f32(xyz[a1] - pos[a1])
                    if value_words & (1 << W_R):
                        value_words &= ~(1 << W_R)
                        if pos == xyz:
                            return STATUS_GCODE_INVALID_TARGET
                        if m.units == 1:
                            blk.r = f32(blk.r * MM_PER_INCH)
                        h = f32(f32(f32(4.0 * f32(blk.r * blk.r)) - f32(dx * dx)) - f32(dy * dy))
                        if h < 0:
                            return STATUS_GCODE_ARC_RADIUS_ERROR
                        h = f32(_fdiv(-math.sqrt(h), f32(math.sqrt(f32(f32(dx * dx) + f32(dy * dy))))))
                        if m.motion == MOTION_CCW_ARC:
                            h = -h
                        if blk.r < 0:
                            h = -h
                            blk.r = -blk.r
                        ijk[a0] = f32(0.5 * f32(dx - f32(dy * h)))
                        ijk[a1] = f32(0.5 * f32(dy + f32(dx * h)))
                    else:
                        if not ijk_words & ((1 << a0) | (1 << a1)):
                            return STATUS_GCODE_NO_OFFSETS_IN_PLANE
                        value_words &= ~((1 << W_I) | (1 << W_J) | (1 << W_K))
                        if m.units == 1:
                            for idx in range(self.n_axis):
                                if ijk_words & (1 << idx):
                                    ijk[idx] = f32(ijk[idx] * MM_PER_INCH)
                        dx = f32(dx - ijk[a0])
                        dy = f32(dy - ijk[a1])
                        target_r = f32(math.sqrt(f32(f32(dx * dx) + f32(dy * dy))))
                        blk.r = f32(math.sqrt(f32(f32(ijk[a0] * ijk[a0]) + f32(ijk[a1] * ijk[a1]))))
                        delta_r = abs(f32(target_r - blk.r))
                        if delta_r > 0.005:
                            if delta_r > 0.5:
                                return STATUS_GCODE_INVALID_TARGET
                            if delta_r > f32(0.001 * blk.r):
                                return STATUS_GCODE_INVALID_TARGET
                else:
                    if m.motion in (MOTION_PROBE_TOWARD_NO_ERROR, MOTION_PROBE_AWAY_NO_ERROR):
                        flags |= GC_PARSER_PROBE_NO_ERROR
                    if m.motion in (MOTION_PROBE_AWAY, MOTION_PROBE_AWAY_NO_ERROR):
                        flags |= GC_PARSER_PROBE_AWAY
                    if not axis_words:
                        return STATUS_GCODE_NO_AXIS_WORDS
                    if pos == xyz:
                        return STATUS_GCODE_INVALID_TARGET
        if flags & GC_PARSER_JOG:
            value_words &= ~((1 << W_N) | (1 << W_F))
        else:
            value_words &= ~((1 << W_N) | (1 << W_F) | (1 << W_S) | (1 << W_T))
        if axis_command:
            value_words &= ~((1 << W_X) | (1 << W_Y) | (1 << W_Z) | (1 << W_A) | (1 << W_B) | (1 << W_C))
        if value_words:
            return STATUS_GCODE_UNUSED_WORDS
        x.flags, x.axis_command, x.axis_words, x.command_words = flags, axis_command, axis_words, command_words
        x.coord_select, x.block_coord_system, x.axis_0, x.axis_1, x.axis_linear = coord_select, block_cs, a0, a1, al
        self._gcb, self._gcx = blk, x
        return None

    def _gc_run(self):
        """Step 4 of gc_execute_line (execute).

        Source: gcode.c:gc_execute_line @ bfb67f0 (lines 504-1133)."""
        gcs, st, s = self.gc, self.settings, self.sys
        blk, x = self._gcb, self._gcx
        m = blk.modal
        flags, axis_command = x.flags, x.axis_command
        pl = _PL()
        if flags & GC_PARSER_JOG:
            if x.command_words & ~((1 << MG_G3) | (1 << MG_G6) | (1 << MG_G0)):
                return STATUS_INVALID_JOG_COMMAND
            if not (blk.nmc == NM_ABS_OVERRIDE or blk.nmc == 0):
                return STATUS_INVALID_JOG_COMMAND
            pl.spindle_speed = gcs.spindle_speed
            pl.condition = gcs.modal.spindle | gcs.modal.coolant
            status = yield from self._jog_execute(pl, blk)
            if status == STATUS_OK:
                gcs.position = list(blk.xyz)
            return status
        laser = bool(st.flags & BIT_LASER_MODE)
        if laser:
            if m.motion not in (MOTION_LINEAR, MOTION_CW_ARC, MOTION_CCW_ARC):
                flags |= GC_PARSER_LASER_DISABLE
            if x.axis_words and axis_command == 2:
                flags |= GC_PARSER_LASER_ISMOTION
            elif gcs.modal.spindle == PL_COND_SPINDLE_CW:
                if gcs.modal.motion in (MOTION_LINEAR, MOTION_CW_ARC, MOTION_CCW_ARC):
                    if flags & GC_PARSER_LASER_DISABLE:
                        flags |= GC_PARSER_LASER_FORCE_SYNC
                elif not flags & GC_PARSER_LASER_DISABLE:
                    flags |= GC_PARSER_LASER_FORCE_SYNC
        gcs.line_number = blk.n
        gcs.modal.feed_rate = m.feed_rate
        if gcs.modal.feed_rate:
            pl.condition |= PL_COND_INVERSE_TIME
        gcs.feed_rate = blk.f
        pl.feed_rate = gcs.feed_rate
        if gcs.spindle_speed != blk.s or flags & GC_PARSER_LASER_FORCE_SYNC:
            if gcs.modal.spindle != 0 and not flags & GC_PARSER_LASER_ISMOTION:
                yield from self._spindle_sync(gcs.modal.spindle, 0.0 if flags & GC_PARSER_LASER_DISABLE else blk.s)
            gcs.spindle_speed = blk.s
        if not flags & GC_PARSER_LASER_DISABLE:
            pl.spindle_speed = gcs.spindle_speed
        gcs.tool = blk.t
        if gcs.modal.spindle != m.spindle:
            yield from self._spindle_sync(m.spindle, pl.spindle_speed)
            gcs.modal.spindle = m.spindle
        pl.condition |= gcs.modal.spindle
        if gcs.modal.coolant != m.coolant:
            yield from self._coolant_sync(m.coolant)
            gcs.modal.coolant = m.coolant
        pl.condition |= gcs.modal.coolant
        if blk.nmc == NM_DWELL:
            yield from self._mc_dwell(blk.p)
        gcs.modal.plane_select = m.plane_select
        gcs.modal.units = m.units
        if axis_command == 3:
            gcs.modal.tool_length = m.tool_length
            if self.n_axis > 2:     # the tool length offset lives on Z
                if gcs.modal.tool_length == 0:
                    blk.xyz[2] = 0.0
                if gcs.tool_length_offset != blk.xyz[2]:
                    gcs.tool_length_offset = blk.xyz[2]
                    yield from self._flag_wco_change()
        if gcs.modal.coord_select != m.coord_select:
            gcs.modal.coord_select = m.coord_select
            gcs.coord_system = list(x.block_coord_system)
            yield from self._flag_wco_change()
        gcs.modal.distance = m.distance
        nmc = blk.nmc
        if nmc == NM_SET_COORD_DATA:
            yield from self._write_coord(x.coord_select, blk.ijk)
            if gcs.modal.coord_select == x.coord_select:
                gcs.coord_system = list(blk.ijk)
                yield from self._flag_wco_change()
        elif nmc in (NM_GO_HOME_0, NM_GO_HOME_1):
            pl.condition |= PL_COND_RAPID
            if axis_command:
                yield from self._mc_line(blk.xyz, pl)
            yield from self._mc_line(blk.ijk, pl)
            gcs.position = list(blk.ijk)
        elif nmc == NM_SET_HOME_0:
            yield from self._write_coord(SETTING_INDEX_G28, gcs.position)
        elif nmc == NM_SET_HOME_1:
            yield from self._write_coord(SETTING_INDEX_G30, gcs.position)
        elif nmc == NM_SET_COORD_OFFSET:
            gcs.coord_offset = list(blk.xyz)
            yield from self._flag_wco_change()
        elif nmc == NM_RESET_COORD_OFFSET:
            gcs.coord_offset = [0.0] * self.n_axis
            yield from self._flag_wco_change()
        gcs.modal.motion = m.motion
        if gcs.modal.motion != MOTION_NONE and axis_command == 2:
            upd = GC_UPDATE_POS_TARGET
            mo = gcs.modal.motion
            if mo == MOTION_LINEAR:
                yield from self._mc_line(blk.xyz, pl)
            elif mo == MOTION_SEEK:
                pl.condition |= PL_COND_RAPID
                yield from self._mc_line(blk.xyz, pl)
            elif mo in (MOTION_CW_ARC, MOTION_CCW_ARC):
                yield from self._mc_arc(blk.xyz, pl, gcs.position, blk.ijk, blk.r, x.axis_0, x.axis_1,
                                        x.axis_linear, bool(flags & GC_PARSER_ARC_CW))
            else:
                pl.condition |= PL_COND_NO_FEED_OVR
                upd = yield from self._mc_probe_cycle(blk.xyz, pl, flags)
            if upd == GC_UPDATE_POS_TARGET:
                gcs.position = list(blk.xyz)
            elif upd == GC_UPDATE_POS_SYSTEM:
                self._gc_sync_position()
        gcs.modal.program_flow = m.program_flow
        if gcs.modal.program_flow:
            yield from self._buffer_sync()
            if gcs.modal.program_flow == PROGRAM_FLOW_PAUSED:
                if s.state != STATE_CHECK_MODE:
                    self.rt_state |= EXEC_FEED_HOLD
                    self._rt_system()
                    if self.critical or s.suspend:
                        yield from self._rt_block()
            else:
                gm = gcs.modal
                gm.motion, gm.plane_select, gm.distance, gm.feed_rate = MOTION_LINEAR, 0, 0, 0
                gm.coord_select, gm.spindle, gm.coolant = 0, 0, 0
                s.f_override, s.r_override, s.spindle_speed_ovr = 100, 100, 100
                if s.state != STATE_CHECK_MODE:
                    gcs.coord_system = self._read_coord(0)
                    yield from self._flag_wco_change()
                    self._spindle_set_state(0, 0.0)
                    self._coolant_set_state(0)
                self.report_feedback_message(MESSAGE_PROGRAM_END)
            gcs.modal.program_flow = 0
        return STATUS_OK
