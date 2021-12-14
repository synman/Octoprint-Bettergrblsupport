#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from octoprint.events import Events
from timeit import default_timer as timer

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
                              octoprint.plugin.EventHandlerPlugin):

    def __init__(self):
        self.hideTempTab = True
        self.hideControlTab = True
        self.hideGCodeTab = True
        self.customControls = False
        self.helloCommand = "$$"
        self.statusCommand = "?$G"
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
        self.showZ = False
        self.weakLaserValue = 1

        self.overrideM8 = False
        self.overrideM9 = False
        self.m8Command = ""
        self.m9Command = ""

        self.grblState = None
        self.grblX = float(0)
        self.grblY = float(0)
        self.grblZ = float(0)
        self.grblSpeed = 0
        self.grblPowerLevel = 0
        self.positioning = 0

        self.timeRef = 0

        self.grblErrors = {}
        self.grblAlarms = {}
        self.grblSettingsNames = {}
        self.grblSettings = {}
        self.grblSettingsText = ""

        self.ignoreErrors = False
        self.doSmoothie = False

        self.customControlsJson = r'[{"layout": "horizontal", "children": [{"commands": ["$10=0", "G28.1", "G92 X0 Y0 Z0"], "name": "Set Origin", "confirm": null}, {"command": "M999", "name": "Reset", "confirm": null}, {"commands": ["G1 F4000 S0", "M5", "$SLP"], "name": "Sleep", "confirm": null}, {"command": "$X", "name": "Unlock", "confirm": null}, {"commands": ["$32=0", "M4 S1"], "name": "Weak Laser", "confirm": null}, {"commands": ["$32=1", "M5"], "name": "Laser Off", "confirm": null}], "name": "Laser Commands"}, {"layout": "vertical", "type": "section", "children": [{"regex": "<([^,]+)[,|][WM]Pos:([+\\-\\d.]+,[+\\-\\d.]+,[+\\-\\d.]+)", "name": "State", "default": "", "template": "State: {0} - Position: {1}", "type": "feedback"}, {"regex": "F([\\d.]+) S([\\d.]+)", "name": "GCode State", "default": "", "template": "Speed: {0}  Power: {1}", "type": "feedback"}], "name": "Realtime State"}]'


    # #~~ SettingsPlugin mixin
    def get_settings_defaults(self):
        self.loadGrblDescriptions()

        return dict(
            hideTempTab = True,
            hideControlTab = True,
            hideGCodeTab = True,
            hello = "$$",
            statusCommand = "?$G",
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
            distance = 10,
            distances = [.1, 1, 10, 100],
            is_printing = False,
            is_operational = False,
            disableModelSizeDetection = True,
            neverSendChecksum = True,
            reOrderTabs = True,
            disablePrinterSafety = True,
            grblSettingsText = "This space intentionally left blank",
            grblSettingsBackup = "",
            showZ = False,
            weakLaserValue = 1,
            overrideM8 = False,
            overrideM9 = False,
            m8Command = "/home/pi/bin/tplink_smartplug.py -t air-assist.shellware.com -c on",
            m9Command = "/home/pi/bin/tplink_smartplug.py -t air-assist.shellware.com -c off",
            ignoreErrors = False,
            doSmoothie = False
        )


    def on_after_startup(self):

        # establish initial state for printer status
        self._settings.set_boolean(["is_printing"], self._printer.is_printing())
        self._settings.set_boolean(["is_operational"], self._printer.is_operational())

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

        self.showZ = self._settings.get_boolean(["showZ"])
        self.weakLaserValue = self._settings.get(["weakLaserValue"])

        self.grblSettingsText = self._settings.get(["grblSettingsText"])

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

        # clean up old versions since octoprint renamed gcodeviewer to plugin_gcodeviewer
        if "gcodeviewer" in disabledTabs:
            disabledTabs.remove("gcodeviewer")

        # clean up old versions since we now disable gcodeviewer instead of just hiding it
        if "plugin_gcodeviewer" in disabledTabs:
            disabledTabs.remove("plugin_gcodeviewer")

        self._settings.global_set(["appearance", "components", "disabled", "tab"], disabledTabs)

        if not self.hideControlTab:
            controls = self._settings.global_get(["controls"])

            if self.customControls and not controls:
                self._logger.info("injecting custom controls")
                self._settings.global_set(["controls"], json.loads(self.customControlsJson))
            else:
                if not self.customControls and controls:
                    self._logger.info("clearing custom controls")
                    self._settings.global_set(["controls"], [])

        orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

        # clean up old versions since octoprint renamed gcodeviewer to plugin_gcodeviewer
        if "gcodeviewer" in orderedTabs:
            orderedTabs.remove("gcodeviewer")
            self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        # ensure i am always the first tab
        if self.reOrderTabs:
            orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

            if "plugin_bettergrblsupport" in orderedTabs:
                orderedTabs.remove("plugin_bettergrblsupport")

            orderedTabs.insert(0, "plugin_bettergrblsupport")
            self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        self._settings.save()

        self.deSerializeGrblSettings()


    def loadGrblDescriptions(self):
        path = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + "static" + os.path.sep + "txt" + os.path.sep

        f = open(path + "grbl_errors.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblErrors[int(match.groups(1)[0])] = match.groups(1)[1]

        f = open(path + "grbl_alarms.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblAlarms[int(match.groups(1)[0])] = match.groups(1)[1]

        f = open(path + "grbl_settings.txt", 'r')

        for line in f:
            match = re.search(r"^(-?[\d\.]+)[\ ]+(-?[\S\ ]*)", line)
            if not match is None:
                self.grblSettingsNames[int(match.groups(1)[0])] = match.groups(1)[1]
                # self._logger.info("setting id={} description={}".format(int(match.groups(1)[0]), match.groups(1)[1]))

        # for k, v in self.grblErrors.items():
        #     self._logger.info("error id={} desc={}".format(k, v))
        #
        # for k, v in self.grblAlarms.items():
        #     self._logger.info("alarm id={} desc={}".format(k, v))


    def deSerializeGrblSettings(self):
        self.grblSettingsText = self._settings.get(["grblSettingsText"])

        for setting in self.grblSettingsText.split("||"):
            if len(setting.strip()) > 0:

                self._logger.debug("deSerializeGrblSettings=[{}]".format(setting))

                set = setting.split("|")
                if not set is None:
                    self.grblSettings.update({int(set[0]): [set[1], self.grblSettingsNames.get(int(set[0]))]})
        return


    def serializeGrblSettings(self):
        ret = ""
        for id, data in sorted(self.grblSettings.items(), key=lambda x: int(x[0])):
            ret = ret + "{}|{}|{}||".format(id, data[0], data[1])

        self._logger.debug("serializeGrblSettings=[\n{}\n]".format(ret))

        self.grblSettingsText = ret;
        return ret


    def on_settings_save(self, data):
        self._logger.info("saving settings")
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        # reload our config
        self.on_after_startup()

        # refresh our grbl settings
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
    #     return dict(grblSettingsText=self.serializeGrblSettings())


    # #-- EventHandlerPlugin mix-in
    def on_event(self, event, payload):
        subscribed_events = Events.FILE_SELECTED + Events.PRINT_STARTED + Events.PRINT_CANCELLED + Events.PRINT_DONE + Events.PRINT_FAILED

        if subscribed_events.find(event) == -1:
            return

        # 'PrintStarted'
        if event == Events.PRINT_STARTED:
            self.grblState = "Run"
            return

        # Print ended (finished / failed / cancelled)
        if event == Events.PRINT_CANCELLED or event == Events.PRINT_DONE or event == Events.PRINT_FAILED:
            self.grblState = "Idle"
            return

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

            self.positioning = 0

            start = timer()

            for line in f:
                #T2 # HACK:
                if line.upper().lstrip().startswith("X"):
                    match = re.search(r"^X *(-?[\d.]+).*", line)
                    if not match is None:
                        command = "G01 " + line.upper().strip()
                    else:
                        command = line.upper().strip()
                else:
                    command = line.upper().strip()

                if "G90" in command.upper():
                    # absolute positioning
                    self.positioning = 0
                    continue

                if "G91" in command.upper():
                    # relative positioning
                    self.positioning = 1
                    continue

                match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
                if not match is None:
                    if self.positioning == 1:
                        x = x + float(match.groups(1)[2])
                    else:
                        x = float(match.groups(1)[2])
                    if x < minX:
                        minX = x
                    if x > maxX:
                        maxX = x

                match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
                if not match is None:
                    if self.positioning == 1:
                        y = y + float(match.groups(1)[2])
                    else:
                        y = float(match.groups(1)[2])
                    if y < minY:
                        minY = y
                    if y > maxY:
                        maxY = y

            length = int(math.ceil(maxY - minY))
            width = int(math.ceil(maxX - minX))

            self._logger.info('finished reading file length={} width={} time={}'.format(length, width, timer() - start))

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_frame_size",
                                                                            length=length,
                                                                            width=width))

            return

        if event == Events.FILE_DESELECTED:
            return

        return


    # #-- gcode sending hook
    def hook_gcode_sending(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):

        # M8 processing - work in progress
        if cmd.upper().strip() == "M8" and self.overrideM8:
            self._logger.info('Turning ON Air Assist')
            subprocess.call(self.m8Command, shell=True)
            return (None,)

        # M9 processing - work in progress
        if cmd.upper().strip() == "M9" and self.overrideM9:
            self._logger.info('Turning OFF Air Assist')
            subprocess.call(self.m9Command, shell=True)
            return (None,)

        # rewrite M115 as $$ (hello)
        if self.suppressM115 and cmd.upper().startswith('M115'):
            self._logger.debug('Rewriting M115 as %s' % self.helloCommand)

            if self.doSmoothie:
                return "Cat /sd/config"

            return self.helloCommand

        # suppress comments and extraneous commands that may cause wayward
        # grbl instances to error out
        if cmd.upper().lstrip().startswith(tuple([';', '(', '%'])):
            self._logger.debug('Ignoring extraneous [%s]', cmd)
            return (None, )

        # suppress reset line #s
        if self.suppressM110 and cmd.upper().startswith('M110'):
            self._logger.debug('Ignoring %s', cmd)
            return (None, )

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

        # rewrite current position
        if self.suppressM114 and cmd.upper().startswith('M114'):
            self._logger.debug('Rewriting M114 as %s' % self.positionCommand)
            return (self.positionCommand, )

        # soft reset / resume (stolen from Marlin)
        if cmd.upper().startswith('M999') and not self.doSmoothie:
            self._logger.info('Sending Soft Reset')
            # self._printer.commands("\x18")
            return ("\x18",)

        if "G90" in cmd.upper():
            # absolute positioning
            self.positioning = 0

        if "G91" in cmd.upper():
            # relative positioning
            self.positioning = 1

        #T2 # HACK:
        if cmd.upper().lstrip().startswith("X"):
            match = re.search(r"^X *(-?[\d.]+).*", cmd)
            if not match is None:
                command = "G01 " + cmd.upper().strip()
            else:
                command = cmd.upper().strip()
        else:
            command = cmd.upper().strip()

        # keep track of distance traveled
        # if command.startswith("G0") or command.startswith("G1") or command.startswith("G2") or command.startswith("G3") or command.startswith("M4"):
        found = False

        match = re.search(r"^G([0][0123]|[0123])(\D.*[Xx]|[Xx])\ *(-?[\d.]+).*", command)
        if not match is None:
            if self.positioning == 1:
                self.grblX = self.grblX + float(match.groups(1)[2])
            else:
                self.grblX = float(match.groups(1)[2])
            found = True

        match = re.search(r"^G([0][0123]|[0123])(\D.*[Yy]|[Yy])\ *(-?[\d.]+).*", command)
        if not match is None:
            if self.positioning == 1:
                self.grblY = self.grblY + float(match.groups(1)[2])
            else:
                self.grblY = float(match.groups(1)[2])
            found = True

        match = re.search(r"^G([0][0123]|[0123])(\D.*[Zz]|[Zz])\ *(-?[\d.]+).*", command)
        if not match is None:
            if self.positioning == 1:
                self.grblZ = self.grblZ + float(match.groups(1)[2])
            else:
                self.grblZ = float(match.groups(1)[2])
            found = True

        match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ff]|[Ff])\ *(-?[\d.]+).*", command)
        if not match is None:
            grblSpeed = round(float(match.groups(1)[2]))

            # make sure we post all speed on / off events
            if (grblSpeed == 0 and self.grblSpeed != 0) or (self.grblSpeed == 0 and grblSpeed != 0):
                self.timeRef = 0

            self.grblSpeed = grblSpeed
            found = True

        match = re.search(r"^[GM]([0][01234]|[01234])(\D.*[Ss]|[Ss])\ *(-?[\d.]+).*", command)
        if not match is None:
            grblPowerLevel = round(float(match.groups(1)[2]))

            # make sure we post all power on / off events
            # if (grblPowerLevel == 0 and self.grblPowerLevel != 0) or (self.grblPowerLevel == 0 and grblPowerLevel != 0):
            #     self.timeRef = 0;

            self.grblPowerLevel = grblPowerLevel
            found = True

        if found:
            currentTime = int(round(time.time() * 1000))
            if currentTime > self.timeRef + 500:
                # self._logger.info("x={} y={} z={} f={} s={}".format(self.grblX, self.grblY, self.grblZ, self.grblSpeed, self.grblPowerLevel))
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
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

        if line.startswith('Grbl'):
             # Hack to make Arduino based GRBL work.
             # When the serial port is opened, it resets and the "hello" command
             # is not processed.
             # This makes Octoprint recognise the startup message as a successful connection.
            return 'ok ' + line

        # look for an alarm
        if line.lower().startswith('alarm:'):
            match = re.search(r'alarm:\ *(-?[\d.]+)', line.lower())

            if not match is None:
                error = int(match.groups(1)[0])
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_alarm",
                                                                                code=error,
                                                                                description=self.grblAlarms.get(error)))

                self._logger.info("alarm received: {} = {}".format(error, self.grblAlarms.get(error)))

            return 'Error: ' + line

        # look for an error
        if not self.ignoreErrors and line.lower().startswith('error:'):
            match = re.search(r'error:\ *(-?[\d.]+)', line.lower())

            if not match is None:
                error = int(match.groups(1)[0])
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_error",
                                                                                code=error,
                                                                                description=self.grblErrors.get(error)))

                self._logger.info("error received: {} = {}".format(error, self.grblErrors.get(error)))

            return 'Error: ' + line

        # auto reset
        if "reset to continue" in line.lower():
            self._printer.commands("M999")
            return 'ok ' + line

        # grbl settings
        if line.startswith("$"):
            match = re.search(r'^[$](-?[\d\.]+)=(-?[\d\.]+)', line)

            if not match is None:
                settingsId = int(match.groups(1)[0])
                settingsValue = match.groups(1)[1]

                self.grblSettings.update({settingsId: [settingsValue, self.grblSettingsNames.get(settingsId)]})
                self._logger.debug("setting id={} value={} description={}".format(settingsId, settingsValue, self.grblSettingsNames.get(settingsId)))

                if settingsId >= 132:
                    self._settings.set(["grblSettingsText"], self.serializeGrblSettings())
                    self._settings.save()

                return line

        # hack to force status updates
        if 'MPos' in line or 'WPos' in line:
             # <Idle,MPos:0.000,0.000,0.000,WPos:0.000,0.000,0.000,RX:3,0/0>
             # <Run|MPos:-17.380,-7.270,0.000|FS:1626,0>

            # match = re.search(r'[WM]Pos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+)', line)
            match = re.search(r'<(-?[^,]+)[,|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+)', line)

            if match is None:
                self._logger.warning('Bad data %s', line.rstrip())
                return line

             # OctoPrint records positions in some instances.
             # It needs a different format. Put both on the same line so the GRBL info is not lost
             # and is accessible for "controls" to read.
            response = 'ok X:{1} Y:{2} Z:{3} E:0 {original}'.format(*match.groups(), original=line)
            self._logger.debug('[%s] rewrote as [%s]', line.strip(), response.strip())

            self.grblState = str(match.groups(1)[0])
            self.grblX = float(match.groups(1)[1])
            self.grblY = float(match.groups(1)[2])
            self.grblZ = float(match.groups(1)[3])

            match = re.search(r'.*\|FS:(-?[\d\.]+),(-?[\d\.]+)', line)
            if not match is None:
                self.grblSpeed = round(float(match.groups(1)[0]))
                self.grblPowerLevel = round(float(match.groups(1)[1]))

            # if self.grblState == "Sleep" or self.grblState == "Run":
            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                            state=self.grblState,
                                                                            x=self.grblX,
                                                                            y=self.grblY,
                                                                            z=self.grblZ,
                                                                            speed=self.grblSpeed,
                                                                            power=self.grblPowerLevel))

            return response

        # match = re.search(r"F(-?[\d.]+) S(-?[\d.]+)", line)
        #
        # if not match is None:
        #     self.grblSpeed = round(float(match.groups(1)[0]))
        #     self.grblPowerLevel = round(float(match.groups(1)[1]))
        #
        #     self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
        #                                                                     state=self.grblState,
        #                                                                     x=self.grblX,
        #                                                                     y=self.grblY,
        #                                                                     z=self.grblZ,
        #                                                                     speed=self.grblSpeed,
        #                                                                     power=self.grblPowerLevel))

        if not line.rstrip().endswith('ok'):
            return line

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


    # i am thinking these commands are all wrong (all framing)
    def send_frame_init_gcode(self):
        self._printer.commands("G4 P0")
        self._printer.commands("$32=0")
        self._printer.commands("$110=1000")
        self._printer.commands("$111=1000")
        self._printer.commands("G00 G17 G40 G21 G54")
        self._printer.commands("G91")
        self._printer.commands("M4 F1000 S{}".format(self.weakLaserValue))
        self._printer.commands("G91")


    def send_frame_end_gcode(self):
        self._printer.commands("G1S0")
        self._printer.commands("M4 F0 S0")
        self._printer.commands("M5")
        self._printer.commands("M2")
        self._printer.commands("G4 P0")
        self._printer.commands("$32=1")
        self._printer.commands("$110=5000")
        self._printer.commands("$111=5000")


    def send_bounding_box_upper_left(self, y, x):
        self._printer.commands("G0 X{:f}".format(x))
        self._printer.commands("G0 Y{:f}".format(y * -1))
        self._printer.commands("G0 X{:f}".format(x * -1))
        self._printer.commands("G0 Y{:f}".format(y))


    def send_bounding_box_upper_center(self, y, x):
        self._printer.commands("G0 X{:f} F2000 S{}".format(x / 2, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x / 2, self.weakLaserValue))


    def send_bounding_box_upper_right(self, y, x):
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))


    def send_bounding_box_center_left(self, y, x):
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y / 2, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y / 2, self.weakLaserValue))


    def send_bounding_box_center(self, y, x):
        self._printer.commands("G0 X{:f} Y{:f} F4000".format(x / 2 * -1, y / 2))
        self._printer.commands("G0 X{} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{} S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{} S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} Y{:f} F4000".format(x / 2, y / 2 * -1))


    def send_bounding_box_center_right(self, y, x):
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y / 2 * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y / 2 * -1, self.weakLaserValue))


    def send_bounding_box_lower_left(self, y, x):
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))


    def send_bounding_box_lower_center(self, y, x):
        self._printer.commands("G0 X{:f} F2000 S{}".format(x / 2 * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x / 2 * -1, self.weakLaserValue))


    def send_bounding_box_lower_right(self, y, x):
        self._printer.commands("G0 X{:f} F2000 S{}".format(x * -1, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y, self.weakLaserValue))
        self._printer.commands("G0 X{:f} F2000 S{}".format(x, self.weakLaserValue))
        self._printer.commands("G0 Y{:f} F2000 S{}".format(y * -1, self.weakLaserValue))


    def get_api_commands(self):
        return dict(
            frame=[],
            toggleWeak=[],
            originz=[],
            originxy=[],
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

        if command == "homing":
            if self.grblPowerLevel > 0 and self.grblState == "Idle":
                self.toggleWeak()

            self._printer.commands("$H")
            return

        if command == "updateGrblSetting":
            self._printer.commands("${}={}".format(data.get("id").strip(), data.get("value").strip()))
            self.grblSettings.update({int(data.get("id")): [data.get("value").strip(), self.grblSettingsNames.get(int(data.get("id")))]})
            self._printer.commands("$$")
            return

        if command == "backupGrblSettings":
            self._settings.set(["grblSettingsBackup"], self.serializeGrblSettings())
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

        # catch-all (should revisit state management) for validating printer State
        if not self._printer.is_ready() or self.grblState != "Idle":
            self._logger.info("ignoring move related command - printer is not available")
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
            return

        if command == "move":
            # do move stuff
            direction = data.get("direction")
            distance = data.get("distance")

            self._logger.debug("move {} {}".format(direction, distance))

            if direction == "home":
                self._printer.commands("G91")
                self._printer.commands("G28 X0 Y0")
                self._printer.commands("G90")

            if direction == "homez":
                self._printer.commands("G91")
                self._printer.commands("G28 Z0")
                self._printer.commands("G90")

            if direction == "forward":
                self._printer.commands("G91")
                self._printer.commands("G0 Y{}".format(distance))
                self._printer.commands("G90")

            if direction == "backward":
                self._printer.commands("G91")
                self._printer.commands("G0 Y{}".format(distance * -1))
                self._printer.commands("G90")

            if direction == "left":
                self._printer.commands("G91")
                self._printer.commands("G0 X{}".format(distance * -1))
                self._printer.commands("G90")

            if direction == "right":
                self._printer.commands("G91")
                self._printer.commands("G0 X{}".format(distance))
                self._printer.commands("G90")

            if direction == "up":
                self._printer.commands("G91")
                self._printer.commands("G0 Z{}".format(distance))
                self._printer.commands("G90")

            if direction == "down":
                self._printer.commands("G91")
                self._printer.commands("G0 Z{}".format(distance * -1))
                self._printer.commands("G90")

            return

        if command == "originxy":
            # do xy-origin stuff

            saveIgnoreErrors = self.ignoreErrors
            self.ignoreErrors = True

            self._printer.commands("$10=0")
            self._printer.commands("G28.1")
            self._printer.commands("G92 X0 Y0")

            time.sleep(1)

            self._printer.commands("$10=0")
            self._printer.commands("G28.1")
            self._printer.commands("G92 X0 Y0")

            self.ignoreErrors = saveIgnoreErrors

            return

        if command == "originz":
            # do z-origin stuff

            saveIgnoreErrors = self.ignoreErrors
            self.ignoreErrors = True

            self._printer.commands("$10=0")
            self._printer.commands("G28.1")
            self._printer.commands("G92 Z0")

            time.sleep(1)

            self._printer.commands("$10=0")
            self._printer.commands("G28.1")
            self._printer.commands("G92 Z0")

            self.ignoreErrors = saveIgnoreErrors

            return

        if command == "toggleWeak":
            return flask.jsonify({'res' : self.toggleWeak()})


    def toggleWeak(self):
        # do laser stuff
        powerLevel = self.grblPowerLevel

        if powerLevel == 0:
            self._printer.commands("$32=0")
            self._printer.commands("M4 F1000 S{}".format(self.weakLaserValue))
            res = "Laser Off"
        else:
            # self._printer.commands("M9")
            self._printer.commands("G1S0")
            self._printer.commands("M4 F0 S0")
            self._printer.commands("$32=1")
            self._printer.commands("M5")
            self._printer.commands("M2")
            res = "Weak Laser"

        return res

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
         'octoprint.comm.protocol.gcode.received': __plugin_implementation__.hook_gcode_received}
