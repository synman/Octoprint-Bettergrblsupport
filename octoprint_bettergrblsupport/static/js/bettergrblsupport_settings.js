/*
 * View model for OctoPrint-Bettergrblsupport
 *
 * Author: Shell M. Shrader
 * License: Apache 2.0
 */
$(function() {
    function BettergrblsupportSettingsViewModel(parameters) {
      var self = this;
      // var $body = $('body');

      // assign the injected parameters, e.g.:
      self.settings = parameters[0];
      self.loginState = parameters[1];

      self.is_printing = ko.observable(false);
      self.is_operational = ko.observable(false);

      self.grblSettingsText = ko.observable("No Settings Found");

      // self.toggleWeak = function() {
      //   $.ajax({
      //     url: API_BASEURL + "plugin/bettergrblsupport",
      //     type: "POST",
      //     dataType: "json",
      //     data: JSON.stringify({
      //       command: "toggleWeak"
      //     }),
      //     contentType: "application/json; charset=UTF-8",
      //     success: function(data) {
      //       var btn = document.getElementById("grblLaserButton");
      //       btn.innerHTML = btn.innerHTML.replace(btn.innerText, data["res"]);
      //     },
      //     error: function (data, status) {
      //       new PNotify({
      //         title: "Laser action failed!",
      //         text: data.responseText,
      //         hide: true,
      //         buttons: {
      //           sticker: false,
      //           closer: true
      //         },
      //         type: "error"
      //       });
      //     }
      //   });
      // };

      self.onBeforeBinding = function() {
        // initialize stuff here
        self.grblSettingsText(self.settings.settings.plugins.bettergrblsupport.grblSettingsText());
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

      self.onDataUpdaterPluginMessage = function(plugin, data) {
        return;
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
      BettergrblsupportSettingsViewModel,
      [ "settingsViewModel", "loginStateViewModel" ],
        "#settings_plugin_bettergrblsupport_settings"
      ]);
});
