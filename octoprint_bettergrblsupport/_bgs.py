#!/usr/bin/python
#
# Written by:  Shell M. Shrader (https://github.com/synman/Octoprint-Bettergrblsupport)
# Copyright [2021] [Shell M. Shrader]
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# References
#
# https://web.archive.org/web/20211123161339/https://wiki.shapeoko.com/index.php/G-Code
# https://citeseerx.ist.psu.edu/viewdoc/download;jsessionid=B69BCE8C0F7F5071B56B464AB4CA8C56?doi=10.1.1.15.7813&rep=rep1&type=pdf
# https://github.com/gnea/grbl/blob/master/doc/markdown/commands.md
# https://github.com/gnea/grbl/wiki/Grbl-v1.1-Jogging
# https://github.com/gnea/grbl/wiki/Grbl-v1.1-Configuration#10---status-report-mask
# https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface#grbl-push-messages
# https://reprap.org/wiki/G-codeimport os
#
import html
import os
import time
import math

import re
import threading
import subprocess
import requests

from timeit import default_timer as timer
from octoprint.events import Events
from octoprint.settings import default_settings
from octoprint.util.version import is_octoprint_compatible

from .zprobe import ZProbe
from .xyprobe import XyProbe

zProbe = None
xyProbe = None
# set when a probe is torn down by an alarm/error/cancel instead of finishing, so a probe that chains another
# (the X/Y probe after the Z probe) can tell the difference and stop
zProbeAborted = False

def load_grbl_descriptions(_plugin):
    path = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep

    f = open(path + "grbl_errors.txt")

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if match is not None:
            _plugin.grblErrors[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching error id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

    f = open(path + "grbl_alarms.txt")

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if match is not None:
            _plugin.grblAlarms[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching alarm id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

    f = open(path + "grbl_settings.txt")

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if match is not None:
            _plugin.grblSettingsNames[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching setting id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])


def load_grbl_settings(_plugin):
    _plugin._logger.debug("_bgs: load_grbl_settings")

    _plugin.grblSettingsText = _plugin._settings.get(["grblSettingsText"])

    if _plugin.grblSettingsText is not None:
        for setting in _plugin.grblSettingsText.split("||"):
            if len(setting.strip()) > 0:

                _plugin._logger.debug(f"load_grbl_settings=[{setting}]")

                set = setting.split("|")
                if set is not None:
                    _plugin.grblSettings.update({int(set[0]): [set[1], _plugin.grblSettingsNames.get(int(set[0]))]})
    return


def save_grbl_settings(_plugin):
    _plugin._logger.debug("_bgs: save_grbl_settings")

    ret = ""
    for id, data in sorted(_plugin.grblSettings.items(), key=lambda x: int(x[0])):
        ret = ret + f"{id}|{data[0]}|{data[1]}||"

    _plugin._logger.debug(f"save_grbl_settings=[{ret}]")

    _plugin.grblSettingsText = ret

    return ret


def cleanup_due_to_uninstall(_plugin, remove_profile=True):
    _plugin._logger.debug(f"_bgs: cleanup_due_to_uninstall remove_profile=[{remove_profile}]")

    # re-enable model size detection, sd card support
    _plugin._settings.global_set_boolean(["feature", "modelSizeDetection"], True)
    _plugin._settings.global_set_boolean(["feature", "sdSupport"], True)

    # load maps of disabled plugins & tabs
    disabledPlugins = _plugin._settings.global_get(["plugins", "_disabled"])
    disabledTabs = _plugin._settings.global_get(["appearance", "components", "disabled", "tab"])
    orderedTabs = _plugin._settings.global_get(["appearance", "components", "order", "tab"])
    orderedSidebar = _plugin._settings.global_get(["appearance", "components", "order", "sidebar"])

    if disabledPlugins is None:
        disabledPlugins = []

    if disabledTabs is None:
        disabledTabs = []

    if orderedTabs is None:
        orderedTabs = []

    if orderedSidebar is None:
        orderedSidebar = []

    # re-enable the printer safety check plugin
    if "printer_safety_check" in disabledPlugins:
        disabledPlugins.remove("printer_safety_check")

    # re-enable the gcodeviewer plugin
    if "gcodeviewer" in disabledPlugins:
        disabledPlugins.remove("gcodeviewer")
    if "plugin_gcodeviewer" in disabledTabs:
        disabledTabs.remove("plugin_gcodeviewer")

    # re-enable the built-in temp tab if it was hidden
    if "temperature" in disabledTabs:
        disabledTabs.remove("temperature")

    # re-enable the built-in control tab if it was hidden
    if "control" in disabledTabs:
        disabledTabs.remove("control")

    # remove me from ordered tabs if i'm in there
    if "plugin_bettergrblsupport" in orderedTabs:
        orderedTabs.remove("plugin_bettergrblsupport")

    # ensure control is first tab
    if "control" in orderedTabs:
        orderedTabs.remove("control")
        orderedTabs.insert(0, "control")

    # remove me from ordered sidebar if i'm in there
    if "plugin_bettergrblsupport" in orderedSidebar:
        orderedSidebar.remove("plugin_bettergrblsupport")

    if remove_profile:
        # restore the original printer profile (if it exists) and delete mine
        old_profile = _plugin._settings.get(["old_profile"])

        if not old_profile or not _plugin._printer_profile_manager.exists(old_profile):
            old_profile = "_default"

        _plugin._printer_profile_manager.select(old_profile)
        _plugin._printer_profile_manager.set_default(old_profile)

        if _plugin._printer_profile_manager.exists("_bgs"):
            _plugin._printer_profile_manager.remove("_bgs")
            _plugin._logger.debug("bgs profile has been deleted")

    _plugin._settings.global_set(["plugins", "_disabled"], disabledPlugins)
    _plugin._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)
    _plugin._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)
    _plugin._settings.global_set(["appearance", "components", "order", "sidebar"], orderedSidebar)

    # add pretty much all of grbl to long running commands list
    longCmds = _plugin._settings.global_get(serial_settings_path(["longRunningCommands"]))
    if longCmds is None:
        longCmds = []

    if "$H" in longCmds:
        longCmds.remove("$H")
    if "G92" in longCmds:
        longCmds.remove("G92")
    if "G30" in longCmds:
        longCmds.remove("G30")
    if "G53" in longCmds:
        longCmds.remove("G53")
    if "G54" in longCmds:
        longCmds.remove("G54")

    if "G20" in longCmds:
        longCmds.remove("G20")
    if "G21" in longCmds:
        longCmds.remove("G21")

    if "G90" in longCmds:
        longCmds.remove("G90")
    if "G91" in longCmds:
        longCmds.remove("G91")

    if "G38.1" in longCmds:
        longCmds.remove("G38.1")
    if "G38.2" in longCmds:
        longCmds.remove("G38.2")
    if "G38.3" in longCmds:
        longCmds.remove("G38.3")
    if "G38.4" in longCmds:
        longCmds.remove("G38.4")
    if "G38.5" in longCmds:
        longCmds.remove("G38.5")

    if "G0" in longCmds:
        longCmds.remove("G0")
    if "G1" in longCmds:
        longCmds.remove("G1")
    if "G2" in longCmds:
        longCmds.remove("G2")
    if "G3" in longCmds:
        longCmds.remove("G3")
    if "G4" in longCmds:
        longCmds.remove("G4")

    if "M3" in longCmds:
        longCmds.remove("M3")
    if "M4" in longCmds:
        longCmds.remove("M4")
    if "M5" in longCmds:
        longCmds.remove("M5")
    if "M7" in longCmds:
        longCmds.remove("M7")
    if "M8" in longCmds:
        longCmds.remove("M8")
    if "M9" in longCmds:
        longCmds.remove("M9")
    if "M30" in longCmds:
        longCmds.remove("M30")

    _plugin._settings.global_set(serial_settings_path(["longRunningCommands"]), longCmds)
    # OctoPrint's own defaults: idle 2 and long 5
    _plugin._settings.global_set(serial_settings_path(["maxCommunicationTimeouts", "idle"]), 2)
    _plugin._settings.global_set(serial_settings_path(["maxCommunicationTimeouts", "long"]), 5)
    set_never_send_checksum(_plugin, False)
    _plugin._settings.global_set(serial_settings_path(["encoding"]), "ascii")
    _plugin._settings.global_set_boolean(serial_settings_path(["sanityCheckTools"]), True)

    # take our filters out of the terminal filters; the user's own stay. When none is left, drop the override
    # so OctoPrint's own defaults (correct for whichever version is running) apply again
    remaining = user_terminal_filters(_plugin._settings.global_get(["terminalFilters"]))
    if remaining:
        _plugin._settings.global_set(["terminalFilters"], remaining)
    else:
        _plugin._settings.global_remove(["terminalFilters"])

    _plugin._settings.save()

    # remove scripts/gcode/afterPrintCancelled because it does stupid stuff with tools
    oldCancelScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "oldAfterPrintCancelled"))
    currentCancelScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "afterPrintCancelled"))

    if os.path.exists(oldCancelScript) and not os.path.exists(currentCancelScript):
        os.rename(oldCancelScript, currentCancelScript)

    # remove our M999 on connect
    oldConnectedScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "oldAfterPrinterConnected"))
    currentConnectedScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "afterPrinterConnected"))

    if os.path.exists(oldConnectedScript):
        if os.path.exists(currentConnectedScript):
            os.remove(currentConnectedScript)
        os.rename(oldConnectedScript, currentConnectedScript)

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="restart_required"))


