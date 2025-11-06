
/*
 * View model for OctoPrint-Bettergrblsupport
 *
 * Author: Shell M. Shrader
 * License: Apache 2.0
 */
$(function() {
    function BettergrblsupportViewModel(parameters) {
        var self = this;

        self.sessionId = guid();

        self.settings = parameters[0];
        self.loginState = parameters[1];
        self.access = parameters[2];
        self.notifications = parameters[3];

        self.my_notifications = ko.observableArray();

        var $controlTab = $("#control");
        var $controlPanel = undefined;

        self.origin_axes = ko.observableArray(["Z", "Y", "X", "XY", "ALL"]);
        self.origin_axis = ko.observable("XY");

        self.coordinate_systems = ko.observableArray(["G54", "G55", "G56", "G57", "G58", "G59"]);
        self.coordinate_system = ko.observable("G54");

        self.operator = ko.observable("=");
        self.distances = ko.observableArray([.1, 1, 5, 10, 50, 100]);
        self.distance = ko.observable(10);

        self.is_printing = ko.observable(false);
        self.is_operational = ko.observable(false);
        self.isLoading = ko.observable(undefined);
        self.probeEnabled = ko.observable(false)

        self.mode = ko.observable("N/A");
        self.state = ko.observable("N/A");
        self.xPos = ko.observable("N/A");
        self.yPos = ko.observable("N/A");
        self.zPos = ko.observable("N/A");
        self.aPos = ko.observable("N/A");
        self.bPos = ko.observable("N/A");
        self.power = ko.observable("N/A");
        self.speed = ko.observable("N/A");
        self.pins = ko.observable("N/A")
        self.positioning = ko.observable("N/A");
        self.coolant = ko.observable("N/A");

        self.feedRate = ko.observable(undefined);
        self.plungeRate = ko.observable(undefined);
        self.powerRate = ko.observable(undefined);

        self.controls = ko.observableArray([]);

        //tab = document.getElementById("mytab_plugin_bettergrblsupport_link");
        //tab.innerHTML = tab.innerHTML.replace("Better Grbl Support", "Grbl Control");

        self.handleFocus = function (event, type, item) {
          window.setTimeout(function () {
              event.target.select();
          }, 0);
        };

        self.isIdleOrJogging = function() {
          return self.is_operational() &&
                 !self.is_printing() &&
                 (self.state() == 'Idle' || self.state() == 'Jog');
        };

        var jogInterval = undefined;

        self.jog = function(direction) {
          self.cancelJog();

          if (self.operator() == "J") {
            jogInterval = setInterval(function() { self.moveHead(direction, 10) }, 200);
          } else {
            self.moveHead(direction);
          }
        };

        self.cancelJog = function() {
          if (jogInterval != undefined) {
            OctoPrint.control.sendGcode("CANCELJOG");
            clearInterval(jogInterval);
            jogInterval = undefined;
          }
        }

        self.toggleWeak = function() {
            OctoPrint.simpleApiCommand("bettergrblsupport", "toggleWeak")
                .done(
                    function(data) {
                        var btn = document.getElementById("grblLaserButton");
                        btn.innerHTML = btn.innerHTML.replace(btn.innerText, data["res"]);
                    }
                )
                .fail(
                    function(data, status) {
                        new PNotify({
                            title: "Laser action failed!",
                            text: data.responseText,
                            hide: true,
                            buttons: {
                                sticker: false,
                                closer: true
                            },
                            type: "error"
                        })
                    }
                );
        };

        self.distanceClicked = function(distance) {
            var operator;
            if (self.operator() == "+") {
                operator = 1;
            } else {
                if (self.operator() == "-") {
                    operator = -1;
                } else {
                    operator = 0;
                }
            }

            if (operator != 0) {
                self.distance(parseFloat(self.distance()) + (parseFloat(distance) * operator));
            } else {
                self.distance(parseFloat(distance));
            }
        };

        self.operatorClicked = function() {
            if (self.operator() == "+") {
                self.operator("-");
            } else {
                if (self.operator() == "-") {
                    self.operator("J");
                } else {
                    if (self.operator() == "=") {
                        self.operator("+");
                    } else {
                      if (self.operator() == "J") {
                        self.operator("=");
                      }
                    }
                }
            }

            if (self.operator == "J") {

            }
        };

        self.moveHead = function(direction, distance) {
            if (distance == undefined) distance = self.distance();
            OctoPrint.simpleApiCommand("bettergrblsupport", "move", { "sessionId": self.sessionId,
                                                                      "direction": direction,
                                                                      "distance": distance,
                                                                      "axis": self.origin_axis() })
                .done(
                    function(data) {
                        if (data != undefined && data["res"] != undefined && data["res"].length > 0) {
                            new PNotify({
                                title: "Unable to Move!",
                                text: data["res"],
                                hide: true,
                                buttons: {
                                    sticker: false,
                                    closer: true
                                },
                                type: "error"
                            });
                        }
                    })
                .fail(
                    function(data, status) {
                        new PNotify({
                            title: "Move Head failed!",
                            text: data.responseText,
                            hide: true,
                            buttons: {
                                sticker: false,
                                closer: true
                            },
                            type: "error"
                        });
                    })
        };

        self.sendCommand = function(command) {
            if (command == "unlock") {
                new PNotify({
                    title: "Unlock Machine",
                    text: "GRBL prefers you re-home your machine rather than unlock it.  Are you sure you want to unlock your machine?",
                    type: "notice",
                    hide: false,
                    animation: "fade",
                    animateSpeed: "slow",
                    sticker: false,
                    closer: true,
                    confirm: {
                        confirm: true,
                        buttons: [{
                                text: "CONFIRM",
                                click: function(notice) {
                                    OctoPrint.simpleApiCommand("bettergrblsupport", command)
                                        .fail(
                                            function(data, status) {
                                                new PNotify({
                                                    title: "Unable to unlock machine!",
                                                    text: data.responseText,
                                                    hide: true,
                                                    buttons: {
                                                        sticker: false,
                                                        closer: true
                                                    },
                                                    type: "error"
                                                });
                                            });
                                    notice.remove();
                                }
                            },
                            {
                                text: "CANCEL",
                                click: function(notice) {
                                    notice.remove();
                                }
                            },
                        ]
                    },
                });
                return;
            }

            OctoPrint.simpleApiCommand("bettergrblsupport", command, { "origin_axis": self.origin_axis(),
                                                                       "feed_rate": self.feedRate(),
                                                                       "plunge_rate": self.plungeRate(),
                                                                       "power_rate": self.powerRate() })
                .done(
                    function(data) {
                        if (command == "feedRate") self.feedRate(undefined);
                        if (command == "plungeRate") self.plungeRate(undefined);
                        if (command == "powerRate") self.powerRate(undefined);    
                    })
                .fail(
                    function(data, status) {
                        new PNotify({
                            title: "Unable to send command: " + command,
                            text: data.responseText,
                            hide: true,
                            buttons: {
                                sticker: false,
                                closer: true
                            },
                            type: "error"
                        });
                    });
        };

        self.onBeforeBinding = function() {
            self.is_printing(self.settings.settings.plugins.bettergrblsupport.is_printing());
            self.is_operational(self.settings.settings.plugins.bettergrblsupport.is_operational());
            
            self.probeEnabled(self.settings.settings.plugins.bettergrblsupport.zprobeMethod() != "NONE");
            self.settings.settings.plugins.bettergrblsupport.zprobeMethod.subscribe(function(newValue) {
                self.probeEnabled(newValue != "NONE");
            });

            self.distance(self.settings.settings.plugins.bettergrblsupport.control_distance());
            self.settings.settings.plugins.bettergrblsupport.control_distance.subscribe(function(newValue) {
                self.distance(newValue);
            });

            if (self.settings.settings.plugins.bettergrblsupport.hasB() == true) { 
                self.origin_axes.unshift("B");
            }
            if (self.settings.settings.plugins.bettergrblsupport.hasA() == true) { 
                self.origin_axes.unshift("A"); 
            }

            //console.log(self.origin_axes());

            self.notifications.requestData = self.overrideRequestData;
            self.notifications.clear = self.overrideClear;
            self.notifications.onDataUpdaterPluginMessage = self.overrideOnDataUpdaterPluginMessage;
        };

        self.overrideRequestData = function() {
            OctoPrint.simpleApiCommand("bettergrblsupport", "getNotifications").done(self.notifications.fromResponse);
        };

        self.overrideClear = function() {
            OctoPrint.simpleApiCommand("bettergrblsupport", "clearNotifications");
        };

        self.overrideOnDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin !== "action_command_notification") {
                return;
            }

            self.notifications.requestData();

            if (data.message) {
                if (self.settings.settings.plugins.action_command_notification.enable_popups()) {
                    new PNotify({
                        title: gettext("Machine Notification"),
                        text: data.message,
                        hide: false,
                        icon: "fa fa-bell-o",
                        buttons: {
                            sticker: false,
                            closer: true
                        }
                    });
                }
            }
        };

        self.coordinateSystemChanged = function (coordinate_system) {
          // self.coordinate_system(coordinate_system)
            OctoPrint.control.sendGcode([coordinate_system, "?"]);
        };



        self.modifyControlTab = function() {
            $controlTab.children("div").not("#webcam_plugins_container, #bettergbrlsupport_control_panel, #control-jog-custom").remove();

            if ($('#bettergrblsupport_control_panel').length > 0) {
                $controlPanel = $('#bettergrblsupport_control_panel');
                //console.log("self.modifyControlTab");
                //console.log($controlPanel);
            }
            if ($controlPanel != undefined) {
                $("#webcam_plugins_container").after($controlPanel);
                $controlPanel.show();
            }
        };

        self.onTabChange = function(next, current) {
            if (next === "#control") {
                   self.modifyControlTab();
            }
        };



        self.fromCurrentData = function(data) {
            self._processStateData(data.state);
        };

        self.fromHistoryData = function(data) {
            self._processStateData(data.state);
        };

        self._processStateData = function(data) {
            self.is_printing(data.flags.printing);
            self.is_operational(data.flags.operational);
            self.isLoading(data.flags.loading);

            if (self.is_printing()) {
              self.state("Run");
            }

            if (!self.is_operational()) {
              self.state("N/A");
            }
        };


        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin == 'bettergrblsupport' && data.type == 'grbl_state') {
                if (data.mode != undefined) self.mode(data.mode);

                if (data.state != undefined && !(self.is_printing() && data.state == "Idle")) {
                  self.state(data.state);
                }

                if (data.x != undefined) self.xPos(Number.parseFloat(data.x).toFixed(2));
                if (data.y != undefined) self.yPos(Number.parseFloat(data.y).toFixed(2));
                if (data.z != undefined) self.zPos(Number.parseFloat(data.z).toFixed(2));
                if (data.a != undefined) self.aPos(Number.parseFloat(data.a).toFixed(2));
                if (data.b != undefined) self.bPos(Number.parseFloat(data.b).toFixed(2));

                if (data.speed != undefined) self.speed(Number.parseFloat(data.speed).toFixed(2));
                if (data.pins != undefined) self.pins(data.pins);

                if (data.power != undefined) {
                  var newPower = Number.parseFloat(data.power);
                  if (data.state != "Run" && data.power != "N/A" && !self.is_printing()) {
                    var btn = document.getElementById("grblLaserButton");
                    var oldPower = Number.parseFloat(self.power);

                    if (btn != null) {
                        if (newPower == 0 && oldPower != 0) {
                            btn.innerHTML = btn.innerHTML.replace(btn.innerText, "Weak Laser");
                        } else {
                            if (oldPower == 0 && newPower != 0) {
                                btn.innerHTML = btn.innerHTML.replace(btn.innerText, "Laser Off");
                            }
                        }
                    }
                  }
                  self.power(newPower.toFixed(2));
                }

                if (data.coord != undefined) self.coordinate_system(data.coord);

                if (data.coolant != undefined) {
                  if (data.coolant == "M7" || data.coolant == "M8") {
                    self.coolant("On");
                  } else {
                    self.coolant("Off");
                  }
                }

                if (data.positioning != undefined) {
                  if (data.positioning == 0) {
                    self.positioning("Absolute");
                  } else {
                    self.positioning("Relative");
                  }
                }
                // console.log("mode=" + data.mode + " state=" + data.state + " x=" + data.x + " y=" + data.y + " z=" + data.z + " power=" + data.power + " speed=" + data.speed);
                return
            }

            if (plugin == 'bettergrblsupport' && data.type == 'simple_notify') {
                if (data.sessionId == undefined || data.sessionId == self.sessionId) {
                    new PNotify({
                        title: data.title,
                        text: data.text,
                        hide: data.hide,
                        animation: "fade",
                        animateSpeed: "slow",
                        mouseReset: true,
                        delay: data.delay,
                        buttons: {
                            sticker: true,
                            closer: true
                        },
                        type: data.notify_type,
                    });
                }
                return
            }

            if (plugin == 'bettergrblsupport' && data.type == 'restart_required') {
                new PNotify({
                    title: "Restart Required",
                    text: "Octoprint may need to be restarted for your changes to take full effect.",
                    hide: false,
                    animation: "fade",
                    animateSpeed: "slow",
                    mouseReset: true,
                    buttons: {
                        sticker: true,
                        closer: true
                    },
                    type: "notice"
                });
                return
            }

            if (plugin == 'bettergrblsupport' && data.type == 'xy_probe') {
                if (data.sessionId != undefined && data.sessionId == self.sessionId) {
                  var text = "";
                  var confirmActions = self.settings.settings.plugins.bettergrblsupport.zProbeConfirmActions();

                  if (!confirmActions && ((data.axes == "XY" && data.step >= 0) || data.axes == "ALL")) {
                    OctoPrint.control.sendGcode(data.gcode);
                    return
                  }

                  text = "Select <B>PROCEED</B> to initiate an X/Y Probe for the [" + data.axis + "] axis.  Please ensure the probe is positioned properly before proceeding.";

                  new PNotify({
                      title: "X/Y Probe",
                      text: text,
                      type: "notice",
                      hide: false,
                      animation: "fade",
                      animateSpeed: "slow",
                      sticker: false,
                      closer: true,
                      confirm: {
                          confirm: true,
                          buttons: [{
                                  text: "PROCEED",
                                  click: function(notice) {
                                    OctoPrint.control.sendGcode(data.gcode);
                                    notice.remove();
                                  }
                              },
                              {
                                  text: "CANCEL",
                                  click: function(notice) {
                                      // we need to inform the plugin we bailed
                                        OctoPrint.simpleApiCommand("bettergrblsupport", "cancelProbe")
                                            .fail(
                                                function(data, status) {
                                                    new PNotify({
                                                        title: "Unable to cancel Multipoint Z-Probe",
                                                        text: data.responseText,
                                                        hide: true,
                                                        buttons: {
                                                            sticker: false,
                                                            closer: true
                                                        },
                                                        type: "error"
                                                    })
                                                  }
                                            );
                                        notice.remove();
                                  }
                              },
                          ]
                      },
                  });
                }
            }

            if (plugin == 'bettergrblsupport' && data.type == 'simple_zprobe') {
                if (data.sessionId != undefined && data.sessionId == self.sessionId) {
                    var text = "";
                    var confirmActions = self.settings.settings.plugins.bettergrblsupport.zProbeConfirmActions();

                    // if (!confirmActions) {
                    //   OctoPrint.control.sendGcode(data.gcode);
                    //   return
                    // }

                    text = "Select <B>PROCEED</B> to initiate Single Point Z-Probe once the machine is at the desired location, and you are ready to continue.";

                    new PNotify({
                        title: "Single Point Z-Probe",
                        text: text,
                        type: "notice",
                        hide: false,
                        animation: "fade",
                        animateSpeed: "slow",
                        sticker: false,
                        closer: true,
                        confirm: {
                            confirm: true,
                            buttons: [{
                                    text: "PROCEED",
                                    click: function(notice) {
                                        OctoPrint.control.sendGcode(data.gcode);
                                        notice.remove();
                                    }
                                },
                                {
                                    text: "CANCEL",
                                    click: function(notice) {
                                        // we need to inform the plugin we bailed
                                        OctoPrint.simpleApiCommand("bettergrblsupport", "cancelProbe")
                                            .fail(
                                                function(data, status) {
                                                    new PNotify({
                                                        title: "Unable to cancel Single Point Z-Probe",
                                                        text: data.responseText,
                                                        hide: true,
                                                        buttons: {
                                                            sticker: false,
                                                            closer: true
                                                        },
                                                        type: "error"
                                                    });
                                                }
                                            );
                                        notice.remove();
                                    }
                                },
                            ]
                        },
                    });
                }
            }

            if (plugin == 'bettergrblsupport' && data.type == 'multipoint_zprobe') {
                if (data.sessionId != undefined && data.sessionId == self.sessionId) {
                    var instruction = data.instruction;
                    var text = "";
                    var confirmActions = self.settings.settings.plugins.bettergrblsupport.zProbeConfirmActions();

                    if (!confirmActions && instruction.action == "move") {
                        OctoPrint.control.sendGcode(instruction.gcode);
                        OctoPrint.control.sendGcode("BGS_MULTIPOINT_ZPROBE_MOVE");
                    return
                    }

                    if (instruction.action == "probe") {
                        text = "Select <B>PROCEED</B> to initiate Z-Probe once the machine has reached the [<B>" + instruction.location + "</B>] location, and you are ready to continue.";
                    } else {
                        text = "Your machine is ready to move to the [<B>" + instruction.location + "</B>] location.  Select <B>PROCEED</B> when you are ready to continue.";
                    }

                    new PNotify({
                        title: "Multipoint Z-Probe",
                        text: text,
                        type: "notice",
                        hide: false,
                        animation: "fade",
                        animateSpeed: "slow",
                        sticker: false,
                        closer: true,
                        confirm: {
                            confirm: true,
                            buttons: [{
                                    text: "PROCEED",
                                    click: function(notice) {
                                        OctoPrint.control.sendGcode(instruction.gcode);
                                        if (instruction.action == "move") {
                                            OctoPrint.control.sendGcode("BGS_MULTIPOINT_ZPROBE_MOVE");
                                        }
                                        notice.remove();
                                    }
                                },
                                {
                                    text: "CANCEL",
                                    click: function(notice) {
                                        // we need to inform the plugin we bailed
                                        OctoPrint.simpleApiCommand("bettergrblsupport", "cancelProbe")
                                            .fail(
                                                function(data, status) {
                                                    new PNotify({
                                                        title: "Unable to cancel Multipoint Z-Probe",
                                                        text: data.responseText,
                                                        hide: true,
                                                        buttons: {
                                                            sticker: false,
                                                            closer: true
                                                        },
                                                        type: "error"
                                                    });
                                                }
                                            );
                                        notice.remove();
                                    }
                                },
                            ]
                        },
                    });
                }
            }

            if (plugin == "bettergrblsupport" && data.type == "notification") {
                self.notifications.onDataUpdaterPluginMessage("action_command_notification", {message: data.message})                    
            }
        }

        self.modeClick = function() {
          if (self.is_operational() && !self.is_printing()) {
            if (self.mode() == "WPos") {
              OctoPrint.control.sendGcode(["$10=1", "?", "$$"]);
            } else {
              OctoPrint.control.sendGcode(["$10=0", "?", "$$"]);
            }
          }
        }

        self.moveClick = function() {
          if (self.is_operational() && !self.is_printing() && self.state() == "Idle") {
            if (self.positioning() == "Absolute") {
              OctoPrint.control.sendGcode(["G91"]);
            } else {
              OctoPrint.control.sendGcode(["G90"]);
            }
          }
        }

        self.coolClick = function() {
          if (self.is_operational()) {
            if (self.coolant() == "Off") {
              OctoPrint.control.sendGcode(["M8"]);
            } else {
              OctoPrint.control.sendGcode(["M9"]);
            }
          }
        }

        self.feedRateResetter = ko.observable();
        self.resetFeedRateDisplay = function() {
            self.cancelFeedRateDisplayReset();
            self.feedRateResetter(
                setTimeout(function() {
                    self.feedRate(undefined);
                    self.feedRateResetter(undefined);
                }, 5000)
            );
        };
        self.cancelFeedRateDisplayReset = function() {
            var resetter = self.feedRateResetter();
            if (resetter) {
                clearTimeout(resetter);
                self.feedRateResetter(undefined);
            }
        };

        self.plungeRateResetter = ko.observable();
        self.resetPlungeRateDisplay = function() {
            self.cancelPlungeRateDisplayReset();
            self.plungeRateResetter(
                setTimeout(function() {
                    self.plungeRate(undefined);
                    self.plungeRateResetter(undefined);
                }, 5000)
            );
        };
        self.cancelPlungeRateDisplayReset = function() {
            var resetter = self.plungeRateResetter();
            if (resetter) {
                clearTimeout(resetter);
                self.plungeRateResetter(undefined);
            }
        };

        self.powerRateResetter = ko.observable();
        self.resetPowerRateDisplay = function() {
            self.cancelPowerRateDisplayReset();
            self.powerRateResetter(
                setTimeout(function() {
                    self.powerRate(undefined);
                    self.powerRateResetter(undefined);
                }, 5000)
            );
        };
        self.cancelPowerRateDisplayReset = function() {
            var resetter = self.powerRateResetter();
            if (resetter) {
                clearTimeout(resetter);
                self.powerRateResetter(undefined);
            }
        };



        self.onKeyDown = function (data, event) {
            if (!self.settings.feature_keyboardControl()) return;

            var button = undefined;
            var visualizeClick = true;
            var simulateTouch = false;

            switch (event.which) {
                case 37: // left arrow key
                    // X-
                    button = $("#control-west");
                    simulateTouch = true;
                    break;
                case 38: // up arrow key
                    // Y+
                    button = $("#control-north");
                    simulateTouch = true;
                    break;
                case 39: // right arrow key
                    // X+
                    button = $("#control-east");
                    simulateTouch = true;
                    break;
                case 40: // down arrow key
                    // Y-
                    button = $("#control-south");
                    simulateTouch = true;
                    break;
                case 49: // number 1
                case 97: // numpad 1
                    // toggle operator
                    button = $("#control-distance-operator");
                    break;
                case 50: // number 2
                case 98: // numpad 2
                    // Distance 0.1
                    button = $("#control-distance-0");
                    break;
                case 51: // number 3
                case 99: // numpad 3
                    // Distance 1
                    button = $("#control-distance-1");
                    break;
                case 52: // number 4
                case 100: // numpad 4
                    // Distance 5
                    button = $("#control-distance-2");
                    break;
                case 53: // number 5
                case 101: // numpad 5
                    // Distance 10
                    button = $("#control-distance-3");
                    break;
                case 54: // number 6
                case 102: // numpad 6
                    // Distance 50
                    button = $("#control-distance-4");
                    break;
                case 55: // number 7
                case 103: // numpad 7
                    // Distance 100
                    button = $("#control-distance-5");
                    break;
                case 33: // page up key
                case 87: // w key
                    // z lift up
                    button = $("#control-zup");
                    break;
                case 34: // page down key
                case 83: // s key
                    // z lift down
                    button = $("#control-zdown");

                    break;
                case 36: // home key
                    // xy home
                    button = $("#control-home");
                    $("#control-axes-XY").click();
                    break;
                case 35: // end key
                    // z home
                    button = $("#control-home");
                    $("#control-axes-Z").click();
                    break;
                default:
                    event.preventDefault();
                    return false;
            }

            if (button === undefined) {
                return false;
            } else {
                event.preventDefault();
                if (visualizeClick) {
                    button.addClass("active");
                    setTimeout(function () {
                        button.removeClass("active");
                    }, 150);
                }
                if (simulateTouch) {
                    button.mousedown();
                    setTimeout(function () {
                        button.mouseup();
                    }, 150);
                } else {
                    button.click();
                }
            }
        };

        $(document).ready(function() {
            $(this).keydown(function(e) {
                if (OctoPrint.coreui.selectedTab != undefined &&
                        OctoPrint.coreui.selectedTab == "#tab_plugin_bettergrblsupport" &&
                        OctoPrint.coreui.browserTabVisible && $(":focus").length == 0) {
                    self.onKeyDown(undefined, e);
                }
            });
        
            $(this).keyup(function(e) {
                // console.log("keyup");
            });
        });
    }

    // cute little hack for removing "Print" from the start button
    $('#job_print')[0].innerHTML = "<i class=\"fas\" data-bind=\"css: {'fa-print': !isPaused(), 'fa-undo': isPaused()}\"></i> <span data-bind=\"text: (isPaused() ? 'Restart' : 'Start')\">Start</span>"

    // cute hack for changing printer to machine for the action notify sidebar plugin
    var x = document.getElementById("sidebar_plugin_action_command_notification_wrapper");
    if (x != undefined) {
        x.outerHTML = x.outerHTML.replace("printer.", "machine.").replace("Printer ", "");
    }

    // cute hack for changing printer to machine for the connection sidebar plugin
    var y = document.getElementById("connection_wrapper");
    if (y != undefined) {
        y.outerHTML = y.outerHTML.replace("Printer ", "Machine ");
    }

    // cute hack for removing print from the state sidebar plugin
    var z = document.getElementById("state_wrapper");
    if (z != undefined) {
        z.innerHTML = z.innerHTML.replaceAll(">Print ", ">Job ");
        z.innerHTML = z.innerHTML.replaceAll(" print ", " ");
        z.innerHTML = z.innerHTML.replaceAll(" Print ", " ");
        z.innerHTML = z.innerHTML.replaceAll(">Printed", ">Bytes");
        z.innerHTML = z.innerHTML.replaceAll(" printed ", " streamed ");
    }

    // cute hack for removing Printer from the Settings Menu
    setTimeout(checkSettings, 100);
    function checkSettings() {
        var a = document.getElementById("UICsettingsMenu");
        if (a != undefined) {
            a.outerHTML = a.outerHTML.replaceAll("Printer ", "Machine ");
        } else {
            setTimeout(checkSettings, 100);
        }
    }


    function guid() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            var r = Math.random() * 16 | 0,
                v = c == 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    var haveEvents = 'ongamepadconnected' in window;
    var controllers = {};
    var controllerInterval = undefined;

    function connecthandler(e) {
      addgamepad(e.gamepad);
    }

    function addgamepad(gamepad) {
      controllers[gamepad.index] = gamepad;
      if (controllerInterval == undefined) {
        console.log("updateStatus interval created");
        controllerInterval = setInterval(updateStatus, 250);
      }
    }

    function disconnecthandler(e) {
      removegamepad(e.gamepad);
    }

    function removegamepad(gamepad) {
      delete controllers[gamepad.index];

      var j;
      var empty = true;

      for (j in controllers) {
        if (j != undefined) {
          empty = false;
          break;
        }
      }

      if (empty) {
        console.log("updateStatus interval destroyed");
        clearInterval(controllerInterval);
        controllerInterval = undefined;

        var state = document.getElementById("bgs_printer_state").innerText;
        if (state != "Run") {
          OctoPrint.control.sendGcode("CANCELJOG");
        }
      }
    }

    function scaleValue(value, from, to) {
    	var scale = (to[1] - to[0]) / (from[1] - from[0]);
    	var capped = Math.min(from[1], Math.max(from[0], value)) - from[0];
    	return ~~(capped * scale + to[0]);
    }

    var lastX = 0;
    var lastY = 0;

    function updateStatus() {
      var state = document.getElementById("bgs_printer_state").innerText;
      if (!(state == "Idle" || state == "Jog")) {
        return;
      }

      if (!haveEvents) {
        scangamepads();
      }

      var i = 0;
      var j;

      for (j in controllers) {
        var controller = controllers[j];
        var x = 0;
        var y = 0;

        for (i = 0; i < 2; i++) {
          if (Math.abs(controller.axes[i]) >= .1) {
            value = controller.axes[i];
            // value = value + .2 * (value > 0 ? -1 : 1);

            if (i == 1 || i == 3) {
              invert = -1;
            } else {
              invert = 1;
            }

            value = value * 20 * invert;

            if (invert == -1) {
              y = value;
            } else {
              x = value;
            }
          } else {
            if (i == 1 || i == 3) {
              y = 0;
            } else {
              x = 0;
            }
          }
        }
      }
      if (x != lastX || y != lastY) {
        if (x == 0 && y == 0) {
          OctoPrint.control.sendGcode("CANCELJOG");
          console.log("gamepad centered");
        }

        lastX = x;
        lastY = y;
      }

      if (x != 0 || y != 0) {
        var fastAxis = 0;

        if (Math.abs(x) > Math.abs(y)) {
          fastAxis = Math.abs(x);
        } else {
          fastAxis = Math.abs(y);
        }

        OctoPrint.control.sendGcode("$J=G91 G21 X" + x + " Y" + y + " F" + scaleValue(fastAxis, [1,20], [100,2500]));
        console.log("x=" + x + " y=" + y);
      }
    }

    function scangamepads() {
      var gamepads = navigator.getGamepads ? navigator.getGamepads() : (navigator.webkitGetGamepads ? navigator.webkitGetGamepads() : []);
      for (var i = 0; i < gamepads.length; i++) {
        if (gamepads[i]) {
          if (gamepads[i].index in controllers) {
            controllers[gamepads[i].index] = gamepads[i];
          } else {
            addgamepad(gamepads[i]);
          }
        }
      }
    }

    window.addEventListener("gamepadconnected", connecthandler);
    window.addEventListener("gamepaddisconnected", disconnecthandler);

    if (!haveEvents) {
     setInterval(scangamepads, 500);
    }

    OCTOPRINT_VIEWMODELS.push([
        BettergrblsupportViewModel,
        ["settingsViewModel", "loginStateViewModel", "accessViewModel", "actionCommandNotificationViewModel"],
        ["#bettergrblsupport_control_panel"]
    ]);

    //OCTOPRINT_VIEWMODELS.push({
    //    construct: BettergrblsupportViewModel,
    //    name: "bettergrblsupportViewModel",
    //    dependencies: ["settingsViewModel", "loginStateViewModel", "accessViewModel", "actionCommandNotificationViewModel"],
    //    elements: ["#bgs_control_panel"]
    //});

});
