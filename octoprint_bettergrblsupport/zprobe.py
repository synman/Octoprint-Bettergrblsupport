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
    _sessionId = None
    _step = -1
    _locations=[]
    _results=[]


    def __init__(self, _plugin, _hook, _sessionId):
        _plugin._logger.debug("ZProbe: __init__ sessionId=[{}]".format(_sessionId))

        self._plugin = _plugin
        self._hook = _hook
        self._sessionId = _sessionId

    def simple_probe(self):
        self._plugin._logger.debug("ZProbe: simple_probe sessionId=[{}]".format(self._sessionId))

        _bgs.addToNotifyQueue(self._plugin, ["Z-Probe Initiated"])

        self._plugin._plugin_manager.send_plugin_message(self._plugin._identifier, dict(type="grbl_state", state="Run"))
        self._plugin._printer.commands(["$G", "G91", "G21", "G38.2 Z-{} F100".format(self._plugin.zLimit if self._plugin.zProbeTravel == 0 else self._plugin.zProbeTravel)], force=True)
        self._plugin._printer.fake_ack()


    def notify(self, notifications):
        self._plugin._logger.debug("ZProbe: notify notifications=[{}] sessionId=[{}]".format(notifications, self._sessionId))

        for notification in notifications:
            # [PRB:0.000,0.000,0.000:0]
            if notification.startswith("[PRB:"):
                firstSplit = notification.replace("[", "").replace("]", "").split(":")
                secondSplit = firstSplit[1].split(",")

                result = int(float(firstSplit[2]))
                position = float(secondSplit[2])

                if (result == 1):
                    self._results.append({"position": position})

                notifications.remove(notification)
                self._hook(self._plugin, result, position)

    def getCurrentLocation(self):
        self._plugin._logger.debug("ZProbe: getCurrentLocation step=[{}] location=[{}] sessionId=[{}]".format(self._step, self._locations[self._step], self._sessionId))
        return self._locations[self._step]


    def resultByCalc(self, calculation):
        self._plugin._logger.debug("ZProbe: resultByCalc calc=[{}] sessionId=[{}]".format(calculation, self._sessionId))
        ordered = sorted(self._results, key = lambda i: i["position"])

        if calculation == "GAP":
            return ordered[-1].get("position") - ordered[0].get("position")
        elif calculation == "MIN":
            return ordered[-1].get("position")
        elif calculation == "MAX":
            return ordered[0].get("position")
        elif calculation == "MEAN":
            return (ordered[-1].get("position") - ordered[0].get("position")) / 2
        elif calculation == "AVG":
            result = float(0)
            for item in ordered:
                result+= item.get("position")
            return result / len(ordered)

        return None


    def teardown(self):
        self._plugin._logger.debug("ZProbe: teardown sessionId=[{}]".format(self._sessionId))

        self._hook = None
        self._plugin = None
        self._sessionId = None
        self._step = -1
        self._locations.clear()
        self._results.clear()
