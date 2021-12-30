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
# https://reprap.org/wiki/G-code
#
from __future__ import absolute_import
from octoprint.events import Events
from timeit import default_timer as timer
from shutil import copyfile

# import sys
import time
import math
import os
import subprocess

import octoprint.plugin
import re
import logging
import json
import flask

class BetterGrblSupportPlugin(octoprint.plugin.SettingsPlugin,
                              octoprint.plugin.SimpleApiPlugin,
                              octoprint.plugin.AssetPlugin,
                              octoprint.plugin.TemplatePlugin,
                              octoprint.plugin.StartupPlugin,
                              octoprint.plugin.EventHandlerPlugin,
                              octoprint.plugin.RestartNeedingPlugin):

    def __init__(self):
        self.hideTempTab = True
        self.hideControlTab = True
        self.hideGCodeTab = True
        self.customControls = False
        self.helloCommand = "$$"
        self.statusCommand = "?"
        self.dwellCommand = "G4 P0"
        self.positionCommand = "?"
        self.suppressM114 = True
        self.suppressM400 = True
        self.suppressM105 = True
        self.suppressM115 = True
        self.suppressM110 = True
        self.disablePolling = True
        self.disableModelSizeDetection = True
        self.neverSendChecksum = True
        self.reOrderTabs = True
        self.disablePrinterSafety = True
        self.zProbeOffset = 15.00
        self.weakLaserValue = 1
        self.framingPercentOfMaxSpeed = 25

        self.lastGCommand = ""

        self.overrideM8 = False
        self.overrideM9 = False
        self.m8Command = ""
        self.m9Command = ""

        self.grblMode = None
        self.grblState = None
        self.grblX = float(0)
        self.grblY = float(0)
        self.grblZ = float(0)
        self.grblSpeed = 0
        self.grblPowerLevel = 0
        self.positioning = 0
        self.distance = float(0)

        self.timeRef = 0

        self.grblErrors = {}
        self.grblAlarms = {}
        self.grblSettingsNames = {}
        self.grblSettings = {}
        self.grblSettingsText = ""

        self.ignoreErrors = False
        self.doSmoothie = False

        self.grblCmdQueue = []
        self.notifyQueue = []

        self.customControlsJson = r'[{"layout": "horizontal", "children": [{"commands": ["$10=0", "G28.1", "G92 X0 Y0 Z0"], "name": "Set Origin", "confirm": null}, {"command": "M999", "name": "Reset", "confirm": null}, {"commands": ["G1 F4000 S0", "M5", "$SLP"], "name": "Sleep", "confirm": null}, {"command": "$X", "name": "Unlock", "confirm": null}, {"commands": ["$32=0", "M4 S1"], "name": "Weak Laser", "confirm": null}, {"commands": ["$32=1", "M5"], "name": "Laser Off", "confirm": null}], "name": "Laser Commands"}, {"layout": "vertical", "type": "section", "children": [{"regex": "<([^,]+)[,|][WM]Pos:([+\\-\\d.]+,[+\\-\\d.]+,[+\\-\\d.]+)", "name": "State", "default": "", "template": "State: {0} - Position: {1}", "type": "feedback"}, {"regex": "F([\\d.]+) S([\\d.]+)", "name": "GCode State", "default": "", "template": "Speed: {0}  Power: {1}", "type": "feedback"}], "name": "Realtime State"}]'

        self.grblVersion = "unknown"

        self.xLimit = float(0)
        self.yLimit = float(0)
        self.zLimit = float(0)

        # load up our item/value pairs for errors, warnings, and settings
        self.loadGrblDescriptions()


    # #~~ SettingsPlugin mixin
    def get_settings_defaults(self):
        return dict(
            hideTempTab = True,
            hideControlTab = True,
            hideGCodeTab = True,
            hello = "$$",
            statusCommand = "?",
            dwellCommand = "G4 P0",
            positionCommand = "?",
            suppressM114 = True,
            suppressM400 = True,
            suppressM105 = True,
            suppressM115 = True,
            suppressM110 = True,
            disablePolling = True,
            customControls = True,
            frame_length = 100,
            frame_width = 100,
            frame_origin = None,
            distance = 0,
            is_printing = False,
            is_operational = False,
            disableModelSizeDetection = True,
            neverSendChecksum = True,
            reOrderTabs = True,
            disablePrinterSafety = True,
            grblSettingsText = None,
            grblSettingsBackup = "",
            zProbeOffset = 15.00,
            weakLaserValue = 1,
            framingPercentOfMaxSpeed = 25,
            overrideM8 = False,
            overrideM9 = False,
            m8Command = "/home/pi/bin/tplink_smartplug.py -t air-assist.shellware.com -c on",
            m9Command = "/home/pi/bin/tplink_smartplug.py -t air-assist.shellware.com -c off",
            ignoreErrors = False,
            doSmoothie = False,
            grblVersion = "unknown",
            laserMode = False,
            old_profile = "_default"
        )


    def on_after_startup(self):
        # establish initial state for printer status
        self._settings.set_boolean(["is_printing"], self._printer.is_printing())
        self._settings.set_boolean(["is_operational"], self._printer.is_operational())

        # fix for V-Carve Grbl Toolpath referencing T1
        dest = self._settings.global_get_basefolder("printerProfiles") + os.path.sep + "_bgs.profile"

        if not os.path.exists(dest):
            src = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep + "_bgs.profile"
            copyfile(src, dest)
            self._settings.set(["old_profile"], self._printer_profile_manager.get_current_or_default()["id"])
            self._printer_profile_manager.select("_bgs")
            self._printer_profile_manager.set_default("_bgs")
            self._logger.info("bgs printer profile created and selected")

        # let's only do stuff if our profile is selected
        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            self._logger.info("bgs printer profile is not active")
            return

        # initialize all of our settings
        self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
        self.hideControlTab = self._settings.get_boolean(["hideControlTab"])
        self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])
        self.customControls = self._settings.get_boolean(["customControls"])

        self.helloCommand = self._settings.get(["hello"])
        self.statusCommand = self._settings.get(["statusCommand"])
        self.dwellCommand = self._settings.get(["dwellCommand"])
        self.positionCommand = self._settings.get(["positionCommand"])
        self.suppressM105 = self._settings.get_boolean(["suppressM105"])
        self.suppressM114 = self._settings.get_boolean(["suppressM114"])
        self.suppressM115 = self._settings.get_boolean(["suppressM115"])
        self.suppressM110 = self._settings.get_boolean(["suppressM110"])
        self.suppressM400 = self._settings.get_boolean(["suppressM400"])
        self.disablePolling = self._settings.get_boolean(["disablePolling"])

        self.disableModelSizeDetection = self._settings.get_boolean(["disableModelSizeDetection"])
        self.neverSendChecksum = self._settings.get_boolean(["neverSendChecksum"])
        self.reOrderTabs = self._settings.get_boolean(["reOrderTabs"])

        self.overrideM8 = self._settings.get_boolean(["overrideM8"])
        self.overrideM9 = self._settings.get_boolean(["overrideM9"])
        self.m8Command = self._settings.get(["m8Command"])
        self.m9Command = self._settings.get(["m9Command"])

        self.ignoreErrors = self._settings.get(["ignoreErrors"])
        self.doSmoothie = self._settings.get(["doSmoothie"])

        self.zProbeOffset = self._settings.get(["zProbeOffset"])
        self.weakLaserValue = self._settings.get(["weakLaserValue"])
        self.framingPercentOfMaxSpeed = self._settings.get(["framingPercentOfMaxSpeed"])

        self.grblSettingsText = self._settings.get(["grblSettingsText"])
        self.grblVersion = self._settings.get(["grblVersion"])

        self.distance = self._settings.get(["distance"])

        # hardcoded global settings -- should revisit how I manage these
        self._settings.global_set_boolean(["feature", "modelSizeDetection"], not self.disableModelSizeDetection)
        self._settings.global_set_boolean(["serial", "neverSendChecksum"], self.neverSendChecksum)

        if self.neverSendChecksum:
            self._settings.global_set(["serial", "checksumRequiringCommands"], [])

        # load a map of disabled plugins
        disabledPlugins = self._settings.global_get(["plugins", "_disabled"])
        if disabledPlugins == None:
            disabledPlugins = []

        # disable the printer safety check plugin
        if self.disablePrinterSafety:
            if "printer_safety_check" not in disabledPlugins:
                disabledPlugins.append("printer_safety_check")
        else:
            if "printer_safety_check" in disabledPlugins:
                disabledPlugins.remove("printer_safety_check")

        # disable the gcodeviewer plugin
        if self.hideGCodeTab:
            if "gcodeviewer" not in disabledPlugins:
                disabledPlugins.append("gcodeviewer")
        else:
            if "gcodeviewer" in disabledPlugins:
                disabledPlugins.remove("gcodeviewer")

        self._settings.global_set(["plugins", "_disabled"], disabledPlugins)

        # process tabs marked as disabled
        disabledTabs = self._settings.global_get(["appearance", "components", "disabled", "tab"])
        if disabledTabs == None:
            disabledTabs = []

        if self.hideTempTab:
            if "temperature" not in disabledTabs:
                disabledTabs.append("temperature")
        else:
            if "temperature" in disabledTabs:
                disabledTabs.remove("temperature")

        if self.hideControlTab:
            if "control" not in disabledTabs:
                disabledTabs.append("control")
        else:
            if "control" in disabledTabs:
                disabledTabs.remove("control")

        if self.hideGCodeTab:
            if "gcodeviewer" not in disabledTabs:
                disabledTabs.append("plugin_gcodeviewer")
        else:
            if "gcodeviewer" in disabledTabs:
                disabledTabs.remove("plugin_gcodeviewer")

        self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)

        if not self.hideControlTab:
            controls = self._settings.global_get(["controls"])

            if self.customControls and not controls:
                self._logger.debug("injecting custom controls")
                self._settings.global_set(["controls"], json.loads(self.customControlsJson))
            else:
                if not self.customControls and controls:
                    self._logger.debug("clearing custom controls")
                    self._settings.global_set(["controls"], [])

        orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

        # ensure i am always the first tab
        if self.reOrderTabs:
            orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

            if "plugin_bettergrblsupport" in orderedTabs:
                orderedTabs.remove("plugin_bettergrblsupport")

            orderedTabs.insert(0, "plugin_bettergrblsupport")
            self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        self._settings.save()
        self.loadGrblSettings()


    def get_settings_version(self):
        return 3

    def on_settings_migrate(self, target, current):
        if current == None or current != target:
            orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])
            if "gcodeviewer" in orderedTabs:
                orderedTabs.remove("gcodeviewer")
                self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

            disabledTabs = self._settings.global_get(["appearance", "components", "disabled", "tab"])

            if "gcodeviewer" in disabledTabs:
                disabledTabs.remove("gcodeviewer")

            if self._settings.get(["statusCommand"]) == "?$G":
                self._settings.set(["statusCommand"], "?")
                self.statusCommand = "?"

            self._settings.remove(["showZ"])

            self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)
            self._settings.save()

            self._logger.info("Migrated to settings v%d from v%d", target, 1 if current == None else current)


    def loadGrblDescriptions(self):
        path = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep

        f = open(path + "grbl_errors.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblErrors[int(match.groups(1)[0])] = match.groups(1)[1]
                # self._logger.debug("matching error id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

        f = open(path + "grbl_alarms.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblAlarms[int(match.groups(1)[0])] = match.groups(1)[1]
                # self._logger.debug("matching alarm id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])

        f = open(path + "grbl_settings.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblSettingsNames[int(match.groups(1)[0])] = match.groups(1)[1]
                # self._logger.debug("matching setting id: [%d] to description: [%s]", int(match.groups(1)[0]), match.groups(1)[1])


    def loadGrblSettings(self):
        self.grblSettingsText = self._settings.get(["grblSettingsText"])

        if not self.grblSettingsText is None:
            for setting in self.grblSettingsText.split("||"):
                if len(setting.strip()) > 0:

                    self._logger.debug("loadGrblSettings=[{}]".format(setting))

                    set = setting.split("|")
                    if not set is None:
                        self.grblSettings.update({int(set[0]): [set[1], self.grblSettingsNames.get(int(set[0]))]})
        return


    def saveGrblSettings(self):
        ret = ""
        for id, data in sorted(self.grblSettings.items(), key=lambda x: int(x[0])):
            ret = ret + "{}|{}|{}||".format(id, data[0], data[1])

        self._logger.debug("saveGrblSettings=[{}]".format(ret))

        self.grblSettingsText = ret
        return ret


    def on_settings_save(self, data):
        self._logger.debug("saving settings")
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        # reload our config
        self.on_after_startup()

        # let's only do stuff if our profile is selected
        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            return

        # refresh our grbl settings
        if not self._printer.is_printing():
            if self.doSmoothie:
                self._printer.commands("Cat /sd/config")
            else:
                self._printer.commands("$$")


    # #~~ AssetPlugin mixin
    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return dict(js=['js/bettergrblsupport.js', 'js/bettergrblsupport_settings.js'],
                    css=['css/bettergrblsupport.css', 'css/bettergrblsupport_settings.css'],
                    less=['less/bettergrblsupport.less', "less/bettergrblsupport.less"])


    # #~~ TemplatePlugin mixin
    def get_template_configs(self):
        return [
            dict(type="settings", template="bettergrblsupport_settings.jinja2", custom_bindings=True)
        ]

    # def get_template_vars(self):
    #     return dict(grblSettingsText=self.saveGrblSettings())


    # #-- EventHandlerPlugin mix-in
    def on_event(self, event, payload):
        subscribed_events = (Events.FILE_SELECTED, Events.PRINT_STARTED, Events.PRINT_CANCELLED, Events.PRINT_CANCELLING,
                            Events.PRINT_PAUSED, Events.PRINT_RESUMED, Events.PRINT_DONE, Events.PRINT_FAILED,
                            Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN, Events.UPLOAD, Events.CONNECTING)

        if event not in subscribed_events:
            self._logger.debug('event [{}] received but not subscribed - discarding'.format(event))
            return

        # our plugin is being uninstalled
        if event == Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN and payload["id"] == self._identifier:
            self._logger.debug('we are being uninstalled :(')
            self.cleanUpDueToUninstall()
            self._logger.debug('uninstall cleanup completed (this house is clean)')
            return

        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            return

        # - CONNECTING
        if event == Events.CONNECTING:
            # let's make sure we don't have any commands queued up
            self.grblCmdQueue.clear()

        # 'PrintStarted'
        if event == Events.PRINT_STARTED:
            if self.grblState != "Idle":
                # we have to stop This
                self._printer.cancel_print()
                return

            self.grblState = "Run"
            addToNotifyQueue(["Pgm Begin"])
            return

        # Print ended (finished / failed / cancelled)
        if event == Events.PRINT_CANCELLED or event == Events.PRINT_DONE or event == Events.PRINT_FAILED:
            self.grblState = "Idle"
            return

        # Print Cancelling
        if event == Events.PRINT_CANCELLING:
            self._logger.debug("canceling job")
            self._printer.commands(["!", "?"], force=True)
            self.queue_cmds_and_send(["M999", "?"], wait=True)

        # Print Paused
        if event == Events.PRINT_PAUSED:
            self._logger.debug("pausing job")
            self._printer.commands("!", force=True)

        # Print Resumed
        if event == Events.PRINT_RESUMED:
            self._logger.debug("resuming job")
            self._printer.commands(["~", "?"], force=True)
            self.queue_cmds_and_send(["?"])

        # File uploaded
        if event == Events.UPLOAD:
            if payload["path"].endswith(".gc") or payload["path"].endswith(".nc"):
                # uploaded_file = self._settings.global_get_basefolder("uploads") + '/' + payload["path"]
                # renamed_file = uploaded_file[:len(uploaded_file) - 2] + "gcode"
                renamed_file = payload["path"][:len(payload["path"]) - 2] + "gcode"

                self._logger.debug("renaming [%s] to [%s]", payload["path"], renamed_file)

                self._file_manager.remove_file(payload["target"], renamed_file)
                self._file_manager.move_file(payload["target"], payload["path"], renamed_file)
                # os.rename(uploaded_file, renamed_file)

        # 'FileSelected'
        if event == Events.FILE_SELECTED:
            selected_file = self._settings.global_get_basefolder("uploads") + '/' + payload['path']
            f = open(selected_file, 'r')

            minX = float("inf")
            minY = float("inf")
            maxX = float("-inf")
            maxY = float("-inf")

            x = float(0)
            y = float(0)

            lastGCommand = ""
            positioning = 0

            start = timer()

            for line in f:
                # save our G command for shorthand post processors
                if line.upper().startswith("G"):
                    lastGCommand = line[:3] if line[2:3].isnumeric() else line[:2]

                # use our saved G command if our line starts with a coordinate
                if line.upper().lstrip().startswith(("X", "Y", "Z")) and line.lstrip()[1:2].isnumeric():
                    command = lastGCommand + " " + line.upper().strip()

                else:
                    command = line.upper().strip()

                if "G90" in command.upper():
                    # absolute positioning
                    positioning = 0
                    continue

                if "G91" in command.upper():
                    # relative positioning
                    positioning = 1
                    continue

                match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
                # match = re.search(r".*[Xx]\ *(-?[\d.]+).*", command)
                if not match is None:
                    if positioning == 1:
                        x = x + float(match.groups(1)[2])
                    else:
                        x = float(match.groups(1)[2])
                    if x < minX:
                        minX = x
                    if x > maxX:
                        maxX = x

                match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
                # match = re.search(r".*[Yy]\ *(-?[\d.]+).*", command)
                if not match is None:
                    if positioning == 1:
                        y = y + float(match.groups(1)[2])
                    else:
                        y = float(match.groups(1)[2])
                    if y < minY:
                        minY = y
                    if y > maxY:
                        maxY = y

            length = math.ceil(maxY - minY)
            width = math.ceil(maxX - minX)

            self._logger.debug('finished reading file length={} width={} time={}'.format(length, width, timer() - start))

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_frame_size",
                                                                            length=length,
                                                                            width=width))
            return

        if event == Events.FILE_DESELECTED:
            return

        return


    def on_plugin_pending_uninstall(self):  # this will work in some next release of octoprint
        self._logger.debug('we are being uninstalled via on_plugin_pending_uninstall :(')
        self.cleanUpDueToUninstall()
        self._logger.debug('uninstall cleanup completed (this house is clean)')


    def cleanUpDueToUninstall(self, remove_profile=True):
        # re-enable model size detection and send checksum
        self._settings.global_set_boolean(["feature", "modelSizeDetection"], self.disableModelSizeDetection)
        self._settings.global_set_boolean(["serial", "neverSendChecksum"], not self.neverSendChecksum)

        # load maps of disabled plugins & tabs
        disabledPlugins = self._settings.global_get(["plugins", "_disabled"])
        disabledTabs = self._settings.global_get(["appearance", "components", "disabled", "tab"])
        orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

        if disabledPlugins == None:
            disabledPlugins = []

        if disabledTabs == None:
            disabledTabs = []

        if orderedTabs == None:
            orderedTabs = []

        # re-enable the printer safety check plugin
        if self.disablePrinterSafety:
            if "printer_safety_check" in disabledPlugins:
                disabledPlugins.remove("printer_safety_check")

        # re-enable the gcodeviewer plugin
        if self.hideGCodeTab:
            if "gcodeviewer" in disabledPlugins:
                disabledPlugins.remove("gcodeviewer")
            if "plugin_gcodeviewer" in disabledTabs:
                disabledTabs.remove("plugin_gcodeviewer")

        # re-enable the built-in temp tab if it was hidden
        if self.hideTempTab:
            if "temperature" in disabledTabs:
                disabledTabs.remove("temperature")

        # re-enable the built-in control tab if it was hidden
        if self.hideControlTab:
            if "control" in disabledTabs:
                disabledTabs.remove("control")

        # delete my custom controls if the built-in control tab is active
        if not self.hideControlTab:
            controls = self._settings.global_get(["controls"])
            if self.customControls and controls:
                self._settings.global_set(["controls"], [])

        # remove me from ordered tabs if i'm in there
        if "plugin_bettergrblsupport" in orderedTabs:
            orderedTabs.remove("plugin_bettergrblsupport")

        if remove_profile:
            # restore the original printer profile (if it exists) and delete mine
            old_profile = self._settings.get(["old_profile"])

            if not old_profile or not self._printer_profile_manager.exists(old_profile):
                old_profile = "_default"

            self._printer_profile_manager.select(old_profile)
            self._printer_profile_manager.set_default(old_profile)

            if self._printer_profile_manager.exists("_bgs"):
                self._printer_profile_manager.remove("_bgs")
                self._logger.debug("bgs profile has been deleted")

        self._settings.global_set(["plugins", "_disabled"], disabledPlugins)
        self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)
        self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        self._settings.save()

    def get_extension_tree(self, *args, **kwargs):
    		return dict(
                    model=dict(
    				grbl_gcode=["gc", "nc"]
    			)
    		)

    # #-- gcode sending hook
    def hook_gcode_sending(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        # let's only do stuff if our profile is selected
        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            return None

        # suppress comments and extraneous commands that may cause wayward
        # grbl instances to error out
        if cmd.upper().lstrip().startswith(tuple([';', '(', '%'])):
            self._logger.debug('Ignoring extraneous [%s]', cmd)
            return (None, )

        # M8 (air assist on) processing - work in progress
        if cmd.upper().strip() == "M8" and self.overrideM8:
            self._logger.debug('Turning ON Air Assist')
            subprocess.call(self.m8Command, shell=True)
            return (None,)

        # M9 (air assist off) processing - work in progress
        if cmd.upper().strip() == "M9" and self.overrideM9:
            self._logger.debug('Turning OFF Air Assist')
            subprocess.call(self.m9Command, shell=True)
            return (None,)

        # rewrite M115 firmware as $$ (hello)
        if self.suppressM115 and cmd.upper().startswith('M115'):
            self._logger.debug('Rewriting M115 as %s' % self.helloCommand)

            if self.doSmoothie:
                return "Cat /sd/config"

            return self.helloCommand

        # suppress reset line #s
        if self.suppressM110 and cmd.upper().startswith('M110'):
            self._logger.debug('Ignoring %s', cmd)
            return ("$I", )

        # suppress initialize SD - M21
        if cmd.upper().startswith('M21'):
            self._logger.debug('Ignoring %s', cmd)
            return (None,)

        # suppress temperature if printer is printing
        if cmd.upper().startswith('M105'):
            if self.disablePolling and self._printer.is_printing():
                self._logger.debug('Ignoring %s', cmd)
                return (None, )
            else:
                if self.suppressM105:
                    self._logger.debug('Rewriting M105 as %s' % self.statusCommand)
                    return (self.statusCommand, )

        # Wait for moves to finish before turning off the spindle
        if self.suppressM400 and cmd.upper().startswith('M400'):
            self._logger.debug('Rewriting M400 as %s' % self.dwellCommand)
            return (self.dwellCommand, )

        # rewrite M114 current position as ? (typically)
        if self.suppressM114 and cmd.upper().startswith('M114'):
            self._logger.debug('Rewriting M114 as %s' % self.positionCommand)
            return (self.positionCommand, )

        # soft reset / resume (stolen from Marlin)
        if cmd.upper().startswith('M999') and not self.doSmoothie:
            self._logger.debug('Sending Soft Reset')
            self.addToNotifyQueue(["Machine has been reset"])
            return ("\x18",)

        # ignore all of these -- they do not apply to GRBL
        # M108 (heater off)
        # M84 (disable motors)
        # M104 (set extruder temperature)
        # M140 (set bed temperature)
        # M106 (fan on/off)
        if cmd.upper().startswith(("M108", "M84", "M104", "M140", "M106")):
            self._logger.debug("ignoring [%s]", cmd)
            return (None, )

        # we need to track absolute position mode for "RUN" position updates
        if "G90" in cmd.upper():
            # absolute positioning
            self.positioning = 0

        # we need to track relative position mode for "RUN" position updates
        if "G91" in cmd.upper():
            # relative positioning
            self.positioning = 1

        # save our G command for shorthand post processors
        if cmd.upper().startswith("G"):
            self.lastGCommand = cmd[:3] if cmd[2:3].isnumeric() else cmd[:2]

        # use our saved G command if our line starts with a coordinate
        if cmd.upper().lstrip().startswith(("X", "Y", "Z")) and cmd.upper().lstrip()[1:2].isnumeric():
            command = self.lastGCommand + " " + cmd.upper().strip()
        else:
            command = cmd.upper().strip()

        # keep track of distance traveled
        found = False

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Xx]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblX = float(match.groups(1)[0]) if self.positioning == 0 else self.grblX + float(match.groups(1)[0])
            found = True

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Yy]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblY = float(match.groups(1)[0]) if self.positioning == 0 else self.grblY + float(match.groups(1)[0])
            found = True

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Zz]|[Zz])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Zz]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblZ = float(match.groups(1)[0]) if self.positioning == 0 else self.grblZ + float(match.groups(1)[0])
            found = True

        # match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ff]|[Ff])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Ff]\ *(-?[\d.]+).*", command)
        if not match is None:
            grblSpeed = round(float(match.groups(1)[0]))

            # make sure we post all speed on / off events
            if (grblSpeed == 0 and self.grblSpeed != 0) or (self.grblSpeed == 0 and grblSpeed != 0):
                self.timeRef = 0

            self.grblSpeed = grblSpeed
            found = True

        # match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ss]|[Ss])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Ss]\ *(-?[\d.]+).*", command)
        if not match is None:
            grblPowerLevel = round(float(match.groups(1)[0]))

            # make sure we post all power on / off events
            self.grblPowerLevel = grblPowerLevel
            found = True

        if found:
            currentTime = int(round(time.time() * 1000))
            if currentTime > self.timeRef + 250:
                # self._logger.info("x={} y={} z={} f={} s={}".format(self.grblX, self.grblY, self.grblZ, self.grblSpeed, self.grblPowerLevel))
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                                mode=self.grblMode,
                                                                                state=self.grblState,
                                                                                x=self.grblX,
                                                                                y=self.grblY,
                                                                                z=self.grblZ,
                                                                                speed=self.grblSpeed,
                                                                                power=self.grblPowerLevel))
                self.timeRef = currentTime

        return (command, )


    # #-- gcode received hook (
    # original author:  https://github.com/mic159
    # source: https://github.com/mic159/octoprint-grbl-plugin)
    def hook_gcode_received(self, comm_instance, line, *args, **kwargs):
        # let's only do stuff if our profile is selected
        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            return None

        if line.startswith('Grbl'):
            # Hack to make Arduino based GRBL work.
            # When the serial port is opened, it resets and the "hello" command
            # is not processed.
            # This makes Octoprint recognise the startup message as a successful connection.

            # force an inquiry
            # self._printer.commands("?")

            # self._plugin_manager.send_plugin_message(self._identifier, dict(type="send_notification", message=line))
            return "ok " + line

        # grbl version signature
        if line.startswith("[VER:"):
            self.grblVersion = line.strip("\n").strip("\r")
            self._settings.set(["grblVersion"], self.grblVersion)
            self._settings.save()
            return

        # grbl opt Signature
        if line.startswith("[OPT:"):
            self.grblVersion = self.grblVersion + " " + line.strip("\n").strip("\r")
            self._settings.set(["grblVersion"], self.grblVersion)
            self._settings.save()
            return

        # look for an alarm
        if line.lower().startswith('alarm:'):
            match = re.search(r'alarm:\ *(-?[\d.]+)', line.lower())

            error = int(0)

            if not match is None:
                error = int(match.groups(1)[0])
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_alarm",
                                                                                code=error,
                                                                                description=self.grblAlarms.get(error)))

                self._logger.warning("alarm received: %d: %s", error, self.grblAlarms.get(error))

            # clear out any pending queued Commands
            if len(self.grblCmdQueue) > 0:
                self._logger.debug("clearing %d commands from the command queue", len(self.grblCmdQueue))
                self.grblCmdQueue.clear()

            # put a message on our notification queue and force an inquiry
            self.addToNotifyQueue([line if error == 0 else self.grblAlarms.get(error)])
            self._printer.commands("?")

            # we need to pause if we are printing
            if self._printer.is_printing():
                self._printer.pause_print()

            return 'Error: ' + line if error == 0 else self.grblAlarms.get(error)

        # look for an error
        if not self.ignoreErrors and line.lower().startswith('error:'):
            match = re.search(r'error:\ *(-?[\d.]+)', line.lower())

            error = int(0)
            desc = line

            if not match is None:
                error = int(match.groups(1)[0])
                desc = self.grblErrors.get(error)

            self._logger.warning("error received: %d: %s", error, desc)

            # clear out any pending queued Commands
            if len(self.grblCmdQueue) > 0:
                self._logger.debug("clearing %d commands from the command queue", len(self.grblCmdQueue))
                self.grblCmdQueue.clear()

            # put a message on our notification queue and force an inquiry
            self.addToNotifyQueue([desc])
            self._printer.commands("?")

            # lets not let octoprint know if we have a gcode lock error
            if error == 9:
                self._logger.debug("not forwarding grbl error 9 to octoprint")
                return "ok " + line
            else:
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_error",
                                                                                code=error,
                                                                                description=desc))
            return 'Error: ' + desc

        # forward any messages to the notification plugin_name
        if "MSG:" in line.upper():
            ignoreList = ["[MSG:'$H'|'$X' to unlock]"]

            if line.strip() not in ignoreList:
                # auto reset
                if "reset to continue" in line.lower():
                    # automatically perform a soft reset if GRBL says we need one
                    self._printer.commands("M999")
                else:
                    self.addToNotifyQueue([line.replace("[","").replace("]","").replace("MSG:","")])

            return

        # add a notification if we just z-probed
        if "PRB:" in line.upper():
            self.addToNotifyQueue([line])
            return

        # grbl settings
        if line.startswith("$"):
            match = re.search(r'^[$](-?[\d\.]+)=(-?[\d\.]+)', line)

            if not match is None:
                settingsId = int(match.groups(1)[0])
                settingsValue = match.groups(1)[1]

                self.grblSettings.update({settingsId: [settingsValue, self.grblSettingsNames.get(settingsId)]})
                self._logger.debug("setting id={} value={} description={}".format(settingsId, settingsValue, self.grblSettingsNames.get(settingsId)))

                if settingsId >= 132:
                    self._settings.set(["grblSettingsText"], self.saveGrblSettings())
                    self._settings.set_boolean(["laserMode"], self.isLaserMode())

                    # lets populate our x,y,z limits
                    self.xLimit = float(self.grblSettings.get(130)[0])
                    self.yLimit = float(self.grblSettings.get(131)[0])
                    self.zLimit = float(self.grblSettings.get(132)[0])

                    # assign our default distance if it is not already set to the lower of x,y limits
                    if self.distance == 0:
                        self.distance = min([self.xLimit, self.yLimit])
                        self._settings.set(["distance"], self.distance)

                    self._settings.save()
                    self.addToNotifyQueue(["Grbl Settings sent"])

                return line

        # hack to force status updates
        if 'MPos' in line or 'WPos' in line:
             # <Idle,MPos:0.000,0.000,0.000,WPos:0.000,0.000,0.000,RX:3,0/0>
             # <Run|MPos:-17.380,-7.270,0.000|FS:1626,0>

            # match = re.search(r'[WM]Pos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+)', line)
            match = re.search(r'<(-?[^,]+)[,|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+)', line)

            if match is None:
                self._logger.warning('Bad data %s', line.rstrip())
                return

             # OctoPrint records positions in some instances.
             # It needs a different format. Put both on the same line so the GRBL info is not lost
             # and is accessible for "controls" to read.
            response = 'X:{1} Y:{2} Z:{3} E:0 {original}'.format(*match.groups(), original=line)

            self.grblMode = "MPos" if "MPos" in line else "WPos" if "WPos" in line else "N/A"
            self.grblState = str(match.groups(1)[0])
            self.grblX = float(match.groups(1)[1])
            self.grblY = float(match.groups(1)[2])
            self.grblZ = float(match.groups(1)[3])

            self._logger.debug('status [%s]', response.strip())

            match = re.search(r'.*\|FS:(-?[\d\.]+),(-?[\d\.]+)', line)
            if not match is None:
                self.grblSpeed = round(float(match.groups(1)[0]))
                self.grblPowerLevel = round(float(match.groups(1)[1]))

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                            mode=self.grblMode,
                                                                            state=self.grblState,
                                                                            x=self.grblX,
                                                                            y=self.grblY,
                                                                            z=self.grblZ,
                                                                            speed=self.grblSpeed,
                                                                            power=self.grblPowerLevel))

            # pop any queued commands if state is IDLE or HOLD:0
            if len(self.grblCmdQueue) > 0 and (self.grblState.upper().strip() == "IDLE" or self.grblState.upper().strip() == "HOLD:0"):
                self._logger.debug('sending queued command [%s] - depth [%d]', self.grblCmdQueue[0], len(self.grblCmdQueue))
                self._printer.commands(self.grblCmdQueue[0])
                self.grblCmdQueue.pop(0)
                return response

            # add a notification if we just homed
            if self.grblState.upper().strip() == "HOME":
                self.addToNotifyQueue(["Machine has been homed"])

            # parse the line to see if we have any other useful data
            # for stat in line.replace("<", "").replace(">", "").split("|"):
            #     # buffer stats and Pin stats
            #     if stat.startswith("Bf:") or stat.startswith("Pn:"):
            #         self.addToNotifyQueue(stat)

            # pop any queued notifications
            if len(self.notifyQueue) > 0:
                notification = self.notifyQueue[0]
                self._logger.debug('sending queued notification [%s] - depth [%d]', notification, len(self.notifyQueue))
                self.notifyQueue.pop(0)
                return "//action:notification " + notification

            return response

        if not line.rstrip().endswith('ok'):
            return

        if line.startswith('{'):
             # Regular ACKs
             # {0/0}ok
             # {5/16}ok
            return 'ok'
        elif '{' in line:
             # Ack with return data
             # F300S1000{0/0}ok
            (before, _, _) = line.partition('{')
            return 'ok ' + before
        else:
            return 'ok'


    def send_frame_init_gcode(self):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        # Linear mode, feedrate f% of max, spindle off
        self._printer.commands("G1 F{} M5".format(f))

        # turn on laser in weak mode if laser mode enabled
        if self.isLaserMode():
            self._printer.commands("M3 S{}".format(self.weakLaserValue))


    def send_frame_end_gcode(self):
        self.queue_cmds_and_send(["M5 S0 G0"], wait=True)
        self.addToNotifyQueue(["Framing operation completed"])
        self._printer.commands("?")

    def send_bounding_box_upper_left(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ",x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))


    def send_bounding_box_upper_center(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2, f))


    def send_bounding_box_upper_right(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))


    def send_bounding_box_center_left(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y / 2, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y / 2, f))


    def send_bounding_box_center(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2 * -1, y / 2, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2, y / 2 * -1, f))


    def send_bounding_box_center_right(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y / 2 * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y / 2 * -1, f))


    def send_bounding_box_lower_left(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))


    def send_bounding_box_lower_center(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2 * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x / 2 * -1, f))


    def send_bounding_box_lower_right(self, y, x):
        f = int(float(self.grblSettings.get(110)[0]) * (float(self.framingPercentOfMaxSpeed) * .01))

        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x * -1, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y, f))
        self._printer.commands("{}G21 G91 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", x, f))
        self._printer.commands("{}G21 G91 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", y * -1, f))


    def get_api_commands(self):
        return dict(
            frame=[],
            toggleWeak=[],
            originz=[],
            origin=[],
            move=[],
            sleep=[],
            reset=[],
            unlock=[],
            homing=[],
            updateGrblSetting=[],
            backupGrblSettings=[],
            restoreGrblSettings=[]
        )


    def on_api_command(self, command, data):
        if command == "sleep":
            self._printer.commands("$SLP")
            return

        if command == "unlock":
            if self.doSmoothie:
                self._printer.commands("M999")
            else:
                self._printer.commands("$X")

            return

        if command == "reset":
            self._printer.commands("M999")
            return

        if command == "updateGrblSetting":
            self._printer.commands("${}={}".format(data.get("id").strip(), data.get("value").strip()))
            self.grblSettings.update({int(data.get("id")): [data.get("value").strip(), self.grblSettingsNames.get(int(data.get("id")))]})
            self._printer.commands("$$")
            return

        if command == "backupGrblSettings":
            self._settings.set(["grblSettingsBackup"], self.saveGrblSettings())
            self._settings.save()
            return

        if command == "restoreGrblSettings":
            settings = self._settings.get(["grblSettingsBackup"])

            if settings is None or len(settings.strip()) == 0:
                return

            for setting in settings.split("||"):
                if len(setting.strip()) > 0:
                    set = setting.split("|")
                    # self._logger.info("restoreGrblSettings: {}".format(set))
                    command = "${}={}".format(set[0], set[1])
                    self._printer.commands(command)

            time.sleep(1)
            return flask.jsonify({'res' : settings})

        if command == "homing" and self._printer.is_ready() and self.grblState in "Idle,Alarm":
            self._printer.commands("$H")
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                            mode="MPos",
                                                                            state="Home",
                                                                            x=0,
                                                                            y=0,
                                                                            z=0,
                                                                            speed="N/A",
                                                                            power="N/A"))
            return

        # catch-all (should revisit state management) for validating printer State
        if not self._printer.is_ready() or self.grblState != "Idle":
            self._logger.debug("ignoring move related command - printer is not available")
            return

        if command == "frame":
            origin = data.get("origin").strip()

            self.send_frame_init_gcode()

            if (origin == "grblTopLeft"):
                self.send_bounding_box_upper_left(float(data.get("length")), float(data.get("width")))

            if (origin == "grblTopCenter"):
                self.send_bounding_box_upper_center(float(data.get("length")), float(data.get("width")))

            if (origin == "grblTopRight"):
                self.send_bounding_box_upper_right(float(data.get("length")), float(data.get("width")))

            if (origin == "grblCenterLeft"):
                self.send_bounding_box_center_left(float(data.get("length")), float(data.get("width")))

            if (origin == "grblCenter"):
                self.send_bounding_box_center(float(data.get("length")), float(data.get("width")))

            if (origin == "grblCenterRight"):
                self.send_bounding_box_center_right(float(data.get("length")), float(data.get("width")))

            if (origin == "grblBottomLeft"):
                self.send_bounding_box_lower_left(float(data.get("length")), float(data.get("width")))

            if (origin == "grblBottomCenter"):
                self.send_bounding_box_lower_center(float(data.get("length")), float(data.get("width")))

            if (origin == "grblBottomRight"):
                self.send_bounding_box_lower_right(float(data.get("length")), float(data.get("width")))

            self.send_frame_end_gcode()

            self._settings.set(["frame_length"], data.get("length"))
            self._settings.set(["frame_width"], data.get("width"))
            self._settings.set(["frame_origin"], data.get("origin"))

            self._settings.save()

            self._logger.debug("frame submitted l={} w={} o={}".format(data.get("length"), data.get("width"), data.get("origin")))
            return

        if command == "move":
            # do move stuff
            direction = data.get("direction")
            distance = float(data.get("distance"))
            axis = data.get("axis")

            # max X feed rate
            xf = int(float(self.grblSettings.get(110)[0]))
            # max Y feed rate
            yf = int(float(self.grblSettings.get(111)[0]))
            # max Z feed rate
            zf = int(float(self.grblSettings.get(112)[0]))

            # we don't need to save distance -- knockout takes of it for us
            if distance != self.distance:
                self.distance = distance

            # if axis != self._settings.get(["origin_axis"]):
            #     self._settings.set(["origin_axis"], axis)
            #     self._settings.save()

            self._logger.debug("move direction={} distance={} axis={}".format(direction, distance, axis))

            if direction == "home":
                self._printer.commands(["G54", "G90"])

                if axis == "X":
                    self._printer.commands(["G0 X0", "G91"])
                elif axis == "Y":
                    self._printer.commands(["G0 Y0", "G91"])
                elif axis == "Z":
                    self._printer.commands(["G0 Z0", "G91"])
                elif axis == "XY":
                    self._printer.commands(["G0 X0 Y0", "G91"])
                else:
                    self._printer.commands(["G0 X0 Y0 Z0", "G91"])

                # add a notification if we just homed
                self.addToNotifyQueue(["Moved to work home for {}".format(axis)])
                return

            if direction == "probez":
                # probe z using offset
                self.queue_cmds_and_send(["G91 G21 G38.2 Z-{} F100 ?".format(self.zLimit),
                                          "?",
                                          "G92 Z{}".format(self.zProbeOffset),
                                          "G0 Z5"])
                return

            # check distance against limits
            if "west" in direction or "east" in direction and abs(distance) > abs(self.xLimit):
                return flask.jsonify({'res' : "Distance exceeds X axis limit"})
            if "north" in direction or "south" in direction and abs(distance) > abs(self.yLimit):
                return flask.jsonify({'res' : "Distance exceeds Y axis limit"})
            if "up" in direction or "down" in direction and abs(distance) > abs(self.zLimit):
                return flask.jsonify({'res' : "Distance exceeds Z axis limit"})

            if direction == "northwest":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * -1, distance, xf if xf < yf else yf))

            if direction == "north":
                self._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance, yf))

            if direction == "northeast":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * 1, distance, xf if xf < yf else yf))

            if direction == "west":
                self._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * -1, xf))

            if direction == "east":
                self._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance, xf))

            if direction == "southwest":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * -1, distance * -1, xf if xf < yf else yf))

            if direction == "south":
                self._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * -1, yf))

            if direction == "southeast":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance, distance * -1, xf if xf < yf else yf))

            if direction == "up":
                self._printer.commands("{}G91 G21 Z{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance, zf))

            if direction == "down":
                self._printer.commands("{}G91 G21 Z{:f} F{}".format("$J=" if self.isGrblOneDotOne() else "G1 ", distance * -1, zf))

            return

        if command == "origin":
            axis = data.get("origin_axis")

            # if axis != self._settings.get(["origin_axis"]):
            #     self._settings.set(["origin_axis"], axis)
            #     self._settings.save()

            self._logger.debug("origin axis={}".format(axis))

            if axis == "X":
                self._printer.commands(["G91 G10 P1 L20 X0"])
            elif axis == "Y":
                self._printer.commands(["G91 G10 P1 L20 Y0"])
            elif axis == "Z":
                self._printer.commands(["G91 G10 P1 L20 Z0"])
            elif axis == "XY":
                self._printer.commands(["G91 G10 P1 L20 X0 Y0"])
            else:
                self._printer.commands(["G91 G10 P1 L20 X0 Y0 Z0"])

            self.addToNotifyQueue(["Work origin for {} set".format(axis)])
            return

        if command == "toggleWeak":
            return flask.jsonify({'res' : self.toggleWeak()})


    def toggleWeak(self):
        # only execute if laser mode enabled
        if not self.isLaserMode():
            return

        f = int(float(self.grblSettings.get(110)[0]))

        if self.grblPowerLevel == 0:
            # turn on laser in weak mode
            self._printer.commands("G1 F{} M3 S{}".format(f, self.weakLaserValue))
            self.addToNotifyQueue(["Weak laser enabled"])
            res = "Laser Off"
        else:
            self._printer.commands(["M3 S0", "M5", "G0"])
            self.addToNotifyQueue(["Weak laser disabled"])
            res = "Weak Laser"

        return res


    def queue_cmds_and_send(self, cmds, wait=False):
        for cmd in cmds:
            self._logger.debug("queuing command [%s] wait=%r", cmd, wait)
            self.grblCmdQueue.append(cmd)

        if wait:
            self._logger.debug("waiting for command queue to drain")

            while len(self.grblCmdQueue) > 0:
                time.sleep(.001)

            self._logger.debug("done waiting for command queue to drain")

    def addToNotifyQueue(self, notifications):
        for notification in notifications:
            self._logger.debug("queuing notification [%s]", notification)
            self.notifyQueue.append(notification)


    def isLaserMode(self):
        return int(float(self.grblSettings.get(32)[0])) != 0


    def isGrblOneDotOne(self):
        return "1.1" in self.grblVersion


    # #~~ Softwareupdate hook
    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
        # for details.

        return dict(bettergrblsupport=dict(  # version check: github repository
                                             # update method: pip
            displayName='Better Grbl Support',
            displayVersion=self._plugin_version,
            type='github_release',
            user='synman',
            repo='OctoPrint-Bettergrblsupport',
            current=self._plugin_version,
            stable_branch={
                    "name": "Stable",
                    "branch": "master",
                    "commitish": ["master"],
                },
            prerelease_branches=[
                    {
                        "name": "Release Candidate",
                        "branch": "rc",
                        "commitish": ["rc", "master"],
                    },
                    {
                        "name": "Development",
                        "branch": "devel",
                        "commitish": ["devel", "rc", "master"],
                    }
                ],
            pip='https://github.com/synman/OctoPrint-Bettergrblsupport/archive/{target_version}.zip'))


# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.

__plugin_name__ = 'Better Grbl Support'
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = BetterGrblSupportPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = \
        {'octoprint.plugin.softwareupdate.check_config': __plugin_implementation__.get_update_information,
         'octoprint.comm.protocol.gcode.sending': __plugin_implementation__.hook_gcode_sending,
         'octoprint.comm.protocol.gcode.received': __plugin_implementation__.hook_gcode_received,
         "octoprint.filemanager.extension_tree": __plugin_implementation__.get_extension_tree}