# #-- EventHandlerPlugin mix-in
def on_event(_plugin, event, payload):
    subscribed_events = (Events.FILE_SELECTED, Events.FILE_ADDED, Events.PRINT_STARTED, Events.PRINT_CANCELLED, Events.PRINT_CANCELLING,
                        Events.PRINT_PAUSED, Events.PRINT_RESUMED, Events.PRINT_DONE, Events.PRINT_FAILED,
                        Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN, Events.PLUGIN_PLUGINMANAGER_DISABLE_PLUGIN, Events.UPLOAD,
                        Events.CONNECTING, Events.CONNECTED, Events.DISCONNECTING, Events.DISCONNECTED, Events.STARTUP, Events.SHUTDOWN)

    if event not in subscribed_events and payload is not None and payload.get("state_id") not in ("PAUSING", "STARTING"):
        _plugin._logger.debug(f'event [{event}] payload [{payload}] received but not subscribed - discarding')
        return

    _plugin._logger.debug(f"_bgs: on_event event=[{event}] payload=[{payload}]")

    # our plugin is being uninstalled
    if event in (Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN, Events.PLUGIN_PLUGINMANAGER_DISABLE_PLUGIN) and payload["id"] == _plugin._identifier:
        _plugin._logger.debug('we are being uninstalled/disabled :(')
        cleanup_due_to_uninstall(_plugin)
        _plugin._logger.debug('plugin cleanup completed (this house is clean)')
        return

    if _plugin._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
        return

    # - CONNECTING
    if event == Events.CONNECTING:
        _plugin.connectionState = event
        # let's make sure we don't have any commands queued up
        _plugin.grblCmdQueue.clear()

    # - CONNECTED
    if event == Events.CONNECTED:
        _plugin._logger.debug('machine connected')

        _plugin.connectionState = event
        _plugin.whenConnected = time.monotonic()
        _plugin.userCommandSinceConnect = False
        _plugin.autoSleepTimer = time.monotonic()

        _plugin.is_operational = True
        _plugin._settings.set_boolean(["is_operational"], _plugin.is_operational)

        _plugin.fluidConfig = None
        _plugin.grblMCode = "M5"
        # a restore owed by a probe on the previous connection is not this controller's: it starts from its own state
        _plugin.pendingDistanceRestore = None
        _plugin._printer.commands(["$I", "$G", "$#"])
        # don't leave grblState at N/A until the next scheduled poll
        schedule_status_refresh(_plugin, 0.5)

    # Disconnecting & Disconnected
    if event in (Events.DISCONNECTING, Events.DISCONNECTED):
        _plugin.connectionState = event
        _plugin.handshakeSent = False
        _plugin.grblState = "N/A"
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="N/A"))

        _plugin.is_operational = False
        _plugin._settings.set_boolean(["is_operational"], _plugin.is_operational)

    # Print Starting
    if payload is not None and payload.get("state_id") == "STARTING":
        add_notifications(_plugin, ["Pgm Begin"])
        return

    # 'PrintStarted'
    if event == Events.PRINT_STARTED:
        _plugin.cancelling = False

        # the cached state lags the controller (e.g. a print started right after $X): ask before judging it
        if _plugin.grblState.upper() not in ("IDLE", "CHECK", "RUN") and "HOLD" not in _plugin.grblState.upper():
            refresh_grbl_state(_plugin)

        if "HOLD" in _plugin.grblState.upper():
            send_command_now(_plugin._printer, _plugin._logger, "~")
        elif _plugin.grblState.upper() not in ("IDLE", "CHECK", "RUN"):
            # we have to stop this
            _plugin._logger.warning("print started but grbl state is [%s], cancelling print", _plugin.grblState)
            _plugin._printer.cancel_print()
            return

        # reset our rate overrides
        _plugin.feedRate = 0
        _plugin.plungeRate = 0
        _plugin.powerRate = 0

        _plugin.grblState = "Run"
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Run"))

        _plugin.is_printing = True
        _plugin._settings.set_boolean(["is_printing"], _plugin.is_printing)

        if _plugin.autoCooldown:
            activate_auto_cooldown(_plugin)

        return

    # Print ended (finished / failed / cancelled)
    if event in (Events.PRINT_CANCELLED, Events.PRINT_DONE, Events.PRINT_FAILED):
        _plugin._logger.debug(f"handling event [{event}]")
        _plugin.pauseHoldActive = False
        # the next job starts with the spindle off (end of program / soft reset), whatever this one last set
        _plugin.grblMCode = "M5"

        # a cancel is still stopping the machine: its own status reports decide the state, not this event
        if not _plugin.cancelMotionActive:
            _plugin.cancelling = False
            _plugin.grblState = "Idle"
            _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Idle"))

        _plugin.is_printing = False
        _plugin._settings.set_boolean(["is_printing"], _plugin.is_printing)

        # reset our rate overrides
        _plugin.feedRate = 0
        _plugin.plungeRate = 0
        _plugin.powerRate = 0
        # the next job's feed and power come from its own lines: never resume or override with this job's values.
        # Reset here, not at PrintStarted, which can arrive after the next job's first lines have been sent.
        _plugin.grblJobFeed = float(0)
        _plugin.grblJobPower = float(0)

        return

    # Print Cancelling
    if event == Events.PRINT_CANCELLING:
        _plugin._logger.debug("cancelling job")
        _plugin.cancelling = True
        _plugin.cancelMotionActive = True
        _plugin.pauseHoldActive = False
        threading.Thread(target=cancel_motion, args=(_plugin,), daemon=True).start()

    # Print Pausing
    if payload is not None and payload.get("state_id") == "PAUSING":
        _plugin._logger.debug("pausing job")

        strategy, why = pause_strategy(_plugin)
        _plugin._logger.info("pause: strategy [%s] (%s)", strategy, why)

        if strategy == "park":
            # the safety-door realtime byte: the firmware stops, retracts, switches the spindle off and (on resume)
            # restores it and plunges back. An alarmed controller ignores it.
            send_realtime(_plugin, b"\x84")
            if _plugin.grblState.upper() != "ALARM":
                set_grbl_state(_plugin, "Door:2")
            _plugin.parkStartZ = _plugin.grblZ
        elif strategy in ("laser", "hold"):
            send_realtime(_plugin, b"!")
            if _plugin.grblState.upper() != "ALARM":
                set_grbl_state(_plugin, "Hold:1")

        if strategy != "drain":
            # a held or parked planner never acknowledges the line OctoPrint last sent
            _plugin.pauseHoldActive = True
            threading.Thread(target=finish_pause_hold, args=(_plugin,), daemon=True).start()

        write_pause_resume_scripts(_plugin, strategy)

    # Print Paused
    if event == Events.PRINT_PAUSED:
        _plugin._logger.debug("paused job")
        _plugin.pauseHoldActive = False

    # Print Resumed
    if event == Events.PRINT_RESUMED:
        _plugin._logger.debug("resuming job")
        _plugin.pauseHoldActive = False
        _plugin.grblState = "Run"
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Run"))

    # starting up
    if event == Events.STARTUP:
        _plugin._logger.info("starting up")

    # shutting down
    if event == Events.SHUTDOWN:
        _plugin._logger.info("shutting down")
        _plugin._settings.save()

    # File uploaded
    if event == Events.UPLOAD:
        if payload["path"].endswith(".gc") or payload["path"].endswith(".nc"):
            renamed_file = payload["path"][:len(payload["path"]) - 2] + "gcode"

            _plugin._logger.debug("renaming [%s] to [%s]", payload["path"], renamed_file)

            _plugin._file_manager.remove_file(payload["target"], renamed_file)
            _plugin._file_manager.move_file(payload["target"], payload["path"], renamed_file)

            generate_metadata_for_file(_plugin, renamed_file, notify=False, force=True)

    # 'FileAdded'
    if event == Events.FILE_ADDED:
        generate_metadata_for_file(_plugin, payload["path"], notify=False, force=True)

    # 'FileSelected'
    if event == Events.FILE_SELECTED:
        generate_metadata_for_file(_plugin, payload["path"], notify=True)
        return

    if event == Events.FILE_DESELECTED:
        return

    return


def truthy(value):
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def parking_available(_plugin):
    """``(True, why)`` when the controller parks on a safety-door event (realtime 0x84), else ``(False, why not)``.

    Read from what was fetched at connect, with no query at pause time: Grbl's ``[OPT:`` letters (``$I``) and
    ``$22`` / ``$32`` (``$$``), FluidNC's ``$CD`` config. A ``$/parking/enable`` typed in the terminal is stale
    until the next connect or config save. ``P`` in a FluidNC ``[OPT:`` is always there (``PH``), so it is never
    consulted for FluidNC. Grbl_Esp32's OPT letters are assumed to mirror Grbl's (not verified).
    """
    if is_grbl_fluidnc(_plugin):
        config = _plugin.fluidYaml if isinstance(_plugin.fluidYaml, dict) else {}
        parking = config.get("parking") or {}
        if not truthy(parking.get("enable")):
            return False, "FluidNC parking: enable is not true"
        axis = str(parking.get("axis") or "Z").lower()
        homing = ((config.get("axes") or {}).get(axis) or {}).get("homing")
        try:
            homed = isinstance(homing, dict) and int(homing.get("cycle", 0)) >= 0
        except (TypeError, ValueError):
            homed = False
        if not homed:
            return False, f"FluidNC axis {axis} has no homing cycle, which parking needs"
        return True, f"FluidNC parking enabled on axis {axis}"

    letters = re.search(r"\[OPT:([^,\]\s]*)", _plugin.grblVersion or "")
    if letters is None or "P" not in letters.group(1):
        return False, "the firmware build has no parking ([OPT: without P)"
    try:
        homing = float((_plugin.grblSettings.get(22) or ["0"])[0]) != 0
    except (TypeError, ValueError):
        homing = False
    if not homing:
        return False, "homing is disabled ($22=0), which parking needs"
    return True, "Grbl build with parking and $22 homing enabled"


def pause_strategy(_plugin):
    """How this pause stops the machine: ``(strategy, reason)``, decided once at PAUSING.

    * ``laser``: laser mode (``$32``/``Grbl/LaserMode``): ``!`` alone, the controller switches the beam off while held.
    * ``park``: the parking setting is on and the controller can park: ``0x84``, firmware retract and restore.
    * ``hold``: no parking, setting says instant hold: ``!`` and the spindle-stop override, no Z lift.
    * ``drain``: no parking, the default: let the planner run dry, stop the spindle, lift Z; resume restores both.
    Pre-1.1 Grbl has neither feed-hold spindle control nor parking and always drains.
    """
    if not (is_grbl_one_dot_one(_plugin) or is_grbl_fluidnc(_plugin)):
        return "drain", "pre-1.1 Grbl cannot hold with the spindle or park"
    if is_laser_mode(_plugin):
        return "laser", "laser mode: ! alone"
    mode = "hold" if _plugin.pauseMode == "hold" else "drain"
    if _plugin.pauseParking:
        parks, why = parking_available(_plugin)
        if parks:
            return "park", why
        return mode, f"parking is enabled but unavailable: {why}"
    return mode, "parking is off"


_G_WORD = re.compile(r"G(\d+(?:\.\d+)?)")
_G_COMMENT = re.compile(r"\(.*?\)|;.*")
_F_WORD = re.compile(r"(?<![A-Z])F\s*(-?\d+(?:\.\d*)?|-?\.\d+)")
_S_WORD = re.compile(r"(?<![A-Z])S\s*(-?\d+(?:\.\d*)?|-?\.\d+)")


def track_modal_state(_plugin, cmd, job=True):
    """Follow the parser's distance mode, units and motion mode from a line as it is sent to the controller.

    OctoPrint suppresses status requests while a job runs, so the sent lines are the only live source. Only
    G-words that really change Grbl's parser state count: a ``$J=`` jog leaves it alone (its own G91 is local to
    the jog), G90.1/G91.1 are the arc-centre mode (a different modal group from G90/G91), and text inside a
    comment is not a word. Returns True when the distance mode was set, so the caller can tell the browser.

    The feed rate and the spindle speed / laser power are followed too, as the F and S words of the job's own
    lines, in the job's units and before any override is applied (``grblJobFeed``, ``grblJobPower``). ``job`` is
    False for a line that is not part of the job (a jog, a probe or framing move, the line an override change
    sends): those change the parser's modal state but are not what a pause has to put back, and one that went
    back through the sending hook would be overridden a second time. A status report's ``FS:`` feed is not a
    substitute: it is always mm/min (unless ``$13=1``) whatever G20/G21 says, and it carries the override.
    """
    line = cmd.upper().strip()
    if line.startswith("$"):
        return False

    distance = False
    code = _G_COMMENT.sub("", line)
    if job:
        feed = _F_WORD.findall(code)
        if feed:
            _plugin.grblJobFeed = float(feed[-1])
        power = _S_WORD.findall(code)
        if power:
            _plugin.grblJobPower = float(power[-1])
    for word in _G_WORD.findall(code):
        number = float(word)
        if number == 90 or number == 91:
            _plugin.positioning = int(number) - 90
            distance = True
        elif number == 20 or number == 21:
            _plugin.grblUnits = "G%d" % number
        elif number in (0, 1, 2, 3, 38.2, 38.3, 38.4, 38.5, 80):
            _plugin.grblMotionMode = "G%s" % (("%d" % number) if number == int(number) else str(number))
    return distance


