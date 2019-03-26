/*
 * View model for OctoPrint-Bettergrblsupport
 *
 * Author: Shell M. Shrader
 * License: Apache 2.0
 */
$(function() {
    function BettergrblsupportViewModel(parameters) {
      var self = this;

      // assign the injected parameters, e.g.:
      self.settings = parameters[0];
      self.loginState = parameters[1];

      self.length = ko.observable("100");
      self.width = ko.observable("100");

      self.distances = ko.observableArray([0.1, 1, 10, 100]);
      self.distance = ko.observable(10);

      self.is_printing = ko.observable(false);
      self.is_operational = ko.observable(false);

      tab = document.getElementById("tab_plugin_bettergrblsupport_link");
      tab.innerHTML = tab.innerHTML.replace("Better Grbl Support", "Grbl Control");

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
            var options = {
              title: "Framing failed.",
              text: data.responseText,
              hide: true,
              buttons: {
                sticker: false,
                closer: true
              },
              type: "error"
            };

            new PNotify(options);
          }
        });
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
            var options = {
              title: "Framing failed.",
              text: data.responseText,
              hide: true,
              buttons: {
                sticker: false,
                closer: true
              },
              type: "error"
            };

            new PNotify(options);
          }
        });
      };

      self.onBeforeBinding = function() {
        self.length(self.settings.settings.plugins.bettergrblsupport.frame_length());
        self.width(self.settings.settings.plugins.bettergrblsupport.frame_width());

        self.distance(self.settings.settings.plugins.bettergrblsupport.distance());
        self.distances(self.settings.settings.plugins.bettergrblsupport.distances());

        self.is_printing(self.settings.settings.plugins.bettergrblsupport.is_printing());
        self.is_operational(self.settings.settings.plugins.bettergrblsupport.is_operational());

        var x = document.getElementsByName("frameOrigin");

        var i;
        for (i = 0; i < x.length; i++) {
          if (x[i].id == self.settings.settings.plugins.bettergrblsupport.frame_origin()) {
            x[i].checked = true;
            break;
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
    }

    /* view model class, parameters for constructor, container to bind to
     * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
     * and a full list of the available options.
     */
    // OCTOPRINT_VIEWMODELS.push({
    //     construct: BettergrblsupportViewModel,
    //     // ViewModels your plugin depends on, e.g. loginStateViewModel, settingsViewModel, ...
    //     dependencies: [ /* "loginStateViewModel", "settingsViewModel" */ ],
    //     // Elements to bind to, e.g. #settings_plugin_bettergrblsupport, #tab_plugin_bettergrblsupport, ...
    //     elements: [ /* ... */ ]
    // });

    OCTOPRINT_VIEWMODELS.push([
      BettergrblsupportViewModel,
        [ "settingsViewModel", "loginStateViewModel" ],
        [ "#tab_plugin_bettergrblsupport" ]
      ]);
});
