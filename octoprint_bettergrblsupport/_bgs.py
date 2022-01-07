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

from .zprobe import ZProbe

zProbe = None

def loadGrblDescriptions(_plugin):
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


def loadGrblSettings(_plugin):
    _plugin.grblSettingsText = _plugin._settings.get(["grblSettingsText"])

    if not _plugin.grblSettingsText is None:
        for setting in _plugin.grblSettingsText.split("||"):
            if len(setting.strip()) > 0:

                _plugin._logger.debug("loadGrblSettings=[{}]".format(setting))

                set = setting.split("|")
                if not set is None:
                    _plugin.grblSettings.update({int(set[0]): [set[1], _plugin.grblSettingsNames.get(int(set[0]))]})
    return


def saveGrblSettings(_plugin):
    ret = ""
    for id, data in sorted(_plugin.grblSettings.items(), key=lambda x: int(x[0])):
        ret = ret + "{}|{}|{}||".format(id, data[0], data[1])

    _plugin._logger.debug("saveGrblSettings=[{}]".format(ret))

    _plugin.grblSettingsText = ret
    return ret


def cleanUpDueToUninstall(_plugin, remove_profile=True):
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

    _plugin._settings.save()


def do_framing(_plugin, data):
    origin = data.get("origin").strip()

    send_frame_init_gcode(_plugin)

    if (origin == "grblTopLeft"):
        send_bounding_box_upper_left(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblTopCenter"):
        send_bounding_box_upper_center(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblTopRight"):
        send_bounding_box_upper_right(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblCenterLeft"):
        send_bounding_box_center_left(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblCenter"):
        send_bounding_box_center(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblCenterRight"):
        send_bounding_box_center_right(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblBottomLeft"):
        send_bounding_box_lower_left(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblBottomCenter"):
        send_bounding_box_lower_center(_plugin, float(data.get("length")), float(data.get("width")))

    if (origin == "grblBottomRight"):
        send_bounding_box_lower_right(_plugin, float(data.get("length")), float(data.get("width")))

    send_frame_end_gcode(_plugin)


def send_frame_init_gcode(_plugin):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    # Linear mode, feedrate f% of max, spindle off
    _plugin._printer.commands("G1 F{} M5".format(f))

    # turn on laser in weak mode if laser mode enabled
    if isLaserMode(_plugin):
        _plugin._printer.commands("M3 S{}".format(_plugin.weakLaserValue))

    _plugin.grblState = "Jog"
    _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type="grbl_state", state="Jog"))


def send_frame_end_gcode(_plugin):
    queue_cmds_and_send(_plugin, ["M5 S0 G0"])

def send_bounding_box_upper_left(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ",x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))


def send_bounding_box_upper_center(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2, f))


def send_bounding_box_upper_right(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))


def send_bounding_box_center_left(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y / 2, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y / 2, f))


def send_bounding_box_center(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 X{:f} Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2 * -1, y / 2, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2, y / 2 * -1, f))


def send_bounding_box_center_right(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y / 2 * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y / 2 * -1, f))


def send_bounding_box_lower_left(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))


def send_bounding_box_lower_center(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2 * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x / 2 * -1, f))


def send_bounding_box_lower_right(_plugin, y, x):
    f = int(float(_plugin.grblSettings.get(110)[0]) * (float(_plugin.framingPercentOfMaxSpeed) * .01))

    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x * -1, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y, f))
    _plugin._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", x, f))
    _plugin._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if isGrblOneDotOne(_plugin) else "G1 ", y * -1, f))


def toggleWeak(_plugin):
    # only execute if laser mode enabled
    if not isLaserMode(_plugin):
        return

    f = int(float(_plugin.grblSettings.get(110)[0]))

    if _plugin.grblPowerLevel == 0:
        # turn on laser in weak mode
        _plugin._printer.commands("G1 F{} M3 S{}".format(f, _plugin.weakLaserValue))
        addToNotifyQueue(_plugin, ["Weak laser enabled"])
        res = "Laser Off"
    else:
        _plugin._printer.commands(["M3 S0", "M5", "G0"])
        addToNotifyQueue(_plugin, ["Weak laser disabled"])
        res = "Weak Laser"

    return res


def do_simple_zprobe(_plugin):
    global zProbe

    if not zProbe == None:
        zProbe.teardown()
        zProbe = None

    zProbe = ZProbe(_plugin, simple_zprobe_hook)
    zProbe.probe()

def simple_zprobe_hook(_plugin, result, position):
    global zProbe
    zProbe.teardown()
    zProbe = None

    type = ""
    title = ""
    text = ""
    notify_type = ""

    if result == 1:
        _plugin._printer.commands(["G91", "G21", "G92 Z{}".format(_plugin.zProbeOffset), "G0 Z{}".format(_plugin.zProbeEndPos)])

        type="simple_notify"
        title="Z Probe Completed"
        text = "Z Axis Home has been calculated and (temporarily) set to machine location: [{:.3f}]".format(position - _plugin.zProbeOffset)
        notify_type="success"

        _plugin._plugin_manager.send_plugin_message(_plugin._identifier, dict(type=type,
                                                                             title=title,
                                                                              text=text,
                                                                              hide=False,
                                                                             delay=0,
                                                                       notify_type=notify_type))
        addToNotifyQueue(_plugin, [text])

    _plugin._logger.debug("zprobe hook position: [%f] result: [%d]", position, result)


def do_multipoint_zprobe(_plugin):
    global zProbe

    if zProbe == None:
        zProbe = ZProbe(_plugin, multipoint_zprobe_hook)

    if zProbe._step == -1:
        zProbe._step+=1

        origin = _plugin._settings.get(["frame_origin"])
        width = float(_plugin._settings.get(["frame_width"])) * .8
        length = float(_plugin._settings.get(["frame_length"])) * .8

        if origin == "grblTopLeft":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblTopCenter":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblTopRight":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblCenterLeft":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblCenter":
            zProbe._locations = [
                                    {x: 0, y: 0, action: "probe"},
                                    {x: width / 2 * -1, y: height / 2 * -1, action: "probe"},
                                    {x: width, y: 0, action: "probe"},
                                    {x: 0, y: width, action: "probe"},
                                    {x: width * -1, y: 0, action: "probe"},
                                    {x: width / 2, y: height / 2, action: "end"}
                                ]


        elif origin == "grblCenterRight":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblBottomLeft":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblBottomCenter":
            zProbe.teardown()
            zProbe = None
        elif origin == "grblBottomRight":
            zProbe.teardown()
            zProbe = None

    zProbe.probe()

def multipoint_zprobe_hook(_plugin, result, position):
    zProbe._step+=1
    _plugin._logger.debug("multipoint_zprobe_hook")



def queue_cmds_and_send(_plugin, cmds, wait=False):
    for cmd in cmds:
        _plugin._logger.debug("queuing command [%s] wait=%r", cmd, wait)
        _plugin.grblCmdQueue.append(cmd)

    if wait:
        _plugin._logger.debug("waiting for command queue to drain")

        while len(_plugin.grblCmdQueue) > 0:
            time.sleep(.001)

        _plugin._logger.debug("done waiting for command queue to drain")


def addToNotifyQueue(_plugin, notifications):

    if not zProbe is None:
        zProbe.notify(notifications)

    for notification in notifications:
        _plugin._logger.debug("queuing notification [%s]", notification)
        _plugin.notifyQueue.append(notification)


def isLaserMode(_plugin):
    return int(float(_plugin.grblSettings.get(32)[0])) != 0


def isGrblOneDotOne(_plugin):
    return "1.1" in _plugin.grblVersion