def write_pause_resume_scripts(_plugin, strategy):
    scripts = os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode")
    pauseScript = os.path.realpath(os.path.join(scripts, "afterPrintPaused"))
    resumeScript = os.path.realpath(os.path.join(scripts, "beforePrintResumed"))

    laser = is_laser_mode(_plugin)

    if strategy in ("laser", "hold"):
        # The machine is already in feed hold and G-code (M5, a Z retract) would only be queued behind the
        # held planner, so the scripts hold nothing for the planner. Laser mode switches the laser off by
        # itself during a hold; for a spindle use Grbl's spindle-stop override, which only works while held.
        # The cycle start (~) restores the spindle and resumes; no modal state was changed, so none is restored.
        with open(pauseScript, 'w') as file:
            if strategy == "hold":
                file.write("TOGGLESPINDLE\n")
        with open(resumeScript, 'w') as file:
            file.write("~\n")
        return

    if strategy == "park":
        # The firmware retracts and stops the spindle by itself. Its cycle start is ignored until it reports
        # Door:0, so PARKRESUME (see resume_from_park) holds the ~ back until then.
        with open(pauseScript, 'w'):
            pass
        with open(resumeScript, 'w') as file:
            file.write("PARKRESUME\n")
        return

    # drain: OctoPrint has already waited for the planner to run dry (its M400 became a dwell), so these lines run on
    # an idle machine. No feed hold here: a `!` right behind the lift would stop it a few steps in and the lift would
    # only run after the resume.
    # the job's own power, as the feed below: grblPowerLevel carries the override, which the resume line gets again
    # A job that set no S of its own runs at the last known level, which already carries the power override (the
    # sending hook multiplied it); take the override back out so the resume line gets it once, not twice.
    pausedPower = _plugin.grblJobPower
    if not pausedPower:
        pausedPower = _plugin.grblPowerLevel / _plugin.powerRate if _plugin.powerRate else _plugin.grblPowerLevel
    pausedPositioning = _plugin.positioning
    # the job's own feed rate, in the job's units: _plugin.grblSpeed also takes the controller's FS: value, which is mm/min
    pausedSpeed = _plugin.grblJobFeed
    pausedGrblMCode = _plugin.grblMCode
    restoreDistanceMode = "G91\n" if pausedPositioning == 1 else "G90\n"
    # The lift and the plunge are 5 mm. Grbl reads the Z word in the job's units, so an inch job (G20) has to be
    # switched to millimetres around them, and back after.
    inches = getattr(_plugin, "grblUnits", "G21") == "G20"
    # Z moves leave Grbl in G0. G0 and G1 can be written back bare; G2/G3 cannot (error:26, no axis words), so a
    # job paused inside an arc mode gets no restore and relies on the arc lines repeating their G word.
    pausedMotionMode = getattr(_plugin, "grblMotionMode", "G0")
    restoreMotionMode = pausedMotionMode if pausedMotionMode in ("G0", "G1") else None
    # a bare G1 with no feed rate set is error:22; the feed rate is tracked from the sent lines and is 0 when it never was
    if restoreMotionMode == "G1" and not pausedSpeed > 0:
        restoreMotionMode = None
    if restoreMotionMode is None and pausedMotionMode in ("G2", "G3"):
        _plugin._logger.warning("pause: paused in %s, which cannot be restored without axis words; the next arc line must repeat it", pausedMotionMode)

    with open(pauseScript, 'w') as file:
        # turn off laser/spindle
        file.write("M5\n")
        if not laser:
            # retract Z 5 away from the work, then put the distance mode (and units) back: the Z move leaves Grbl in G91.
            # Away from the work is the jog's "up": +Z, or -Z with Invert Z on (see jog in __init__.py)
            if inches:
                file.write("G21\n")
            file.write(f"G91 G0 Z{5 * _plugin.invertZ}\n")
            file.write(restoreDistanceMode)
            if inches:
                file.write("G20\n")

    with open(resumeScript, 'w') as file:
        # reset our M-code
        file.write(f"{pausedGrblMCode}\n")
        # reset our spindle speed / power
        file.write(f"S{pausedPower}\n")
        # reset our feedrate
        file.write(f"F{pausedSpeed}\n")
        if not laser:
            # let the spindle come up to speed, move back down 5
            file.write("G4 P10\n")
            if inches:
                file.write("G21\n")
            file.write(f"G91 G0 Z{-5 * _plugin.invertZ}\n")
        # reset our positioning mode, the Z move above leaves Grbl in G91
        file.write(restoreDistanceMode)
        if not laser:
            if inches:
                file.write("G20\n")
            # the Z moves also left the motion mode at G0: a job line with no G word of its own would rapid
            if restoreMotionMode == "G1":
                file.write("G1\n")


