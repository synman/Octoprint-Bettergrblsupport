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
import os
import time
import math

import re
import threading
import subprocess

from timeit import default_timer as timer
from octoprint.events import Events
from octoprint.util.version import is_octoprint_compatible

from .zprobe import ZProbe
from .xyprobe import XyProbe

zProbe = None
xyProbe = None

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
        longCmds.append("G53")
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
    _plugin._settings.global_set(serial_settings_path(["maxCommunicationTimeouts", "long"]), 5)
    set_never_send_checksum(_plugin, False)
    _plugin._settings.global_set(serial_settings_path(["encoding"]), "ascii")
    _plugin._settings.global_set_boolean(serial_settings_path(["sanityCheckTools"]), True)

    _plugin._settings.global_set(["terminalFilters"], _plugin.octo_filters)
    
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
        _plugin.autoSleepTimer = time.monotonic()

        _plugin.is_operational = True
        _plugin._settings.set_boolean(["is_operational"], _plugin.is_operational)

        _plugin.fluidConfig = None
        _plugin._printer.commands(["$I", "$G", "$#"])

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
        _plugin.grblState = "Idle"
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Idle"))

        _plugin.is_printing = False
        _plugin._settings.set_boolean(["is_printing"], _plugin.is_printing)

        # reset our rate overrides
        _plugin.feedRate = 0
        _plugin.plungeRate = 0
        _plugin.powerRate = 0

        return

    # Print Cancelling
    if event == Events.PRINT_CANCELLING:
        _plugin._logger.debug("cancelling job")

        if "HOLD" in _plugin.grblState.upper():
            send_command_now(_plugin._printer, _plugin._logger, ["~", "M5"])
        else:
            send_command_now(_plugin._printer, _plugin._logger, ["M5"])

    # Print Pausing
    if payload is not None and payload.get("state_id") == "PAUSING":
        _plugin._logger.debug("pausing job")

        pausedPower = _plugin.grblPowerLevel
        pausedPositioning = _plugin.positioning
        pausedSpeed = _plugin.grblSpeed
        pausedGrblMCode = _plugin.grblMCode

        pauseScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "afterPrintPaused"))
        resumeScript = os.path.realpath(os.path.join(_plugin._settings.global_get_basefolder("scripts"), "gcode", "beforePrintResumed"))

        with open(pauseScript, 'w') as file:
            # turn off laser/spindle
            file.write("M5\n")
            # retract Z 5 if not laser mode
            if not is_laser_mode(_plugin):
                file.write("G91 G0 Z5\n")
            # start feed hold
            file.write("!\n")

        with open(resumeScript, 'w') as file:
            # release feed hold
            file.write("~\n")
            # reset our M-code
            file.write(f"{pausedGrblMCode}\n")
            # reset our spindle speed / power 
            file.write(f"S{pausedPower}\n")
            # reset our feedrate
            file.write(f"F{pausedSpeed}\n")
            # reset our positioning mode
            file.write("G91\n" if pausedPositioning == 1 else "G90\n")
            # move our spindle back down 5
            if not is_laser_mode(_plugin):
                file.write("G4 P10\n")
                file.write("G91 G0 Z-5\n")

    # Print Paused
    if event == Events.PRINT_PAUSED:
        _plugin._logger.debug("paused job")

    # Print Resumed
    if event == Events.PRINT_RESUMED:
        _plugin._logger.debug("resuming job")
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

    _plugin.grblState = "Jog"
    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Jog"))

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
    match = re.search(r'<(-?[^,]+)[,|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+),?(-?[\d\.]+)?,?(-?[\d\.]+)?', msg)
    response = 'X:{1} Y:{2} Z:{3} E:0 {original}'.format(*match.groups(), original=msg)
    
    _plugin.grblMode = "MPos" if "MPos" in msg else "WPos" if "WPos" in msg else "N/A"
    
    if _plugin.is_printing and _plugin.grblState == "Idle":
        _plugin.grblState = "Run"
    else:
        _plugin.grblState = str(match.groups(1)[0])

    if _plugin.grblState == "Idle" and _plugin.M9DelayActive:
        _plugin._logger.debug('Turning OFF Air Assist')
        subprocess.Popen(_plugin.m9Command, shell=True)
        _plugin.M9DelayActive = False   
        _plugin.coolant = "M9"     

    _plugin.grblX = float(match.groups(1)[1])
    _plugin.grblY = float(match.groups(1)[2])
    _plugin.grblZ = float(match.groups(1)[3])

    if match.groups(1)[5]:
        _plugin.grblA = float(match.groups(1)[4])
        _plugin.grblB = float(match.groups(1)[5])
        
    if match.groups(1)[4] and not match.groups(1)[5] and _plugin.hasB:
        _plugin.grblB = float(match.groups(1)[4]) 
    else:
        _plugin.grblA = float(match.groups(1)[4])
        
    match = re.search(r'.*\|Pn:([XYZABPDHRS]+)', msg)
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

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                    title=f"Grbl Alarm #{error} Received",
                                                                    text=desc,
                                                                    hide=True,
                                                                    delay=10000,
                                                                    notify_type="notice"))

    _plugin._logger.warning("alarm received: %d: %s", error, _plugin.grblAlarms.get(error))

    # inform _bgs in case it has something going on (probing)
    grbl_alarm_or_error_occurred(_plugin)

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


