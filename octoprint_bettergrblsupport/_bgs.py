#!/usr/bin/python
# -*- coding: utf-8 -*-
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
import re
import requests
import threading

from .zprobe import ZProbe

zProbe = None

def load_grbl_descriptions(_plugin):
    path = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep

    f = open(path + "grbl_errors.txt", 'r')

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if not match is None:
            _plugin.grblErrors[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching error id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

    f = open(path + "grbl_alarms.txt", 'r')

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if not match is None:
            _plugin.grblAlarms[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching alarm id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

    f = open(path + "grbl_settings.txt", 'r')

    for line in f:
        match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
        if not match is None:
            _plugin.grblSettingsNames[int(match.groups(1)[0])] = match.groups(1)[1]
            # _plugin._logger.debug("matching setting id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])


def load_grbl_settings(_plugin):
    _plugin._logger.debug("_bgs: load_grbl_settings")

    _plugin.grblSettingsText = _plugin._settings.get(["grblSettingsText"])

    if not _plugin.grblSettingsText is None:
        for setting in _plugin.grblSettingsText.split("||"):
            if len(setting.strip()) > 0:

                _plugin._logger.debug("load_grbl_settings=[{}]".format(setting))

                set = setting.split("|")
                if not set is None:
                    _plugin.grblSettings.update({int(set[0]): [set[1], _plugin.grblSettingsNames.get(int(set[0]))]})
    return


def save_grbl_settings(_plugin):
    _plugin._logger.debug("_bgs: save_grbl_settings")

    ret = ""
    for id, data in sorted(_plugin.grblSettings.items(), key=lambda x: int(x[0])):
        ret = ret + "{}|{}|{}||".format(id, data[0], data[1])

    _plugin._logger.debug("save_grbl_settings=[{}]".format(ret))

    _plugin.grblSettingsText = ret

    return ret


def cleanup_due_to_uninstall(_plugin, remove_profile=True):
    _plugin._logger.debug("_bgs: cleanup_due_to_uninstall remove_profile=[{}]".format(remove_profile))

    # re-enable model size detection, sd card support, and send checksum
    _plugin._settings.global_set_boolean(["feature", "modelSizeDetection"], True)
    _plugin._settings.global_set_boolean(["feature", "sdSupport"], True)
    _plugin._settings.global_set_boolean(["serial", "neverSendChecksum"], False)

    # load maps of disabled plugins & tabs
    disabledPlugins = _plugin._settings.global_get(["plugins", "_disabled"])
    disabledTabs = _plugin._settings.global_get(["appearance", "components", "disabled", "tab"])
    orderedTabs = _plugin._settings.global_get(["appearance", "components", "order", "tab"])

    if disabledPlugins == None:
        disabledPlugins = []

    if disabledTabs == None:
        disabledTabs = []

    if orderedTabs == None:
        orderedTabs = []

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

    # delete my custom controls if the built-in control tab is active
    controls = _plugin._settings.global_get(["controls"])
    if _plugin.customControls and controls:
        _plugin._settings.global_set(["controls"], [])

    # remove me from ordered tabs if i'm in there
    if "plugin_bettergrblsupport" in orderedTabs:
        orderedTabs.remove("plugin_bettergrblsupport")

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

    # add pretty much all of grbl to long running commands list
    longCmds = self._settings.global_get(["serial", "longRunningCommands"])
    if longCmds == None:
        longCmds = []

    if "$H" in longCmds: longCmds.remove("$H")
    if "G92" in longCmds: longCmds.remove("G92")
    if "G30" in longCmds: longCmds.remove("G30")
    if "G53" in longCmds: longCmds.append("G53")
    if "G54" in longCmds: longCmds.remove("G54")

    if "G20" in longCmds: longCmds.remove("G20")
    if "G21" in longCmds: longCmds.remove("G21")

    if "G90" in longCmds: longCmds.remove("G90")
    if "G91" in longCmds: longCmds.remove("G91")

    if "G38.1" in longCmds: longCmds.remove("G38.1")
    if "G38.2" in longCmds: longCmds.remove("G38.2")
    if "G38.3" in longCmds: longCmds.remove("G38.3")
    if "G38.4" in longCmds: longCmds.remove("G38.4")
    if "G38.5" in longCmds: longCmds.remove("G38.5")

    if "G0" in longCmds: longCmds.remove("G0")
    if "G1" in longCmds: longCmds.remove("G1")
    if "G2" in longCmds: longCmds.remove("G2")
    if "G3" in longCmds: longCmds.remove("G3")
    if "G4" in longCmds: longCmds.remove("G4")

    if "M3" in longCmds: longCmds.remove("M3")
    if "M4" in longCmds: longCmds.remove("M4")
    if "M5" in longCmds: longCmds.remove("M5")
    if "M7" in longCmds: longCmds.remove("M7")
    if "M8" in longCmds: longCmds.remove("M8")
    if "M9" in longCmds: longCmds.remove("M9")
    if "M30" in longCmds: longCmds.remove("M30")

    self._settings.global_set(["serial", "longRunningCommands"], longCmds)
    self._settings.global_set(["serial", "maxCommunicationTimeouts", "long"], 5)

    _plugin._settings.save()


def do_framing(_plugin, data):
    _plugin._logger.debug("_bgs: do_framing data=[{}]".format(data))

    origin = data.get("origin").strip()
    length = float(data.get("length"))
    width = float(data.get("width"))

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

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    # Linear mode, feedrate f% of max, spindle off
    _plugin._printer.commands("G1 F{} M5".format(f))

    # turn on laser in weak mode if laser mode enabled
    if is_laser_mode(_plugin):
        _plugin._printer.commands("M3 S{}".format(_plugin.weakLaserValue))

    _plugin.grblState = "Jog"
    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Jog"))


def send_frame_end_gcode(_plugin):
    _plugin._logger.debug("_bgs: send_frame_end_gcode")
    queue_cmds_and_send(_plugin, ["M5 S0 G0"])

def send_bounding_box_upper_left(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_upper_left y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ",x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))


def send_bounding_box_upper_center(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_upper_center y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, f))


def send_bounding_box_upper_right(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_upper_right y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))


def send_bounding_box_center_left(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_center_left y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2, f))