def resume_from_park(_plugin, timeout=60.0):
    """The cycle start for a parked pause. The controller ignores ``~`` until its retract is complete and the
    door is closed (it reports ``Door:0``), so wait for that before sending it. Returns True when it was sent.

    ``Door:1`` (a real door switch still open) and ``Door:2`` (still retracting) keep waiting; ``Door:3`` means a
    restore is already running. Any other state is not a parked machine: a hold gets its ``~``, anything else
    (alarm, reset) has nothing to resume. The wait ends at a cancel, and after ``timeout`` the ``~`` goes out anyway
    (the controller drops it harmlessly if it is not ready), with a warning.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end and not _plugin.cancelling:
        # the controller's own word from a fresh report: grblState can be overwritten by a PrintResumed event meanwhile
        refresh_grbl_state(_plugin, 0.5)
        state = _plugin.grblReportedState.upper()
        if state in ("DOOR:1", "DOOR:2"):
            time.sleep(0.1)
            continue
        if state == "DOOR:3":
            return False
        if state != "DOOR:0" and not state.startswith("HOLD"):
            _plugin._logger.info("resume: controller is [%s], not parked: no cycle start sent", _plugin.grblReportedState)
            return False
        moved = _plugin.grblZ - _plugin.parkStartZ
        if state == "DOOR:0":
            if moved:
                _plugin._logger.info("resume: parking moved Z by %.3f to %.3f", moved, _plugin.grblZ)
            else:
                _plugin._logger.warning("resume: parking did not move Z (still %.3f): the tool was already above the "
                                        "controller's parking target, or parking is not active", _plugin.grblZ)
        send_realtime(_plugin, b"~")
        schedule_status_refresh(_plugin)
        return True

    if _plugin.cancelling:
        return False
    _plugin._logger.warning("resume: no Door:0 within %.0fs (state [%s]), sending the cycle start anyway", timeout, _plugin.grblReportedState)
    send_realtime(_plugin, b"~")
    schedule_status_refresh(_plugin)
    return True


def finish_pause_hold(_plugin):
    """Let OctoPrint finish pausing a machine that is in feed hold.

    A planner that is full never accepts the line OctoPrint last sent while it is held, so that line's `ok`
    never comes and OctoPrint's send loop (which would send the position request, then the pause script)
    waits for it. Grbl keeps what it already holds, so acknowledging on its behalf only lets the next
    commands queue up behind it.
    """
    while _plugin._printer.is_pausing() and _plugin.pauseHoldActive:
        time.sleep(0.5)
        if _plugin._printer.is_pausing() and _plugin.pauseHoldActive:
            _plugin._logger.debug("_bgs: finish_pause_hold: acknowledging for the held planner")
            fake_ack(_plugin)


def fake_ack(_plugin):
    printer = _plugin._printer
    # renamed in OctoPrint 2.0; the old name is only a deprecation shim there
    (getattr(printer, "repair_communication", None) or printer.fake_ack)()


def cancel_motion(_plugin):
    """Stop motion for a cancel: feed hold, wait until the machine has stopped, then soft reset.

    The reset flushes the planner (so buffered moves stop cutting) and switches off the spindle and laser.
    Resetting while still decelerating raises ALARM:3 and loses position, resetting after the hold has
    completed does not; hence the wait. The decision is made on fresh status reports, never on grblState as
    cached, which can still say Run when the machine was paused seconds ago.
    """
    _plugin._logger.debug("_bgs: cancel_motion")

    # an alarmed machine ignores a hold (a critical alarm ignores status requests too): nothing to wait for
    alarmed = _plugin.grblState.upper() == "ALARM"

    send_realtime(_plugin, b"!")
    if not alarmed:
        set_grbl_state(_plugin, "Hold:1")

    # a parked pause (Door:2 retracting, Door:3 restoring) is still moving: a reset during a parking motion is an
    # aborted cycle (ALARM:3), so give it longer than a plain deceleration
    started = time.monotonic()
    limit = 0 if alarmed else 10
    in_door = False
    while time.monotonic() < started + limit:
        if refresh_grbl_state(_plugin, 0.5):
            state = _plugin.grblState.upper()
            if state in ("DOOR:2", "DOOR:3"):
                limit = 30
                in_door = True
            elif in_door and state in ("RUN", "HOLD:1"):
                # the controller ignores `!` while in a door state, and when its restore finishes it resumes the
                # held job by itself: hold again, or the cancel would wait for the planner to run dry
                in_door = False
                send_realtime(_plugin, b"!")
            if state not in ("RUN", "JOG", "HOLD:1", "HOME", "DOOR:2", "DOOR:3"):
                break
        time.sleep(0.1)
    else:
        if not alarmed:
            _plugin._logger.warning("cancel: machine did not report a completed hold within %ds, resetting anyway", limit)

    send_realtime(_plugin, b"\x18")

    # a reset restores Grbl's default modal state and discards whatever was outstanding
    _plugin.positioning = 0
    _plugin.grblUnits = "G21"
    _plugin.grblMotionMode = "G0"
    _plugin.grblJobFeed = float(0)
    _plugin.grblJobPower = float(0)
    _plugin.pendingDistanceRestore = None
    _plugin.grblMCode = "M5"
    _plugin.lastRequest = []
    _plugin.lastResponse = ""
    _plugin.grblCmdQueue.clear()
    queue_cmds_and_send(_plugin, ["$G"])

    # leftovers of the resume script may still be queued in OctoPrint: let them be dropped, then take the
    # state from the controller (a reset can leave it in Alarm, e.g. with homing required)
    time.sleep(1.0)
    _plugin.cancelling = False
    _plugin.cancelMotionActive = False
    refresh_grbl_state(_plugin)


def do_framing(_plugin, data):
    _plugin._logger.debug(f"_bgs: do_framing data=[{data}]")

    origin = data.get("origin").strip()
    length = float(data.get("length")) * _plugin.invertY
    width = float(data.get("width")) * _plugin.invertX

    send_frame_init_gcode(_plugin)

    if (origin == "grblTopLeft"):
        send_bounding_box_upper_left(_plugin, length, width)

    if (origin == "grblTopCenter"):
        send_bounding_box_upper_center(_plugin, length, width)

    if (origin == "grblTopRight"):
        send_bounding_box_upper_right(_plugin, length, width)

    if (origin == "grblCenterLeft"):
        send_bounding_box_center_left(_plugin, length, width)

    if (origin == "grblCenter"):
        send_bounding_box_center(_plugin, length, width)

    if (origin == "grblCenterRight"):
        send_bounding_box_center_right(_plugin, length, width)

    if (origin == "grblBottomLeft"):
        send_bounding_box_lower_left(_plugin, length, width)

    if (origin == "grblBottomCenter"):
        send_bounding_box_lower_center(_plugin, length, width)

    if (origin == "grblBottomRight"):
        send_bounding_box_lower_right(_plugin, length, width)

    send_frame_end_gcode(_plugin)

def send_frame_init_gcode(_plugin):
    _plugin._logger.debug("_bgs: send_frame_init_gcode")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    # cancel jog if grbl 1.1+is_grbl_one_dot_one
    if is_grbl_one_dot_one(_plugin) and is_latin_encoding_available(_plugin):
        send_command_now(_plugin._printer, _plugin._logger, "CANCELJOG")

    # Linear mode, feedrate f% of max
    _plugin._printer.commands(f"G1 F{f}")

    # turn on laser in weak mode if laser mode enabled
    if is_laser_mode(_plugin):
        _plugin._printer.commands(f"M3 S{_plugin.weakLaserValue}")

    start_jog_guard(_plugin)

def send_frame_end_gcode(_plugin):
    _plugin._logger.debug("_bgs: send_frame_end_gcode")

    queue_cmds_and_send(_plugin, ["?", "?", "?"])
    queue_cmds_and_send(_plugin, ["M5 S0 G0"])

def send_bounding_box_upper_left(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_upper_left y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ",x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))

def send_bounding_box_upper_center(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_upper_center y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, f))

def send_bounding_box_upper_right(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_upper_right y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))

def send_bounding_box_center_left(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_center_left y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2, f))

def send_bounding_box_center(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_center y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, y / 2, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, y / 2 * -1, f))

def send_bounding_box_center_right(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_center_right y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2 * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2 * -1, f))

def send_bounding_box_lower_left(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_lower_left y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))

def send_bounding_box_lower_center(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_lower_center y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, f))

def send_bounding_box_lower_right(_plugin, y, x):
    _plugin._logger.debug(f"_bgs: send_bounding_box_lower_right y=[{y}] x=[{x}]")

    f = max(get_axes_max_rates(_plugin)) * (float(_plugin.framingPercentOfMaxSpeed) * .01)

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))


def toggle_weak(_plugin):
    _plugin._logger.debug("_bgs: toggle_weak")

    # only execute if laser mode enabled
    if not is_laser_mode(_plugin):
        return

    f = max(get_axes_max_rates(_plugin))

    if _plugin.grblPowerLevel == 0:
        # turn on laser in weak mode
        _plugin._printer.commands(f"G1 F{f} M3 S{_plugin.weakLaserValue:.2f}")
        add_notifications(_plugin, ["Weak laser enabled"])
        res = "Laser Off"
    else:
        _plugin._printer.commands(["M3 S0", "M5", "G0"])
        add_notifications(_plugin, ["Weak laser disabled"])
        res = "Weak Laser"

    return res


def process_grbl_status_msg(_plugin, msg):
    # Z, A and B are optional -- 2-axis machines (e.g. lasers) report only X and Y
    match = re.search(r'<(-?[^,]+)[,|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+)(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?', msg)
    if match is None:
        _plugin._logger.debug("unrecognized status report: %s", msg)
        return msg

    state, x, y, z, a, b = match.groups()
    # the controller reports one position value per axis it has
    _plugin.hasZ = z is not None
    if z is None:
        z = "0.000"

    response = f'X:{x} Y:{y} Z:{z} E:0 {msg}'

    _plugin.grblMode = "MPos" if "MPos" in msg else "WPos" if "WPos" in msg else "N/A"
    
    reported = str(state)
    if _plugin.jogGuardUntil:
        if reported.upper().startswith("JOG"):
            _plugin.jogSeen = True
        elif reported.upper() == "IDLE" and not _plugin.jogSeen and time.monotonic() < _plugin.jogGuardUntil:
            reported = "Jog"
        else:
            _plugin.jogGuardUntil = 0

    if _plugin.is_printing and _plugin.grblState == "Idle":
        _plugin.grblState = "Run"
    else:
        _plugin.grblState = reported

    _plugin.grblReportedState = str(state)
    _plugin.grblStatusSeq += 1
    # a queued status request is answered: let the command queue go on
    _plugin.grblQueueHold = 0
    release_queued_command(_plugin)

    if _plugin.grblState == "Idle" and _plugin.M9DelayActive:
        _plugin._logger.debug('Turning OFF Air Assist')
        subprocess.Popen(_plugin.m9Command, shell=True)
        _plugin.M9DelayActive = False   
        _plugin.coolant = "M9"     

    _plugin.grblX = float(x)
    _plugin.grblY = float(y)
    _plugin.grblZ = float(z)

    if b is not None:
        _plugin.grblA = float(a)
        _plugin.grblB = float(b)
    elif a is not None:
        # a lone 4th axis is B when the machine is configured with B
        if _plugin.hasB:
            _plugin.grblB = float(a)
        else:
            _plugin.grblA = float(a)

    match = re.search(r'.*\|Pn:([A-Z]+)', msg)
    if match is not None:
        _plugin.grblActivePins = match.groups(1)[0]
    else:
        _plugin.grblActivePins = "None"

    match = re.search(r'.*\|FS:(-?[\d\.]+),(-?[\d\.]+)', msg)
    if match is not None:
        _plugin.grblSpeed = round(float(match.groups(1)[0]))
        _plugin.grblPowerLevel = float(match.groups(1)[1])

    # _plugin._logger.info("status received: printing=[%s] mode=[%s] state=[%s] x=[%.3f] y=[%.3f] z=[%.3f] a=[%.3f] b=[%.3f] pins=[%s] speed=[%d] power=[%.2f]",
    #                     "yes" if _plugin.is_printing else "no",
    #                     _plugin.grblMode,
    #                     _plugin.grblState,
    #                     _plugin.grblX,
    #                     _plugin.grblY,
    #                     _plugin.grblZ,
    #                     _plugin.grblA,
    #                     _plugin.grblB,
    #                     _plugin.grblActivePins,
    #                     _plugin.grblSpeed,
    #                     _plugin.grblPowerLevel)
    
    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state",
                                                                    mode=_plugin.grblMode,
                                                                    state=_plugin.grblState,
                                                                    x=_plugin.grblX,
                                                                    y=_plugin.grblY,
                                                                    z=_plugin.grblZ,
                                                                    a=_plugin.grblA,
                                                                    b=_plugin.grblB,
                                                                    pins=_plugin.grblActivePins,
                                                                    hasZ=_plugin.hasZ,
                                                                    speed=_plugin.grblSpeed,
                                                                    power=_plugin.grblPowerLevel,
                                                                    coord=_plugin.grblCoordinateSystem,
                                                                    coolant=_plugin.coolant,
                                                                    positioning=_plugin.positioning))

    # add a notification if we just homed
    if _plugin.grblState.upper() == "HOME":
        add_notifications(_plugin, ["Machine has been homed"])

    # the only thing useful remaining is buffer stats
    # and we currently don't do anything with it
    # for stat in msg.replace("<", "").replace(">", "").split("|"):
    #     # buffer stats and Pin stats
    #     if stat.startswith("Bf:") or stat.startswith("Pn:"):
    #         _plugin.add_to_notify_queue(stat)

    return response


def process_grbl_alarm(_plugin, msg):
    error = 0
    desc = msg

    match = re.search(r'alarm:\ *(-?[\d.]+)', msg.lower())
    if match is not None:
        error = int(match.groups(1)[0])

        desc = _plugin.grblAlarms.get(error)
        if desc is None:
            desc = f"Grbl Alarm #{error} - Alarm description not available"

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                    title=f"Grbl Alarm #{error} Received",
                                                                    text=html.escape(desc),
                                                                    hide=True,
                                                                    delay=10000,
                                                                    notify_type="notice"))

    _plugin._logger.warning("alarm received: %d: %s", error, desc)

    # an updateGrblSetting request cannot be answered by a machine in alarm
    resolve_setting_waiter(_plugin, False, f"Grbl alarm {error}: {desc}")

    # inform _bgs in case it has something going on (probing); the controller now rejects G-code until $X
    grbl_alarm_or_error_occurred(_plugin, locked=True)

    # a critical alarm (1, 2) silences Grbl, status requests included, until it is reset: no report will correct this
    set_grbl_state(_plugin, "Alarm")

    # clear out any pending queued Commands
    if len(_plugin.grblCmdQueue) > 0:
        _plugin._logger.debug("clearing %d commands from the command queue", len(_plugin.grblCmdQueue))
        _plugin.grblCmdQueue.clear()

    # put a message on our notification queue and force an inquiry
    add_notifications(_plugin, [desc])
    _plugin._printer.commands("?")

    # we need to pause if we are printing
    if _plugin._printer.is_printing():
        _plugin._printer.pause_print()

    # our sync buffers are no longer reliable
    _plugin.lastRequest = []
    _plugin.lastResponse = ""

    # return 'Error: ' + desc
    return "ok " + desc


def drop_errored_request(_plugin):
    """An error is the complete answer to one line. When that line was a tracked one, its entry is the oldest in
    lastRequest and the partial response collected so far is not its own: retire both."""
    if _plugin.lastRequest:
        _plugin.lastRequest.pop(0)
    _plugin.lastResponse = ""


def process_grbl_error(_plugin, msg):
    error = 0
    desc = msg

    match = re.search(r'error:\ *(-?[\d.]+)', msg.lower())
    if match is not None:
        error = int(match.groups(1)[0])

        desc = _plugin.grblErrors.get(error)
        if desc is None:
            desc = f"Grbl Error #{error} - Error description not available"

    # an updateGrblSetting request is waiting for this answer: it reports the error itself
    if resolve_setting_waiter(_plugin, False, f"Grbl error {error}: {desc}"):
        return "ok "

    # suppress the errors that answer the connect handshake, but nothing the user asked for
    if time.monotonic() - _plugin.whenConnected < 20 and not _plugin.userCommandSinceConnect:
        # the error is that handshake line's whole answer: retire its tracked request, or it stays at the head of
        # the FIFO and every later reply is filed under the wrong command
        drop_errored_request(_plugin)
        return "ok "

    # lets not deal with file not found
    if error == 65:
        return "ok "

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                    title=f"Grbl Error #{error} Received",
                                                                    text=html.escape(desc),
                                                                    hide=True,
                                                                    delay=10000,
                                                                    notify_type="error"))
    _plugin._logger.warning("error received: %d: %s", error, desc)

    # inform _bgs in case it has something going on (probing)
    grbl_alarm_or_error_occurred(_plugin)

    # # clear out any pending queued Commands
    # if len(_plugin.grblCmdQueue) > 0:
    #     _plugin._logger.debug("clearing %d commands from the command queue", len(_plugin.grblCmdQueue))
    #     _plugin.grblCmdQueue.clear()

    # our sync buffers are no longer reliable
    _plugin.lastRequest = []
    _plugin.lastResponse = ""

    # put a message on our notification queue and force an inquiry
    add_notifications(_plugin, [desc])
    # _plugin._printer.commands("?")

    # we need to pause if we are printing
    if _plugin._printer.is_printing():
        _plugin._printer.pause_print()

    # don't tell octoprint because it will freak out
    return "ok "

def process_parser_status_msg(_plugin, msg):
    parserState = msg.replace("[", "").replace("]", "").replace("GC:", "")

    for state in parserState.split(" "):
        if state in ("G90", "G91"):
            _plugin.positioning = int(state[2:3])
            _plugin._logger.debug("parser state indicates [%s] distance mode", "absolute" if _plugin.positioning == 0 else "relative")

        elif state in ("G0", "G1", "G2", "G3", "G38.2", "G38.3", "G38.4", "G38.5", "G80"):
            _plugin.grblMotionMode = state
            _plugin._logger.debug("parser state indicates [%s] motion mode", state)
        elif state in ("G54", "G55", "G56", "G57", "G58", "G59"):
            _plugin.grblCoordinateSystem = state
            _plugin._logger.debug("parser state indicates [%s] coordinate system active", _plugin.grblCoordinateSystem)
        elif state in ("G17", "G18", "G19"):
            _plugin._logger.debug("parser state indicates [%s] plane selected", state)
        elif state in ("G20", "G21"):
            _plugin.grblUnits = state
            _plugin._logger.debug("parser state indicates [%s] uom active", "metric" if state == "G21" else "imperial")
        elif state in ("G93", "G94"):
            _plugin._logger.debug("parser state indicates [%s] feed rate mode", state)
        elif state in ("M3", "M4", "M5"):
            _plugin._logger.debug("parser state indicates [%s] spindle state", state)
        elif state in ("M7", "M8", "M9"):
            if state.upper() != _plugin.coolant and not _plugin.M9DelayActive:
                _plugin.coolant = state.upper()
                # M8 (air assist on) processing - work in progress
                if _plugin.coolant in ("M7", "M8") and _plugin.overrideM8:
                        _plugin.M9DelayActive = False
                        _plugin._logger.debug('Turning ON Air Assist')
                        subprocess.Popen(_plugin.m8Command, shell=True)
                # M9 (air assist off) processing - work in progress
                if _plugin.coolant == "M9" and _plugin.overrideM9:
                        if not _plugin.delayM9:
                            _plugin._logger.debug('Turning OFF Air Assist')
                            subprocess.Popen(_plugin.m9Command, shell=True)
                        else:
                            _plugin.coolant = "M8"  # revert coolant state
                            _plugin._logger.debug('Delaying OFF Air Assist command until Idle state detected')
                            _plugin.M9DelayActive = True
            _plugin._logger.debug("parser state indicates [%s] coolant state", state)
        elif state.startswith("F"):
            _plugin.grblSpeed = round(float(state.replace("F", "")))
            _plugin._logger.debug("parser state indicates feed rate of [%d]", _plugin.grblSpeed)
        elif state.startswith("S"):
            _plugin.grblPowerLevel = float(state.replace("S", ""))
            _plugin._logger.debug("parser state indicates spindle speed of [%f]", _plugin.grblPowerLevel)
        elif state.startswith("T"):
            _plugin._logger.debug("parser state indicates tool #[%s] active", state.replace("T", ""))

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state",
                                                                        speed=_plugin.grblSpeed,
                                                                        power=_plugin.grblPowerLevel,
                                                                        coord=_plugin.grblCoordinateSystem,
                                                                        coolant=_plugin.coolant,
                                                                        positioning=_plugin.positioning))


def do_xyz_probe(_plugin, sessionId):
    # we need something in the background to track this
    threading.Thread(target=defer_do_xyz_probe, args=(_plugin, sessionId)).start()

def defer_do_xyz_probe(_plugin, sessionId):
    global zProbe
    global xyProbe
    global zProbeAborted

    # the Z probe hands the mode and the units back, but its restore may still be queued when the X/Y probe starts:
    # take them now
    priorPositioning = _plugin.positioning
    priorUnits = _plugin.grblUnits

    zProbeAborted = False
    do_simple_zprobe(_plugin, sessionId)

    # wait for the z probe to run out of scope
    while zProbe is not None:
        time.sleep(1)

    # a Z probe that alarmed or was cancelled leaves nothing to start an X/Y probe on
    if zProbeAborted:
        _plugin._logger.debug("_bgs: defer_do_xyz_probe z probe aborted, not starting the X/Y probe")
        return

    do_xy_probe(_plugin, "XY", sessionId, priorPositioning, priorUnits)


def do_xy_probe(_plugin, axes, sessionId, priorPositioning=None, priorUnits=None):
    global xyProbe
    _plugin._logger.debug("_bgs: do_xy_probe step=[{}] axes=[{}] sessionId=[{}]".format(xyProbe._step if xyProbe is not None else "N/A", axes, sessionId))

    frameOrigin = _plugin._settings.get(["frame_origin"])

    # do we do not support xy probe for center origins
    if "Center" in frameOrigin:
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                         sessionId=sessionId,
                                                                             title="X/Y Probe",
                                                                              text="You must select a <i>Material Framing</i> corner <b>Starting Position</b> to perform an X and/or Y axis probe.",
                                                                              hide=False,
                                                                             delay=0,
                                                                       notify_type="notice"))
        return

    if xyProbe is None:
        xyProbe = XyProbe(_plugin, xy_probe_hook, axes, sessionId)
        # the probe runs in relative mode (G91) and millimetres (G21): remember what it must hand back
        xyProbe._priorPositioning = _plugin.positioning if priorPositioning is None else priorPositioning
        xyProbe._priorUnits = _plugin.grblUnits if priorUnits is None else priorUnits
        if axes == "Y":
            xyProbe._step = 0

    xyProbeTravel = float(_plugin._settings.get(["xyProbeTravel"]))

    xf, yf, zf = get_axes_max_rates(_plugin)
    xyf = min([xf, yf]) * (_plugin.framingPercentOfMaxSpeed * .01)
    zf = zf * (_plugin.framingPercentOfMaxSpeed * .01)

    originInvert = -1 if "Left" in frameOrigin else 1
    distance = xyProbeTravel * _plugin.invertX * originInvert

    gcode = [
                "G21",
                f"G0 G91 X{distance} F{xyf}",
                f"G0 G91 Z{15 * _plugin.invertZ * -1} F{zf}",
                f"G38.2 X{distance * -1} F200"
            ]
    axis = "X"

    if xyProbe._step == 0 and axes != "X":
        originInvert = -1 if "Bottom" in frameOrigin else 1
        distance = xyProbeTravel * _plugin.invertY * originInvert

        gcode = [
                    "G21",
                    f"G0 G91 Y{distance} F{xyf}",
                    f"G0 G91 Z{15 * _plugin.invertZ * -1} F{zf}",
                    f"G38.2 Y{distance * -1} F200"
                ]
        axis = "Y"

    elif len(xyProbe._results) > 1 or (len(xyProbe._results) > 0 and axis in ("X", "Y")):
        restore = "G91" if xyProbe._priorPositioning == 1 else "G90"
        units = xyProbe._priorUnits
        if axes == "XY":
            text = f"X/Y Axis Home has been calculated and set to machine position: X[<B>{xyProbe._results[0]:.3f}</B>] Y[<B>{xyProbe._results[1]:.3f}</B>]"
            _plugin._printer.commands([f"G0 G90 X0 Y0 F{xyf}", restore, units])
        else:
            text = f"{axes} Axis Home has been calculated and set to machine position: [<B>{xyProbe._results[0]:.3f}</B>]]"
            _plugin._printer.commands([f"G0 G90 {axes}0 F{xyf}", restore, units])

        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                         sessionId=xyProbe._sessionId,
                                                                             title="X/Y Probe",
                                                                              text=text,
                                                                              hide=False,
                                                                             delay=0,
                                                                       notify_type="info"))

        add_notifications(_plugin, [text.replace("<B>", "").replace("</B>", "")])

        xyProbe.teardown()
        xyProbe = None
        return
    elif xyProbe._step != -1:
        xyProbe.teardown()
        xyProbe = None
        return

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="xy_probe",
                                                                     sessionId=xyProbe._sessionId,
                                                                          axis=axis,
                                                                          axes=axes,
                                                                          step=xyProbe._step,
                                                                         gcode=gcode))

def xy_probe_hook(_plugin, result, position, axis):
    global xyProbe
    _plugin._logger.debug(f"_bgs: xy_probe_hook result=[{result}] position=[{position}] axis=[{axis}] sessionId=[{xyProbe._sessionId}]")

    # did we have a problem?
    if result == 0:
        grbl_alarm_or_error_occurred(_plugin)
        return

    notification = f"X/Y Probe: [{axis}] axis result [{position:.3f}]"
    add_notifications(_plugin, [notification])

    # defer commands and setup of the next step
    threading.Thread(target=defer_do_xy_probe, args=(_plugin, position, axis, xyProbe._sessionId)).start()

def defer_do_xy_probe(_plugin, position, axis, sessionId):
    global xyProbe
    _plugin._logger.debug(f"_bgs: defer_do_xy_probe sessionId=[{sessionId}]")

    _plugin.grblCmdQueue.append("%%% eat me %%%")
    _plugin._printer.commands("?")
    wait_for_empty_cmd_queue(_plugin)
    if xyProbe is None:
        return

    xf, yf, zf = get_axes_max_rates(_plugin)
    xyf = min([xf, yf]) * (_plugin.framingPercentOfMaxSpeed * .01)
    zf = zf * (_plugin.framingPercentOfMaxSpeed * .01)

    frameOrigin = _plugin._settings.get(["frame_origin"])
    originInvert = -1 if "Left" in frameOrigin else 1
    invert = _plugin.invertX

    if axis == "Y":
        originInvert = -1 if "Bottom" in frameOrigin else 1
        invert = _plugin.invertY

    program = int(float(_plugin.grblCoordinateSystem.replace("G", "")))
    program = -53 + program

    # set home for our current axis and travel back to where we started, then back to the distance mode the probe
    # found (the next step asks for G91 itself)
    _plugin._printer.commands([
            f"G10 P{program} L2 {axis}{position:f}",
            f"G0 {axis}{10 * originInvert * invert} Z{15 * _plugin.invertZ} F{zf}",
            f"G0 G90 {axis}{10 * originInvert * invert * -1} F{xyf}",
            "G91" if xyProbe._priorPositioning == 1 else "G90"
        ])

    # update our offsets
    _plugin.offsets[_plugin.grblCoordinateSystem][axis.lower()] = position

    do_xy_probe(_plugin, xyProbe._axes, sessionId)


def do_simple_zprobe(_plugin, sessionId):
    _plugin._logger.debug(f"_bgs: do_simple_zprobe sessionId=[{sessionId}]")

    global zProbe

    if zProbe is not None:
        zProbe.teardown()
        zProbe = None

    zProbe = ZProbe(_plugin, simple_zprobe_hook, sessionId)
    # the probe runs in relative mode (G91) and millimetres (G21): remember what it must hand back
    zProbe._priorPositioning = _plugin.positioning
    zProbe._priorUnits = _plugin.grblUnits

    xl, yl, zl = get_axes_limits(_plugin)
    zTravel = zl if _plugin.zProbeTravel == 0 else _plugin.zProbeTravel
    zTravel = zTravel * -1 * _plugin.invertZ

    gcode = f"G91 G21 G38.2 Z{zTravel} F100"
    zProbe._locations = [{"gcode": gcode,  "action": "simple_zprobe", "location": "Current"}]

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_zprobe",
                                                                     sessionId=zProbe._sessionId,
                                                                         gcode=gcode))

def simple_zprobe_hook(_plugin, result, position):
    global zProbe
    _plugin._logger.debug(f"_bgs: simple_zprobe_hook result=[{result}] position=[{position}] sessionId=[{zProbe._sessionId}]")

    sessionId = zProbe._sessionId

    type = ""
    title = ""
    text = ""
    notify_type = ""

    z0 = position + _plugin.zProbeOffset * _plugin.invertZ * -1

    # no contact (a probe that raises ALARM:5 is already torn down; this is the one that does not): stop and hand
    # the distance mode back instead of waiting on a result that will not come
    if result == 0:
        grbl_alarm_or_error_occurred(_plugin)
        return

    if result == 1:
        # defer commands because we are out of sync
        threading.Thread(target=defer_simple_z_probe, args=(_plugin, z0)).start()

        type="simple_notify"
        title="Single Point Z-Probe"
        text = f"Z Axis Home has been calculated and set to machine position: [<B>{z0:.3f}</B>]"
        notify_type="info"

        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type=type,
                                                                         sessionId=sessionId,
                                                                             title=title,
                                                                              text=text,
                                                                              hide=False,
                                                                             delay=0,
                                                                       notify_type=notify_type))

        add_notifications(_plugin, [text.replace("<B>", "").replace("</B>", "")])

    _plugin._logger.debug("zprobe hook position: [%f] result: [%d]", position, result)

def defer_simple_z_probe(_plugin, z0):
    global zProbe

    _plugin.grblCmdQueue.append("%%% eat me %%%")
    _plugin._printer.commands("?")
    wait_for_empty_cmd_queue(_plugin)

    program = int(float(_plugin.grblCoordinateSystem.replace("G", "")))
    program = -53 + program

    # the retract is relative; then back to the distance mode the probe found
    restore = "G91" if zProbe is not None and zProbe._priorPositioning == 1 else "G90"
    units = zProbe._priorUnits if zProbe is not None else "G21"
    _plugin._printer.commands(["G91", "G21", f"G10 P{program} L2 Z{z0:f}", f"G0 Z{_plugin.zProbeEndPos * _plugin.invertZ}", restore, units])

    # update our offsets
    _plugin.offsets[_plugin.grblCoordinateSystem]["z"] = z0

    zProbe.teardown()
    zProbe = None

def do_multipoint_zprobe(_plugin, sessionId):
    global zProbe
    _plugin._logger.debug(f"_bgs: do_multipoint_zprobe step=[{zProbe._step + 1 if zProbe is not None else 0}] sessionId=[{sessionId}]")

    if zProbe is None:
        zProbe = ZProbe(_plugin, multipoint_zprobe_hook, sessionId)
        # every move runs in relative mode (G91) and millimetres (G21): remember what it must hand back
        zProbe._priorPositioning = _plugin.positioning
        zProbe._priorUnits = _plugin.grblUnits

    zProbe._step+=1

    if zProbe._step == 0:
        origin = _plugin._settings.get(["frame_origin"])
        width = float(_plugin._settings.get(["frame_width"])) * _plugin.invertX
        length = float(_plugin._settings.get(["frame_length"])) * _plugin.invertY
        preamble = "$J=" if is_grbl_one_dot_one(_plugin) else "G1 "

        xl, yl, zl = get_axes_limits(_plugin)
        zTravel = zl if _plugin.zProbeTravel == 0 else _plugin.zProbeTravel
        zTravel = zTravel * -1 * _plugin.invertZ

        xf, yf, zf = get_axes_max_rates(_plugin)
        feedrate = min([xf, yf]) * (_plugin.framingPercentOfMaxSpeed * .01)

        if origin == "grblTopLeft":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width:f} F{feedrate}", "action": "move", "location": "Top Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Right"},
                                    {"gcode": f"{preamble}G91 G21 Y{length * -1:f} F{feedrate}", "action": "move", "location": "Bottom Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width * -1:f} F{feedrate}", "action": "move", "location": "Bottom Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Left"},
                                ]
        elif origin == "grblTopCenter":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Center"},
                                ]
        elif origin == "grblTopRight":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Right"},
                                    {"gcode": f"{preamble}G91 G21 Y{length * -1:f} F{feedrate}", "action": "move", "location": "Bottom Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width * -1:f} F{feedrate}", "action": "move", "location": "Bottom Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": f"{preamble}G91 G21 Y{length:f} F{feedrate}", "action": "move", "location": "Top Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Right"},
                                ]
        elif origin == "grblCenterLeft":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center Left"},
                                ]
        elif origin == "grblCenter":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} F{feedrate}", "action": "move", "location": "Top Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} F{feedrate}", "action": "move", "location": "Top Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Right"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Right"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} F{feedrate}", "action": "move", "location": "Center"},
                                ]
        elif origin == "grblCenterRight":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Center"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} F{feedrate}", "action": "move", "location": "Center Right"},
                                ]
        elif origin == "grblBottomLeft":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": f"{preamble}G91 G21 Y{length:f} F{feedrate}", "action": "move", "location": "Top Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width:f} F{feedrate}", "action": "move", "location": "Top Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Right"},
                                    {"gcode": f"{preamble}G91 G21 Y{length * -1:f} F{feedrate}", "action": "move", "location": "Bottom Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Left"},
                                ]
        elif origin == "grblBottomCenter":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Center Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2:f} F{feedrate}", "action": "move", "location": "Top Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Center"},
                                ]
        elif origin == "grblBottomRight":
            zProbe._locations = [
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width * -1:f} F{feedrate}", "action": "move", "location": "Bottom Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": f"{preamble}G91 G21 Y{length:f} F{feedrate}", "action": "move", "location": "Top Left"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Left"},
                                    {"gcode": f"{preamble}G91 G21 X{width:f} F{feedrate}", "action": "move", "location": "Top Right"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Top Right"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2 * -1:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Center"},
                                    {"gcode": f"G91 G21 G38.2 Z{zTravel} F100",  "action": "probe", "location": "Center"},
                                    {"gcode": f"{preamble}G91 G21 X{width / 2:f} Y{length / 2 * -1:f} F{feedrate}", "action": "move", "location": "Bottom Right"},
                                ]
        else:
            # we shouldn't be here
            zProbe.teardown()
            zProbe = None
            return
    else:
        if zProbe._step > len(zProbe._locations) - 1:
            positionTuple = zProbe.resultByCalc(_plugin._settings.get(["zprobeCalc"]))

            position = positionTuple[0]
            location = positionTuple[1]

            program = int(float(_plugin.grblCoordinateSystem.replace("G", "")))
            program = -53 + program

            queue_cmds_and_send(_plugin, [f"G10 P{program} L2 Z{position:f}"] + probe_restore_commands(zProbe._priorPositioning, zProbe._priorUnits))

            # update our offsets
            _plugin.offsets[_plugin.grblCoordinateSystem]["z"] = position

            text = "Z Axis Home has been calculated and set to machine position: [<B>{:.3f}</B>] ({})\r\n\r\n Result Details:\r\n\r\nVariance: {:.3f}mm\r\n\r\nHighest Point: {:.3f} ({})\r\nLowest Point: {:.3f} ({})\r\nMean Point: {:.3f}\r\nComputed Average: {:.3f}".format(
                position,
                location,
                zProbe.resultByCalc("GAP")[0],
                zProbe.resultByCalc("MIN")[0], zProbe.resultByCalc("MIN")[1],
                zProbe.resultByCalc("MAX")[0], zProbe.resultByCalc("MAX")[1],
                zProbe.resultByCalc("MEAN")[0],
                zProbe.resultByCalc("AVG")[0]
            )
            _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                             sessionId=zProbe._sessionId,
                                                                                 title="Multipoint Z-Probe",
                                                                                  text=text,
                                                                                  hide=False,
                                                                                 delay=0,
                                                                           notify_type="info"))

            add_notifications(_plugin, [text.replace("<B>", "").replace("</B>", "")])

            zProbe.teardown()
            zProbe = None
            return

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="multipoint_zprobe",
                                                                     sessionId=zProbe._sessionId,
                                                                   instruction=zProbe.getCurrentLocation()))

def multipoint_zprobe_hook(_plugin, result, position):
    global zProbe
    _plugin._logger.debug(f"_bgs: multipoint_zprobe_hook result=[{result}] position=[{position}] sessionId=[{zProbe._sessionId}]")

    # did we have a problem?
    if result == 0:
        grbl_alarm_or_error_occurred(_plugin)
        return
    else:
        location = zProbe.getCurrentLocation()['location']
        notification = f"Z-Probe [{location}] location result [{position:.3f}]"
        add_notifications(_plugin, [notification])

        # max z feed rate -- we'll do 50% of it
        xf, yf, zf = get_axes_max_rates(_plugin)
        zf = round(zf * .5)
        _plugin._printer.commands("{}G91 G21 Z{} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G0 ", _plugin.zProbeEndPos, zf))

    # defer setup of the next step
    threading.Thread(target=defer_do_multipoint_zprobe, args=(_plugin, zProbe._sessionId)).start()

def defer_do_multipoint_zprobe(_plugin, sessionId):
    global zProbe
    _plugin._logger.debug(f"_bgs: defer_do_multipoint_zprobe sessionId=[{sessionId}]")

    _plugin.grblCmdQueue.append("%%% eat me %%%")
    _plugin._printer.commands("?")
    wait_for_empty_cmd_queue(_plugin)

    if zProbe is not None:
        do_multipoint_zprobe(_plugin, sessionId)

def multipoint_zprobe_move(_plugin):
    global zProbe
    _plugin._logger.debug(f"_bgs: multipoint_zprobe_move sessionId=[{zProbe._sessionId}]")

    # setup the next step
    do_multipoint_zprobe(_plugin, zProbe._sessionId)


# An interrupted probe leaves the parser in G38.2. G0 is the motion mode every finished probe path ends in (its last
# lines are G0 moves), and it is valid without axis words; restoring a prior G1 would need a known feed rate and a
# prior arc mode cannot be restored bare (see write_pause_resume_scripts), so G0 is the one safe choice.
PROBE_MOTION_RESTORE = "G0"


def probe_restore_commands(positioning, units):
    """The lines that hand the parser back what a probe changed: the distance mode (0 absolute, 1 relative) and the
    units it forced (G91 / G21 on every probe move) and the motion mode it left (G38.2)."""
    return ["G91" if positioning == 1 else "G90", units, PROBE_MOTION_RESTORE]


def grbl_alarm_or_error_occurred(_plugin, locked=False):
    """Tear down a probe that was interrupted (alarm, error, no contact, cancel) and hand back the parser state.

    The probes run in G91 and G21 and restore the job's distance mode and units only when they finish. Grbl keeps its
    parser state through an alarm, so an interrupted probe would leave the machine in G91, in millimetres, and in
    G38.2. ``locked`` is True when the controller is in alarm and rejects G-code: the restore then waits for the next
    ``$X`` (see ``send_pending_distance_restore``).
    """
    global zProbe
    global xyProbe
    global zProbeAborted

    _plugin._logger.debug("_bgs: grbl_alarm_or_error_occurred")

    # the X/Y probe's state is the one from before either probe started
    prior = None
    for probe in (xyProbe, zProbe):
        if probe is not None and prior is None:
            prior = (probe._priorPositioning, probe._priorUnits)

    if zProbe is not None:
        zProbeAborted = True
        zProbe.teardown()
        zProbe = None

    if xyProbe is not None:
        xyProbe.teardown()
        xyProbe = None

    if prior is None:
        return

    restore = probe_restore_commands(*prior)
    if locked:
        _plugin.pendingDistanceRestore = restore
        _plugin._logger.debug("_bgs: probe interrupted by an alarm, %s goes out after the next unlock", restore)
    else:
        _plugin._printer.commands(restore)


def send_pending_distance_restore(_plugin):
    """Send the parser state an alarmed probe could not restore. Called as ``$X`` goes out, so it queues right behind it."""
    restore = getattr(_plugin, "pendingDistanceRestore", None)
    if restore:
        _plugin.pendingDistanceRestore = None
        _plugin._printer.commands(restore)


def activate_auto_cooldown(_plugin):
    _plugin._logger.debug("_bgs: activate_auto_cooldown")
    threading.Thread(target=auto_cooldown_monitor, args=(_plugin,)).start()


def auto_cooldown_monitor(_plugin):
    _plugin._logger.debug("_bgs: auto_cooldown_monitor")

    frequency = _plugin.autoCooldownFrequency * 60
    duration = _plugin.autoCooldownDuration * 60

    while _plugin._printer.is_printing():
        _plugin._logger.debug("auto cooldown loop started")

        startTime = time.monotonic()

        while _plugin._printer.is_printing() and time.monotonic() < startTime + frequency:
            time.sleep(1)

        if _plugin._printer.is_printing():
            _plugin._logger.debug("auto cooldown pausing job")
            _plugin._printer.pause_print()
        else:
            _plugin._logger.debug("job appears to have unexpectedly ended while waiting for cooldown frequency")
            break

        startTime = time.monotonic()

        while (_plugin._printer.is_pausing() or _plugin._printer.is_paused()) and time.monotonic() < startTime + duration:
            time.sleep(1)

        if _plugin._printer.is_paused():
            _plugin._logger.debug("auto cooldown resuming job")
            _plugin._printer.resume_print()
        else:
            _plugin._logger.debug("job appears to have unexpectedly ended while waiting for cooldown duration")
            continue

        _plugin._logger.debug("auto cooldown loop complete")
    _plugin._logger.debug("auto cooldown monitor shutting down")


def queue_cmds_and_send(_plugin, cmds, wait=False):
    _plugin._logger.debug(f"_bgs: queue_cmds_and_send cmds=[{cmds}] wait=[{wait}]")

    for cmd in cmds:
        _plugin._logger.debug("queuing command [%s] wait=%r", cmd, wait)
        _plugin.grblCmdQueue.append(cmd)

    if wait:
        wait_for_empty_cmd_queue(_plugin)


def wait_for_empty_cmd_queue(_plugin):
    _plugin._logger.debug("_bgs: wait_for_empty_cmd_queue")

    while len(_plugin.grblCmdQueue) > 0:
        time.sleep(.001)

    _plugin._logger.debug("done waiting for command queue to drain")


def add_notifications(_plugin, notifications):
    _plugin._logger.debug(f"_bgs: add_notifications notifications=[{notifications}]")

    if zProbe is not None:
        zProbe.notify(notifications)
    if xyProbe is not None:
        xyProbe.notify(notifications)

    for notification in notifications:
        _plugin.notifications.append((time.time(), notification))
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="notification", message=notification))


def generate_metadata_for_file(_plugin, filename, notify=False, force=False):
    metadata = _plugin._file_manager.get_metadata("local", filename)
    created = os.path.getctime(_plugin._file_manager.path_on_disk("local", filename))

    processing = True if metadata.get("bgs_processing") == "true" else False
    length = metadata.get("bgs_length")
    width = metadata.get("bgs_width")
    origin = metadata.get("bgs_origin")
    timestamp = metadata.get("bgs_timestamp")

    if timestamp is None or created > timestamp:
        force = True

    _plugin._logger.debug(f"_bgs: generate_metadata_for_file filename=[{filename}] notify=[{notify}] force=[{force}] processing=[{processing}] length=[{length}] width=[{width}] origin=[{origin}]")

    if length is None or width is None or not origin or force:
        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_width")
        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_length")
        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_origin")

        if processing and notify:
            threading.Thread(target=wait_for_metadata_processing, args=(_plugin, filename, notify)).start()
        else:
            _plugin._file_manager.set_additional_metadata("local", filename, "bgs_processing", "true", overwrite=True)
            threading.Thread(target=defer_generate_metadata_for_file, args=(_plugin, filename, notify)).start()
    else:
        if notify:
            _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_frame_size",
                                                                                 length=length,
                                                                                  width=width,
                                                                                  origin=origin))

def defer_generate_metadata_for_file(_plugin, filename, notify):
    _plugin._logger.debug(f"_bgs: defer_generate_metadata_for_file filename=[{filename}] notify=[{notify}]")

    try:
        file = _plugin._file_manager.path_on_disk("local", filename)
        created = os.path.getctime(file)

        # G-code in the wild carries Latin-1 in comments: a decode error must not cost the whole scan
        f = open(file, encoding="utf-8", errors="replace")

        minX = float("inf")
        minY = float("inf")
        maxX = float("-inf")
        maxY = float("-inf")

        x = float(0)
        y = float(0)

        overX = False
        overY = False
        underX = False
        underY = False

        lastGCommand = ""
        positioning = _plugin.positioning
        origin = ""

        start = timer()

        with f:
            for line in f:
                # skip comments / etc
                if line.upper().lstrip().startswith((";", "(", "%")):
                    continue

                # save our G command for shorthand post processors
                if line.upper().lstrip().startswith("G"):
                    lastGCommand = line.lstrip()[:3] if line.lstrip()[2:3].isnumeric() else line.lstrip()[:2]

                # use our saved G command if our line starts with a coordinate
                if line.upper().lstrip().startswith(("X", "Y", "Z")):
                    command = lastGCommand.upper() + " " + line.upper().strip()
                else:
                    command = line.upper().strip()

                # only G commands matter (except G53)
                if not command.upper().lstrip().startswith("G") or "G53" in command.upper():
                    continue

                # G90.1 / G91.1 set the arc-centre mode and leave the axis words as they were
                if re.search(r"G0*90(?![\d.])", command.upper()):
                    # absolute positioning
                    positioning = 0

                if re.search(r"G0*91(?![\d.])", command.upper()):
                    # relative positioning
                    positioning = 1

                # match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
                match = re.search(r".*[X]\ *(-?[\d.]+).*", command)
                # _plugin._logger.debug("command=[{}]".format(command))
                if match is not None:
                    x = float(match.groups(1)[0]) if positioning == 0 else x + float(match.groups(1)[0])
                    if x < minX:
                        if not underX and x < -1:
                            _plugin._logger.debug(f"underX x=[{x}]")
                            underX = True
                        minX = x
                    if x > maxX:
                        if not overX and x > 1:
                            _plugin._logger.debug(f"overX x=[{x}]")
                            overX = True
                        maxX = x

                # match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
                match = re.search(r".*[Y]\ *(-?[\d.]+).*", command)
                if match is not None:
                    y = float(match.groups(1)[0]) if positioning == 0 else y + float(match.groups(1)[0])
                    if y < minY:
                        if not underY and y < -1:
                            _plugin._logger.debug(f"underY y=[{y}]")
                            underY = True
                        minY = y
                    if y > maxY:
                        if not overY and y > 1:
                            _plugin._logger.debug(f"overY y=[{y}]")
                            overY = True
                        maxY = y

        # a job without a move on an axis has no extent on it (no min/max were ever set)
        length = math.ceil(maxY - minY) if minY <= maxY else 0
        width = math.ceil(maxX - minX) if minX <= maxX else 0

        # where the job sits around the machine origin; an axis the job barely leaves (within 1mm of the origin)
        # extends positively from it
        horizontal = "Center" if overX and underX else "Right" if underX else "Left"
        vertical = "Center" if overY and underY else "Top" if underY else "Bottom"
        origin = "grblCenter" if horizontal == "Center" and vertical == "Center" else f"grbl{vertical}{horizontal}"

        _plugin._file_manager.set_additional_metadata("local", filename, "bgs_length", length, overwrite=True)
        _plugin._file_manager.set_additional_metadata("local", filename, "bgs_width", width, overwrite=True)
        _plugin._file_manager.set_additional_metadata("local", filename, "bgs_origin", origin, overwrite=True)
        _plugin._file_manager.set_additional_metadata("local", filename, "bgs_timestamp", created, overwrite=True)

        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_processing")

        _plugin._logger.debug(f'finished reading file=[{filename}] length=[{length}] width=[{width}] origin=[{origin}] positioning=[{positioning}] time=[{timer() - start}]')

        if notify:
            _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_frame_size",
                                                                             length=length,
                                                                             width=width,
                                                                             origin=origin))
    except BaseException as e:
        _plugin._logger.error(f"defer_generate_metadata_for_file: [{str(e)}]")
        # never leave the file marked as being processed
        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_processing")

def wait_for_metadata_processing(_plugin, filename, notify):
    _plugin._logger.debug(f"_bgs: wait_for_metadata_processing filename=[{filename}] notify=[{notify}]")

    metadata = _plugin._file_manager.get_metadata("local", filename)
    processing = True if metadata.get("bgs_processing") == "true" else False
    seconds = 0

    while seconds < 300 and processing:
        time.sleep(1)
        metadata = _plugin._file_manager.get_metadata("local", filename)
        processing = True if metadata.get("bgs_processing") == "true" else False
        seconds += 1

    if not processing and notify:
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_frame_size",
                                                                            length=metadata.get("bgs_length"),
                                                                             width=metadata.get("bgs_width"),
                                                                             origin=metadata.get("bgs_origin")))
    else:
        _plugin._file_manager.remove_additional_metadata("local", filename, "bgs_processing")
        _plugin._logger.warning("gave up waiting for metadata processing")


def is_laser_mode(_plugin):
    try:
        if not is_grbl_fluidnc(_plugin):
            _plugin._logger.debug(f"_bgs: is_laser_mode={int(float(_plugin.grblSettings.get(32)[0])) != 0}")
            return int(float(_plugin.grblSettings.get(32)[0])) != 0
        else:
            return int(float(_plugin.fluidSettings.get("Grbl/LaserMode", 0.0))) != 0
    except Exception as e:
        _plugin._logger.warn(f"_bgs: is_laser_mode: {e}")

    return False

def is_grbl_one_dot_one(_plugin):
    # True when the controller speaks the Grbl 1.1 protocol (realtime override bytes, $J= jogging, jog cancel):
    # Grbl 1.1+, Grbl_Esp32 and FluidNC (which reports its own version, VER:3.x).  Pre-1.1 Grbl (VER:1.0) does not.
    oneDotOne = ("VER:1." in _plugin.grblVersion and "VER:1.0" not in _plugin.grblVersion) or is_grbl_fluidnc(_plugin)
    _plugin._logger.debug(f"_bgs: is_grbl_one_dot_one result=[{oneDotOne}]")
    return oneDotOne

def is_grbl_esp32(_plugin):
    oneDotOne = "VER:1." in _plugin.grblVersion and "VER:1.0" not in _plugin.grblVersion and "VER:1.1" not in _plugin.grblVersion
    _plugin._logger.debug(f"_bgs: is_grbl_esp32 result=[{oneDotOne}]")
    return oneDotOne

def is_grbl_fluidnc(_plugin):
    oneDotOne = " FLUIDNC " in _plugin.grblVersion.upper()
    _plugin._logger.debug(f"_bgs: is_grbl_fluidnc result=[{oneDotOne}]")
    return oneDotOne

def is_latin_encoding_available(_plugin):
    octoprintVersion = _plugin.octoprintVersion
    latinEncoding = int(octoprintVersion.split(".")[0]) > 1 or int(octoprintVersion.split(".")[1]) >= 8
    _plugin._logger.debug(f"_bgs: is_latin_encoding_available result=[{latinEncoding}]")
    return latinEncoding

# OctoPrint 1.x logs "Send: " / "Recv: ", 2.x logs ">>> " / "<<< " -- the filters match both
_TERMINAL_PREFIXES = {
    "1.x": ("Send: ", "Recv: "),
    "2.x": (">>> ", "<<< "),
    "both": ("(Send: |>>> )", "(Recv: |<<< )"),
}

_TERMINAL_FILTER_TEMPLATES = [
    ("Suppress status report requests", "^{send}\\?$"),
    ("Suppress acknowledgement responses", "^{recv}ok$"),
    ("Suppress status report responses", "^{recv}<.*[\\x2c|][WM]Pos:.+"),
    ("Suppress blank responses", "^{recv}$"),
]

def bgs_terminal_filters(prefixes="both"):
    send, recv = _TERMINAL_PREFIXES[prefixes]
    return [{"name": name, "regex": template.format(send=send, recv=recv)} for name, template in _TERMINAL_FILTER_TEMPLATES]

def _bgs_filter_regexes():
    # every spelling of our filters, from any release: 1.x-only, 2.x-only and the dual-prefix one
    return {f["regex"] for prefixes in _TERMINAL_PREFIXES for f in bgs_terminal_filters(prefixes)}

def user_terminal_filters(filters):
    # the user's own filters: neither ours nor OctoPrint's shipped defaults (what terminalFilters reads as
    # until it is first written, so a fresh install reports them as if the user had chosen them)
    owned = _bgs_filter_regexes() | {f.get("regex") for f in default_settings.get("terminalFilters", [])}
    return [f for f in (filters or []) if f.get("regex") not in owned]

def merge_terminal_filters(_plugin):
    # keep the user's own terminal filters; ours replace OctoPrint's defaults and any older spelling of ours
    current = _plugin._settings.global_get(["terminalFilters"])
    merged = user_terminal_filters(current) + bgs_terminal_filters()
    if merged != current:
        _plugin._settings.global_set(["terminalFilters"], merged)

def normalize_active_filters(_plugin):
    # active filters are saved by regex, so selections saved by older releases (1.x-only regexes)
    # no longer match a filter -- map them to the current regex
    current = [f["regex"] for f in bgs_terminal_filters()]
    variants = {}
    for prefixes in _TERMINAL_PREFIXES:
        for old, new in zip((f["regex"] for f in bgs_terminal_filters(prefixes)), current):
            variants[old] = new

    activeFilters = _plugin._settings.get(["activeFilters"]) or []
    normalized = [variants.get(f, f) for f in activeFilters]
    if normalized != activeFilters:
        _plugin._logger.debug(f"_bgs: normalize_active_filters {activeFilters} -> {normalized}")
        _plugin._settings.set(["activeFilters"], normalized)

FRAME_ORIGINS = ("grblTopLeft", "grblTopCenter", "grblTopRight", "grblCenterLeft", "grblCenter", "grblCenterRight",
                 "grblBottomLeft", "grblBottomCenter", "grblBottomRight")

def parse_distance(value, label):
    """A finite number from a request value, or the message to answer a 400 with."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{label} must be a number"
    if not math.isfinite(number):
        return f"{label} must be a finite number"
    return number