def process_grbl_error(_plugin, msg):
    error = 0
    desc = msg

    match = re.search(r'error:\ *(-?[\d.]+)', msg.lower())
    if match is not None:
        error = int(match.groups(1)[0])

        desc = _plugin.grblErrors.get(error)
        if desc is None:
            desc = f"Grbl Error #{error} - Error description not available"

    # hack to suppress errors on connect
    if time.monotonic() - _plugin.whenConnected < 20:
        return "ok "

    # lets not deal with file not found
    if error == 65:
        return "ok "

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                    title=f"Grbl Error #{error} Received",
                                                                    text=desc,
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
            _plugin._logger.debug("parser state indicates [%s] motion mode", state)
        elif state in ("G54", "G55", "G56", "G57", "G58", "G59"):
            _plugin.grblCoordinateSystem = state
            _plugin._logger.debug("parser state indicates [%s] coordinate system active", _plugin.grblCoordinateSystem)
        elif state in ("G17", "G18", "G19"):
            _plugin._logger.debug("parser state indicates [%s] plane selected", state)
        elif state in ("G20", "G21"):
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

    do_simple_zprobe(_plugin, sessionId)

    # wait for the z probe to run out of scope
    while zProbe is not None:
        time.sleep(1)

    do_xy_probe(_plugin, "XY", sessionId)


def do_xy_probe(_plugin, axes, sessionId):
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
        if axes == "XY":
            text = f"X/Y Axis Home has been calculated and set to machine position: X[<B>{xyProbe._results[0]:.3f}</B>] Y[<B>{xyProbe._results[1]:.3f}</B>]"
            _plugin._printer.commands([f"G0 G90 X0 Y0 F{xyf}", "G91"])
        else:
            text = f"{axes} Axis Home has been calculated and set to machine position: [<B>{xyProbe._results[0]:.3f}</B>]]"
            _plugin._printer.commands([f"G0 G90 {axes}0 F{xyf}", "G91"])

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
        xyProbe.teardown()
        xyProbe = None
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

    # set home for our current axis and travel back to where we started
    _plugin._printer.commands([
            f"G10 P{program} L2 {axis}{position:f}",
            f"G0 {axis}{10 * originInvert * invert} Z{15 * _plugin.invertZ} F{zf}",
            f"G0 G90 {axis}{10 * originInvert * invert * -1} F{xyf}",
            "G91"
        ])

    # update our offsets
    _plugin.offsets[_plugin.grblCoordinateSystem][axis] = position

    do_xy_probe(_plugin, xyProbe._axes, sessionId)


def do_simple_zprobe(_plugin, sessionId):
    _plugin._logger.debug(f"_bgs: do_simple_zprobe sessionId=[{sessionId}]")

    global zProbe

    if zProbe is not None:
        zProbe.teardown()
        zProbe = None

    zProbe = ZProbe(_plugin, simple_zprobe_hook, sessionId)

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

    _plugin._printer.commands(["G91", "G21", f"G10 P{program} L2 Z{z0:f}", f"G0 Z{_plugin.zProbeEndPos * _plugin.invertZ}"])

    # update our offsets
    _plugin.offsets[_plugin.grblCoordinateSystem]["z"] = z0

    zProbe.teardown()
    zProbe = None

def do_multipoint_zprobe(_plugin, sessionId):
    global zProbe
    _plugin._logger.debug(f"_bgs: do_multipoint_zprobe step=[{zProbe._step + 1 if zProbe is not None else 0}] sessionId=[{sessionId}]")

    if zProbe is None:
        zProbe = ZProbe(_plugin, multipoint_zprobe_hook, sessionId)

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

            queue_cmds_and_send(_plugin, [f"G10 P{program} L2 Z{position:f}"])

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
        zProbe.teardown()
        zProbe = None
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


