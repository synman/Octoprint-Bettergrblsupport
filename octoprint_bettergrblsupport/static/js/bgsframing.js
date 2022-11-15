$(function () {
    function BgsFramingSidebarViewModel(parameters) {
        var self = this;

        self.loginState = parameters[0];
        self.access = parameters[1];
        self.settings = parameters[2];

        self.length = ko.observable("100");
        self.width = ko.observable("100");

        self.is_printing = ko.observable(false);
        self.is_operational = ko.observable(false);

        self.state = ko.observable("unknown");

        self.handleFocus = function (event, type, item) {
          window.setTimeout(function () {
              event.target.select();
          }, 0);
        };

        self.onBeforeBinding = function() {
          self.length(self.settings.settings.plugins.bettergrblsupport.frame_length());

          self.length.subscribe(function (newValue) {
            self.settings.settings.plugins.bettergrblsupport.frame_length(newValue);
            self.settings.saveData();
          });

          self.width(self.settings.settings.plugins.bettergrblsupport.frame_width());

          self.width.subscribe(function (newValue) {
            self.settings.settings.plugins.bettergrblsupport.frame_width(newValue);
            self.settings.saveData();
          });

          self.is_printing(self.settings.settings.plugins.bettergrblsupport.is_printing());
          self.is_operational(self.settings.settings.plugins.bettergrblsupport.is_operational());

          var x = document.getElementsByName("frameOrigin");

          var i;
          for (i = 0; i < x.length; i++) {
            x[i].checked = false;
            if (x[i].id == self.settings.settings.plugins.bettergrblsupport.frame_origin()) {
              x[i].checked = true;
            }
          }
        };

        self.fromCurrentData = function (data) {
            self._processStateData(data.state);
        };

        self.fromHistoryData = function (data) {
            self._processStateData(data.state);
        };

        self._processStateData = function (data) {
            self.is_printing(data.flags.printing);
            self.is_operational(data.flags.operational);
        };

        self.originChanged = function(newOrigin) {
          self.settings.settings.plugins.bettergrblsupport.frame_origin(newOrigin);
          self.settings.saveData();
        };

        self.doFrame = function() {
          var o;
          var x = document.getElementsByName("frameOrigin");
          var i;

          for (i = 0; i < x.length; i++) {
            if (x[i].checked) {
              o = x[i].id;
              break;
            }
          }

          $.ajax({
            url: API_BASEURL + "plugin/bettergrblsupport",
            type: "POST",
            dataType: "json",
            data: JSON.stringify({
              command: "frame",
              length: self.length(),
              width: self.width(),
              origin: o
            }),
            contentType: "application/json; charset=UTF-8",
            error: function (data, status) {
              var error = JSON.parse(data.responseText).error;
              if (error == undefined) error = data.responseText;
              new PNotify({
                title: "Framing failed!",
                text: error,
                hide: true,
                buttons: {
                  sticker: false,
                  closer: true
                },
                type: "error"
              });
            }
          });
        };

        self.onDataUpdaterPluginMessage = function(plugin, data) {
          if (plugin == 'bettergrblsupport' && data.type == 'grbl_state') {
            if (data.state != undefined) self.state(data.state);
          }

          if (plugin == 'bettergrblsupport' && data.type == 'grbl_frame_size') {
            width = Number.parseFloat(data.width).toFixed(0);
            length = Number.parseFloat(data.length).toFixed(0);

            self.width(width);
            self.length(length);

            var origin = document.getElementById(data.origin);
            origin.checked = true;

            doNotify = self.settings.settings.plugins.bettergrblsupport.notifyFrameSize();

            if (doNotify) {
              new PNotify({
                title: "Frame Size Computed",
                text: "Dimensions are " + length + "L x " + width + "W",
                hide: true,
                animation: "fade",
                animateSpeed: "slow",
                mouseReset: true,
                delay: 10000,
                buttons: {
                  sticker: true,
                  closer: true
                },
                type: "success"
              });
            }

            console.log("frame length=" + data.length + " width=" + data.width + " origin=" + origin.id);
            return
          }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BgsFramingSidebarViewModel,
        dependencies: ["loginStateViewModel", "accessViewModel", "settingsViewModel"],
        elements: ["#sidebar_plugin_bettergrblsupport_wrapper"]
    });
});