# what OctoPrint and the plugin send while connecting: answers to these are the connect noise
STARTUP_COMMANDS = ("", "?", "$$", "$+", "$I", "$G", "$#", "$S", "$CD", "$BUILD/INFO", "$SETTINGS/LIST", "$CONFIG/DUMP",
                    "$GCODE/MODES", "M115", "CAT /SD/CONFIG")

def is_startup_command(cmd):
    cmd = cmd.strip().upper()
    # $Report/Interval=<n> is the plugin's own follow-up to the $S download, so its error is not the user's either
    return cmd in STARTUP_COMMANDS or cmd.startswith(("M110", "M105", "M114", "\x18", "$REPORT/INTERVAL="))


class SettingWaiter:
    """An updateGrblSetting request waiting for Grbl's answer to the `$<id>=<value>` line it sent."""
    def __init__(self, command):
        self.command = command
        self.sent = False
        self.done = threading.Event()
        self.accepted = False
        self.reason = ""

def set_grbl_setting_and_wait(_plugin, setting_id, value, timeout=8):
    """Send `$<id>=<value>` and wait for Grbl's ok or error. Returns (accepted, reason)."""
    command = f"${setting_id}={value}"
    if _plugin.settingWaiter is not None and not _plugin.settingWaiter.done.is_set():
        return False, "Another Grbl setting update is still waiting for its answer"

    waiter = SettingWaiter(command)
    _plugin.settingWaiter = waiter
    try:
        _plugin._printer.commands(command)
        if not waiter.done.wait(timeout):
            return False, f"Grbl did not acknowledge {command} within {timeout} seconds; the value was not changed on screen"
        return waiter.accepted, waiter.reason
    finally:
        _plugin.settingWaiter = None

