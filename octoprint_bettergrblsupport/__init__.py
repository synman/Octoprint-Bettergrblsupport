# coding=utf-8
from __future__ import absolute_import

### (Don't forget to remove me)
# This is a basic skeleton for your plugin's __init__.py. You probably want to adjust the class name of your plugin
# as well as the plugin mixins it's subclassing from. This is really just a basic skeleton to get you started,
# defining your plugin as a template plugin, settings and asset plugin. Feel free to add or remove mixins
# as necessary.
#
# Take a look at the documentation on what other plugin mixins are available.

import octoprint.plugin
import re
import logging

class BettergrblsupportPlugin(octoprint.plugin.SettingsPlugin,
                              octoprint.plugin.AssetPlugin,
                              octoprint.plugin.TemplatePlugin,
                              octoprint.plugin.StartupPlugin,
                              octoprint.plugin.EventHandlerPlugin):

	def on_after_startup(self):
         self._logger.info("Better Grbl Support On After Startup")


	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			# put your plugin's default settings here
		)

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/bettergrblsupport.js"],
			css=["css/bettergrblsupport.css"],
			less=["less/bettergrblsupport.less"]
		)

    ##-

	def on_event(self, event, payload):
         self._logger.info('on_event: [%s]' % event)

         subscribed_events = 'FileSelected FileDeselected'

         if subscribed_events.find(event):
            self._logger.debug('EventHandlerPlugin: [%s]' % event)
         else:
            self._logger.info('EventHandlerPlugin: [%s]' % event)
            return

         if (event == 'FileSelected'):
            file = open(payload.path + "/" + payload.name, "r")

            for line in file:
                self._logger.debug(line)

            return

         if (event == "FileDeselected"):
            return

         return


	def hook_gcode_sending(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
         # rewrite M115 as M5 (hello)
         if cmd.upper().startswith('M115'):
             self._logger.info("Rewriting M115 as M5")
             return "M5",

         # suppress unsupported commands - temperature & reset line #s
         if cmd.upper().startswith('M110') or cmd.upper().startswith("M105"):
             return None,

         # Wait for moves to finish before turning off the spindle
         if cmd.upper().startswith('M400'):
             return 'G4 P0',

         # rewrite current position
         if cmd.upper().startswith('M114'):
             return '?',

         return None

	def hook_gcode_received(comm_instance, line, *args, **kwargs):
         """
         This plugin moves Grbl's ok from the end to the start.
         OctoPrint needs the 'ok' to be at the start of the line.
         """
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
             return 'ok X:{0} Y:{1} Z:{2} E:0 {original}'.format(
                 *match.groups(),
                 original=line
             )

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
             before, _, _ = line.partition('{')
             return 'ok ' + before
         else:
             return 'ok'

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			bettergrblsupport=dict(
				displayName="Bettergrblsupport Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="synman",
				repo="OctoPrint-Bettergrblsupport",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/synman/OctoPrint-Bettergrblsupport/archive/{target_version}.zip"
			)
		)


# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "Bettergrblsupport Plugin"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = BettergrblsupportPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.sending": __plugin_implementation__.hook_gcode_sending,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.hook_gcode_received
	}
