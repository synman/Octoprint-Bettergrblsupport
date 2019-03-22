#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import

import octoprint.plugin
import re
import logging


class BetterGrblSupportPlugin(octoprint.plugin.SettingsPlugin,
                              octoprint.plugin.AssetPlugin,
                              octoprint.plugin.TemplatePlugin,
                              octoprint.plugin.StartupPlugin,
                              octoprint.plugin.EventHandlerPlugin):

    def __init__(self):
        self.hideTempTab = True
        self.hideGCodeTab = True
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

    # def on_settings_initialized(self):
    #     self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
    #     self._logger.info("hideTempTab: %s" % self.hideTempTab)
    #
    #     self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])
    #     self._logger.info("hideGCodeTab: %s" % self.hideGCodeTab)

    def on_after_startup(self):

        self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
        self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])
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

        self._settings.global_set_boolean(["feature", "temperatureGraph"], not self.hideTempTab)
        self._settings.global_set_boolean(["feature", "gCodeVisualizer"], not self.hideGCodeTab)
        self._settings.global_set_boolean(["feature", "modelSizeDetection"], False)
        self._settings.global_set_boolean(["feature", "sdSupport"], False)

        self._settings.global_set_boolean(["gcodeViewer", "enabled"], not self.hideGCodeTab)

        self._settings.global_set_boolean(["serial", "capabilities", "autoreport_sdstatus"], False)
        self._settings.global_set_boolean(["serial", "capabilities", "autoreport_temp"], False)
        self._settings.global_set_boolean(["serial", "capabilities", "busy_protocol"], False)
        self._settings.global_set_boolean(["serial", "disconnectOnErrors"], False)
        self._settings.global_set_boolean(["serial", "firmwareDetection"], False)
        self._settings.global_set_boolean(["serial", "neverSendChecksum"], True)

        self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "idle"], 0)
        self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "long"], 0)
        self._settings.global_set_int(["serial", "maxCommunicationTimeouts", "printing"], 0)

        if self.hideTempTab:
            self._settings.global_set(["appearance", "components", "disabled", "tab"], ["temperature"])
        else:
            self._settings.global_set(["appearance", "components", "disabled", "tab"], [])

        self._settings.global_set(["plugins", "_disabled"], ["printer_safety_check"])

        self._settings.global_set(["serial", "checksumRequiringCommands"], [])
        self._settings.global_set(["serial", "helloCommand"], self.helloCommand)
        self._settings.global_set(["serial", "supportResendsWithoutOk"], "never")

        self._settings.save()

    # #~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(
            firstTime = True,
            hideTempTab = True,
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
            disablePolling = True
        )

    # def on_settings_save(self, data):
    #     octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
    #
    #     self.hideTempTab = self._settings.get_boolean(["hideTempTab"])
    #     self.hideGCodeTab = self._settings.get_boolean(["hideGCodeTab"])

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
                    hideGCodeTab=self._settings.get_boolean(["hideGCodeTab"]),
                    disablePolling=self._settings.get_boolean(["disablePolling"]),
                    suppressM105=self._settings.get_boolean(["suppressM105"]),
                    suppressM110=self._settings.get_boolean(["suppressM110"]),
                    suppressM115=self._settings.get_boolean(["suppressM115"]),
                    suppressM400=self._settings.get_boolean(["suppressM400"]),
                    suppressM114=self._settings.get_boolean(["suppressM114"]),
                    positionCommand=self._settings.get(["positionCommand"]),
                    statusCommand=self._settings.get(["statusCommand"]),
                    dwellCommand=self._settings.get(["dwellCommand"]),
                    helloCommand=self._settings.get(["helloCommand"]))

    # #-- EventHandlerPlugin mix-in

    def on_event(self, event, payload):
        subscribed_events = 'FileSelected FileDeselected'

        if subscribed_events.find(event) == -1:
            return

        if event == 'FileSelected':
            selected_file = self._settings.global_get_basefolder("uploads") + '/' + payload['path']
            f = open(selected_file, 'r')

            for line in f:
                self._logger.info("[%s]" % line.strip())

            self._logger.info('finished reading [%s]', selected_file)
            return

        if event == 'FileDeselected':
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

        return None

    # #-- gcode received hook

    def hook_gcode_received(self, comm_instance, line, *args, **kwargs):

        if 'MPos' in line:
             # <Idle,MPos:0.000,0.000,0.000,WPos:0.000,0.000,0.000,RX:3,0/0>
             # <Run|MPos:-17.380,-7.270,0.000|FS:1626,0>

            match = re.search(r'MPos:(-?[\d\.]+),(-?[\d\.]+),(-?[\d\.]+)', line)

            if match is None:
                log.warning('Bad data %s', line.rstrip())
                return line

             # OctoPrint records positions in some instances.
             # It needs a different format. Put both on the same line so the GRBL info is not lost
             # and is accessible for "controls" to read.

            response = 'ok X:{0} Y:{1} Z:{2} E:0'.format(*match.groups())
            self._logger.debug('[%s] rewrote as [%s]', line.strip(), response.strip())

            return response

        if line.startswith('Grbl'):

             # Hack to make Arduino based GRBL work.
             # When the serial port is opened, it resets and the "hello" command
             # is not processed.
             # This makes Octoprint recognise the startup message as a successful connection.

            return 'ok ' + line

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