def note_setting_sent(_plugin, cmd):
    waiter = _plugin.settingWaiter
    if waiter is not None and not waiter.sent and cmd.strip() == waiter.command:
        waiter.sent = True

def resolve_setting_waiter(_plugin, accepted, reason=""):
    """The next ok / error after the line was sent answers it (OctoPrint has one line in flight). True if claimed."""
    waiter = _plugin.settingWaiter
    if waiter is None or not waiter.sent or waiter.done.is_set():
        return False
    waiter.accepted = accepted
    waiter.reason = reason
    waiter.done.set()
    return True


def serial_settings_path(path):
    if is_octoprint_compatible(">=2"):
        return ["plugins", "serial_connector"] + path
    return ["serial"] + path

def set_never_send_checksum(_plugin, value):
    if is_octoprint_compatible(">=2"):
        _plugin._settings.global_set(["plugins", "serial_connector", "sendChecksum"], "never" if value else "print")
    else:
        _plugin._settings.global_set_boolean(["serial", "neverSendChecksum"], value)


def send_command_now(printer, logger, cmd, waitTime=0):
    try:
        if waitTime > 0:
            time.sleep(waitTime)
        printer.commands(cmd, force=True)
        logger.debug("_bgs: send_command")
    except Exception as e:
        logger.error("_bgs: send_command_now: %s", e)