def grbl_alarm_or_error_occurred(_plugin):
    global zProbe
    global xyProbe

    _plugin._logger.debug("_bgs: grbl_alarm_or_error_occurred")

    if zProbe is not None:
        zProbe.teardown()
        zProbe = None

    if xyProbe is not None:
        xyProbe.teardown()
        xyProbe = None


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

    if length is None or width is None or origin is None or force:
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

        f = open(file)

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

            if "G90" in command.upper():
                # absolute positioning
                positioning = 0

            if "G91" in command.upper():
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
                    if not underY and y <= -1:
                        _plugin._logger.debug(f"underY y=[{y}]")
                        underY = True
                    minY = y
                if y > maxY:
                    if not overY and y > 1:
                        _plugin._logger.debug(f"overY y=[{y}]")
                        overY = True
                    maxY = y

        length = math.ceil(maxY - minY)
        width = math.ceil(maxX - minX)

        # bottom
        if overY and not underY and overX and not underX:
            origin = "grblBottomLeft"
        if overY and not underY and overX and underX:
            origin = "grblBottomCenter"
        if overY and not underY and not overX and underX:
            origin = "grblBottomRight"

        # center
        if overY and underY and overX and not underX:
            origin = "grblCenterLeft"
        if overY and underY and overX and underX:
            origin = "grblCenter"
        if overY and underY and not overX and underX:
            origin = "grblCenterRight"

        # top
        if not overY and underY and overX and not underX:
            origin = "grblTopLeft"
        if not overY and underY and overX and underX:
            origin = "grblTopCenter"
        if not overY and underY and not overX and underX:
            origin = "grblTopRight"

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
    oneDotOne = "VER:1." in _plugin.grblVersion and "VER:1.0" not in _plugin.grblVersion
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


def defer_resuming_status_reports(_plugin, waitTime, showFinalize):
    if showFinalize:
        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_notify",
                                                                title="Finalizing Changes. . .",
                                                                    text="Please wait while FluidNC's configuration and settings are finalized.",
                                                                    hide=True,
                                                                delay=15000,
                                                            notify_type="notice"))
    time.sleep(waitTime)
    _plugin.noStatusRequests = False


def update_fluid_config(_plugin):
    _plugin._logger.debug("_bgs: update_fluid_config")

    configName = _plugin.fluidSettings.get("Config/Filename", "config.yaml")
    _plugin._printer.commands(f"$LocalFS/Delete={configName}")

    for key, value in _plugin.fluidYaml.items():
        process_fluid_config_item(_plugin, key, value)

    queue_cmds_and_send(_plugin, [f"$CD={configName}"])
    
def process_fluid_config_item(_plugin, key, value, path=""):
    if isinstance(value, dict):
        path = f"{path}{key}/"
        for child_key, child_value in value.items():
            process_fluid_config_item(_plugin, child_key, child_value, path)
    else:
        if value is not None and "PIN" not in key.upper() and "MOTOR" not in path.upper() and not is_spindle(path):
            _plugin._printer.commands("$/{}{}={}".format(path, key, value.replace("null", "")))

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

        
def get_axes_max_rates(_plugin):
    _plugin._logger.debug("_bgs: get_axes_max_rates")
    
    # seed with defaults
    xf = 1000.0
    yf = 1000.0
    zf = 300.0

    try:
        if is_grbl_fluidnc(_plugin):
            xf = float(_plugin.fluidSettings.get("Grbl/MaxRate/X"))
            yf = float(_plugin.fluidSettings.get("Grbl/MaxRate/Y"))
            zf = float(_plugin.fluidSettings.get("Grbl/MaxRate/Z"))
        else:
            xf = float(_plugin.grblSettings.get(110)[0])
            yf = float(_plugin.grblSettings.get(111)[0])
            zf = float(_plugin.grblSettings.get(112)[0])
    except Exception as e:
        _plugin._logger.warn(f"_bgs: get_axes_max_rates: {e}")

    _plugin._logger.debug(f"_bgs: get_axes_max_rates x={xf} y={yf} z={zf}")
    return xf, yf, zf


def get_axes_limits(_plugin):
    _plugin._logger.debug("_bgs: get_axes_limits")

    # seed with defaults
    xl = 200.0
    yl = 200.0
    zl = 50.0

    distance = 1

    try:
        if is_grbl_fluidnc(_plugin):
            xl = float(_plugin.fluidSettings.get("Grbl/MaxTravel/X"))
            yl = float(_plugin.fluidSettings.get("Grbl/MaxTravel/Y"))
            zl = float(_plugin.fluidSettings.get("Grbl/MaxTravel/Z"))
        else:
            xl = float(_plugin.grblSettings.get(130)[0])
            yl = float(_plugin.grblSettings.get(131)[0])
            zl = float(_plugin.grblSettings.get(132)[0])

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
    pgm = int(program.replace("G", "")) - 53
    newvalue = _plugin.offsets[program][axis.lower()] + increment

    _plugin._printer.commands(f"G10 L2 P{pgm} {axis.upper()}{newvalue}")

    # update our offsets
    _plugin.offsets[program][axis] = newvalue