def send_bounding_box_center(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_center y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, y / 2, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2, y / 2 * -1, f))


def send_bounding_box_center_right(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_center_right y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2 * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y / 2 * -1, f))


def send_bounding_box_lower_left(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_lower_left y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))


def send_bounding_box_lower_center(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_lower_center y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x / 2 * -1, f))


def send_bounding_box_lower_right(_plugin, y, x):
    _plugin._logger.debug("_bgs: send_bounding_box_lower_right y=[{}] x=[{}]".format(y, x))

    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G1 ", y * -1, f))


def toggle_weak(_plugin):
    _plugin._logger.debug("_bgs: toggle_weak")

    # only execute if laser mode enabled
    if not is_laser_mode(_plugin):
        return

    f = int(float(_plugin.grblSettings.get(110)[0]))

    if _plugin.grblPowerLevel == 0:
        # turn on laser in weak mode
        _plugin._printer.commands("G1 F{} M3 S{}".format(f, _plugin.weakLaserValue))
        add_to_notify_queue(_plugin, ["Weak laser enabled"])
        res = "Laser Off"
    else:
        _plugin._printer.commands(["M3 S0", "M5", "G0"])
        add_to_notify_queue(_plugin, ["Weak laser disabled"])
        res = "Weak Laser"

    return res


def do_simple_zprobe(_plugin, sessionId):
    _plugin._logger.debug("_bgs: do_simple_zprobe sessionId=[{}]".format(sessionId))

    global zProbe

    if not zProbe == None:
        zProbe.teardown()
        zProbe = None

    zProbe = ZProbe(_plugin, simple_zprobe_hook, sessionId)

    zTravel = _plugin.zLimit if _plugin.zProbeTravel == 0 else _plugin.zProbeTravel
    gcode = "G91 G21 G38.2 Z-{} F100".format(zTravel)

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="simple_zprobe",
                                                                     sessionId=zProbe._sessionId,
                                                                         gcode=gcode))


