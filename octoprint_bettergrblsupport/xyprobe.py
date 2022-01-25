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

class XyProbe:
    _plugin = None
    _hook = None
    _axes = None
    _sessionId = None
    _step = -1
    _results=[]


    def __init__(self, _plugin, _hook, _axes, _sessionId):
        _plugin._logger.debug("XyProbe: __init__ sessionId=[{}]".format(_sessionId))

        self._plugin = _plugin
        self._hook = _hook
        self._axes = _axes
        self._sessionId = _sessionId


    def notify(self, notifications):
        self._plugin._logger.debug("XyProbe: notify notifications=[{}] step=[{}] sessionId=[{}]".format(notifications, self._step, self._sessionId))

        for notification in notifications:
            # [PRB:0.000,0.000,0.000:0]
            if notification.startswith("[PRB:"):
                self._step+=1

                frameOrigin = self._plugin._settings.get(["frame_origin"])
                xProbeOffset = float(self._plugin._settings.get(["xProbeOffset"])) * self._plugin.invertX
                yProbeOffset = float(self._plugin._settings.get(["yProbeOffset"])) * self._plugin.invertY

                offset = 0

                if self._step == 0:
                    originInvert = 1 if "Left" in frameOrigin else -1
                    offset = xProbeOffset * originInvert
                else:
                    originInvert = 1 if "Bottom" in frameOrigin else -1
                    offset = yProbeOffset * originInvert

                firstSplit = notification.replace("[", "").replace("]", "").split(":")
                secondSplit = firstSplit[1].split(",")

                result = int(float(firstSplit[2]))
                position = float(secondSplit[self._step]) + offset

                if (result == 1):
                    self._results.append(position)

                notifications.remove(notification)
                self._hook(self._plugin, result, position, "X" if self._step == 0 else "Y")


    def teardown(self):
        self._plugin._logger.debug("XyProbe: teardown sessionId=[{}]".format(self._sessionId))

        self._hook = None
        self._results.clear()
        self._step = -1
        self._axes = None
        self._plugin = None
        self._sessionId = None