def send_realtime(_plugin, data):
    """Write Grbl realtime byte(s) straight to the transport.

    OctoPrint's send loop releases the next line only after the previous one was acknowledged, and Grbl
    withholds the acknowledgement of a blocking line (G4, M3/M5, ...) until the planner drains or a hold is
    released, so a realtime byte queued through printer.commands() can sit behind a line that cannot finish.
    Grbl handles realtime bytes outside its line buffer, so they are safe to write at any time.
    """
    printer = _plugin._printer
    comm = getattr(printer, "_comm", None) or getattr(getattr(printer, "_connection", None), "_comm", None)
    port = getattr(comm, "_serial", None)
    shown = data.decode("latin-1").encode("unicode_escape").decode("ascii")

    if port is not None:
        try:
            port.write(data)
            comm._log(("Send: " if not is_octoprint_compatible(">=2") else ">>> ") + shown + " [realtime]")
            return True
        except Exception as e:
            _plugin._logger.error("_bgs: send_realtime: direct write of [%s] failed: %s", shown, e)

    _plugin._logger.warning("_bgs: send_realtime: no direct transport, queueing [%s]", shown)
    send_command_now(printer, _plugin._logger, data.decode("latin-1"))
    return False


def refresh_grbl_state(_plugin, timeout=1.0):
    """Ask Grbl for a status report now and wait for the reply, so grblState is fresh. True if one arrived."""
    seq = _plugin.grblStatusSeq
    send_realtime(_plugin, b"?")

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _plugin.grblStatusSeq != seq:
            return True
        time.sleep(0.02)

    _plugin._logger.debug("_bgs: refresh_grbl_state: no status report within %.1fs", timeout)
    return False


