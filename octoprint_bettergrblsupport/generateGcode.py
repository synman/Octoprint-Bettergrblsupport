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
#
##### this gcode runs a 400 x 380 5mm y / .1mm z increment
##### back and forth grid for surfacing your spoilboard
#
xsign = 1
ysign = 1

print "M3 S10000"
print "G21"
print "G1 G90 Z0 F100"
print "G91"
print "G1 Z.1 F100"

for z in range(0, 7):
    print ""
    print "%%% Z DEPTH = {} %%%".format(z * .1)
    print ""
    print "G1 Z-.1 F100"

    for y in range(1, 380 / 5):
        print "G1 X{} F800".format(400 * xsign)
        print "G1 Y{} F800".format(5 * ysign)
        xsign = xsign * -1

    ysign = ysign * -1

print ""
print "G1 G90 Z5 F100"
print "M5 S0"
print "M30"
