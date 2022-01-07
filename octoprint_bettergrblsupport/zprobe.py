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
from . import _bgs

class ZProbe:
    _plugin = None
    _hook = None
    _step = -1
    _locations=[]


    def __init__(self, _plugin, _hook):
        self._plugin = _plugin
        self._hook = _hook
        _plugin._logger.debug("ZProbe initialized")


    def probe(self):
        _bgs.addToNotifyQueue(self._plugin, ["Z-Probe Initiated"])

        self._plugin._plugin_manager.send_plugin_message(self._plugin._identifier, dict(type="grbl_state", state="Run"))
        self._plugin._printer.commands(["$G", "G91", "G21", "G38.2 Z-{} F100".format(self._plugin.zLimit if self._plugin.zProbeTravel == 0 else self._plugin.zProbeTravel)], force=True)
        self._plugin._logger.debug("ZProbe probe")


    def notify(self, notifications):
        for notification in notifications:
            # [PRB:0.000,0.000,0.000:0]
            if notification.startswith("[PRB:"):
                firstSplit = notification.replace("[", "").replace("]", "").split(":")
                secondSplit = firstSplit[1].split(",")

                result = int(float(firstSplit[2]))
                position = float(secondSplit[2])

                notifications.remove(notification)
                self._hook(self._plugin, result, position)


    def teardown(self):
        self._hook = None
        self._plugin = None
