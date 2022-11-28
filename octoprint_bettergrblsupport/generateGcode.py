#!/usr/bin/python3
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
# def backAndForth():
#     axis1sign = 1
#     axis2sign = 1
#
#     axis2inc = 2
#     zinc = .1
#
#     depth = .6
#
#     print("G1 Z{} F{}".format(zinc, plunge)
#
#     for z in range(0, int(round(depth / zinc)) + 1):
#         print(""
#         print("%%% Z DEPTH = {} %%%".format(z * zinc)
#         print(""
#         print("G1 Z-{} F{}".format(zinc, plunge)
#
#         for a in range(0, int(round(length / axis2inc))):
#             print(";% y={}".format(a * axis2inc)
#             print("G1 X{} F{}".format(width / 2 * axis1sign, feed)
#             print("G1 X{} F{}".format(width / 2 * axis1sign, feed)
#             print("G1 Y{} F{}".format(axis2inc * axis2sign, feed)
#             axis1sign = axis1sign * -1
#
#         axis2sign = axis2sign * -1

def SideToSideAndOutAndBack():
    axis1sign = 1
    axis2inc = 3

    depth = .1

    print("%")
    print("% Side To Side")
    print("%")
    print("%%% Z DEPTH = {} %%%".format(depth))
    print("%")
    print("G1 Z-{} F{}".format(depth, plunge))

    for a in range(0, int(round(length / axis2inc))):
        print(";% y={}".format(a * axis2inc))
        print("G1 X{} F{}".format(width / 2 * axis1sign, feed))
        print("G1 X{} F{}".format(width / 2 * axis1sign, feed))
        print("G1 Y{} F{}".format(axis2inc, feed))
        axis1sign = axis1sign * -1

    axis1sign = 1

    print("%")
    print("% Out and Back")
    print("%")

    print("G53 G0 Z-5")
    print("M5 S0")
    print("G90")
    print("G0 X0 Y0")

    print("M3 S16000")
    print("G1 Z0 F{}".format(plunge))
    print("G91")

    print("%")
    print("%%% Z DEPTH = {} %%%".format(depth))
    print("%")
    print("G1 Z-{} F{}".format(depth, plunge))

    for a in range(0, int(round(width / axis2inc))):
        print(";% x={}".format(a * axis2inc))
        print("G1 Y{} F{}".format(length / 2 * axis1sign, feed))
        print("G1 Y{} F{}".format(length / 2 * axis1sign, feed))
        print("G1 X{} F{}".format(axis2inc, feed))
        axis1sign = axis1sign * -1

# def box():
#     depth = 1
#     layer = .2
#
#     for a in range(0, int(round(depth / layer))):
#         print(";% z={}".format((a+1) * layer)
#         print("G1 Z-{} F{}".format(layer, plunge)
#         print("G1 Y{} F{}".format(length, feed)
#         print("G1 X{} F{}".format(width, feed)
#         print("G1 Y{} F{}".format(length * -1, feed)
#         print("G1 X{} F{}".format(width * -1, feed)

plunge = 100
feed = 2000

length = 495
width = 495

print("M3 S16000")
print("G21")
print("G90")
print("G1 Z0 F{}".format(plunge))
print("G91")

# gridDown()
# grid1Pass()
# box()
SideToSideAndOutAndBack()

print("%")
print("G53 G0 Z-10")
print("M5 S0")
print("G90")
print("G0 X0 Y0")
print("%")
print("M30")
