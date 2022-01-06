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

from . import _bgs

import octoprint.plugin

import os
import time
import math
import subprocess

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

        self.zProbeOffset = float(15.00)
        self.zProbeTravel = float(0.00)
        self.zProbeEndPos = float(5.00)

        self.feedRate = float(0)
        self.plungeRate = float(0)
        self.powerRate = float(0)

        # load up our item/value pairs for errors, warnings, and settings
        _bgs.loadGrblDescriptions(self)

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
            zProbeOffset = float(15.00),
            zProbeTravel = float(0.00),
            zProbeEndPos = float(5.00),
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
            old_profile = "_default",
            useDevChannel = False
        )


    def on_after_startup(self):

        # self.zProbe.doStuff(self)

        # establish initial state for printer status
        self._settings.set_boolean(["is_printing"], self._printer.is_printing())
        self._settings.set_boolean(["is_operational"], self._printer.is_operational())

        # fix for V-Carve Grbl Toolpath referencing T1
        dest = self._settings.global_get_basefolder("printerProfiles") + os.path.sep + "_bgs.profile"

        if not os.path.exists(dest):
            src = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep + "__bgs.profile"
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

        self.weakLaserValue = self._settings.get(["weakLaserValue"])
        self.framingPercentOfMaxSpeed = self._settings.get(["framingPercentOfMaxSpeed"])

        self.grblSettingsText = self._settings.get(["grblSettingsText"])
        self.grblVersion = self._settings.get(["grblVersion"])

        self.distance = self._settings.get(["distance"])

        self.zProbeOffset = self._settings.get(["zProbeOffset"])
        self.zProbeTravel = self._settings.get(["zProbeTravel"])
        self.zProbeEndPos = self._settings.get(["zProbeEndPos"])

        # hardcoded global settings -- should revisit how I manage these
        self._settings.global_set_boolean(["feature", "modelSizeDetection"], not self.disableModelSizeDetection)
        self._settings.global_set_boolean(["feature", "sdSupport"], False)
        self._settings.global_set_boolean(["serial", "neverSendChecksum"], self.neverSendChecksum)

        if self.neverSendChecksum:
            self._settings.global_set(["serial", "checksumRequiringCommands"], [])

        # initialize config.yaml disabled plugins list
        disabledPlugins = self._settings.global_get(["plugins", "_disabled"])
        if disabledPlugins == None:
            disabledPlugins = []

        # initialize config.yaml disabled tabs list
        disabledTabs = self._settings.global_get(["appearance", "components", "disabled", "tab"])
        if disabledTabs == None:
            disabledTabs = []

        # initialize config.yaml ordered tabs list
        orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])
        if orderedTabs == None:
            orderedTabs = []

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
            if "plugin_gcodeviewer" not in disabledTabs:
                disabledTabs.append("plugin_gcodeviewer")
        else:
            if "gcodeviewer" in disabledPlugins:
                disabledPlugins.remove("gcodeviewer")
            if "plugin_gcodeviewer" in disabledTabs:
                disabledTabs.remove("plugin_gcodeviewer")

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

        if not self.hideControlTab:
            controls = self._settings.global_get(["controls"])

            if self.customControls and not controls:
                self._logger.debug("injecting custom controls")
                self._settings.global_set(["controls"], json.loads(self.customControlsJson))
            else:
                if not self.customControls and controls:
                    self._logger.debug("clearing custom controls")
                    self._settings.global_set(["controls"], [])

        # ensure i am always the first tab
        if "plugin_bettergrblsupport" in orderedTabs:
            orderedTabs.remove("plugin_bettergrblsupport")
        if self.reOrderTabs:
            orderedTabs.insert(0, "plugin_bettergrblsupport")

        self._settings.global_set(["plugins", "_disabled"], disabledPlugins)
        self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)
        self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        self._settings.save()
        _bgs.loadGrblSettings(self)


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
                self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)

            if self._settings.get(["statusCommand"]) == "?$G":
                self._settings.set(["statusCommand"], "?")
                self.statusCommand = "?"

            self._settings.remove(["showZ"])
            self._settings.save()

            self._logger.info("Migrated to settings v%d from v%d", target, 1 if current == None else current)


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
                            Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN, Events.UPLOAD, Events.CONNECTING, Events.CONNECTED)

        if event not in subscribed_events:
            self._logger.debug('event [{}] received but not subscribed - discarding'.format(event))
            return

        # our plugin is being uninstalled
        if event == Events.PLUGIN_PLUGINMANAGER_UNINSTALL_PLUGIN and payload["id"] == self._identifier:
            self._logger.debug('we are being uninstalled :(')
            _bgs.cleanUpDueToUninstall(self)
            self._logger.debug('uninstall cleanup completed (this house is clean)')
            return

        if self._printer_profile_manager.get_current_or_default()["id"] != "_bgs":
            return

        # - CONNECTING
        if event == Events.CONNECTING:
            # let's make sure we don't have any commands queued up
            self.grblCmdQueue.clear()

        # - CONNECTED
        if event == Events.CONNECTED:
            self._logger.debug('machine connected')
            _bgs.queue_cmds_and_send(self, ["$G"])
            # self._printer.commands("$G")

        # 'PrintStarted'
        if event == Events.PRINT_STARTED:
            if not self.grblState in ("Idle", "Check"):
                # we have to stop This
                self._printer.cancel_print()
                return

            # reset our rate overrides
            self.feedRate = 0
            self.plungeRate = 0
            self.powerRate = 0

            _bgs.addToNotifyQueue(self, ["Pgm Begin"])
            self._printer.commands("?", force=True)
            return

        # Print ended (finished / failed / cancelled)
        if event == Events.PRINT_CANCELLED or event == Events.PRINT_DONE or event == Events.PRINT_FAILED:
            self.grblState = "Idle"
            return

        # Print Cancelling
        if event == Events.PRINT_CANCELLING:
            self._logger.debug("canceling job")
            self._printer.commands(["!", "?"], force=True)
            _bgs.queue_cmds_and_send(self, ["M999", "?"], wait=True)

        # Print Paused
        if event == Events.PRINT_PAUSED:
            self._logger.debug("pausing job")
            self._printer.commands("!", force=True)

        # Print Resumed
        if event == Events.PRINT_RESUMED:
            self._logger.debug("resuming job")
            self._printer.commands(["~", "?"], force=True)
            _bgs.queue_cmds_and_send(self, ["?"])

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
            positioning = self.positioning

            start = timer()

            for line in f:
                # save our G command for shorthand post processors
                if line.upper().startswith("G"):
                    lastGCommand = line[:3] if line[2:3].isnumeric() else line[:2]

                # use our saved G command if our line starts with a coordinate
                if line.upper().lstrip().startswith(("X", "Y", "Z")):
                    command = lastGCommand.upper() + " " + line.upper().strip()
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

                # match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
                match = re.search(r".*[X]\ *(-?[\d.]+).*", command)
                if not match is None:
                    x = float(match.groups(1)[0]) if positioning == 0 else x + float(match.groups(1)[0])
                    if x < minX: minX = x
                    if x > maxX: maxX = x

                # match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
                match = re.search(r".*[Y]\ *(-?[\d.]+).*", command)
                if not match is None:
                    y = float(match.groups(1)[0]) if positioning == 0 else y + float(match.groups(1)[0])
                    if y < minY: minY = y
                    if y > maxY: maxY = y

                # self._logger.debug("x=%.2f y=%.2f", x, y)

            length = math.ceil(maxY - minY)
            width = math.ceil(maxX - minX)

            self._logger.debug('finished reading file length={} width={} positioning={} time={}'.format(length, width, positioning, timer() - start))

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_frame_size",
                                                                            length=length,
                                                                            width=width))
            return

        if event == Events.FILE_DESELECTED:
            return

        return


    def on_plugin_pending_uninstall(self):  # this will work in some next release of octoprint
        self._logger.debug('we are being uninstalled via on_plugin_pending_uninstall :(')
        _bgs.cleanUpDueToUninstall(self)
        self._logger.debug('uninstall cleanup completed (this house is clean)')


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
        if cmd.upper().lstrip().startswith((";", "(", "%")):
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
            return ("$I" if self.grblState == "Idle" else "?" , )

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
            _bgs.addToNotifyQueue(self, ["Machine has been reset"])
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
        if cmd.upper().lstrip().startswith(("X", "Y", "Z")):
            command = self.lastGCommand.upper() + " " + cmd.upper().strip()
        else:
            command = cmd.upper().strip()

        # keep track of distance traveled
        found = False
        foundZ = False

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[X]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblX = float(match.groups(1)[0]) if self.positioning == 0 else self.grblX + float(match.groups(1)[0])
            found = True

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Y]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblY = float(match.groups(1)[0]) if self.positioning == 0 else self.grblY + float(match.groups(1)[0])
            found = True

        # match = re.search(r"^G([0][0123]|[0123])(\D.*[Zz]|[Zz])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[Z]\ *(-?[\d.]+).*", command)
        if not match is None:
            self.grblZ = float(match.groups(1)[0]) if self.positioning == 0 else self.grblZ + float(match.groups(1)[0])
            found = True
            foundZ = True

        # match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ff]|[Ff])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[F]\ *(-?[\d.]+).*", command)
        if not match is None:
            grblSpeed = round(float(match.groups(1)[0]))

            if (self.feedRate != 0 or self.plungeRate != 0) and grblSpeed != 0:
                # check if feed rate is overridden
                if self.feedRate != 0:
                    if not foundZ:
                        grblSpeed = round(grblSpeed * self.feedRate)
                        command = command.replace("F" + match.groups(1)[0], "F{}".format(grblSpeed))
                        command = command.replace("F " + match.groups(1)[0], "F {}".format(grblSpeed))
                        # self._logger.debug("feed rate modified from [{}] to [{}]".format(match.groups(1)[0], grblSpeed))

                # check if plunge rate is overridden
                if self.plungeRate != 0:
                    if foundZ:
                        grblSpeed = roud(grblSpeed * self.plungeRate)
                        command = command.replace("F" + match.groups(1)[0], "F{:.5f}".format(grblSpeed))
                        command = command.replace("F " + match.groups(1)[0], "F {:.5f}".format(grblSpeed))
                        # self._logger.debug("plunge rate modified from [{}] to [{}]".format(match.groups(1)[0], grblSpeed))

            # make sure we post all speed on / off events
            if (grblSpeed == 0 and self.grblSpeed != 0) or (self.grblSpeed == 0 and grblSpeed != 0):
                self.timeRef = 0

            self.grblSpeed = grblSpeed
            found = True

        # match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ss]|[Ss])\ *(-?[\d.]+).*", command)
        match = re.search(r".*[S]\ *(-?[\d.]+).*", command)
        if not match is None:
            grblPowerLevel = round(float(match.groups(1)[0]))

            # check if power rate is overridden
            if self.powerRate != 0 and grblPowerLevel != 0:
                grblPowerLevel = round(grblPowerLevel * self.powerRate)
                command = command.replace("S" + match.groups(1)[0], "S{:.5f}".format(grblPowerLevel))
                command = command.replace("S " + match.groups(1)[0], "S {:.5f}".format(grblPowerLevel))
                # self._logger.debug("power rate modified from [{}] to [{}]".format(match.groups(1)[0], grblPowerLevel))

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

        # $G response
        if line.startswith("[GC:"):
            parserState = line.replace("[", "").replace("]", "").replace("GC:", "")

            for state in parserState.split(" "):
                if state in ("G90", "G91"):
                    self.positioning = int(state[2:3])
                    self._logger.debug("parser state indicates [%s] distance mode", "absolute" if self.positioning == 1 else "relative")

                elif state in ("G0", "G1", "G2", "G3", "G38.2", "G38.3", "G38.4", "G38.5", "G80"):
                    self._logger.debug("parser state indicates [%s] motion mode", state)
                elif state in ("G54", "G55", "G56", "G57", "G58", "G59"):
                    self._logger.debug("parser state indicates [%s] coordinate system available", state)
                elif state in ("G17", "G18", "G19"):
                    self._logger.debug("parser state indicates [%s] plane selected", state)
                elif state in ("G20", "G21"):
                    self._logger.debug("parser state indicates [%s] uom active", "metric" if state == "G21" else "imperial")
                elif state in ("G93", "G94"):
                    self._logger.debug("parser state indicates [%s] feed rate mode", state)
                elif state in ("M3", "M4", "M5"):
                    self._logger.debug("parser state indicates [%s] spindle state", state)
                elif state in ("M7", "M8", "M9"):
                    self._logger.debug("parser state indicates [%s] coolant state", state)

                elif state.startswith("F"):
                    self.grblSpeed = round(float(state.replace("F", "")))
                    self._logger.debug("parser state indicates feed rate of [%d]", self.grblSpeed)
                elif state.startswith("S"):
                    self.grblPowerLevel = round(float(state.replace("S", "")))
                    self._logger.debug("parser state indicates spindle speed of [%d]", self.grblPowerLevel)
                elif state.startswith("T"):
                    self._logger.debug("parser state indicates tool #[%s] active", state.replace("T", ""))

            return

        # look for an alarm
        if line.lower().startswith('alarm:'):
            error = int(0)
            desc = line

            match = re.search(r'alarm:\ *(-?[\d.]+)', line.lower())
            if not match is None:
                error = int(match.groups(1)[0])
                desc = self.grblAlarms.get(error)

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_alarm",
                                                                            code=error,
                                                                            description=desc))

            self._logger.warning("alarm received: %d: %s", error, self.grblAlarms.get(error))

            # clear out any pending queued Commands
            if len(self.grblCmdQueue) > 0:
                self._logger.debug("clearing %d commands from the command queue", len(self.grblCmdQueue))
                self.grblCmdQueue.clear()

            # put a message on our notification queue and force an inquiry
            _bgs.addToNotifyQueue(self, [desc])
            self._printer.commands("?")

            # we need to pause if we are printing
            if self._printer.is_printing():
                self._printer.pause_print()

            # return 'Error: ' + desc
            return "ok " + desc

        # look for an error
        if not self.ignoreErrors and line.lower().startswith('error:'):
            match = re.search(r'error:\ *(-?[\d.]+)', line.lower())

            error = int(0)
            desc = line

            if not match is None:
                error = int(match.groups(1)[0])
                desc = self.grblErrors.get(error)

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_error",
                                                                            code=error,
                                                                            description=desc))
            self._logger.warning("error received: %d: %s", error, desc)

            # clear out any pending queued Commands
            if len(self.grblCmdQueue) > 0:
                self._logger.debug("clearing %d commands from the command queue", len(self.grblCmdQueue))
                self.grblCmdQueue.clear()

            # put a message on our notification queue and force an inquiry
            _bgs.addToNotifyQueue(self, [desc])
            self._printer.commands("?")

            # we need to pause if we are printing
            if self._printer.is_printing():
                self._printer.pause_print()

            # lets not let octoprint know if we have an "informational" error
            # if error in (9, 15):
            #     self._logger.debug("not forwarding grbl error [%d] to octoprint", error)
            #     return "ok " + line

            # return 'Error: ' + desc
            return "ok " + desc

        # forward any messages to the notification plugin_name
        if "MSG:" in line.upper():
            ignoreList = ["[MSG:'$H'|'$X' to unlock]"]

            if line.strip() not in ignoreList:
                # auto reset
                if "reset to continue" in line.lower():
                    # automatically perform a soft reset if GRBL says we need one
                    self._printer.commands("M999")
                else:
                    # replace MSG: Disabled / Enabled with check mode info
                    line = line.replace("MSG:Disabled", "Check Mode Disabled")
                    line = line.replace("MSG:Enabled", "Check Mode Enabled")
                    # general clean up of the message
                    line = line.replace("[","").replace("]","").replace("MSG:","")
                    line = line.replace("\n", "").replace("\r", "")

                    _bgs.addToNotifyQueue(self, [line])
                    self._printer.commands("?", force=True)

            return

        # add a notification if we just z-probed
        if "PRB:" in line.upper():
            _bgs.addToNotifyQueue(self, [line])
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
                    self._settings.set(["grblSettingsText"], _bgs.saveGrblSettings(self))
                    self._settings.set_boolean(["laserMode"], _bgs.isLaserMode(self))

                    # lets populate our x,y,z limits
                    self.xLimit = float(self.grblSettings.get(130)[0])
                    self.yLimit = float(self.grblSettings.get(131)[0])
                    self.zLimit = float(self.grblSettings.get(132)[0])

                    # assign our default distance if it is not already set to the lower of x,y limits
                    if self.distance == 0:
                        self.distance = min([self.xLimit - 5, self.yLimit - 5])
                        self._settings.set(["distance"], self.distance)

                    self._settings.save()
                    _bgs.addToNotifyQueue(self, ["Grbl Settings sent"])

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

            # pop any queued commands if state is IDLE or HOLD:0 or Check
            if len(self.grblCmdQueue) > 0 and self.grblState.upper().strip() in ("IDLE", "HOLD:0", "CHECK"):
                self._logger.debug('sending queued command [%s] - depth [%d]', self.grblCmdQueue[0], len(self.grblCmdQueue))
                self._printer.commands(self.grblCmdQueue[0])
                self.grblCmdQueue.pop(0)
                return response

            # add a notification if we just homed
            if self.grblState.upper().strip() == "HOME":
                _bgs.addToNotifyQueue(self, ["Machine has been homed"])

            # parse the line to see if we have any other useful data
            # for stat in line.replace("<", "").replace(">", "").split("|"):
            #     # buffer stats and Pin stats
            #     if stat.startswith("Bf:") or stat.startswith("Pn:"):
            #         self.addToNotifyQueue(stat)

            # pop any queued notifications
            if len(self.notifyQueue) > 0:
                notification = self.notifyQueue[0]
                if notification == "Pgm Begin": self.grblState = "Run"

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
            restoreGrblSettings=[],
            feedRate=[],
            plungeRate=[],
            powerRate=[]
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
            self._printer.commands("M999", force=True)
            return

        if command == "updateGrblSetting":
            self._printer.commands("${}={}".format(data.get("id").strip(), data.get("value").strip()))
            self.grblSettings.update({int(data.get("id")): [data.get("value").strip(), self.grblSettingsNames.get(int(data.get("id")))]})
            self._printer.commands("$$")
            return

        if command == "backupGrblSettings":
            self._settings.set(["grblSettingsBackup"], _bgs.saveGrblSettings(self))
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

        if command == "feedRate":
            feedRate = float(data.get("feed_rate"))
            if not feedRate in (0, 100):
                self.feedRate = feedRate * .01
            else:
                self.feedRate = float(0)

            self._logger.info("feed rate overriden by %.0f%%", feedRate)
            return

        if command == "plungeRate":
            plungeRate = float(data.get("plunge_rate"))
            if not plungeRate in (0, 100):
                self.plungeRate = plungeRate * .01
            else:
                self.plungeRate = float(0)

            self._logger.info("plunge rate overriden by %.0f%%", plungeRate)
            return

        if command == "powerRate":
            powerRate = float(data.get("power_rate"))
            if not powerRate in (0, 100):
                self.powerRate = powerRate * .01
            else:
                self.powerRate = float(0)

            self._logger.info("power rate overriden by %.0f%%", powerRate)
            return

        # catch-all (should revisit state management) for validating printer State
        if not self._printer.is_ready() or not self.grblState in ("Idle", "Jog"):
            self._logger.debug("ignoring move related command - printer is not available")
            return

        if command == "frame":
            _bgs.do_framing(self, data)

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

            # we don't need to save distance -- knockout takes care of it for us
            if distance != self.distance:
                self.distance = distance

            # if axis != self._settings.get(["origin_axis"]):
            #     self._settings.set(["origin_axis"], axis)
            #     self._settings.save()

            self._logger.debug("move direction={} distance={} axis={} xlimit={} ylimit={} zlimit={}".format(direction, distance, axis, self.xLimit, self.yLimit, self.zLimit))

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
                _bgs.addToNotifyQueue(self, ["Moved to work home for {}".format(axis)])
                return

            if direction == "probez":
                # probe z using offset
                _bgs.do_probez(self)
                # _bgs.queue_cmds_and_send(self, ["G91 G21 G38.2 Z-{} F100 ?".format(self.zLimit if self.zProbeTravel == 0 else self.zProbeTravel),
                #                           "?",
                #                           "G92 Z{}".format(self.zProbeOffset),
                #                           "G0 Z{}".format(self.zProbeEndPos)])
                return

            # check distance against limits
            if ("west" in direction or "east" in direction) and abs(distance) > abs(self.xLimit):
                return flask.jsonify({'res' : "Distance exceeds X axis limit"})
            if ("north" in direction or "south" in direction) and abs(distance) > abs(self.yLimit):
                return flask.jsonify({'res' : "Distance exceeds Y axis limit"})
            if ("up" in direction or "down" in direction) and abs(distance) > abs(self.zLimit):
                return flask.jsonify({'res' : "Distance exceeds Z axis limit"})

            if direction == "northwest":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * -1, distance, xf if xf < yf else yf))

            if direction == "north":
                self._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance, yf))

            if direction == "northeast":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * 1, distance, xf if xf < yf else yf))

            if direction == "west":
                self._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * -1, xf))

            if direction == "east":
                self._printer.commands("{}G91 G21 X{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance, xf))

            if direction == "southwest":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * -1, distance * -1, xf if xf < yf else yf))

            if direction == "south":
                self._printer.commands("{}G91 G21 Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * -1, yf))

            if direction == "southeast":
                self._printer.commands("{}G91 G21 X{:f} Y{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance, distance * -1, xf if xf < yf else yf))

            if direction == "up":
                self._printer.commands("{}G91 G21 Z{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance, zf))

            if direction == "down":
                self._printer.commands("{}G91 G21 Z{:f} F{}".format("$J=" if _bgs.isGrblOneDotOne(self) else "G1 ", distance * -1, zf))

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

            _bgs.addToNotifyQueue(self, ["Work origin for {} set".format(axis)])
            return

        if command == "toggleWeak":
            return flask.jsonify({'res' : _bgs.toggleWeak(self)})


    # #~~ Softwareupdate hook
    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
        # for details.

        useDevChannel = self._settings.get_boolean(["useDevChannel"])

        if useDevChannel:
            __plugin_version__ = str(os.path.getmtime(os.path.realpath(__file__)))

            return dict(bettergrblsupport=dict(  # version check: github repository
                                                 # update method: pip
                displayName='Better Grbl Support (Development Branch)',
                type='github_commit',
                user='synman',
                repo='OctoPrint-Bettergrblsupport',
                branch="devel",
                method="pip",
                current="909e128f6e73309457fb23afa0b8ac06c3b9dd33",
                pip="https://github.com/synman/Octoprint-Bettergrblsupport/archive/{target_version}.zip"))

        else:
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
