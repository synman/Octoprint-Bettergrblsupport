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

      self.grblSettings = ko.observableArray([]);

      self.updateSetting = function(id, value, oldvalue) {
        if (value != oldvalue) {
          $.ajax({
            url: API_BASEURL + "plugin/bettergrblsupport",
            type: "POST",
            dataType: "json",
            data: JSON.stringify({
              command: "updateGrblSetting",
              id: id,
              value: value
            }),
            contentType: "application/json; charset=UTF-8",
            success: function(data) {
              for (var i in self.grblSettings()) {
                if (self.grblSettings()[i].id == id) {
                   self.grblSettings()[i].oldvalue = value;
                   break;
                }
              }

              new PNotify({
                title: "Grbl Settings Update",
                text: "$" + id + " has been set to " + value,
                hide: false,
                buttons: {
                  sticker: true,
                  closer: true
                },
                type: "success"
              });
            },
            error: function (data, status) {
              new PNotify({
                title: "Grbl setting update failed!",
                text: data.responseText,
                hide: true,
                buttons: {
                  sticker: false,
                  closer: true
                },
                type: "error"
              });

              for (var i in self.grblSettings()) {
                if (self.grblSettings()[i].id == id) {
                   self.grblSettings()[i].value = oldvalue;
                   break;
                }
              }
            }
          });
        }
      };

      self.backupSettings = function() {
        $.ajax({
          url: API_BASEURL + "plugin/bettergrblsupport",
          type: "POST",
          dataType: "json",
          data: JSON.stringify({
            command: "backupGrblSettings"
          }),
          contentType: "application/json; charset=UTF-8",
          success: function(data) {
            new PNotify({
              title: "Grbl Settings Backup",
              text: "The backup has been completed successfully.",
              hide: false,
              buttons: {
                sticker: true,
                closer: true
              },
              type: "success"
            });
          },
          error: function (data, status) {
            new PNotify({
              title: "Grbl Settings Backup",
              text: data.responseText,
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

      self.restoreSettings = function() {
        $.ajax({
          url: API_BASEURL + "plugin/bettergrblsupport",
          type: "POST",
          dataType: "json",
          data: JSON.stringify({
            command: "restoreGrblSettings"
          }),
          contentType: "application/json; charset=UTF-8",
          success: function(data) {
            new PNotify({
              title: "Grbl Settings Restore",
              text: "The restore has been completed successfully.",
              hide: true,
              buttons: {
                sticker: true,
                closer: true
              },
              type: "success"
            });
            self.pushGrblSettings(data["res"]);
          },
          error: function (data, status) {
            new PNotify({
              title: "Grbl Settings Restore",
              text: data.responseText,
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

      self.onBeforeBinding = function() {
        // initialize stuff here
        self.pushGrblSettings(self.settings.settings.plugins.bettergrblsupport.grblSettingsText());
      };

      self.pushGrblSettings = function(grblSettingsText) {
        self.grblSettings.removeAll();

        var grblSettings = grblSettingsText.split("||");
        var settingsSize = grblSettings.length - 1;

        for (var i = 0; i < settingsSize; i++) {
          var setting = grblSettings[i].split("|");

          self.grblSettings.push({
              id: setting[0],
              value: setting[1],
              oldvalue: setting[1],
              description: setting[2]
          });
        }
      }

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

      ko.bindingHandlers.numeric = {
          init: function (element, valueAccessor) {
              $(element).on("keydown", function (event) {
                  // Allow: backspace, delete, tab, escape, and enter
                  if (event.keyCode == 46 || event.keyCode == 8 || event.keyCode == 9 || event.keyCode == 27 || event.keyCode == 13 ||
                      // Allow: Ctrl+A
                      (event.keyCode == 65 && event.ctrlKey === true) ||
                      // Allow: . ,
                      (event.keyCode == 188 || event.keyCode == 190 || event.keyCode == 110) ||
                      // Allow: home, end, left, right
                      (event.keyCode >= 35 && event.keyCode <= 39)) {
                      // let it happen, don't do anything
                      return;
                  }
                  else {
                      // Ensure that it is a number and stop the keypress
                      if (event.shiftKey || (event.keyCode < 48 || event.keyCode > 57) && (event.keyCode < 96 || event.keyCode > 105)) {
                          event.preventDefault();
                      }
                  }
              });
          }
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
        "#settings_plugin_bettergrblsupport"
      ]);
});
