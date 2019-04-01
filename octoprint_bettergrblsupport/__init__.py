#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from octoprint.events import Events

# import sys
import time

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
        self.helloCommand = "M5"
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

        self.grblState = None
        self.grblX = float(0)
        self.grblY = float(0)
        self.grblSpeed = 0
        self.grblPowerLevel = 0

        self.timeRef = 0

        # self.grblLastX = sys.float_info.min
        # self.grblLastY = sys.float_info.min


        self.customControlsJson = r'[{"layout": "horizontal", "children": [{"commands": ["$10=0", "G28.1", "G92 X0 Y0 Z0"], "name": "Set Origin", "confirm": null}, {"command": "M999", "name": "Reset", "confirm": null}, {"commands": ["G1 F6000 S0", "M5", "$SLP"], "name": "Sleep", "confirm": null}, {"command": "$X", "name": "Unlock", "confirm": null}, {"commands": ["$32=0", "M4 S1"], "name": "Weak Laser", "confirm": null}, {"commands": ["$32=1", "M5"], "name": "Laser Off", "confirm": null}], "name": "Laser Commands"}, {"layout": "vertical", "type": "section", "children": [{"regex": "<([^,]+)[,|][WM]Pos:([+\\-\\d.]+,[+\\-\\d.]+,[+\\-\\d.]+)", "name": "State", "default": "", "template": "State: {0} - Position: {1}", "type": "feedback"}, {"regex": "F([\\d.]+) S([\\d.]+)", "name": "GCode State", "default": "", "template": "Speed: {0}  Power: {1}", "type": "feedback"}], "name": "Realtime State"}]'

    # def on_settings_initialized(self):
    #     self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
    #     self._logger.info("hideTempTab: %s" % self.hideTempTab)
    #
    #     self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])
    #     self._logger.info("hideGCodeTab: %s" % self.hideGCodeTab)

    def on_after_startup(self):

        self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
        self.hideControlTab = self._settings.get_boolean(["hideControlTab"])
        self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])
        self.customControls = self._settings.get_boolean(["customControls"])

        self.helloCommand = self._settings.get(["helloCommand"])
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

        # self._settings.global_set_boolean(["feature", "temperatureGraph"], not self.hideTempTab)
        # self._settings.global_set_boolean(["feature", "gCodeVisualizer"], not self.hideGCodeTab)
        # self._settings.global_set_boolean(["gcodeViewer", "enabled"], not self.hideGCodeTab)

        # hardcoded global settings -- should revisit how I manage these
        self._settings.global_set_boolean(["feature", "modelSizeDetection"], not self.disableModelSizeDetection)
        self._settings.global_set_boolean(["serial", "neverSendChecksum"], self.neverSendChecksum)

        if self.neverSendChecksum:
            self._settings.global_set(["serial", "checksumRequiringCommands"], [])

        self._settings.global_set(["serial", "helloCommand"], self.helloCommand)

        # disable the printer safety check plugin
        if self.disablePrinterSafety:
            disabledPlugins = self._settings.global_get(["plugins", "_disabled"])
            if disabledPlugins == None:
                disabledPlugins = []

            if "printer_safety_check" not in disabledPlugins:
                disabledPlugins.append("printer_safety_check")

            self._settings.global_set(["plugins", "_disabled"], disabledPlugins)

        # establish initial state for printer status
        self._settings.set_boolean(["is_printing"], self._printer.is_printing())
        self._settings.set_boolean(["is_operational"], self._printer.is_operational())

        # self._settings.global_set_boolean(["feature", "sdSupport"], False)
        # self._settings.global_set_boolean(["serial", "capabilities", "autoreport_sdstatus"], False)

        # self._settings.global_set_boolean(["serial", "capabilities", "autoreport_temp"], False)
        # self._settings.global_set_boolean(["serial", "capabilities", "busy_protocol"], False)
        # self._settings.global_set_boolean(["serial", "disconnectOnErrors"], False)
        # self._settings.global_set_boolean(["serial", "firmwareDetection"], False)


        # self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "idle"], 0)
        # self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "long"], 0)
        # self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "printing"], 0)

        # self._settings.global_set(["serial", "supportResendsWithoutOk"], "detect")

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
                disabledTabs.append("gcodeviewer")
        else:
            if "gcodeviewer" in disabledTabs:
                disabledTabs.remove("gcodeviewer")

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

        # ensure i am always the first tab
        if self.reOrderTabs:
            orderedTabs = self._settings.global_get(["appearance", "components", "order", "tab"])

            if "plugin_bettergrblsupport" in orderedTabs:
                orderedTabs.remove("plugin_bettergrblsupport")

            orderedTabs.insert(0, "plugin_bettergrblsupport")
            self._settings.global_set(["appearance", "components", "order", "tab"], orderedTabs)

        self._settings.save()

    # #~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(
            hideTempTab = True,
            hideControlTab = True,
            hideGCodeTab = True,
            helloCommand = "M5",
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
            disablePrinterSafety = True
        )

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.on_after_startup()

    # #~~ AssetPlugin mixin

    def get_assets(self):

        # Define your plugin's asset files to automatically include in the
        # core UI here.

        return dict(js=['js/bettergrblsupport.js'],
                    css=['css/bettergrblsupport.css'],
                    less=['less/bettergrblsupport.less'])

    # #~~ TemplatePlugin mixin

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    def get_template_vars(self):
        return dict(hideTempTab=self._settings.get_boolean(["hideTempTab"]),
                    hideControlTab=self._settings.get_boolean(["hideControlTab"]),
                    hideGCodeTab=self._settings.get_boolean(["hideGCodeTab"]),
                    customControls=self._settings.get_boolean(["customControls"]),
                    disablePolling=self._settings.get_boolean(["disablePolling"]),
                    suppressM105=self._settings.get_boolean(["suppressM105"]),
                    suppressM110=self._settings.get_boolean(["suppressM110"]),
                    suppressM115=self._settings.get_boolean(["suppressM115"]),
                    suppressM400=self._settings.get_boolean(["suppressM400"]),
                    suppressM114=self._settings.get_boolean(["suppressM114"]),
                    positionCommand=self._settings.get(["positionCommand"]),
                    statusCommand=self._settings.get(["statusCommand"]),
                    dwellCommand=self._settings.get(["dwellCommand"]),
                    disableModelSizeDetection=self._settings.get(["disableModelSizeDetection"]),
                    neverSendChecksum=self._settings.get(["neverSendChecksum"]),
                    reOrderTabs=self._settings.get(["reOrderTabs"]),
                    disablePrinterSafety=self._settings.get(["disablePrinterSafety"]),
                    helloCommand=self._settings.get(["helloCommand"]))

    # #-- EventHandlerPlugin mix-in

    def on_event(self, event, payload):

        is_printing = self._printer.is_printing()
        is_operational = self._printer.is_operational()

        if self._settings.get_boolean(["is_printing"]) != is_printing or self._settings.get_boolean(["is_operational"]) != is_operational:
            self._settings.set_boolean(["is_printing"], is_printing)
            self._settings.set_boolean(["is_operational"], is_operational)
            self._settings.save()

        subscribed_events = Events.FILE_SELECTED + Events.PRINT_STARTED
        if subscribed_events.find(event) == -1:
            return

        # 'PrintStarted'
        if event == Events.PRINT_STARTED:
            self.grblState = "Run"

        # 'FileSelected'
        if event == Events.FILE_SELECTED:
            selected_file = self._settings.global_get_basefolder("uploads") + '/' + payload['path']
            f = open(selected_file, 'r')

            minX = float(0)
            minY = float(0)
            maxX = float(0)
            maxY = float(0)

            x = float(0)
            y = float(0)

            for line in f:
                if line.startswith("G0") or line.startswith("G1"):
                    match = re.search(r"^G[01].*X\ *(-?[\d.]+).*", line)
                    if not match is None:
                        x = x + float(match.groups(1)[0])
                        if x < minX:
                            minX = x
                        if x > maxX:
                            maxX = x

                    match = re.search(r"^G[01].*Y\ *(-?[\d.]+).*", line)
                    if not match is None:
                        y = y + float(match.groups(1)[0])
                        if y < minY:
                            minY = y
                        if y > maxY:
                            maxY = y

            length = maxY - minY
            width = maxY - minX

            self._logger.info('finished reading {} length={} width={}'.format(selected_file, length, width))

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_frame_size",
                                                                            length=length,
                                                                            width=width))

            return

        if event == Events.FILE_DESELECTED:
            return

        return

    # #-- gcode sending hook
    def hook_gcode_sending(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):

         # rewrite M115 as M5 (hello)
        if self.suppressM115 and cmd.upper().startswith('M115'):
            self._logger.debug('Rewriting M115 as %s' % self.helloCommand)
            return (self.helloCommand, )

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
        if cmd.upper().startswith('M999'):
            self._logger.info('Sending Soft Reset')
            # self._printer.commands("\x18")
            return ("\x18",)

        # keep track of distance traveled
        if cmd.startswith("G0") or cmd.startswith("G1"):
            found = False
            match = re.search(r"^G[01].*X\ *(-?[\d.]+).*", cmd)
            if not match is None:
                self.grblX = self.grblX + float(match.groups(1)[0])
                found = True

            match = re.search(r"^G[01].*Y\ *(-?[\d.]+).*", cmd)
            if not match is None:
                self.grblY = self.grblY + float(match.groups(1)[0])
                found = True

            match = re.search(r"^G[01].*F\ *(-?[\d.]+).*", cmd)
            if not match is None:
                self.grblSpeed = int(match.groups(1)[0])
                found = True

            match = re.search(r"^G[01].*S\ *(-?[\d.]+).*", cmd)
            if not match is None:
                self.grblPowerLevel = int(float(match.groups(1)[0]))
                found = True

            if found:
                currentTime = int(round(time.time() * 1000))
                if currentTime > self.timeRef + 500:
                    # self._logger.info("x={} y={} f={} s={}".format(self.grblX, self.grblY, self.grblSpeed, self.grblPowerLevel))
                    self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                                    state=self.grblState,
                                                                                    x=self.grblX,
                                                                                    y=self.grblY,
                                                                                    speed=self.grblSpeed,
                                                                                    power=self.grblPowerLevel))
                    self.timeRef = currentTime

        return None

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

        # hack to force status updates
        # if line.startswith("[MSG:Pgm End]") and self.grblState == "Run":
        #     if self.suppressM105:
        #         self._printer.commands(self.statusCommand)

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

            if self.grblState == "Sleep" or self.grblState == "Run":
                self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                                state=self.grblState,
                                                                                x=self.grblX,
                                                                                y=self.grblY,
                                                                                speed=self.grblSpeed,
                                                                                power=self.grblPowerLevel))

            return response

        match = re.search(r"F(-?[\d.]+) S(-?[\d.]+)", line)
        if not match is None:
            self.grblSpeed = int(match.groups(1)[0])
            self.grblPowerLevel = int(match.groups(1)[1])

            self._plugin_manager.send_plugin_message(self._identifier, dict(type="grbl_state",
                                                                            state=self.grblState,
                                                                            x=self.grblX,
                                                                            y=self.grblY,
                                                                            speed=self.grblSpeed,
                                                                            power=self.grblPowerLevel))

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


    def send_frame_init_gcode(self):
        self._printer.commands("G00 G17 G40 G21 G54")
        self._printer.commands("G91")
        self._printer.commands("$32=0")
        self._printer.commands("M4 S1")
        self._printer.commands("M8")

    def send_frame_end_gcode(self):
        self._printer.commands("M9")
        self._printer.commands("G1S0")
        self._printer.commands("$32=1")
        self._printer.commands("M5")
        self._printer.commands("M2")


    def send_bounding_box_center(self, y, x):
        if not self._printer.is_ready():
            return

        self.send_frame_init_gcode()

        self._printer.commands("G0 X{:f} Y{:f} F6000".format(x / 2 * -1, y / 2))

        self._printer.commands("G91")
        self._printer.commands("G0 X{} F4000 S1".format(x))
        self._printer.commands("G0Y{:f} S1".format(y * -1))
        self._printer.commands("G0 X{} S1".format(x * -1))
        self._printer.commands("G0 Y{} S1".format(y))
        self._printer.commands("G0 X{:f} Y{:f} F6000".format(x / 2, y / 2 * -1))

        self.send_frame_end_gcode()

    def send_bounding_box_lower_left(self, y, x):
        if not self._printer.is_ready():
            return

        self.send_frame_init_gcode()

        self._printer.commands("G91")
        self._printer.commands("G0 Y{} F4000 S1".format(y))
        self._printer.commands("G0 X{} F4000 S1".format(x))
        self._printer.commands("G0 Y{} F4000 S1".format(y * -1))
        self._printer.commands("G0 X{} F4000 S1".format(x * -1))

        self.send_frame_end_gcode()

    def get_api_commands(self):
        return dict(
            frame=[],
            toggleWeak=[],
            origin=[],
            move=[],
            sleep=[],
            reset=[],
            unlock=[]
        )

    def on_api_command(self, command, data):

        # catch-all (should revisit state management) for validating printer State
        if not self._printer.is_operational() or self._printer.is_printing():
            self._logger.info("ignoring command - printer is not available")
            return

        if command == "frame":
            origin = data.get("origin").strip()

            if (origin == "grblCenter"):
                self.send_bounding_box_center(float(data.get("length")), float(data.get("width")))

            if (origin == "grblBottomLeft"):
                self.send_bounding_box_lower_left(float(data.get("length")), float(data.get("width")))

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

            if direction == "up":
                self._printer.commands("G91")
                self._printer.commands("G1 Y{} F6000".format(distance))
                self._printer.commands("G90")

            if direction == "down":
                self._printer.commands("G91")
                self._printer.commands("G1 Y{} F6000".format(distance * -1))
                self._printer.commands("G90")

            if direction == "left":
                self._printer.commands("G91")
                self._printer.commands("G1 X{} F6000".format(distance * -1))
                self._printer.commands("G90")

            if direction == "right":
                self._printer.commands("G91")
                self._printer.commands("G1 X{} F6000".format(distance))
                self._printer.commands("G90")

            return

        if command == "origin":
            # do origin stuff
            self._printer.commands("$10=0")
            self._printer.commands("G28.1")
            self._printer.commands("G92 X0 Y0 Z0")

            return

        if command == "toggleWeak":
            # do laser stuff
            powerLevel = self.grblPowerLevel

            if powerLevel == 0:
                self._printer.commands("$32=0")
                self._printer.commands("M4 S1")
                res = "Laser Off"
            else:
                self._printer.commands("M9")
                self._printer.commands("G1S0")
                self._printer.commands("$32=1")
                self._printer.commands("M5")
                self._printer.commands("M2")
                res = "Weak Laser"

            return flask.jsonify({'res' : res})

        if command == "sleep":
            self._printer.commands("$SLP")
            return

        if command == "unlock":
            self._printer.commands("$X")
            return

        if command == "reset":
            self._printer.commands("M999")
            return


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
            pip='https://github.com/synman/OctoPrint-Bettergrblsupport/archive/{target_version}.zip'))

# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.

__plugin_name__ = 'Better Grbl Support'

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = BetterGrblSupportPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = \
        {'octoprint.plugin.softwareupdate.check_config': __plugin_implementation__.get_update_information,
         'octoprint.comm.protocol.gcode.sending': __plugin_implementation__.hook_gcode_sending,
         'octoprint.comm.protocol.gcode.received': __plugin_implementation__.hook_gcode_received}
