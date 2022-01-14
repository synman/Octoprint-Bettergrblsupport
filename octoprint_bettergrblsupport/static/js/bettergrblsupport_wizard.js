/*
 * View model for OctoPrint-Bettergrblsupport
 *
 * Author: Shell M. Shrader
 * License: Apache 2.0
 */
$(function() {
    function BettergrblsupportWizardViewModel(parameters) {
      var self = this;
      // var $body = $('body');

      // assign the injected parameters, e.g.:
      self.settingsViewModel = parameters[0];
      self.loginState = parameters[1];

      self.settings = undefined;

      self.is_printing = ko.observable(false);
      self.is_operational = ko.observable(false);

      self.onBeforeBinding = function() {
        // initialize stuff here
        self.settings = self.settingsViewModel.settings;


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
  }

  // var x = document.getElementById("wizard_dialog");
  // if (x != undefined) {
  //     x.firstElementChild.outerHTML = x.firstElementChild.outerHTML.replace("Setup Wizard", "Better Grbl Support");
  // }


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
    BettergrblsupportWizardViewModel,
    [ "settingsViewModel", "loginStateViewModel" ],
      "#bettergrblsupport_wizard"
    ]);
});