def simple_zprobe_hook(_plugin, result, position):
    global zProbe
    _plugin._logger.debug("_bgs: simple_zprobe_hook result=[{}] position=[{}] sessionId=[{}]".format(result, position, zProbe._sessionId))

    sessionId = zProbe._sessionId
    zProbe.teardown()
    zProbe = None

    type = ""
    title = ""
    text = ""
    notify_type = ""

    if result == 1:
        _plugin._printer.commands(["G91", "G21", "G92 Z{}".format(_plugin.zProbeOffset), "G0 Z{}".format(_plugin.zProbeEndPos)])

        type="simple_notify"
        title="Single Point Z-Probe"
        text = "Z Axis Home has been calculated and (temporarily) set to machine position: [<B>{:.3f}</B>]".format(position - _plugin.zProbeOffset)
        notify_type="success"

        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type=type,
                                                                         sessionId=sessionId,
                                                                             title=title,
                                                                              text=text,
                                                                              hide=True,
                                                                             delay=10000,
                                                                       notify_type=notify_type))
        add_to_notify_queue(_plugin, [text])

    _plugin._logger.debug("zprobe hook position: [%f] result: [%d]", position, result)


def do_multipoint_zprobe(_plugin, sessionId):
    global zProbe
    _plugin._logger.debug("_bgs: do_multipoint_zprobe step=[{}] sessionId=[{}]".format(zProbe._step + 1 if zProbe != None else 0, sessionId))

    if zProbe == None:
        zProbe = ZProbe(_plugin, multipoint_zprobe_hook, sessionId)

    zProbe._step+=1

    if zProbe._step == 0:
        origin = _plugin._settings.get(["frame_origin"])
        width = float(_plugin._settings.get(["frame_width"]))
        length = float(_plugin._settings.get(["frame_length"]))
        preamble = "$J=" if is_grbl_one_dot_one(_plugin) else "G1 "
        zTravel = _plugin.zLimit if _plugin.zProbeTravel == 0 else _plugin.zProbeTravel
        feedrate = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

        if origin == "grblTopLeft":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width, feedrate), "action": "move", "location": "Top Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Right"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length * -1, feedrate), "action": "move", "location": "Bottom Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width * -1, feedrate), "action": "move", "location": "Bottom Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Top Left"},
                                ]
        elif origin == "grblTopCenter":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2 * -1, feedrate), "action": "move", "location": "Center Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Right"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Center Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2, feedrate), "action": "move", "location": "Top Center"},
                                ]
        elif origin == "grblTopRight":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Right"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length * -1, feedrate), "action": "move", "location": "Bottom Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width * -1, feedrate), "action": "move", "location": "Bottom Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length, feedrate), "action": "move", "location": "Top Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Left"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2 * -1, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2, feedrate), "action": "move", "location": "Top Right"},
                                ]
        elif origin == "grblCenterLeft":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Left"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2, feedrate), "action": "move", "location": "Top Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2 * -1, feedrate), "action": "move", "location": "Center Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Right"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2 * -1, feedrate), "action": "move", "location": "Center Left"},
                                ]
        elif origin == "grblCenter":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Top Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2, feedrate), "action": "move", "location": "Top Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Center"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2, feedrate), "action": "move", "location": "Top Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Right"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2 * -1, feedrate), "action": "move", "location": "Center Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Right"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2 * -1, feedrate), "action": "move", "location": "Bottom Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2 * -1, feedrate), "action": "move", "location": "Bottom Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2, feedrate), "action": "move", "location": "Center Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2, feedrate), "action": "move", "location": "Center"},
                                ]
        elif origin == "grblCenterRight":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Right"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Center Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Left"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2, feedrate), "action": "move", "location": "Top Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Center"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2 * -1, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2, feedrate), "action": "move", "location": "Center Right"},
                                ]
        elif origin == "grblBottomLeft":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length, feedrate), "action": "move", "location": "Top Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width, feedrate), "action": "move", "location": "Top Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Right"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length * -1, feedrate), "action": "move", "location": "Bottom Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Left"},
                                ]
        elif origin == "grblBottomCenter":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2, feedrate), "action": "move", "location": "Center Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Left"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2, feedrate), "action": "move", "location": "Top Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2 * -1, feedrate), "action": "move", "location": "Center Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center Right"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width / 2 * -1, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Center"},
                                ]
        elif origin == "grblBottomRight":
            zProbe._locations = [
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Right"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width * -1, feedrate), "action": "move", "location": "Bottom Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Bottom Left"},
                                    {"gcode": "{}G91 G21 Y{:f} F{}".format(preamble, length, feedrate), "action": "move", "location": "Top Left"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Left"},
                                    {"gcode": "{}G91 G21 X{:f} F{}".format(preamble, width, feedrate), "action": "move", "location": "Top Right"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Top Right"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2 * -1, length / 2 * -1, feedrate), "action": "move", "location": "Center"},
                                    {"gcode": "G91 G21 G38.2 Z-{} F100".format(zTravel),  "action": "probe", "location": "Center"},
                                    {"gcode": "{}G91 G21 X{:f} Y{:f} F{}".format(preamble, width / 2, length / 2 * -1, feedrate), "action": "move", "location": "Bottom Right"},
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

            queue_cmds_and_send(_plugin, ["G10 P1 L2 Z{:f}".format(position)])

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

            add_to_notify_queue(_plugin, [text.replace("<B>", "").replace("</B>", "")])

            zProbe.teardown()
            zProbe = None
            return

    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="multipoint_zprobe",
                                                                     sessionId=zProbe._sessionId,
                                                                   instruction=zProbe.getCurrentLocation()))


def multipoint_zprobe_hook(_plugin, result, position):
    global zProbe
    _plugin._logger.debug("_bgs: multipoint_zprobe_hook result=[{}] position=[{}] sessionId=[{}]".format(result, position, zProbe._sessionId))

    # did we have a problem?
    if result == 0:
        zProbe.teardown()
        zProbe = None
        return
    else:
        location = zProbe.getCurrentLocation()['location']
        notification = "Z-Probe [{}] location result [{:.3f}]".format(location, position)
        add_to_notify_queue(_plugin, [notification])

        # max z feed rate -- we'll do 50% of it
        zf = round(float(_plugin.grblSettings.get(112)[0]) * .5)
        _plugin._printer.commands("{}G91 G21 Z{} F{}".format("$J=" if is_grbl_one_dot_one(_plugin) else "G0 ", _plugin.zProbeEndPos, zf))

    # defer setup of the next step
    threading.Thread(target=defer_do_multipoint_zprobe, args=(_plugin, zProbe._sessionId)).start()


def defer_do_multipoint_zprobe(_plugin, sessionId):
    _plugin._logger.debug("_bgs: defer_do_multipoint_zprobe sessionId=[{}]".format(sessionId))
    # time.sleep(1)
    _plugin.grblCmdQueue.append("%%% eat me %%%")
    wait_for_empty_cmd_queue(_plugin)

    do_multipoint_zprobe(_plugin, sessionId)


def multipoint_zprobe_move(_plugin):
    global zProbe
    _plugin._logger.debug("_bgs: multipoint_zprobe_move sessionId=[{}]".format(zProbe._sessionId))

    # setup the next step
    do_multipoint_zprobe(_plugin, zProbe._sessionId)


def is_zprobe_active():
    global zProbe
    return zProbe != None


def grbl_alarm_or_error_occurred(_plugin):
    global zProbe
    _plugin._logger.debug("_bgs: grbl_alarm_or_error_occurred sessionId=[{}]".format(zProbe._sessionId if zProbe != None else "{None}"))

    if zProbe != None:
        zProbe.teardown()
        zProbe = None


def queue_cmds_and_send(_plugin, cmds, wait=False):
    _plugin._logger.debug("_bgs: queue_cmds_and_send cmds=[{}] wait=[{}]".format(cmds, wait))

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


def add_to_notify_queue(_plugin, notifications):
    _plugin._logger.debug("_bgs: add_to_notify_queue notifications=[{}]".format(notifications))

    if not zProbe is None:
        zProbe.notify(notifications)

    for notification in notifications:
        _plugin._logger.debug("queuing notification [%s]", notification)
        _plugin.notifyQueue.append(notification)


def is_laser_mode(_plugin):
    _plugin._logger.debug("_bgs: is_laser_mode={}".format(int(float(_plugin.grblSettings.get(32)[0])) != 0))
    return int(float(_plugin.grblSettings.get(32)[0])) != 0


def is_grbl_one_dot_one(_plugin):
    _plugin._logger.debug("_bgs: is_grbl_one_dot_one")
    return "1.1" in _plugin.grblVersion


def do_fake_ack(printer, logger):
    time.sleep(1)
    printer.fake_ack()
    logger.debug("_bgs: do_fake_ack")