def schedule_status_refresh(_plugin, delay=0.2):
    """Request a status report shortly after a command that changes the controller state ($X, ~, !)."""
    timer = threading.Timer(delay, send_realtime, args=(_plugin, b"?"))
    timer.daemon = True
    timer.start()


def start_jog_guard(_plugin, duration=2.0):
    """Mark the machine as jogging until Grbl reports it. Status replies to our own `?` (the CANCELJOG
    `? 0x85 ?`) are still in flight when we switch to Jog, and would switch it straight back to Idle."""
    _plugin.jogSeen = False
    _plugin.jogGuardUntil = time.monotonic() + duration
    if _plugin.grblState != "Jog":
        set_grbl_state(_plugin, "Jog")


def release_queued_command(_plugin):
    """Send the next command waiting in grblCmdQueue if the machine is in a state that accepts it.

    A queued status request is a wait point: nothing else leaves the queue until its report arrives (bounded,
    in case the request is suppressed and no report ever comes). Called on every poll and on every report, so
    a command waits for the controller's state, not for the next poll.
    """
    if (len(_plugin.grblCmdQueue) > 0
            and _plugin.grblState.upper() in ("IDLE", "HOLD:0", "DOOR:0", "CHECK", "ALARM")
            and time.monotonic() >= _plugin.grblQueueHold):
        try:
            queued = _plugin.grblCmdQueue.pop(0)
        except IndexError:
            # another thread (a poll, a status report, the end of a deferred wait) released it first
            return
        _plugin._logger.debug('sending queued command [%s] - depth [%d]', queued, len(_plugin.grblCmdQueue) + 1)
        if queued == _plugin.statusCommand:
            _plugin.grblQueueHold = time.monotonic() + 2
        _plugin._printer.commands(queued)


def set_grbl_state(_plugin, state):
    _plugin.grblState = state
    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state=state))


# a Grbl M-word for spindle state: M3/M4/M5 (leading zeros allowed), never the prefix of M30, M400, M50, M40 ...
_SPINDLE_MCODE = re.compile(r"M0*([345])(?![0-9.])", re.IGNORECASE)
_GCODE_COMMENT = re.compile(r"\(.*?\)|;.*")

def spindle_mcode(cmd):
    """The spindle M-word (M3/M4/M5) a G-code line sets, or None."""
    found = _SPINDLE_MCODE.findall(_GCODE_COMMENT.sub("", cmd))
    return f"M{found[-1]}" if found else None


def defer_resuming_status_reports(_plugin, waitTime, showFinalize):
    time.sleep(waitTime)
    _plugin.noStatusRequests = False

    if showFinalize:
        # the configuration is finalized from here -- the queued $CD= goes out now, not when the save was made
        release_queued_command(_plugin)
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                title="Finalizing Changes. . .",
                                                                    text="Please wait while FluidNC's configuration and settings are finalized.",
                                                                    hide=True,
                                                                delay=15000,
                                                            notify_type="notice"))


def upload_fluid_config(_plugin):
    """Upload the new configuration to FluidNC's web server, over the existing file.

    The old file is never deleted first: a delete followed by a failed upload would leave the controller with
    no configuration at all. The size goes ahead of the file in the "<name>S" form field, which is what
    WebUIServer.cpp reads: with it the controller refuses a file that cannot fit before it opens (and so
    truncates) the existing one; without it a write that runs out of space has already emptied the old file.
    Returns (True, "") when the controller answered 2xx, else (False, why) and the caller falls back to the
    serial path (update_fluid_config).
    """
    name = _plugin.fluidSettings.get("Config/Filename")
    url = "http://{}:{}/files".format(_plugin.fluidSettings.get("Hostname"), _plugin.fluidSettings.get("HTTP/Port"))
    content = _plugin.fluidConfig.encode("utf-8")

    try:
        with requests.post(url, data={f"{name}S": str(len(content))}, files={'file': (name, content)}, timeout=15) as r:
            if not r.ok:
                return False, f"{url} answered HTTP {r.status_code}"
    except Exception as e:
        return False, f"{url}: {e}"

    return True, ""


def update_fluid_config(_plugin):
    _plugin._logger.debug("_bgs: update_fluid_config")

    configName = _plugin.fluidSettings.get("Config/Filename", "config.yaml")

    # Build every command before sending any: a value that cannot be serialised must not leave the controller
    # with half a config.  The existing file is never deleted -- $CD=<file> overwrites it, so if the new config
    # is not written the old one is still there.
    try:
        cmds = fluid_config_commands(_plugin.fluidYaml)
    except Exception as e:
        _plugin._logger.error(f"_bgs: update_fluid_config: unable to build FluidNC config commands: {e}")
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                title="FluidNC Configuration Not Saved",
                                                                text=html.escape(f"The configuration could not be processed ({e}). Nothing was sent to the controller; its existing configuration is unchanged."),
                                                                hide=False,
                                                                notify_type="error"))
        return

    for cmd in cmds:
        _plugin._printer.commands(cmd)

    queue_cmds_and_send(_plugin, [f"$CD={configName}"])


def fluid_config_commands(yaml_config):
    cmds = []
    for key, value in yaml_config.items():
        process_fluid_config_item(cmds, key, value)
    return cmds


def fluid_config_value(value):
    # yaml gives us python types: render what FluidNC parses (true/false, plain numbers), not python reprs
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).replace("null", "")


def process_fluid_config_item(cmds, key, value, path=""):
    if isinstance(value, dict):
        path = f"{path}{key}/"
        for child_key, child_value in value.items():
            process_fluid_config_item(cmds, child_key, child_value, path)
    else:
        if value is not None and "PIN" not in key.upper() and "MOTOR" not in path.upper() and not is_spindle(path):
            cmds.append("$/{}{}={}".format(path, key, fluid_config_value(value)))

def is_spindle(path):
    if path.upper().startswith("10V/"):
        return True
    elif path.upper().startswith("BESC/"):
        return True
    elif path.upper().startswith("DAC/"):
        return True
    elif path.upper().startswith("H2A/"):
        return True
    elif path.upper().startswith("H100/"):
        return True
    elif path.upper().startswith("HBRIDGE/"):
        return True
    elif path.upper().startswith("HUANYANG/"):
        return True
    elif path.upper().startswith("LASER/"):
        return True
    elif path.upper().startswith("NOWFOREVER/"):
        return True
    elif path.upper().startswith("NOSPINDLE/"):
        return True
    elif path.upper().startswith("ONOFF/"):
        return True
    elif path.upper().startswith("PWM/"):
        return True
    elif path.upper().startswith("RELAY/"):
        return True
    elif path.upper().startswith("YL620/"):
        return True

    return False

        
def axis_setting(_plugin, fluid_key, grbl_id):
    """The controller's setting for one axis (a float), or None when it is not known (settings not read yet,
    or the machine has no such axis)."""
    if is_grbl_fluidnc(_plugin):
        value = (_plugin.fluidSettings or {}).get(fluid_key)
    else:
        value = (_plugin.grblSettings.get(grbl_id) or [None])[0]
    return None if value is None else float(value)


def get_axes_max_rates(_plugin):
    _plugin._logger.debug("_bgs: get_axes_max_rates")

    # seed with defaults for whatever the controller has not told us (yet)
    xf = 1000.0
    yf = 1000.0
    zf = 300.0

    try:
        rates = [axis_setting(_plugin, f"Grbl/MaxRate/{axis}", id) for axis, id in zip("XYZ", (110, 111, 112))]
        xf, yf, zf = [default if rate is None else rate for rate, default in zip(rates, (xf, yf, zf))]
    except Exception as e:
        _plugin._logger.warn(f"_bgs: get_axes_max_rates: {e}")

    _plugin._logger.debug(f"_bgs: get_axes_max_rates x={xf} y={yf} z={zf}")
    return xf, yf, zf


def get_axes_limits(_plugin):
    _plugin._logger.debug("_bgs: get_axes_limits")

    # seed with defaults for whatever the controller has not told us (yet)
    xl = 200.0
    yl = 200.0
    zl = 50.0

    distance = 1

    try:
        limits = [axis_setting(_plugin, f"Grbl/MaxTravel/{axis}", id) for axis, id in zip("XYZ", (130, 131, 132))]

        if limits[0] is None or limits[1] is None:
            # settings have not arrived yet: keep the defaults and leave control_distance alone
            _plugin._logger.debug("_bgs: get_axes_limits: X/Y limits not known yet")
        else:
            # a machine without a Z axis simply has no Z limit
            xl, yl = limits[0], limits[1]
            if limits[2] is not None:
                zl = limits[2]

            # assign our default distance if it is not already set to the lower of x,y limits
            distance = float(_plugin._settings.get(["distance"]))

            if distance == 0 or distance > min([xl, yl]):
                distance = float(min([xl, yl]))

            _plugin._settings.set(["control_distance"], distance)
            _plugin._settings.save(trigger_event=True)
    except Exception as e:
        _plugin._logger.warn(f"_bgs: get_axes_limits: {e}")

    _plugin._logger.debug(f"_bgs: get_axes_limits x={xl} y={yl} z={zl} distance={distance}")
    return xl, yl, zl

def babystep_offset(_plugin, program, axis, increment):
    axis = axis.lower()
    known = _plugin.offsets.get(program, {}).get(axis)

    if known is None:
        # The current work offset of this axis is unknown (the axis is not on this machine, or $# has not been
        # read yet), so an absolute G10 L2 would move the work origin by a guess: send nothing and say so.
        _plugin._logger.warning(f"_bgs: babystep_offset: no known {axis.upper()} offset for {program}, ignoring M290")
        if program not in _plugin.offsets:
            queue_cmds_and_send(_plugin, ["$#"])
        add_notifications(_plugin, [f"Babystep ignored: {axis.upper()} offset of {program} is not known"])
        return

    pgm = int(program.replace("G", "")) - 53
    newvalue = round(known + increment, 3)

    _plugin._printer.commands(f"G10 L2 P{pgm} {axis.upper()}{newvalue:.3f}")

    # update our offsets
    _plugin.offsets[program][axis] = newvalue
